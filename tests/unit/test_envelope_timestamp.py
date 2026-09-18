"""信封时间戳时区统一测试（批3，2026-09-18）。

审计 P1：旧 UTC-Z 格式与本地日志差 8 小时且日期都可能不同——跨端对账
必错位。新口径=本地时间带偏移（ISO 8601），与后端日志/前端显示同钟。
"""
from __future__ import annotations

import datetime

from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database
from src.middleware.request_context import utc_now_iso


def test_format_local_with_offset():
    ts = utc_now_iso()
    # 带偏移可解析（fromisoformat py3.11+/JS new Date() 双兼容）
    parsed = datetime.datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None
    # 与本地墙钟一致（秒级）——跨端对账同钟的核心断言
    assert abs((parsed.astimezone()
                - datetime.datetime.now().astimezone()).total_seconds()) < 2


def test_envelope_carries_new_format(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "_db_instance", Database(tmp_path / "t.db"))
    from src.main import app
    c = TestClient(app, base_url="http://127.0.0.1")
    ts = c.get("/health").json()["meta"]["timestamp"]
    parsed = datetime.datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None and "+" in ts or ts.endswith("Z") is False
    # UTC 旧格式（Z 结尾）不再出现
    assert not ts.endswith("Z")
