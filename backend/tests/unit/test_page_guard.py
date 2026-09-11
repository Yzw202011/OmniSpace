"""页面守卫状态机单测（2026-09-03 方案A：页面全关自动退出）。

核心契约：
  - 只有「开过页面又全部关闭」才是退出信号——从头无页面（无头/
    API-only 实例）永不触发；
  - 任一页面重连即取消倒计时；
  - 倒计时到点：忙 → abort 且重新武装（任务跑完自动续），闲 → fire；
  - 未启用：永不 fire（saw_pages 保留，重新启用后无需再开页面）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

from backend.services.page_guard import GuardState, step


def test_never_opened_never_fires():
    """无头/API-only：count 恒 0，不武装不触发。"""
    st = GuardState()
    for i in range(100):
        action = step(st, count=0, busy=False, enabled=True,
                      countdown_s=60, now=i * 5.0)
        assert action == "none", f"第{i}轮不该触发: {action}"
    assert st.saw_pages is False


def test_close_starts_countdown_then_fires_when_idle():
    st = GuardState()
    assert step(st, count=1, busy=False, enabled=True,
                countdown_s=60, now=0) == "none"
    assert st.saw_pages is True
    # 全关：武装
    assert step(st, count=0, busy=False, enabled=True,
                countdown_s=60, now=5) == "arm"
    assert st.deadline == 65.0
    # 倒计时中（55s）：未到点
    assert step(st, count=0, busy=False, enabled=True,
                countdown_s=60, now=55) == "none"
    # 到点且闲：fire
    assert step(st, count=0, busy=False, enabled=True,
                countdown_s=60, now=65.1) == "fire"


def test_reconnect_cancels_countdown():
    st = GuardState()
    step(st, count=1, busy=False, enabled=True, countdown_s=60, now=0)
    step(st, count=0, busy=False, enabled=True, countdown_s=60, now=5)
    assert st.deadline == 65.0
    # 重连：取消
    assert step(st, count=2, busy=False, enabled=True,
                countdown_s=60, now=30) == "none"
    assert st.deadline is None
    # 再关：重新武装（新的 60s）
    assert step(st, count=0, busy=False, enabled=True,
                countdown_s=60, now=40) == "arm"
    assert st.deadline == 100.0


def test_busy_at_deadline_aborts_and_rearms():
    st = GuardState()
    step(st, count=1, busy=False, enabled=True, countdown_s=60, now=0)
    step(st, count=0, busy=False, enabled=True, countdown_s=60, now=5)
    # 到点但忙：abort 且 deadline 清空（下轮重新武装）
    assert step(st, count=0, busy=True, enabled=True,
                countdown_s=60, now=65.1) == "abort"
    assert st.deadline is None
    # 任务跑完后的下一轮：重新武装并最终 fire（关了页面就该退）
    assert step(st, count=0, busy=False, enabled=True,
                countdown_s=60, now=70) == "arm"
    assert step(st, count=0, busy=False, enabled=True,
                countdown_s=60, now=130.1) == "fire"


def test_disabled_never_fires_but_keeps_saw_pages():
    st = GuardState()
    step(st, count=1, busy=False, enabled=True, countdown_s=60, now=0)
    assert st.saw_pages is True
    for t in (5, 100, 10000):
        assert step(st, count=0, busy=False, enabled=False,
                    countdown_s=60, now=t) == "none"
    assert st.saw_pages is True, "重新启用后不应要求再开一次页面"


def test_short_countdown_respected():
    st = GuardState()
    step(st, count=1, busy=False, enabled=True, countdown_s=10, now=0)
    assert step(st, count=0, busy=False, enabled=True,
                countdown_s=10, now=5) == "arm"
    assert st.deadline == 15.0
    assert step(st, count=0, busy=False, enabled=True,
                countdown_s=10, now=14.9) == "none"
    assert step(st, count=0, busy=False, enabled=True,
                countdown_s=10, now=15.1) == "fire"
