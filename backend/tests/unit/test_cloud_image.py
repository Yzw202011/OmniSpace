"""云端API接入批2 单测（2026-09-06，mock 图片服务商全链）。

覆盖：
- 适配器 openai_image：b64/url 两响应形态、尺寸归一、参考图拒绝（带出路）；
- 适配器 task_image：DashScope 三段式（PENDING→RUNNING→SUCCEEDED）、
  参考图计数（i2i）、FAILED 终态带出路、轮询间协作取消；
- Provider 协议闸：openai_image/task_image 可建、task_video 仍拒绝；
- 图片槽位路由：绑定→端点、未绑定/停用/协议不匹配→None（本地）；
  set_binding 跨类协议拒绝（文本连接绑图片工位）；
- 图像队列云端道：cloud 任务不 acquire paint 锁、云道并发（2 任务
  并行）、排队位次、运行中取消收割、snapshot cloud_running、
  本地任务照走本地道（云任务积压不阻塞本地）；
- 接线哨兵：draw/keyframe/comic_asset 生成链的 get_image_endpoint
  调用与 cloud 标记必须存在（防回归误删）。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import backend.services.cloud_provider_service as cs
import backend.services.image_queue as iq
from backend.services.cloud_provider_service import (
    CloudEndpoint,
    CloudProviderError,
    create_provider,
    get_image_concurrency,
    get_image_endpoint,
    set_binding,
)
from backend.services.inference import cloud_image_client as cic
from backend.tests.unit.mock_cloud_image import MockCloudImage

_ROOT_DIR = Path(__file__).resolve().parents[3]


@pytest.fixture()
def mock_img():
    srv = MockCloudImage(port=0)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture()
def mem_kv(monkeypatch: pytest.MonkeyPatch):
    """内存 KV（隔离真机 DB 与迁移标记），并清路由缓存。"""
    store: dict = {}

    def _read(key, default=None):
        return store.get(key, default)

    def _write(key, value):
        store[key] = value
        return True

    monkeypatch.setattr(cs, "_read_kv", _read)
    monkeypatch.setattr(cs, "_write_kv", _write)
    cs._invalidate_route_cache()
    yield store
    cs._invalidate_route_cache()


@pytest.fixture()
def fast_poll(monkeypatch: pytest.MonkeyPatch):
    """云适配器轮询提速（3s→0.05s，测试毫秒级）。"""
    monkeypatch.setattr(cic, "POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(cic, "TASK_TIMEOUT_S", 10.0)


def _task_ep(srv: MockCloudImage, protocol: str = "task_image",
             model: str = "wanx2.1-t2i-turbo") -> CloudEndpoint:
    return CloudEndpoint(base_url=srv.base_url, api_key="sk-mock",
                         model=model, provider_id="prov_mock",
                         provider_name="Mock云图", protocol=protocol,
                         extra={"vendor": "dashscope"})


# ── 适配器：openai_image ─────────────────────────────────────────

def test_openai_image_b64_and_size(mock_img) -> None:
    ep = _task_ep(mock_img, protocol="openai_image")
    img = cic.generate_image(ep, "一只红衣少女，雪夜", width=512, height=288)
    assert img.size == (512, 288)  # 归一到目标尺寸（mock 回 1x1）
    assert mock_img.last_request["payload"]["prompt"].startswith("一只红衣少女")
    assert mock_img.last_request["payload"]["size"] == "512x288"


def test_openai_image_url_download(mock_img) -> None:
    mock_img.b64_mode = False
    ep = _task_ep(mock_img, protocol="openai_image")
    img = cic.generate_image(ep, "url 形态", width=64, height=64)
    assert img.size == (64, 64)


def test_openai_image_rejects_refs_with_guidance(mock_img) -> None:
    from PIL import Image

    ep = _task_ep(mock_img, protocol="openai_image")
    with pytest.raises(RuntimeError, match="task_image"):
        cic.generate_image(ep, "图生图", width=64, height=64,
                           ref_images=[Image.new("RGB", (8, 8))])


# ── 适配器：task_image（DashScope 三段式）────────────────────────

def test_task_image_t2i_three_phase(mock_img, fast_poll) -> None:
    ep = _task_ep(mock_img)
    img = cic.generate_image(ep, "武侠少女立绘", width=1024, height=576)
    assert img.size == (1024, 576)
    assert mock_img.last_request["path"].endswith(
        "/api/v1/services/aigc/text2image/image-synthesis")
    assert mock_img.last_request["payload"]["model"] == "wanx2.1-t2i-turbo"
    # 官方 size 格式="宽*高"（2026-09-06 文档核对后修复的回归哨兵）
    assert mock_img.last_request["payload"]["parameters"]["size"] \
        == "1024*576"


def test_wan_size_clamp_official_constraints() -> None:
    """官方约束（wan2.2：宽高 [512,1440]）外的目标尺寸先归档再回缩放。"""
    assert cic._wan_size(1280, 720) == "1280*720"      # 约束内不动
    assert cic._wan_size(2560, 1440) == "1440*810"     # 资产图超限等比收缩
    assert cic._wan_size(512, 288) == "910*512"        # 低于下限等比放大
    assert cic._wan_size(1024, 1024) == "1024*1024"


def test_task_image_multimodal_refs(mock_img, fast_poll) -> None:
    from PIL import Image

    ep = _task_ep(mock_img, model="qwen-image-edit")
    img = cic.generate_image(ep, "参考图画同人", width=512, height=512,
                             ref_images=[Image.new("RGB", (16, 16)),
                                         Image.new("RGB", (16, 16))])
    assert img.size == (512, 512)
    assert mock_img.last_request["path"].endswith(
        "/multimodal-generation/generation")
    assert mock_img.last_request["ref_count"] == 2  # 参考图全部编码上传


def test_task_image_failed_status(mock_img, fast_poll) -> None:
    mock_img.task_status_override = "FAILED"
    ep = _task_ep(mock_img)
    with pytest.raises(RuntimeError, match="云端图片任务失败"):
        cic.generate_image(ep, "x", width=64, height=64)


def test_task_image_cancel_between_polls(mock_img, fast_poll) -> None:
    ep = _task_ep(mock_img)

    class _Cancelled(Exception):
        pass

    calls = {"n": 0}

    def check() -> None:
        calls["n"] += 1
        if calls["n"] >= 3:  # 第 2 次轮询后取消
            raise _Cancelled()

    with pytest.raises(_Cancelled):
        cic.generate_image(ep, "x", width=64, height=64, check_cancel=check)


def test_probe_image_provider_protocol_aware(mock_img) -> None:
    """协议感知连通性测试（批2 实弹抓出的缺口哨兵）：task_image 连接
    不能用文本探测（/health+/v1/models 假报不可达），须走任务端点
    鉴权探测；401/403 须如实报 Key 被拒。"""
    # task_image：mock 无 /health（文本探测会两连 404 假报不可达）
    ep = _task_ep(mock_img)
    ok, detail = cic.probe_image_provider(ep)
    assert ok and detail == "", f"task_image 协议探测应可达: {detail}"

    # 401：Key 被拒，如实透出（requests.get 打桩）
    import requests as _requests

    bad = CloudEndpoint(base_url=mock_img.base_url, api_key="sk-wrong",
                        protocol="task_image")
    orig = _requests.get

    def _fake_get(*a, **k):  # noqa: ANN002, ANN003
        return type("R", (), {"status_code": 401, "text": "denied"})()

    _requests.get = _fake_get
    try:
        ok2, detail2 = cic.probe_image_provider(bad)
    finally:
        _requests.get = orig
    assert ok2 is False and "Key" in detail2, f"401 应报 Key 被拒: {detail2}"


# ── Provider 协议闸与图片槽位路由 ────────────────────────────────

def test_image_protocol_gate(mem_kv) -> None:
    item = create_provider("万相", "task_image",
                           "https://dashscope.aliyuncs.com", "sk-k",
                           ["wanx2.1-t2i-turbo"])
    assert item["protocol"] == "task_image"
    item2 = create_provider("硅基", "openai_image",
                            "https://api.siliconflow.cn", "sk-k", ["Kwai-Kolors"])
    assert item2["protocol"] == "openai_image"
    # 未知协议仍拒绝（task_video 批3 已开放，语义迁移至 test_cloud_video）
    with pytest.raises(CloudProviderError):
        create_provider("X", "future_proto", "https://x.example.com", "k")


def test_image_slot_routing(mem_kv) -> None:
    assert get_image_endpoint("paint.image") is None  # 未绑定=本地
    prov = create_provider("万相", "task_image",
                           "https://dashscope.aliyuncs.com", "sk-k",
                           ["wanx2.1-t2i-turbo", "qwen-image-edit"])
    set_binding("keyframe.image", prov["id"], "qwen-image-edit")
    ep = get_image_endpoint("keyframe.image")
    assert ep is not None
    assert ep.model == "qwen-image-edit"
    assert ep.protocol == "task_image"
    assert ep.api_key == "sk-k"
    # 其他图片槽位不受影响
    assert get_image_endpoint("paint.image") is None
    # 停用即回落本地
    from backend.services.cloud_provider_service import update_provider
    update_provider(prov["id"], enabled=False)
    assert get_image_endpoint("keyframe.image") is None


def test_binding_protocol_mismatch_rejected(mem_kv) -> None:
    text_prov = create_provider("DeepSeek", "openai_text",
                                "https://api.deepseek.com", "sk-t",
                                ["deepseek-chat"])
    with pytest.raises(CloudProviderError) as ei:
        set_binding("paint.image", text_prov["id"], "deepseek-chat")
    assert ei.value.code == "CLOUD_PROTOCOL_MISMATCH"
    img_prov = create_provider("万相", "task_image",
                               "https://dashscope.aliyuncs.com", "sk-k", [])
    with pytest.raises(CloudProviderError):
        set_binding("dialog.text", img_prov["id"], "wanx2.1-t2i-turbo")


def test_image_concurrency_setting(mem_kv) -> None:
    assert get_image_concurrency() == 2  # 默认
    mem_kv[cs.KV_SETTINGS] = {"image_concurrency": 4}
    assert get_image_concurrency() == 4
    mem_kv[cs.KV_SETTINGS] = {"image_concurrency": 99}
    assert get_image_concurrency() == 8  # 钳 1~8
    mem_kv[cs.KV_SETTINGS] = {"image_concurrency": "bad"}
    assert get_image_concurrency() == 2  # 坏值回默认


# ── 图像队列云端道 ───────────────────────────────────────────────

def _new_queue() -> iq.ImageTaskQueue:
    """独立队列实例（避开单例状态污染）。"""
    return iq.ImageTaskQueue()


def _wait_done(box: dict, timeout: float = 15.0) -> None:
    assert box["event"].wait(timeout), "任务超时未完成"


def test_cloud_task_skips_local_lock(monkeypatch) -> None:
    """云端任务不得触碰本地准入（锁/热保护/vLLM 让渡）。"""
    q = _new_queue()
    for name in ("_acquire_paint_lock", "_sleep_vllm_for_generation",
                 "_thermal_paused"):
        def _boom(*a, _n=name, **k):  # noqa: ANN002, ANN003
            raise AssertionError(f"云端任务触碰了本地准入: {_n}")
        monkeypatch.setattr(q, name, _boom)
    box: dict = {"event": threading.Event(), "result": None}

    def runner(task, check_cancel):  # noqa: ANN001
        box["result"] = "cloud-done"
        return box["result"]

    q.submit({"task_id": "c1", "kind": "paint", "runner": runner,
              "cloud": True, "on_finish": lambda e: box["event"].set()})
    _wait_done(box)
    assert box["result"] == "cloud-done"


def test_cloud_lane_concurrency(monkeypatch) -> None:
    """云道并发=2：两个 0.4s 任务总耗时应 < 串行的 0.8s。"""
    q = _new_queue()
    monkeypatch.setattr(q, "_cloud_concurrency", lambda: 2)
    done_events: list[threading.Event] = [threading.Event() for _ in range(2)]
    overlaps = {"now": 0, "max": 0}
    lock = threading.Lock()

    def make_runner(i: int):  # noqa: ANN001
        def runner(task, check_cancel):  # noqa: ANN001
            with lock:
                overlaps["now"] += 1
                overlaps["max"] = max(overlaps["max"], overlaps["now"])
            time.sleep(0.4)
            with lock:
                overlaps["now"] -= 1
            return i
        return runner

    t0 = time.time()
    for i, ev in enumerate(done_events):
        q.submit({"task_id": f"cc{i}", "kind": "paint",
                  "runner": make_runner(i), "cloud": True,
                  "on_finish": lambda e, _ev=ev: _ev.set()})  # type: ignore[assignment]
    for ev in done_events:
        assert ev.wait(15.0)
    elapsed = time.time() - t0
    assert overlaps["max"] == 2, "两个云任务应并行执行"
    assert elapsed < 0.75, f"并发执行耗时应≈0.4s，实测 {elapsed:.2f}s"


def test_cloud_task_cancel_while_running() -> None:
    q = _new_queue()
    cx_event = threading.Event()
    box = {"err": None}

    def runner(task, check_cancel):  # noqa: ANN001
        for _ in range(200):
            time.sleep(0.02)
            check_cancel()  # 轮询间收割取消
        return "should-not-reach"

    def on_finish(err):  # noqa: ANN001
        box["err"] = err
        cx_event.set()

    q.submit({"task_id": "cx", "kind": "paint", "runner": runner,
              "cloud": True, "on_finish": on_finish})
    # 等任务进入 cloud_running 再取消
    for _ in range(100):
        with q._cond:
            if "cx" in q._cloud_running:
                break
        time.sleep(0.02)
    assert q.cancel("cx") == "running"
    assert cx_event.wait(15.0), "取消后任务未收敛"
    assert isinstance(box["err"], iq.ImageTaskCancelled)


def test_local_lane_not_blocked_by_cloud_backlog() -> None:
    """云任务占满云道时，本地任务照常被本地 worker 消费。"""
    q = _new_queue()
    monkey_conc = 1
    q.__dict__["_cloud_concurrency"] = lambda: monkey_conc  # 云道 1 并发
    cloud_gate = threading.Event()
    cloud_done = threading.Event()
    local_done = threading.Event()

    def cloud_runner(task, check_cancel):  # noqa: ANN001
        cloud_gate.wait(10.0)  # 占住云道唯一名额
        return "c"
    def local_runner(task, check_cancel):  # noqa: ANN001
        return "l"

    q.submit({"task_id": "cb1", "kind": "paint", "runner": cloud_runner,
              "cloud": True, "on_finish": lambda e: cloud_done.set()})
    # 等 cb1 进入运行（云道满），再排入第二个云任务 + 一个本地任务
    for _ in range(100):
        with q._cond:
            if "cb1" in q._cloud_running:
                break
        time.sleep(0.02)
    q.submit({"task_id": "cb2", "kind": "paint", "runner": cloud_runner,
              "cloud": True, "on_finish": lambda e: None})
    q.submit({"task_id": "loc1", "kind": "paint", "runner": local_runner,
              "on_finish": lambda e: local_done.set()})
    # 本地任务无须等待云道（本地道独立消费；准入桩默认可通过——
    # 测试环境无 feature_lock/thermal 阻塞）
    assert local_done.wait(10.0), "本地任务被云任务积压阻塞了"
    cloud_gate.set()
    assert cloud_done.wait(10.0)


def test_snapshot_includes_cloud_state() -> None:
    q = _new_queue()
    q.__dict__["_cloud_concurrency"] = lambda: 1  # 云道单并发（造积压）
    gate = threading.Event()

    def gated(task, check_cancel):  # noqa: ANN001
        gate.wait(10.0)
        return "x"

    q.submit({"task_id": "s1", "kind": "paint", "runner": gated,
              "cloud": True})
    # 等 s1 进入云道运行（占住唯一名额）
    for _ in range(200):
        with q._cond:
            if "s1" in q._cloud_running:
                break
        time.sleep(0.02)
    q.submit({"task_id": "s2", "kind": "paint",
              "runner": lambda t, c: "x", "cloud": True})
    time.sleep(0.1)
    snap = q.snapshot()
    assert snap["cloud_running"] == ["s1"]
    queued = snap["queued"]
    assert queued and queued[0]["task_id"] == "s2"
    assert queued[0]["cloud"] is True  # 排队项带云道标记
    gate.set()


# ── 接线哨兵（防回归误删）────────────────────────────────────────

def test_image_wiring_sentinels() -> None:
    draw_src = (_ROOT_DIR / "backend/api/draw.py").read_text(encoding="utf-8")
    assert 'get_image_endpoint("paint.image")' in draw_src
    assert '"cloud": cloud_ep is not None' in draw_src
    assert "cloud_endpoint=cloud_endpoint" in draw_src

    kf_src = (_ROOT_DIR / "backend/api/manga/keyframe.py").read_text(
        encoding="utf-8")
    assert 'get_image_endpoint("keyframe.image")' in kf_src
    assert "cloud_endpoint=_cloud_ep" in kf_src
    # 生成核心：云端分支 + 引擎装载守卫 + 参考图加载条件
    assert "cloud_endpoint is not None" in kf_src
    assert "(flux or cloud_endpoint is not None)" in kf_src

    asset_src = (_ROOT_DIR / "backend/api/manga/comic_asset.py").read_text(
        encoding="utf-8")
    assert 'get_image_endpoint("asset.image")' in asset_src

    common_src = (_ROOT_DIR / "backend/api/manga/common.py").read_text(
        encoding="utf-8")
    assert "cloud_endpoint is not None" in common_src
    assert '"engine": "cloud"' in common_src
