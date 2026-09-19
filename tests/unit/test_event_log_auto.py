"""自愈批2 API 事件日志自动兜底单测（2026-09-11，docs/自愈与横切内建方案 §批2）。

验收口径（方案 §四.3）：假 app 上——
  - POST（变更类）出行：level=success、模块映射正确、friendly 带用时
  - GET 成功且不慢：不出行（防刷屏）
  - 失败信封出行：level=error、friendly 带人话 message+suggestion（批1 联动）
  - 慢请求出行（阈值可调）
不碰 GPU、不碰真实服务；事件目录 monkeypatch 到 tmp_path。
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient
from starlette.types import Message

from src.middleware import event_log_auto
from src.middleware.error_handler import SUGGESTION_DEFAULTS, error, ok
from src.middleware.event_log_auto import EventLogAutoMiddleware, _module_for
from src.services import event_log


@pytest.fixture()
def events_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """事件日志写入重定向到临时目录（按测试隔离、不污染真实时间线）。

    批3 加固（2026-09-19 实弹）：跨测试异步泄漏实锤——早先测试把对话
    引擎单例置就绪后，其空闲卸载看门狗线程按墙钟在后续测试窗口触发
    unload→log_event 落进"当前"临时目录，污染本文件精确计数断言
    （全量跑 4/5 复现、单跑恒过、stash 批3 即绿的时序型偶发）。
    对策：_entries 只认本测试开始时刻之后的事件（测试密闭性）；
    引擎单例复位断根见 conftest autouse 夹具。
    """
    d = tmp_path / "events"
    d.mkdir()
    monkeypatch.setattr(event_log, "EVENTS_DIR", d)
    from datetime import datetime, timezone
    events_dir._t0 = datetime.now(timezone.utc)  # noqa: SLF001
    return d


@pytest.fixture()
def client(events_dir) -> TestClient:
    """假 app：真信封 helper（ok/error）+ 待测中间件，无其他中间件干扰。"""
    app = FastAPI()
    app.add_middleware(EventLogAutoMiddleware)

    @app.post("/api/v1/draw/generate")
    async def draw_generate() -> dict[str, Any]:
        return ok({"task_id": "t1"})

    @app.get("/api/v1/models/status")
    async def models_status() -> dict[str, Any]:
        return ok({"loaded": []})

    @app.post("/api/v1/knowledge/import")
    async def knowledge_import() -> JSONResponse:
        # 不传 suggestion——批1 起信封恒带默认出路，本层应把它拼进 friendly
        return error("KNOWLEDGE_PARSE_FAILED", "知识解析失败")

    @app.get("/api/v1/logs/flows/trace/bad")
    async def flow_trace_bad() -> JSONResponse:
        return error("SYSTEM_NOT_FOUND", "流程不存在: bad")

    @app.post("/api/v1/hardware/profile")
    async def hardware_profile() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.post("/api/v1/system/boom-big")
    async def boom_big() -> JSONResponse:
        # 超捕获上限（4KB）的失败信封：success 仍为首键，走前缀嗅探兜底
        return error("SYSTEM_INTERNAL_ERROR", detail={"blob": "x" * 20000})

    return TestClient(app)


def _entries() -> list[dict]:
    """本测试时间窗内的事件（滤掉跨测试异步泄漏的迟到写入）。"""
    from datetime import timezone
    t0 = getattr(events_dir, "_t0", None)
    items = event_log.query_events(limit=100)["items"]
    if t0 is None:
        return items
    out = []
    for it in items:
        try:
            ts = fromisoformat_local(it.get("ts") or "")
            if ts is not None and ts.astimezone(timezone.utc) >= t0:
                out.append(it)
        except (ValueError, TypeError):
            out.append(it)  # 时间解析不了的不滤（宁可失败不静默丢）
    return out


def fromisoformat_local(s: str):
    from datetime import datetime
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


# ── 三条件判定 ───────────────────────────────────────────────────

def test_post_success_logged_with_label(client: TestClient) -> None:
    r = client.post("/api/v1/draw/generate", json={})
    assert r.json()["success"] is True
    items = _entries()
    assert len(items) == 1
    e = items[0]
    assert e["module"] == "paint"
    assert e["event"] == "api_call"
    assert e["level"] == "success"
    assert "图片生成" in e["friendly"]
    assert "用时" in e["friendly"]
    assert e["duration_ms"] >= 0
    assert "POST" in e["detail"]


def test_get_success_fast_not_logged(client: TestClient) -> None:
    r = client.get("/api/v1/models/status")
    assert r.json()["success"] is True
    assert _entries() == []


def test_post_failure_envelope_error_level_with_suggestion(
        client: TestClient) -> None:
    r = client.post("/api/v1/knowledge/import", json={})
    assert r.json()["success"] is False
    items = _entries()
    assert len(items) == 1
    e = items[0]
    assert e["module"] == "knowledge"
    assert e["level"] == "error"
    assert e["event"] == "api_failed"
    assert "知识解析失败" in e["friendly"]
    # 批1 联动：默认出路自动拼进大白话
    assert SUGGESTION_DEFAULTS["KNOWLEDGE_PARSE_FAILED"] in e["friendly"]


def test_get_failure_envelope_logged(client: TestClient) -> None:
    r = client.get("/api/v1/logs/flows/trace/bad")
    assert r.json()["success"] is False
    items = _entries()
    assert len(items) == 1
    e = items[0]
    assert e["module"] == "system"
    assert e["level"] == "error"
    assert "流程不存在: bad" in e["friendly"]


def test_non_json_post_activity_logged(client: TestClient) -> None:
    r = client.post("/api/v1/hardware/profile")
    assert r.status_code == 200
    items = _entries()
    assert len(items) == 1
    e = items[0]
    assert e["module"] == "hardware"
    assert e["level"] == "success"


def test_non_api_path_not_logged(client: TestClient) -> None:
    r = client.get("/health")
    assert r.json() == {"status": "ok"}
    assert _entries() == []


def test_slow_get_logged_when_threshold_low(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(event_log_auto, "EVENT_LOG_SLOW_MS", 0)
    r = client.get("/api/v1/models/status")
    assert r.json()["success"] is True
    items = _entries()
    assert len(items) == 1
    assert items[0]["module"] == "models"
    assert items[0]["level"] == "success"


def test_oversized_failure_body_sniff_fallback(client: TestClient) -> None:
    r = client.post("/api/v1/system/boom-big", json={})
    assert r.json()["success"] is False
    items = _entries()
    assert len(items) == 1
    e = items[0]
    assert e["level"] == "error"
    assert e["event"] == "api_failed"


# ── 映射表与直调边界 ────────────────────────────────────────────

@pytest.mark.parametrize(("path", "expected"), [
    ("/api/v1/draw/generate", "paint"),
    ("/api/v1/paint/upscale", "paint"),
    ("/api/v1/manga/keyframe/generate", "manga"),
    ("/api/v1/learning/session/start", "learn"),
    ("/api/v1/knowledge/search", "knowledge"),
    ("/api/v1/models/load", "models"),
    ("/api/v1/dialog/send", "dialog"),
    ("/api/v1/brand_new_module/do", "brand_new_module"),  # 未登记自动可见
])
def test_module_mapping(path: str, expected: str) -> None:
    assert _module_for(path) == expected


def test_websocket_scope_passthrough(events_dir) -> None:
    """WS 作用域直接放行：不落档、不抛错。"""
    seen: list = []

    async def inner_app(scope, receive, send) -> None:
        seen.append(scope["type"])

    async def stub_receive() -> Message:
        return {"type": "websocket.connect"}

    async def stub_send(message: Message) -> None:
        return None

    import asyncio

    mw = EventLogAutoMiddleware(inner_app)
    asyncio.run(mw({"type": "websocket", "path": "/ws"},
                   stub_receive, stub_send))
    assert seen == ["websocket"]
    assert event_log.query_events(limit=10)["items"] == []
