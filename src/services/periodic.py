"""周期巡检 daemon 工厂（B5 步 5，2026-09-14）。

病根（R2 锁序普查）：event_log / flow_trace / knowledge_checkup 三处
各自克隆「while True + try 巡检 + sleep 间隔」的 daemon 循环——克隆
漂移温床（heartbeat / resource_sampler 用 Event.wait 形态自带停止
语义，不在收敛范围，诚实记录）。

用法：
    from src.services.periodic import start_periodic_daemon
    start_periodic_daemon("log-cleanup", 86400.0, cleanup_expired)

语义：
  - daemon 线程，fn 异常被捕获记日志（巡检失败不影响业务）；
  - 先等 interval 再跑（run_immediately=True 则先跑一次）；
  - stop_event 可选（需要优雅停止的调用方传入，wait 可中断优于 sleep）。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger("omnispace.services.periodic")


def start_periodic_daemon(
    name: str,
    interval_s: float,
    fn: Callable[[], Any],
    *,
    run_immediately: bool = False,
    stop_event: threading.Event | None = None,
    log_success: bool = False,
) -> threading.Thread:
    """启动周期巡检 daemon 线程并立即返回。

    Args:
        name: 线程名（任务管理器/日志排障用）
        interval_s: 巡检间隔（秒）
        fn: 巡检函数（异常捕获记日志，绝不上抛）
        run_immediately: True=启动即先跑一次再进入等待循环
        stop_event: 可选停止事件（set 后线程退出；None=进程生命周期）
        log_success: True=每次巡检成功记一条 info（默认静默）
    """
    stop = stop_event if stop_event is not None else threading.Event()

    def _loop() -> None:
        first = run_immediately
        while not stop.wait(0.0 if first else interval_s):
            first = False
            try:
                fn()
                if log_success:
                    log.info("周期巡检完成: %s", name)
            except Exception:  # noqa: BLE001 - 巡检失败不影响业务
                log.exception("周期巡检异常: %s", name)
            # 统一在巡检后等待（首跑立即模式在循环头已消化零等待）

    t = threading.Thread(target=_loop, name=name, daemon=True)
    t.start()
    log.info("周期巡检已启动: %s（每 %.0fs）", name, interval_s)
    return t
