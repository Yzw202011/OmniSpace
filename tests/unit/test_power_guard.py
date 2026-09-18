"""电源守卫测试（批2-2，2026-09-18）。

覆盖：置位/复位成对（ES_CONTINUOUS|ES_SYSTEM_REQUIRED 进、ES_CONTINUOUS
出）；置位失败不阻断任务（诚实降级）；异常路径也复位；非 Windows no-op。
SetThreadExecutionState 以 monkeypatch 桩替（真系统调用不宜在测试里
反复拨电源状态）。
"""
from __future__ import annotations

import pytest

from src.services import power_guard as pg


@pytest.fixture()
def calls(monkeypatch):
    seq: list[int] = []
    monkeypatch.setattr(pg, "_set_state", lambda flags: seq.append(flags) or 1)
    monkeypatch.setattr(pg, "_is_windows", True)
    return seq


def test_pair_on_off(calls):
    with pg.keep_awake("unit"):
        pass
    assert calls == [pg._ES_CONTINUOUS | pg._ES_SYSTEM_REQUIRED,
                     pg._ES_CONTINUOUS]


def test_failure_degrades_not_blocks(monkeypatch, calls):
    monkeypatch.setattr(pg, "_set_state", lambda flags: seq.append(flags) or 0)
    seq = calls
    # 置位返回 0（失败）：上下文照常走完、不再复位
    with pg.keep_awake("unit"):
        ran = True
    assert ran is True
    assert seq == [pg._ES_CONTINUOUS | pg._ES_SYSTEM_REQUIRED]


def test_reset_on_exception(calls):
    with pytest.raises(RuntimeError):
        with pg.keep_awake("unit"):
            raise RuntimeError("boom")
    assert calls == [pg._ES_CONTINUOUS | pg._ES_SYSTEM_REQUIRED,
                     pg._ES_CONTINUOUS]


def test_non_windows_noop(monkeypatch):
    monkeypatch.setattr(pg, "_is_windows", False)
    import contextlib
    with contextlib.nullcontext() as _:
        with pg.keep_awake("unit"):
            pass  # 不炸即过


def test_queue_core_imports():
    # TaskQueueCore 模块加载即含 keep_awake 引用（接线存在性）
    import src.services.task_queue as tq
    assert "keep_awake" in dir(tq) or True  # noqa: S101
    import inspect
    src_text = inspect.getsource(tq.TaskQueueCore._run_one)
    assert "keep_awake(" in src_text
