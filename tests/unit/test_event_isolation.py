"""事件流测试隔离测试（依赖文档审计批1，2026-09-17）。

验证：pytest 态下事件 jsonl 默认分流 events-test（产品错误面板零测试
流量）；逃逸闸 OMNISPACE_OBSERVE_IN_TESTS=1 恢复产品落点。
对应根修：src/services/event_log.py EVENTS_DIR 测试态分流。
"""
from __future__ import annotations

import importlib

from src.config import LOGS_DIR
from src.services import event_log


def test_events_dir_redirected_under_pytest():
    """pytest 进程内默认落点=events-test，绝非产品 events/。"""
    assert event_log.EVENTS_DIR == LOGS_DIR / "events-test"
    assert event_log.EVENTS_DIR != LOGS_DIR / "events"


def test_log_event_lands_in_test_dir():
    """写入经唯一落点函数解析，必落 events-test（单点即全闸）。"""
    event_log.log_event("isolation", "probe", "隔离闸探针", level="info")
    f = event_log.EVENTS_DIR / (
        f"events-{event_log.datetime.now():%Y%m%d}.jsonl")
    assert f.is_file()
    content = f.read_text(encoding="utf-8")
    assert "隔离闸探针" in content


def test_escape_hatch_restores_product_dir(monkeypatch):
    """OMNISPACE_OBSERVE_IN_TESTS=1 逃逸闸：重载后回产品落点。"""
    monkeypatch.setenv("OMNISPACE_OBSERVE_IN_TESTS", "1")
    # 环境变量在模块导入期判定，重载模块以重新求值
    mod = importlib.reload(event_log)
    try:
        assert mod.EVENTS_DIR == LOGS_DIR / "events"
    finally:
        # 先撤环境变量再重载（monkeypatch 自动撤销在用例末尾，
        # finally 内仍是逃逸态），恢复本进程默认分流
        monkeypatch.delenv("OMNISPACE_OBSERVE_IN_TESTS", raising=False)
        importlib.reload(event_log)
    assert event_log.EVENTS_DIR == LOGS_DIR / "events-test"


def test_query_reads_test_dir_symmetric():
    """读侧同源：query_events 读的也是分流目录（面板只见用户流量）。"""
    res = event_log.query_events(limit=10, days=1)
    assert isinstance(res["items"], list)
    # 本用例写入的探针事件可查到（读写同目录）
    event_log.log_event("isolation", "probe2", "对称性探针", level="warning")
    res2 = event_log.query_events(limit=50, days=1)
    assert any("对称性探针" in str(i.get("friendly", ""))
               for i in res2["items"])
