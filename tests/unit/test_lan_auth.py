"""LAN 鉴权中间件测试（批2-1，2026-09-18）。

纯 ASGI 中间件直测（手搓 scope/receive/send）：回环豁免/非回环无令牌
401 拒绝/有效令牌放行/豁免路径放行/WS 无令牌 4401 关闭/静态资源放行/
闸关闭（回环绑定）全通透。TestClient 级另验信封格式。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database
from src.middleware import lan_auth as la


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.called_app = False

    async def send(self, msg: dict) -> None:
        self.sent.append(msg)

    async def app(self, scope, receive, send):
        self.called_app = True
        await send({"type": "http.response.start", "status": 200,
                    "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


def _scope(path="/api/v1/models", client="192.168.1.50",
           stype="http", headers=None, qs=b""):
    return {"type": stype, "path": path,
            "headers": headers or [], "query_string": qs,
            "client": (client, 12345)}


@pytest.fixture()
def armed(monkeypatch):
    monkeypatch.setattr(la, "HOST", "0.0.0.0", raising=False)
    monkeypatch.setattr(la, "get_lan_token", lambda: "tok123")
    return None


@pytest.mark.asyncio
async def test_loopback_bypass(armed):
    r = _Recorder()
    await la.LanAuthMiddleware(r.app)(_scope(client="127.0.0.1"),
                                      _receive, r.send)
    assert r.called_app is True  # 回环直通


@pytest.mark.asyncio
async def test_remote_no_token_rejected(armed):
    r = _Recorder()
    await la.LanAuthMiddleware(r.app)(_scope(), _receive, r.send)
    assert r.called_app is False
    start = r.sent[0]
    assert start["status"] == 401
    body = json.loads(r.sent[1]["body"])
    assert body["error"]["code"] == "LAN_AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_remote_valid_token_passes(armed):
    r = _Recorder()
    hdrs = [(b"x-omni-token", b"tok123")]
    await la.LanAuthMiddleware(r.app)(_scope(headers=hdrs), _receive, r.send)
    assert r.called_app is True


@pytest.mark.asyncio
async def test_token_via_query_string(armed):
    r = _Recorder()
    await la.LanAuthMiddleware(r.app)(_scope(qs=b"token=tok123"),
                                      _receive, r.send)
    assert r.called_app is True


@pytest.mark.asyncio
async def test_exempt_paths(armed):
    for path in ("/", "/assets/app.js", "/health", "/api/quit"):
        r = _Recorder()
        await la.LanAuthMiddleware(r.app)(_scope(path=path), _receive, r.send)
        assert r.called_app is True, f"{path} 应豁免"


@pytest.mark.asyncio
async def test_websocket_rejected_without_token(armed):
    r = _Recorder()
    await la.LanAuthMiddleware(r.app)(
        _scope(stype="websocket", path="/api/v1/dialog/stream/s1"),
        _receive, r.send)
    assert r.called_app is False
    assert r.sent == [{"type": "websocket.close", "code": 4401,
                       "reason": "LAN token required"}]


@pytest.mark.asyncio
async def test_disabled_when_loopback(monkeypatch):
    monkeypatch.setattr(la, "HOST", "127.0.0.1", raising=False)
    r = _Recorder()
    await la.LanAuthMiddleware(r.app)(_scope(), _receive, r.send)
    assert r.called_app is True  # 回环绑定=闸不参与


def test_api_level_envelope(tmp_path, monkeypatch):
    """TestClient 级：闸关闭（默认回环绑定）时 lan-token 端点可用。"""
    monkeypatch.setattr(db_mod, "_db_instance", Database(tmp_path / "t.db"))
    from src.main import app
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.get("/api/v1/system/lan-token")
    body = r.json()
    assert body["success"] is True
    assert body["data"]["enabled"] is False and body["data"]["token"] == ""
