"""批3 远程引擎单测（2026-09-05，mock vLLM 全链）。

覆盖（实施计划 §3.3）：
- 健康探测：/health 404 回退 /v1/models、冷启动前 N 次 503 有界轮询后就绪；
- chat_stream SSE 解析（增量拼接）+ 请求透传链路（Authorization/model/
  repetition_penalty 并入请求体）；
- 连接失败/持续不可达 → 带出路的 RuntimeError / load False；
- 资源域：远程启用时 dialog→"remote" 伪域（与本地绘画天然并行），
  关闭时回落本地卡号；功能锁真实链路验证异域并行；
- 接线哨兵：引擎短路/工厂注册/资源域接线源码必须存在。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.engines import gpu_domains
from src.middleware.feature_lock import FeatureLockManager
from src.services.inference.backends.remote_backend import (
    RemoteDialogBackend,
    probe_remote_health,
)
from tests.unit.mock_remote_vllm import FakeRemoteVLLM

_ROOT_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture()
def mock_vllm():
    srv = FakeRemoteVLLM(port=0)
    srv.start()
    yield srv
    srv.stop()


def _backend_on(srv: FakeRemoteVLLM) -> RemoteDialogBackend:
    """指向 mock 服务器的后端实例（快轮询/短超时，测试毫秒级跑完）。"""
    return RemoteDialogBackend(health_poll_s=0.05, health_wait_s=5.0)


def _enable_remote(monkeypatch: pytest.MonkeyPatch, base_url: str,
                   model: str = "mock-32b") -> None:
    """注入「远程已启用」配置（绕开真实 KV 读取与 TTL 缓存）。

    批1 云端API（2026-09-06）：旧配置兼容层仅在「未迁移」机器生效——
    一并模拟未迁移态（cloud KV 全空），否则已迁移开发机上兼容层退位、
    旧配置注入被无视。
    """
    import src.services.cloud_provider_service as _cs
    import src.services.inference.backends.remote_backend as rb
    monkeypatch.setattr(rb, "_read_settings_kv",
                        lambda: {"remote_dialog_enabled": True,
                                 "remote_dialog_base_url": base_url,
                                 "remote_dialog_model": model})
    monkeypatch.setattr(rb, "_cfg_cache", None)
    monkeypatch.setattr(_cs, "_read_kv",
                        lambda key, default=None: default)
    _cs._invalidate_route_cache()


# ── 健康探测与装载 ────────────────────────────────────────────────

def test_probe_health_ok_and_closed_port_unreachable(mock_vllm):
    ok, err = probe_remote_health(f"http://127.0.0.1:{mock_vllm.port}")
    assert ok and err == ""
    ok2, _ = probe_remote_health(f"http://127.0.0.1:{mock_vllm.port + 1}")
    assert ok2 is False  # 无人监听的端口必须不可达


def test_load_bounded_wait_survives_cold_start(monkeypatch):
    """远端冷启动（前 2 次 503）→ 有界轮询后装载成功。"""
    srv = FakeRemoteVLLM(port=0, fail_health_first=2)
    srv.start()
    try:
        _enable_remote(monkeypatch, f"http://127.0.0.1:{srv.port}")
        b = RemoteDialogBackend(health_poll_s=0.05, health_wait_s=5.0)
        assert b.load("mock-32b") is True
        assert b.is_ready()
    finally:
        srv.stop()


def test_load_timeout_reports_honest_error(monkeypatch):
    """持续不可达 → load False + 报错带出路（不留假 ready）。"""
    srv = FakeRemoteVLLM(port=0)  # 起了又停 → 端口必然拒连
    srv.start()
    port = srv.port
    srv.stop()
    _enable_remote(monkeypatch, f"http://127.0.0.1:{port}")
    b = RemoteDialogBackend(health_poll_s=0.05, health_wait_s=0.2)
    assert b.load("mock-32b") is False
    assert "持续不可达" in b.last_error()
    assert "测试连接" in b.last_error()


# ── 对话流与请求透传 ──────────────────────────────────────────────

def test_chat_stream_parses_sse_and_passes_params(mock_vllm, monkeypatch):
    _enable_remote(monkeypatch, f"http://127.0.0.1:{mock_vllm.port}")
    b = _backend_on(mock_vllm)
    assert b.load("mock-32b") is True
    chunks = list(b.chat_stream(
        [{"role": "user", "content": "hi"}],
        temperature=0.8, max_new_tokens=64,
        extra_params={"repetition_penalty": 1.15}))
    assert "".join(chunks) == "你好，世界"
    req = mock_vllm.last_request
    assert req["payload"]["model"] == "mock-32b"
    assert req["payload"]["repetition_penalty"] == 1.15
    assert req["payload"]["stream"] is True


def test_chat_stream_sends_bearer_auth(mock_vllm, monkeypatch):
    import src.services.cloud_provider_service as _cs
    import src.services.inference.backends.remote_backend as rb
    monkeypatch.setattr(rb, "_read_settings_kv",
                        lambda: {"remote_dialog_enabled": True,
                                 "remote_dialog_base_url":
                                     f"http://127.0.0.1:{mock_vllm.port}",
                                 "remote_dialog_api_key": "sk-test-123",
                                 "remote_dialog_model": "mock-32b"})
    monkeypatch.setattr(rb, "_cfg_cache", None)
    # 批1 云端API：模拟未迁移机器（旧配置兼容层才生效）
    monkeypatch.setattr(_cs, "_read_kv",
                        lambda key, default=None: default)
    _cs._invalidate_route_cache()
    b = _backend_on(mock_vllm)
    assert b.load("mock-32b") is True
    list(b.chat_stream([{"role": "user", "content": "hi"}]))
    assert mock_vllm.last_request["authorization"] == "Bearer sk-test-123"


# ── 资源域：remote 伪域与本地并行 ─────────────────────────────────

def test_gpu_domain_remote_for_dialog(mock_vllm, monkeypatch):
    _enable_remote(monkeypatch, f"http://127.0.0.1:{mock_vllm.port}")
    assert gpu_domains.resolve_feature_domain("dialog") == "remote"
    # 本地功能不受影响（单卡机 → "0"）
    assert gpu_domains.resolve_feature_domain("paint") == "0"


def test_gpu_domain_local_when_disabled(monkeypatch):
    import src.services.inference.backends.remote_backend as rb
    monkeypatch.setattr(rb, "_read_settings_kv", lambda: {})
    monkeypatch.setattr(rb, "_cfg_cache", None)
    # 同步隔离槽位绑定 KV（实弹验收会在库里留下真绑定，必须钉死空场）
    import src.services.cloud_provider_service as cps
    monkeypatch.setattr(cps, "_read_kv", lambda key, default=None: default)
    monkeypatch.setattr(cps, "_write_kv", lambda key, value: None)
    monkeypatch.setattr(cps, "_endpoint_cache", None)
    assert gpu_domains.resolve_feature_domain("dialog") == "0"


def test_feature_lock_remote_dialog_parallel_with_local_paint(mock_vllm,
                                                              monkeypatch):
    """真链路：远程对话 + 本地绘画同域互斥解除（D3 红利）。"""
    _enable_remote(monkeypatch, f"http://127.0.0.1:{mock_vllm.port}")
    # 真实 _domain_of（内部走 gpu_domains→remote_backend 真实解析）
    m = FeatureLockManager()
    assert asyncio.run(m.acquire("dialog")) is True   # remote 伪域
    assert asyncio.run(m.acquire("paint")) is True    # 本地 0 卡，并行！
    holders = m.holders
    assert holders.get("remote") == "dialog"
    assert holders.get("0") == "paint"
    asyncio.run(m.release("dialog"))
    asyncio.run(m.release("paint"))
    assert m.holders == {}


def test_feature_lock_release_domain_hot_switch_fallback(monkeypatch):
    """远程开关热切换兜底：acquire 在旧域、release 在新域也能放锁。"""
    from src.middleware import feature_lock as fl_mod
    monkeypatch.setattr(fl_mod, "_domain_of", lambda f: "0")
    m = FeatureLockManager()
    # 手工制造「dialog 持有在 remote 域」的现场（acquire 时远程开启）
    import time as _t
    m._holders["remote"] = "dialog"
    m._hold_counts["remote"] = 1
    m._acquired_ats["remote"] = _t.time()
    # 释放时域解析回落本地 "0"（模拟远程开关刚被关闭）→ 应兜底找回并放锁
    asyncio.run(m.release("dialog"))
    assert m.holders == {}


# ── 接线哨兵 ──────────────────────────────────────────────────────

def test_remote_wiring_sentinels():
    dlg = (_ROOT_DIR / "src" / "services" / "inference"
           / "dialog_engine.py").read_text(encoding="utf-8")
    assert "_remote_dialog_enabled" in dlg, "引擎远程短路被删"
    assert 'name", "") == "remote"' in dlg, "远程就绪快速返回被删"
    init = (_ROOT_DIR / "src" / "services" / "inference"
            / "backends" / "__init__.py").read_text(encoding="utf-8")
    assert 'kind == "remote"' in init, "工厂未注册 remote 后端"
    gdom = (_ROOT_DIR / "src" / "engines" / "gpu_domains.py").read_text(
        encoding="utf-8")
    assert "is_remote_dialog_enabled" in gdom, "资源域 remote 伪域接线被删"
    sysapi = (_ROOT_DIR / "src" / "api" / "system.py").read_text(
        encoding="utf-8")
    assert "dialog-remote/test" in sysapi, "测试连接端点被删"
# 本项目仅供学习使用，商业授权请+Q 3559331368
