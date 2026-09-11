"""知识库体检周报（知识学习升级方案 批4，2026-09-11）。

体检常态化：启动时 + 每 7 天自动巡检一次知识库，把四类脏数据计数与
检索延迟写成大白话事件（日志页时间线可见）；脏数据 >0 或延迟持续
超标记 WARNING。只读巡检，不做任何删除（清理走批1 人工终审链路）。

与 knowledge_quality_gate 的关系：闸管入库（拒收），本模块管存量
（巡查）——规则同源（复用 gate 判定函数），保证「入库拒什么、
存量巡什么」口径一致。
"""
from __future__ import annotations

import logging
import threading
import time

from . import knowledge_quality_gate as gate

log = logging.getLogger("omnispace.knowledge_checkup")

# 巡检间隔（7 天；启动即先查一次）
_CHECKUP_INTERVAL_S = 7 * 86400
# 检索延迟 WARNING 线（ms；方案 §一 连带发现：bge CPU 路 >1s）
_LATENCY_WARN_MS = 1000.0
# 延迟探针 query（真实使用形状）
_PROBE_QUERIES = ("怎么写短剧剧本", "什么是反转设计", "女主角人设怎么立")


def _all_items(svc) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        r = svc.list_knowledge(page=page, page_size=200)
        batch = r.get("items") or []
        if not batch:
            break
        items.extend(batch)
        if len(items) >= int(r.get("total") or 0):
            break
        page += 1
    return items


def _count_dirty(items: list[dict]) -> tuple[int, list[str]]:
    """按闸规则扫存量（高精度；与入库拒收口径一致）。"""
    dirty = 0
    samples: list[str] = []
    for it in items:
        content = str(it.get("content") or "")
        ktype = str(it.get("type") or "")
        reasons = []
        if gate.is_crawler_noise(content):
            reasons.append("crawler_noise")
        if gate.is_answerless_qa(ktype, content):
            reasons.append("answerless_qa")
        if gate.is_too_short(content):
            reasons.append("too_short")
        if reasons:
            dirty += 1
            if len(samples) < 5:
                samples.append(f"[{reasons[0]}] {content[:50]}")
    return dirty, samples


def _probe_latency(inj) -> float:
    """延迟探针：取多次检索的最大耗时（观测用，异常按 0 处理）。"""
    worst = 0.0
    for q in _PROBE_QUERIES:
        try:
            inj.retrieve(q, top_k=5)
            worst = max(worst, float(getattr(inj, "last_latency_ms", 0.0) or 0.0))
        except Exception:  # noqa: BLE001 - 探针失败不影响体检结论
            continue
    return worst


def run_checkup() -> dict:
    """执行一次只读体检并写大白话事件。返回报告 dict（测试可断言）。"""
    from .event_log import log_event
    from .injection_service import get_injection_service
    from .knowledge_service import get_knowledge_service

    svc = get_knowledge_service()
    total = int(svc.count() or 0)
    dirty, samples = _count_dirty(_all_items(svc))
    latency_ms = _probe_latency(get_injection_service())

    problems: list[str] = []
    if dirty:
        problems.append(f"{dirty} 条脏数据")
    if latency_ms > _LATENCY_WARN_MS:
        problems.append(f"检索延迟 {latency_ms:.0f}ms 超过 1 秒")
    friendly = (
        f"知识库体检：共 {total} 条知识，"
        + ("发现 " + "、".join(problems) if problems else "一切正常")
        + "；每周自动巡检一次")

    log_event("system", "knowledge_checkup", friendly,
              level="warning" if problems else "info",
              detail=f"dirty={dirty} latency_ms={latency_ms:.0f} "
                     f"samples={samples}",
              duration_ms=latency_ms)
    log.info("知识库体检完成: %s", friendly)
    return {"total": total, "dirty": dirty, "latency_ms": round(latency_ms, 1),
            "friendly": friendly}


def start_checkup_task() -> None:
    """启动体检后台线程（幂等）：启动即查一次 + 每 7 天巡检。"""
    t = threading.Thread(target=_loop, name="knowledge-checkup", daemon=True)
    t.start()


_started = False
_start_lock = threading.Lock()


def _loop() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    while True:
        try:
            run_checkup()
        except Exception:  # noqa: BLE001 - 体检失败不影响业务
            log.exception("知识库体检异常")
        time.sleep(_CHECKUP_INTERVAL_S)
