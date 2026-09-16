"""云端API接入批3 单测（2026-09-06，mock 视频服务商全链）。

覆盖：
- 适配器 task_video：DashScope 三段式（PENDING→RUNNING→SUCCEEDED）、
  首帧 data URL 编码、时长/画幅参数透传、FAILED 终态带出路、
  轮询间协作取消、协议不符拒绝、无首帧拒绝、未知 vendor 拒绝；
- 协议感知连通性测试（probe_video_provider 可达/Key 被拒）；
- Provider 协议闸：task_video 可创建（批3 开放）；绑定视频槽位须
  视频协议（跨类拒绝）、图片连接绑视频槽位拒绝；
- 视频槽位路由：绑定→端点、未绑定/停用→None（本地旧行为）；
- video_queue 云端道：cloud 任务不触碰本地准入（锁/热保护/vLLM）、
  云道并发=2、运行中取消收割、snapshot cloud_running、本地任务不
  被云任务积压阻塞、next_kind 云端任务不触发 keep_loaded 语义；
- 接线哨兵：video.py 提交点 get_video_endpoint 调用/cloud 标记/
  无关键帧拒绝、video_queue 分道消费源码存在（防回归误删）。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from PIL import Image

import src.services.cloud_provider_service as cs
import src.services.video_queue as vq
from src.services.cloud_provider_service import (
    CloudEndpoint,
    CloudProviderError,
    create_provider,
    get_video_concurrency,
    get_video_endpoint,
    set_binding,
)
from src.services.inference import cloud_video_client as cvc
from tests.unit.unit.mock_cloud_video import MockCloudVideo

_ROOT_DIR = Path(__file__).resolve().parents[3]


@pytest.fixture()
def mock_vid():
    srv = MockCloudVideo(port=0)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture()
def mem_kv(monkeypatch: pytest.MonkeyPatch):
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
    monkeypatch.setattr(cvc, "POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(cvc, "TASK_TIMEOUT_S", 10.0)


def _ep(srv: MockCloudVideo, model: str = "wanx-i2v-turbo") -> CloudEndpoint:
    return CloudEndpoint(base_url=srv.base_url, api_key="sk-mock",
                         model=model, provider_id="prov_mockv",
                         provider_name="Mock云视频", protocol="task_video",
                         extra={"vendor": "dashscope"})


def _frame() -> Image.Image:
    return Image.new("RGB", (64, 36), (10, 120, 200))


# ── 适配器 ───────────────────────────────────────────────────────

def test_generate_video_three_phase(mock_vid, fast_poll) -> None:
    ep = _ep(mock_vid)
    data = cvc.generate_video(ep, "少女持剑转身", _frame(),
                              duration_seconds=5.0, aspect="16:9")
    assert data.startswith(b"\x00\x00\x00\x18ftypmp42")
    req = mock_vid.last_request
    assert req["payload"]["model"] == "wanx-i2v-turbo"
    assert req["img_url_is_dataurl"] is True  # 首帧已编码上传
    assert req["payload"]["parameters"]["duration"] == 5
    assert req["payload"]["parameters"]["aspect_ratio"] == "16:9"


def test_generate_video_failed_status(mock_vid, fast_poll) -> None:
    mock_vid.status_override = "FAILED"
    with pytest.raises(RuntimeError, match="云端视频任务失败"):
        cvc.generate_video(_ep(mock_vid), "x", _frame())


def test_generate_video_cancel_between_polls(mock_vid, fast_poll) -> None:
    class _Cancelled(Exception):
        pass

    calls = {"n": 0}

    def check() -> None:
        calls["n"] += 1
        if calls["n"] >= 3:
            raise _Cancelled()

    with pytest.raises(_Cancelled):
        cvc.generate_video(_ep(mock_vid), "x", _frame(), check_cancel=check)


def test_generate_video_guards(mock_vid) -> None:
    # 协议不符
    bad = CloudEndpoint(base_url=mock_vid.base_url, protocol="openai_image")
    with pytest.raises(RuntimeError, match="不支持视频"):
        cvc.generate_video(bad, "x", _frame())
    # 无首帧
    with pytest.raises(RuntimeError, match="关键帧"):
        cvc.generate_video(_ep(mock_vid), "x", None)
    # 未知 vendor
    unk = _ep(mock_vid)
    unk.extra = {"vendor": "kling"}
    with pytest.raises(RuntimeError, match="尚未支持"):
        cvc.generate_video(unk, "x", _frame())


def test_probe_video_provider(mock_vid) -> None:
    ok, detail = cvc.probe_video_provider(_ep(mock_vid))
    assert ok and detail == ""
    import requests as _requests
    orig = _requests.get

    def _fake_get(*a, **k):  # noqa: ANN002, ANN003
        return type("R", (), {"status_code": 401, "text": "denied"})()

    _requests.get = _fake_get
    try:
        ok2, detail2 = cvc.probe_video_provider(_ep(mock_vid))
    finally:
        _requests.get = orig
    assert ok2 is False and "Key" in detail2


def test_probe_video_provider_404_discrimination() -> None:
    """404 双义甄别（2026-09-10 MiniMax 实测根修）：DashScope 任务形态
    JSON（含 request_id）=可达且鉴权通过；网关 HTML 404（协议不匹配，
    鉴权未被验证）=不得假报「连接成功」。"""
    import requests as _requests
    orig = _requests.get
    ep = CloudEndpoint(base_url="https://vendor.example",
                       api_key="sk-k", protocol="task_video")

    def _fake(status: int, text: str):
        return lambda *a, **k: type("R", (), {
            "status_code": status, "text": text})()

    # DashScope 形态：结构化 JSON（实测样本）
    _requests.get = _fake(
        404, '{"request_id":"e7a","output":{"task_id":"__probe__",'
             '"task_status":"UNKNOWN"}}')
    try:
        ok, detail = cvc.probe_video_provider(ep)
    finally:
        _requests.get = orig
    assert ok and detail == "", f"DashScope 形 404 应通过: {detail}"

    # MiniMax 实测形态：nginx HTML
    _requests.get = _fake(
        404, "<html><head><title>404 Not Found</title></head>"
             "<body><center><h1>404 Not Found</h1></center></body></html>")
    try:
        ok2, detail2 = cvc.probe_video_provider(ep)
    finally:
        _requests.get = orig
    assert ok2 is False, "网关 HTML 404 不得假报连接成功"
    assert "任务端点" in detail2, f"应带协议不匹配出路指引: {detail2}"


# ── Provider 协议闸与视频槽位路由 ────────────────────────────────

def test_video_protocol_gate(mem_kv) -> None:
    item = create_provider("可灵", "task_video",
                           "https://dashscope.aliyuncs.com", "sk-k",
                           ["wanx-i2v-turbo"])
    assert item["protocol"] == "task_video"  # 批3 开放


def test_video_slot_routing_and_mismatch(mem_kv) -> None:
    assert get_video_endpoint() is None  # 未绑定=本地
    img_prov = create_provider("万相图", "task_image",
                               "https://dashscope.aliyuncs.com", "sk-k", [])
    with pytest.raises(CloudProviderError):  # 图片连接绑视频工位=拒绝
        set_binding("manga.video", img_prov["id"], "wanx-i2v-turbo")
    vid_prov = create_provider("万相视频", "task_video",
                               "https://dashscope.aliyuncs.com", "sk-v",
                               ["wanx-i2v-turbo"])
    set_binding("manga.video", vid_prov["id"], "wanx-i2v-turbo")
    ep = get_video_endpoint()
    assert ep is not None and ep.protocol == "task_video"
    assert ep.model == "wanx-i2v-turbo"
    from src.services.cloud_provider_service import update_provider
    update_provider(vid_prov["id"], enabled=False)
    assert get_video_endpoint() is None  # 停用即回落本地


def test_video_concurrency_setting(mem_kv) -> None:
    assert get_video_concurrency() == 1  # 默认串行（视频计费重）
    mem_kv[cs.KV_SETTINGS] = {"video_concurrency": 3}
    assert get_video_concurrency() == 3
    mem_kv[cs.KV_SETTINGS] = {"video_concurrency": 99}
    assert get_video_concurrency() == 4  # 钳 1~4


# ── video_queue 云端道 ───────────────────────────────────────────

def _new_queue() -> vq.VideoTaskQueue:
    return vq.VideoTaskQueue()


def test_cloud_video_task_skips_local_admission(monkeypatch) -> None:
    q = _new_queue()
    for name in ("_acquire_video_gen", "_sleep_vllm_for_generation",
                 "_thermal_paused", "_unload_paint_pipeline"):
        def _boom(*a, _n=name, **k):  # noqa: ANN002, ANN003
            raise AssertionError(f"云端视频任务触碰了本地准入: {_n}")
        monkeypatch.setattr(q, name, _boom)
    box: dict = {"result": None}

    def runner(task, check_cancel):  # noqa: ANN001
        box["result"] = "cloud-video-done"
        return box["result"]

    q.submit({"task_id": "cv1", "kind": "cloud_video", "runner": runner,
              "cloud": True,
              "update_status": lambda tid, patch: None,
              "on_finish": None} | {"on_finish": None})
    # video_queue 无 on_finish 钩子：用轮询等待 runner 生效
    for _ in range(300):
        if box["result"]:
            break
        time.sleep(0.02)
    assert box["result"] == "cloud-video-done"


def test_cloud_video_lane_concurrency(monkeypatch) -> None:
    q = _new_queue()
    monkeypatch.setattr(q, "_cloud_concurrency", lambda: 2)
    done: list[threading.Event] = [threading.Event() for _ in range(2)]
    overlaps = {"now": 0, "max": 0}
    lock = threading.Lock()

    def make_runner(i, ev):  # noqa: ANN001
        def runner(task, check_cancel):  # noqa: ANN001
            with lock:
                overlaps["now"] += 1
                overlaps["max"] = max(overlaps["max"], overlaps["now"])
            time.sleep(0.3)
            with lock:
                overlaps["now"] -= 1
            # video 契约：runner 自写终态（经 task["update_status"]）
            task["update_status"](str(task["task_id"]), {"status": "done"})
            return i
        return runner

    def make_update(ev):  # noqa: ANN001  终态回写桩：done 即 set
        def update(tid, patch):  # noqa: ANN001
            if patch.get("status") in ("done", "error", "cancelled"):
                ev.set()
        return update

    t0 = time.time()
    for i, ev in enumerate(done):
        q.submit({"task_id": f"cc{i}", "kind": "cloud_video",
                  "runner": make_runner(i, ev), "cloud": True,
                  "update_status": make_update(ev)})
    for ev in done:
        assert ev.wait(15.0)
    assert overlaps["max"] == 2
    assert time.time() - t0 < 0.6


def test_cloud_video_cancel_and_snapshot() -> None:
    q = _new_queue()
    q._cloud_concurrency = lambda: 1  # type: ignore[method-assign]
    gate = threading.Event()
    seen = {"running": False}

    def runner(task, check_cancel):  # noqa: ANN001
        seen["running"] = True
        gate.wait(10.0)
        for _ in range(200):
            check_cancel()  # 收割取消
            time.sleep(0.01)
        return "x"

    q.submit({"task_id": "vx", "kind": "cloud_video", "runner": runner,
              "cloud": True, "update_status": lambda tid, patch: None})
    for _ in range(200):
        with q._cond:
            if "vx" in q._cloud_running:
                break
        time.sleep(0.02)
    assert q.cancel("vx") == "running"
    gate.set()
    for _ in range(300):
        with q._cond:
            if "vx" not in q._cloud_running:
                break
        time.sleep(0.02)
    snap = q.snapshot()
    assert "vx" not in snap["cloud_running"]


def test_local_video_not_blocked_by_cloud_backlog() -> None:
    q = _new_queue()
    q.__dict__["_cloud_concurrency"] = lambda: 1
    cloud_gate = threading.Event()
    local_done = threading.Event()

    def cloud_runner(task, check_cancel):  # noqa: ANN001
        cloud_gate.wait(10.0)
        return "c"

    def local_runner(task, check_cancel):  # noqa: ANN001
        # video 契约：runner 自写终态
        task["update_status"](str(task["task_id"]), {"status": "done"})
        return "l"

    def update(tid, patch):  # noqa: ANN001
        if patch.get("status") in ("done", "error", "cancelled"):
            local_done.set()

    q.submit({"task_id": "cb1", "kind": "cloud_video",
              "runner": cloud_runner, "cloud": True,
              "update_status": lambda tid, patch: None})
    for _ in range(200):
        with q._cond:
            if "cb1" in q._cloud_running:
                break
        time.sleep(0.02)
    q.submit({"task_id": "cb2", "kind": "cloud_video",
              "runner": cloud_runner, "cloud": True,
              "update_status": lambda tid, patch: None})
    # 本地任务不持锁走本地道（准入桩 monkeypatch 放行）
    orig_adm = q._wait_admission
    q._wait_admission = lambda task: None  # type: ignore[method-assign]
    try:
        q.submit({"task_id": "loc1", "kind": "local",
                  "runner": local_runner, "update_status": update})
        assert local_done.wait(10.0), "本地视频任务被云任务积压阻塞了"
    finally:
        q._wait_admission = orig_adm  # type: ignore[method-assign]
        cloud_gate.set()


def test_next_kind_cloud_not_h3(monkeypatch) -> None:
    """云任务占住队首（并发=0 取不走）时，next_kind 返回 cloud_video
    ——非 h3_chain，H3 keep_loaded 正常卸载语义。"""
    q = _new_queue()
    monkeypatch.setattr(q, "_cloud_concurrency", lambda: 0)  # 云道冻结
    q.submit({"task_id": "nk1", "kind": "cloud_video",
              "runner": lambda t, c: None, "cloud": True,
              "update_status": lambda tid, patch: None})
    time.sleep(0.1)  # 给 dispatcher 机会（并发=0 不会取）
    assert q.next_kind() == "cloud_video"


# ── 接线哨兵 ─────────────────────────────────────────────────────

def test_video_wiring_sentinels() -> None:
    video_src = (_ROOT_DIR / "src/api/manga/video.py").read_text(
        encoding="utf-8")
    assert '_resolve_video_cloud_endpoint' in video_src
    assert '"cloud": _cloud_ep is not None' in video_src
    assert "没有当前关键帧" in video_src  # 无首帧拒绝带出路
    assert 'model_override or "") == "local"' in video_src  # local 回归口
    queue_src = (_ROOT_DIR / "src/services/video_queue.py").read_text(
        encoding="utf-8")
    assert "_pop_lane_locked" in queue_src
    assert "_cloud_dispatcher_loop" in queue_src
# 本项目仅供学习使用，商业授权请+Q 3559331368
