"""文本槽位拆分 + 系统日志升级 单测（2026-09-06，用户组合场景）。

覆盖（用户假设的组合场景逐条落地验证）：
- 对话=云端、绘画=本地：dialog.text 绑定后 paint 域不受影响；
- 对话=本地、漫剧文字=云端：dialog.text 不绑 + manga.text 绑定 →
  is_remote_dialog_enabled() False（对话走本地）且
  resolve_slot_cloud_model("manga.text") 非 None（漫剧文字走云端）；
- 漫剧文字与写作台绑不同连接：两个槽位解析出不同 provider；
- 跨槽位协议闸：图片连接绑文本槽位拒绝；
- remote_backend.load 请求级解析：cloud:: 请求不依赖 dialog.text 绑定
  （漫剧/写作台绑定时引擎能就绪）；
- 云端调用事件日志：record_cloud_call 落 events JSONL（成功/失败/
  friendly 大白话），日志失败不炸业务；
- 诊断项：_probe_cloud_api 三态（未配置 pass / 配置汇报 / 异常 warn）；
- 接线哨兵：storyboard/novel_service 源码含槽位解析（防回归误删）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import backend.services.cloud_provider_service as cs
import backend.services.inference.backends.remote_backend as rb
from backend.services.cloud_provider_service import (
    CloudProviderError,
    create_provider,
    record_cloud_call,
    resolve_slot_cloud_model,
    set_binding,
)
from backend.services.inference.backends.remote_backend import (
    RemoteDialogBackend,
    is_remote_dialog_enabled,
)

_ROOT_DIR = Path(__file__).resolve().parents[3]


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


def _disable_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rb, "_read_settings_kv", lambda: {})
    monkeypatch.setattr(rb, "_cfg_cache", None)


@pytest.fixture()
def mock_vllm():
    from backend.tests.unit.mock_remote_vllm import FakeRemoteVLLM
    srv = FakeRemoteVLLM(port=0)
    srv.start()
    yield srv
    srv.stop()


# ── 用户组合场景 ─────────────────────────────────────────────────

def test_scene_dialog_cloud_paint_local(
        mem_kv, monkeypatch: pytest.MonkeyPatch) -> None:
    """对话=云端，绘画=本地。"""
    _disable_legacy(monkeypatch)
    from backend.engines import gpu_domains
    prov = create_provider("A", "openai_text", "http://10.0.0.1", "",
                           ["ma"])
    set_binding("dialog.text", prov["id"], "ma")
    assert is_remote_dialog_enabled() is True
    assert gpu_domains.resolve_feature_domain("dialog") == \
        gpu_domains.REMOTE_DOMAIN  # 对话云端=remote 伪域
    assert gpu_domains.resolve_feature_domain("paint") == \
        str(gpu_domains.resolve_feature_device("paint"))  # 绘画本地不变
    assert resolve_slot_cloud_model("manga.text") is None  # 漫剧文字未绑


def test_scene_dialog_local_manga_cloud(
        mem_kv, monkeypatch: pytest.MonkeyPatch) -> None:
    """对话=本地、漫剧文字=云端（文本槽位拆分的核心价值）。"""
    _disable_legacy(monkeypatch)
    prov = create_provider("B", "openai_text", "http://10.0.0.2", "",
                           ["mb"])
    set_binding("manga.text", prov["id"], "mb")
    assert is_remote_dialog_enabled() is False  # 对话页仍走本地
    got = resolve_slot_cloud_model("manga.text")
    assert got == f"cloud::{prov['id']}::mb"  # 漫剧文字走云端
    assert resolve_slot_cloud_model("novel.text") is None  # 写作台未绑


def test_scene_independent_text_slots(
        mem_kv, monkeypatch: pytest.MonkeyPatch) -> None:
    """漫剧文字与写作台绑不同连接，互不影响。"""
    _disable_legacy(monkeypatch)
    a = create_provider("漫剧云", "openai_text", "http://10.0.1.1", "", ["ma"])
    b = create_provider("写作云", "openai_text", "http://10.0.1.2", "", ["nb"])
    set_binding("manga.text", a["id"], "ma")
    set_binding("novel.text", b["id"], "nb")
    assert resolve_slot_cloud_model("manga.text") == f"cloud::{a['id']}::ma"
    assert resolve_slot_cloud_model("novel.text") == f"cloud::{b['id']}::nb"
    set_binding("dialog.text", b["id"], "nb")  # 对话页绑写作云也成立
    assert is_remote_dialog_enabled() is True
    # 停用漫剧云只影响漫剧文字
    from backend.services.cloud_provider_service import update_provider
    update_provider(a["id"], enabled=False)
    assert resolve_slot_cloud_model("manga.text") is None
    assert resolve_slot_cloud_model("novel.text") == f"cloud::{b['id']}::nb"


def test_text_slot_protocol_gate(mem_kv) -> None:
    img = create_provider("万相", "task_image",
                          "https://dashscope.aliyuncs.com", "k", [])
    for slot in ("manga.text", "novel.text", "dialog.text"):
        with pytest.raises(CloudProviderError) as ei:
            set_binding(slot, img["id"])
        assert ei.value.code == "CLOUD_PROTOCOL_MISMATCH"


# ── remote_backend.load 请求级解析 ───────────────────────────────

def test_remote_load_resolves_request_level_cloud_model(
        mem_kv, monkeypatch: pytest.MonkeyPatch, mock_vllm) -> None:
    """cloud:: 请求不依赖 dialog.text 绑定（漫剧/写作台绑定即可用）。"""
    _disable_legacy(monkeypatch)
    # dialog.text 故意不绑；只绑 manga.text 指向 mock vLLM
    prov = create_provider("漫剧云", "openai_text",
                           f"http://127.0.0.1:{mock_vllm.port}",
                           "", ["mock-32b"])
    set_binding("manga.text", prov["id"], "mock-32b")
    b = RemoteDialogBackend(health_poll_s=0.05, health_wait_s=5.0)
    assert b.load(f"cloud::{prov['id']}::mock-32b") is True
    assert b.model_id == "mock-32b"
    assert b._provider_name == "漫剧云"  # 事件日志数据源


def test_engine_first_cloud_load_with_dialog_local(
        mem_kv, monkeypatch: pytest.MonkeyPatch, mock_vllm) -> None:
    """对话本地 + 写作台云端：首次 cloud:: 装载直通（2026-09-06 实弹修复）。

    修复前：dialog.text 未绑定时 _remote_dialog_enabled() 为 False，
    cloud:: 请求掉进 model_manager 被「模型未下载」拒绝。
    """
    _disable_legacy(monkeypatch)
    from backend.services.inference.dialog_engine import get_dialog_engine
    prov = create_provider("写作云", "openai_text",
                           f"http://127.0.0.1:{mock_vllm.port}",
                           "", ["mock-32b"])
    set_binding("novel.text", prov["id"], "mock-32b")
    cs._invalidate_route_cache()
    assert is_remote_dialog_enabled() is False  # 对话仍本地
    eng = get_dialog_engine()
    assert eng.ensure_loaded(f"cloud::{prov['id']}::mock-32b"), eng.last_error()
    from backend.services.inference.backends.remote_backend import RemoteDialogBackend
    assert isinstance(eng._backend, RemoteDialogBackend)
    assert eng._backend._base_url.endswith(str(mock_vllm.port))


def test_engine_round_trip_between_two_providers(
        mem_kv, monkeypatch: pytest.MonkeyPatch, mock_vllm) -> None:
    """引擎级 A→B→A 往返（2026-09-06 实弹双 bug 回归哨兵）。

    bug1：load_model 持锁调 unload_model 重抢非重入锁 → 切换永挂；
    bug2：_pick_model 返回裸模型名 → load() 回落 dialog 槽位端点，
    对话=A 漫剧=B 时切 B 实际连 A。两个 bug 单看后端单测都测不到，
    必须走引擎全程。
    """
    _disable_legacy(monkeypatch)
    from backend.services.inference.dialog_engine import get_dialog_engine

    prov_a = create_provider("云A", "openai_text",
                             f"http://127.0.0.1:{mock_vllm.port}",
                             "", ["mock-32b"])
    # 服务商 B 用另一个 mock 端口（不同端点身份）
    from backend.tests.unit.mock_remote_vllm import FakeRemoteVLLM
    srv_b = FakeRemoteVLLM(port=0)
    srv_b.start()
    try:
        prov_b = create_provider("云B", "openai_text",
                                 f"http://127.0.0.1:{srv_b.port}",
                                 "", ["mock-32b"])
        set_binding("dialog.text", prov_a["id"], "mock-32b")
        set_binding("manga.text", prov_b["id"], "mock-32b")
        cs._invalidate_route_cache()
        eng = get_dialog_engine()
        req_a = f"cloud::{prov_a['id']}::mock-32b"
        req_b = f"cloud::{prov_b['id']}::mock-32b"
        def base() -> str:
            from backend.services.inference.backends.remote_backend import RemoteDialogBackend
            assert isinstance(eng._backend, RemoteDialogBackend)
            return eng._backend._base_url
        assert eng.ensure_loaded(req_a)
        assert base().endswith(str(mock_vllm.port))  # A
        assert eng.ensure_loaded(req_b), eng.last_error()
        assert base().endswith(str(srv_b.port)), base()  # 真切到 B（bug2）
        assert eng.ensure_loaded(req_a)
        assert base().endswith(str(mock_vllm.port))  # 往返回 A
        import time
        t0 = time.time()
        assert eng.ensure_loaded(req_b)
        assert time.time() - t0 < 3.0  # 幂等短路（毫秒级健康探测）
    finally:
        srv_b.stop()


# ── 云端调用事件日志 ─────────────────────────────────────────────

def test_record_cloud_call_writes_events(
        mem_kv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.services import event_log
    monkeypatch.setattr(event_log, "EVENTS_DIR", tmp_path / "events")
    record_cloud_call("text", "百炼-文本", "qwen-turbo", True, 400,
                      slot="dialog.text")
    record_cloud_call("image", "万相", "wanx2.1-t2i-turbo", False, 1200,
                      slot="keyframe.image", detail="HTTP 400: bad size")
    files = list((tmp_path / "events").glob("events-*.jsonl"))
    assert files, "事件日志文件未生成"
    lines = [json.loads(x) for x in
             files[0].read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(lines) == 2
    ok_ev, fail_ev = lines
    assert ok_ev["module"] == "cloud" and ok_ev["level"] == "success"
    assert "百炼-文本" in ok_ev["friendly"] and "0.4 秒" in ok_ev["friendly"]
    assert "对话" in ok_ev["detail"]  # slot 中文名落入 detail
    assert fail_ev["level"] == "error"
    assert "失败" in fail_ev["friendly"] and "测试连接" in fail_ev["friendly"]


def test_record_cloud_call_never_raises(
        mem_kv, monkeypatch: pytest.MonkeyPatch) -> None:
    """日志通道炸了不能影响业务（写坏 event_log 后调用不抛）。"""
    import backend.services.event_log as el
    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("disk full")
    monkeypatch.setattr(el, "log_event", _boom)
    record_cloud_call("video", "x", "m", False, 5, detail="e")  # 不应抛


# ── 诊断项 ───────────────────────────────────────────────────────

def test_probe_cloud_api_states(mem_kv, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.api.system import _probe_cloud_api
    _disable_legacy(monkeypatch)  # 隔离真机旧配置（迁移逻辑会迁它进来）
    # 未配置：pass
    status, detail = _probe_cloud_api()
    assert status == "pass" and "未配置" in detail
    # 配置了：汇报连接与绑定（打码）
    prov = create_provider("A", "openai_text", "http://10.0.0.1",
                           "sk-secret-9999", ["ma"])
    set_binding("novel.text", prov["id"], "ma")
    status2, detail2 = _probe_cloud_api()
    assert status2 == "pass"
    assert "1 条" in detail2 and "写作台" in detail2
    assert "sk-secret" not in detail2  # 绝不泄漏 Key


# ── 接线哨兵 ─────────────────────────────────────────────────────

def test_slot_split_wiring_sentinels() -> None:
    sb = (_ROOT_DIR / "backend/api/manga/storyboard.py").read_text(
        encoding="utf-8")
    assert 'resolve_slot_cloud_model("manga.text")' in sb  # AI 切分+描述词
    nv = (_ROOT_DIR / "backend/services/novel_service.py").read_text(
        encoding="utf-8")
    assert 'resolve_slot_cloud_model("novel.text")' in nv  # 写作台
    rb_src = (_ROOT_DIR / "backend/services/inference/backends/"
              "remote_backend.py").read_text(encoding="utf-8")
    assert "parse_cloud_model_id(model_id or \"\")" in rb_src  # 请求级 load
    sys_src = (_ROOT_DIR / "backend/api/system.py").read_text(
        encoding="utf-8")
    assert "云端 API 配置" in sys_src  # 诊断项

def test_dialog_ws_whitelist_passes_cloud_model() -> None:
    """对话 WS 白名单放行 cloud::（2026-09-06 真浏览器走查抓出的拦截）。

    模块白名单是本地模型注册表的管控语义，云端虚拟模型来自设置页
    用户配置的服务商连接，不在注册表内——不适用。源码级哨兵 +
    分支逻辑单验。
    """
    src = (_ROOT_DIR / "backend/api/dialog.py").read_text(encoding="utf-8")
    assert 'not str(model_req).startswith("cloud::")' in src

    # 分支逻辑直验：cloud:: 前缀绕过 allowed 集合
    allowed = {"qwen3-vl-4b"}
    model_req = "cloud::prov_x::mock-32b"
    rejected = (model_req and not str(model_req).startswith("cloud::")
                and allowed is not None and model_req not in allowed)
    assert rejected is False  # cloud:: 不再被白名单拦截
    local_req = "some-local-model"
    rejected2 = (local_req and not str(local_req).startswith("cloud::")
                 and allowed is not None and local_req not in allowed)
    assert rejected2 is True  # 本地未登记模型仍被拦（旧行为不变）
# 本项目仅供学习使用，商业授权请+Q 3559331368
