"""引擎惰性复位口单测（批5 专项根修，2026-09-19）。

锁定 reset_instance 三条契约：
1. 无实例时绝不构造（零副作用）——当年 conftest 反例的病根；
2. 有实例时清引用 + 置看门狗 stop 事件；
3. pytest 进程内看门狗线程不启动（PYTEST_CURRENT_TEST 闸契约）。
"""
# 本项目仅供学习使用，商业授权请+Q 3553191368
from __future__ import annotations

import pytest

from src.services.inference import dialog_engine as de


@pytest.fixture()
def _preserve_singleton():
    """保存/恢复模块级单例引用（测试不污染同进程其他测试）。"""
    old = de._engine_instance
    yield
    de._engine_instance = old


def test_reset_without_instance_never_constructs(_preserve_singleton) -> None:
    """契约①：无实例 → 返回 False 且不构造（零副作用）。

    当年 conftest 反例=拿 get_dialog_engine() 复位，每个测试凭空
    构造引擎+看门狗线程；本口必须在无实例时零动作。
    """
    de._engine_instance = None
    assert de.reset_instance() is False
    assert de._engine_instance is None  # 仍未构造


def test_reset_clears_instance_and_stops_watchdog(_preserve_singleton) -> None:
    """契约②：有实例 → 清引用 + 看门狗 stop 事件置位。"""
    eng = de.get_dialog_engine()  # 显式构造（测试愿意承担）
    assert de._engine_instance is eng
    assert de.reset_instance() is True
    assert de._engine_instance is None  # 引用已清
    assert eng._watchdog_stop.is_set()  # 看门狗收到停止信号
    assert de.reset_instance() is False  # 幂等：再清无东西


def test_reconstruct_after_reset_gets_fresh_instance(_preserve_singleton) -> None:
    """复位后再取 → 全新实例（跨测试状态不残留的语义保证）。"""
    old = de.get_dialog_engine()
    de.reset_instance()
    fresh = de.get_dialog_engine()
    assert fresh is not old
    de.reset_instance()


def test_watchdog_thread_not_started_in_pytest(_preserve_singleton) -> None:
    """契约③：pytest 进程内构造引擎，看门狗线程不启动
    （PYTEST_CURRENT_TEST 闸——批4 加入的时间污染断根保险）。"""
    import threading

    eng = de.get_dialog_engine()
    t = getattr(eng, "_watchdog_thread", None)
    assert t is not None
    assert not t.is_alive()
    assert t.name == "dialog-idle-watchdog"
    del t, eng, threading  # 显式表明仅检查生命周期
