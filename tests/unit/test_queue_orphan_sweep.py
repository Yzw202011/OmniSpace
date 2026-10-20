"""批4 队列孤儿根治单测（2026-10-20，F-14 审计 SH-GAP-03 收口）。

不碰真实 DB/真实线程：
  - 启动清扫词表：train/style 补 queued、char_lora_tasks 全 ghost 态、
    learning_sessions running→interrupted（既有状态词）
  - char_lora cancel 存活判别：真训练置旗标；幽灵训练行直接落库取消
  - style resume 存活校验：worker 存活才放行；重启后 worker_gone 诚实拒绝
"""
from __future__ import annotations

import threading
from typing import Any

import pytest

from src.services import character_lora_service as cls_mod
from src.services import recovery
from src.services import style_lora_service as style_mod


class _FakeDb:
    """只记 SQL 不落盘：按表名回 preset 行数。"""

    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts
        self.statements: list[str] = []

    def sql(self, sql: str, params: tuple = ()) -> int:  # type: ignore[override]
        self.statements.append(sql)
        for table, n in self.counts.items():
            if table in sql:
                return n
        return 0


# ── 启动清扫词表（recovery.sweep_stale_tasks）────────────────────


def _run_sweep(monkeypatch: pytest.MonkeyPatch,
               counts: dict[str, int]) -> tuple[dict[str, int], _FakeDb]:
    fake = _FakeDb(counts)
    monkeypatch.setattr(recovery, "get_db_safe", lambda: fake)
    return recovery.sweep_stale_tasks(), fake


def test_sweep_covers_queued_for_train_and_style(
        monkeypatch: pytest.MonkeyPatch) -> None:
    swept, fake = _run_sweep(monkeypatch,
                             {"train_tasks": 2, "style_tasks": 1})
    assert swept["train_tasks"] == 2
    assert swept["style_tasks"] == 1
    train_sql = [s for s in fake.statements if "train_tasks" in s][0]
    style_sql = [s for s in fake.statements if "style_tasks" in s][0]
    for sql in (train_sql, style_sql):
        assert "'queued'" in sql, "queued 幽灵必须进清扫词表（锁死自动微调根因）"
        assert "'running'" in sql and "'training'" in sql


def test_sweep_covers_char_lora_ghosts(
        monkeypatch: pytest.MonkeyPatch) -> None:
    swept, fake = _run_sweep(monkeypatch, {"char_lora_tasks": 3})
    assert swept["char_lora_tasks"] == 3
    sql = [s for s in fake.statements if "char_lora_tasks" in s][0]
    assert "'queued'" in sql and "'training'" in sql
    assert "error=" in sql, "ghost 训练行必须带走中断原因"


def test_sweep_learning_sessions_running_interrupted(
        monkeypatch: pytest.MonkeyPatch) -> None:
    swept, fake = _run_sweep(monkeypatch, {"learning_sessions": 1})
    assert swept["learning_sessions"] == 1
    sql = [s for s in fake.statements if "learning_sessions" in s][0]
    assert "'interrupted'" in sql
    assert "stop_reason" in sql


def test_sweep_tolerates_missing_tables(
        monkeypatch: pytest.MonkeyPatch) -> None:
    counts: dict[str, int] = {}  # 全部 0 行也不许抛
    swept, _ = _run_sweep(monkeypatch, counts)
    assert isinstance(swept, dict)


def test_sweep_no_db_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recovery, "get_db_safe", lambda: None)
    assert recovery.sweep_stale_tasks() == {}


# ── char_lora cancel 存活判别 ────────────────────────────────────


class _StubCharService(cls_mod.CharacterLoraService):
    """绕过 __init__（不碰 DB/线程），只搭 cancel 分支所需最小状态。"""

    def __init__(self, training_id: str | None,
                 task: dict[str, Any]) -> None:
        self._cancel_flags: set[str] = set()
        self._training_task_id = training_id
        self._queue = []
        self._queue_lock = threading.Lock()
        self._mem_tasks = {}
        self._table_ready = False
        self.upserts: list[dict] = []
        self._task = task

    def _upsert_task(self, task: dict[str, Any]) -> None:
        self.upserts.append(dict(task))

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self._task


def _make_char_svc(training_id: str | None,
                   task: dict[str, Any]) -> tuple[_StubCharService,
                                                  list[dict]]:
    svc = _StubCharService(training_id, task)
    return svc, svc.upserts


def test_char_cancel_ghost_training_writes_db(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cls_mod, "_broadcast", lambda *a, **k: None)
    task = {"id": "t1", "status": "training"}
    svc, upserts = _make_char_svc(training_id=None, task=task)
    result = svc.cancel("t1")
    assert result["status"] == "cancelled", "幽灵训练行必须落库取消"
    assert len(upserts) == 1
    assert svc._cancel_flags == set(), "旗标无人消费，不许假装已置"


def test_char_cancel_live_training_sets_flag(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cls_mod, "_broadcast", lambda *a, **k: None)
    task = {"id": "t1", "status": "training"}
    svc, upserts = _make_char_svc(training_id="t1", task=task)
    result = svc.cancel("t1")
    assert "t1" in svc._cancel_flags, "真训练走旗标中断路径"
    assert result["status"] == "training", "真训练不由 cancel 直接改终态"
    assert upserts == []


# ── style resume 存活校验 ────────────────────────────────────────


class _StubStyleService(style_mod.StyleLoraService):
    """绕过 __init__（不碰 DB/线程），只搭 resume 存活校验所需最小状态。"""

    def __init__(self, training_id: str | None) -> None:
        self._training_task_id = training_id
        self._control_flags: dict[str, dict[str, bool]] = {}
        self.updates: list[tuple] = []

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return {"id": task_id, "status": "paused"}

    def _update_task(self, task_id: str, **fields: Any) -> None:
        self.updates.append((task_id, fields))


def _make_style_svc(training_id: str | None) -> _StubStyleService:
    return _StubStyleService(training_id)


def test_style_resume_worker_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(style_mod, "_broadcast", lambda *a, **k: None)
    svc = _make_style_svc("t1")
    ok, state = svc.resume_task("t1")
    assert ok is True and state == "training"
    assert svc._control_flags["t1"]["pause"] is False


def test_style_resume_worker_gone_honest_reject(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(style_mod, "_broadcast", lambda *a, **k: None)
    svc = _make_style_svc(None)  # 重启后：无训练线程存活
    ok, state = svc.resume_task("t1")
    assert ok is False
    assert state == "worker_gone", "盲翻 training=假 running，必须诚实拒绝"
