"""A6 对话排队位次登记表单测（2026-09-12，决策#8 位次广播）。

dialog.py 模块级登记表：位次 = 1 + 更早等待者数（按到达 monotonic 排序）；
退出幂等；未登记者按第 1 位报告。
"""
from __future__ import annotations

import time

from backend.api.dialog import (
    _dialog_queue_enter,
    _dialog_queue_exit,
    _dialog_queue_position,
)


def test_position_ordering_fifo_by_arrival():
    a, b, c = "sid-a", "sid-b", "sid-c"
    try:
        assert _dialog_queue_enter(a) == 1
        time.sleep(0.001)
        assert _dialog_queue_enter(b) == 2
        time.sleep(0.001)
        assert _dialog_queue_enter(c) == 3
        # 未登记者查询按第 1 位
        assert _dialog_queue_position("sid-x") == 1
        # 中间者退出，后位前移
        _dialog_queue_exit(b)
        assert _dialog_queue_position(c) == 2
    finally:
        _dialog_queue_exit(a)
        _dialog_queue_exit(c)
    assert _dialog_queue_position(a) == 1  # 已退出=无等待者时按 1 报告


def test_exit_idempotent():
    _dialog_queue_enter("sid-z")
    _dialog_queue_exit("sid-z")
    _dialog_queue_exit("sid-z")  # 幂等，不抛
    assert _dialog_queue_position("sid-z") == 1
