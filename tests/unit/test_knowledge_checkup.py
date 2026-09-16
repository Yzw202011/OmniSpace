"""知识库体检周报单测（知识学习升级方案 批4，2026-09-11）。

假服务注入：不碰真实库/真实向量索引。
  - 脏数据计数与延迟探测汇成大白话事件（脏>0 或延迟>1s → warning）
  - 事件落档到临时目录（不污染真实时间线）
"""
from __future__ import annotations

import pytest

from src.services import event_log
from src.services import knowledge_checkup as kc


class _FakeKbSvc:
    def count(self) -> int:
        return 3

    def list_knowledge(self, page: int = 1, page_size: int = 20, **kw) -> dict:
        return {"items": [
            {"id": "k1", "type": "fact",
             "content": "短剧《x》类型为都市爱情，主题为逆袭。"},
            {"id": "k2", "type": "qa",
             "content": "问：怎么设计开场钩子？答：前三秒抛出核心冲突。"},
            {"id": "k3", "type": "fact",
             "content": "本书数字版权由果麦文化提供，并由其授权制作发行。"},
        ], "total": 3}


class _FakeInj:
    last_latency_ms = 0.0

    def retrieve(self, q: str, top_k: int = 5) -> list:
        self.last_latency_ms = 1500.0  # 触发延迟 WARNING 线
        return []


def test_run_checkup_reports_dirty_and_latency(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    ev = tmp_path / "events"
    ev.mkdir()
    monkeypatch.setattr(event_log, "EVENTS_DIR", ev)
    monkeypatch.setattr("src.services.knowledge_service.get_knowledge_service",
                        lambda: _FakeKbSvc())
    monkeypatch.setattr("src.services.injection_service.get_injection_service",
                        lambda: _FakeInj())

    report = kc.run_checkup()
    assert report["total"] == 3
    assert report["dirty"] == 1  # k3 = 图书商品页残渣
    assert report["latency_ms"] >= 1500.0
    assert "1 条脏数据" in report["friendly"]
    assert "超过 1 秒" in report["friendly"]

    items = event_log.query_events(limit=10)["items"]
    assert len(items) == 1
    e = items[0]
    assert e["module"] == "system"
    assert e["level"] == "warning"
    assert "知识库体检" in e["friendly"]


def test_run_checkup_all_green(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    ev = tmp_path / "events"
    ev.mkdir()
    monkeypatch.setattr(event_log, "EVENTS_DIR", ev)
    monkeypatch.setattr("src.services.knowledge_service.get_knowledge_service",
                        lambda: _CleanKbSvc())

    class _FastInj(_FakeInj):
        def retrieve(self, q: str, top_k: int = 5) -> list:
            self.last_latency_ms = 12.0
            return []

    monkeypatch.setattr("src.services.injection_service.get_injection_service",
                        lambda: _FastInj())

    report = kc.run_checkup()
    assert report["dirty"] == 0
    assert report["total"] == 2
    assert report["latency_ms"] < 1000.0
    assert "一切正常" in report["friendly"]
    items = event_log.query_events(limit=10)["items"]
    assert items and items[0]["level"] == "info"


class _CleanKbSvc(_FakeKbSvc):
    def list_knowledge(self, page: int = 1, page_size: int = 20, **kw) -> dict:
        return {"items": [
            {"id": "k1", "type": "fact",
             "content": "短剧《x》类型为都市爱情，主题为逆袭。"},
            {"id": "k2", "type": "qa",
             "content": "问：怎么设计开场钩子？答：前三秒抛出核心冲突。"},
        ], "total": 2}

    def count(self) -> int:
        return 2
