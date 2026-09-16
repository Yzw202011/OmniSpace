"""自愈批3 模型自动加载依赖工厂单测（2026-09-11，方案 §批3）。

假引擎直测 + FastAPI Depends 集成，不碰 GPU/真实引擎：
  - 就绪引擎直通（零开销，不装载/不广播/不落档）
  - 未就绪→装载成功：返回引擎 + WS 广播 + 时间线 success
  - 未就绪→装载返回 False：MODEL_LOAD_FAILED + 时间线 error
  - 未就绪→装载抛异常：同上，错误串透传
  - 端点集成：Depends 声明后卸载态直调即自动装载并成功
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.api.deps import require_engine
from src.middleware.error_handler import ApiError, error, ok
from src.services import event_log


class FakeEngine:
    """四态可拧的假引擎（协议对齐 paint 系：is_ready/ensure_loaded/get_status）。"""

    def __init__(self, *, ready: bool = False, load_result: bool = True,
                 exc: Exception | None = None) -> None:
        self.is_ready = ready
        self.load_result = load_result
        self.exc = exc
        self.load_calls: list[str | None] = []

    def ensure_loaded(self, hint: str | None = None) -> bool:
        self.load_calls.append(hint)
        if self.exc is not None:
            raise self.exc
        if self.load_result:
            self.is_ready = True
        return self.load_result

    def get_status(self) -> dict[str, Any]:
        return {"last_error": "boom-detail"}


class FakeHub:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def broadcast(self, payload: dict) -> None:
        self.payloads.append(payload)


@pytest.fixture()
def events_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    d = tmp_path / "events"
    d.mkdir()
    monkeypatch.setattr(event_log, "EVENTS_DIR", d)
    return d


@pytest.fixture()
def hub(monkeypatch: pytest.MonkeyPatch) -> FakeHub:
    fake = FakeHub()

    def _fake_get_ws_hub() -> FakeHub:
        return fake

    monkeypatch.setattr(
        __import__("src.services.ws_hub", fromlist=["get_ws_hub"]),
        "get_ws_hub", _fake_get_ws_hub)
    return fake


# ── 依赖直调四态 ─────────────────────────────────────────────────

@pytest.mark.asyncio()
async def test_ready_engine_passthrough(events_dir, hub) -> None:
    engine = FakeEngine(ready=True)
    dep = require_engine(lambda: engine, label="绘画模型", module="paint")
    assert await dep() is engine
    assert engine.load_calls == []
    assert hub.payloads == []
    assert event_log.query_events(limit=10)["items"] == []


@pytest.mark.asyncio()
async def test_not_ready_load_success(events_dir, hub) -> None:
    engine = FakeEngine(ready=False, load_result=True)
    dep = require_engine(lambda: engine, hint="flux2-klein-4b",
                         label="绘画模型", module="paint")
    assert await dep() is engine
    assert engine.load_calls == ["flux2-klein-4b"]
    # WS 广播：起点 running + 终点 success
    statuses = [p["data"]["status"] for p in hub.payloads]
    assert statuses == ["running", "success"]
    assert "绘画模型加载中" in hub.payloads[0]["data"]["label"]
    # 时间线 success 自动可见
    items = event_log.query_events(limit=10)["items"]
    assert len(items) == 1
    assert items[0]["module"] == "paint"
    assert items[0]["level"] == "success"
    assert items[0]["event"] == "engine_autoload"
    assert "绘画模型加载完成" in items[0]["friendly"]


@pytest.mark.asyncio()
async def test_not_ready_load_false_raises(events_dir, hub) -> None:
    engine = FakeEngine(ready=False, load_result=False)
    dep = require_engine(lambda: engine, label="绘画模型", module="paint")
    with pytest.raises(ApiError) as ei:
        await dep()
    assert ei.value.code == "MODEL_LOAD_FAILED"
    assert "boom-detail" in ei.value.message
    statuses = [p["data"]["status"] for p in hub.payloads]
    assert statuses == ["running", "error"]
    items = event_log.query_events(limit=10)["items"]
    assert len(items) == 1
    assert items[0]["level"] == "error"
    assert "自动加载失败" in items[0]["friendly"]


@pytest.mark.asyncio()
async def test_not_ready_load_exception_raises(events_dir, hub) -> None:
    engine = FakeEngine(ready=False, exc=RuntimeError("cuda boom"))
    dep = require_engine(lambda: engine, label="绘画模型", module="paint")
    with pytest.raises(ApiError) as ei:
        await dep()
    assert ei.value.code == "MODEL_LOAD_FAILED"
    assert "cuda boom" in ei.value.message


# ── FastAPI Depends 集成 ─────────────────────────────────────────

def _app_with_handler() -> tuple[FastAPI, FakeEngine]:
    app = FastAPI()
    engine = FakeEngine(ready=False, load_result=True)

    @app.exception_handler(ApiError)
    async def api_error_handler(_: Any, exc: ApiError) -> Any:
        return error(exc.code, exc.message, exc.detail, suggestion=exc.suggestion)

    @app.post("/api/v1/fake/gen")
    async def fake_gen(
        engine: Any = Depends(require_engine(
            lambda: engine, label="绘画模型", module="paint")),
    ) -> dict[str, Any]:
        return ok({"used_engine": engine is engine})

    return app, engine


def test_endpoint_depends_autoload_then_success(events_dir, hub) -> None:
    app, engine = _app_with_handler()
    client = TestClient(app)
    assert engine.is_ready is False
    r = client.post("/api/v1/fake/gen")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["used_engine"] is True
    assert engine.is_ready is True
    # 二次调用：引擎已就绪，零装载直通
    calls_before = len(engine.load_calls)
    r2 = client.post("/api/v1/fake/gen")
    assert r2.json()["success"] is True
    assert len(engine.load_calls) == calls_before


def test_endpoint_failure_envelope_carries_suggestion(events_dir, hub,
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, engine = _app_with_handler()
    engine.load_result = False
    client = TestClient(app)
    r = client.post("/api/v1/fake/gen")
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "MODEL_LOAD_FAILED"
    # 批1 联动：信封恒带默认出路
    assert body["error"]["suggestion"].strip()
