# 本项目仅供学习使用，商业授权请+Q 3559331368
"""电源守卫（批2-2，2026-09-18）：长任务期间阻止 Windows 按电源计划睡眠。

四轮审计「未考虑到」清单实项：小时级漫剧/视频生成/训练任务期间，系统
睡眠即硬中断任务（无挂起协议、无恢复续跑）——keep_awake 上下文管理器
经 SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED) 抑制。

接线位（单一职责，勿散装）：
- services/task_queue.py TaskQueueCore._run_one：统一队列每任务包一层
  （图像/视频/一切入队任务全覆盖）；
- 三个训练服务 worker 的训练段（learn/style/character 队列不走
  TaskQueueCore）。

注意：SetThreadExecutionState 为**线程亲和**（continuous 标记随线程
退出自动清除）——只在线程体内成对使用，勿跨线程 engage/disengage。
非 Windows 平台 no-op（保守：不炸不拦）。
"""
from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager

log = logging.getLogger("omnispace.services.power_guard")

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

_is_windows = sys.platform == "win32"


def _set_state(flags: int) -> int:
    import ctypes
    return ctypes.windll.kernel32.SetThreadExecutionState(flags)


@contextmanager
def keep_awake(reason: str = "task") -> Iterator[None]:
    """任务执行段抑制系统睡眠（线程内成对：进=置位，出=复位）。

    置位失败不阻断任务（诚实降级：记 debug 日志继续跑——睡眠风险
    回到"无防御"基线，而非让任务因电源 API 失败而拒跑）。
    """
    if not _is_windows:
        yield
        return
    ok = _set_state(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
    if ok == 0:
        log.debug("keep_awake 置位失败（任务继续，睡眠防御退回基线）: %s",
                     reason)
    try:
        yield
    finally:
        if ok != 0:
            _set_state(_ES_CONTINUOUS)
