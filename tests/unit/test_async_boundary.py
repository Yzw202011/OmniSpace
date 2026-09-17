"""异步边界固化测试（TASK-P1-06，审计 P08）。

锁定的契约：
  1. 全后端源码无"调用方须放线程池"类注释级口头契约（义务性短语
     一律为结构保证取代）；
  2. API 层零 asyncio.to_thread 直调——推理/重计算唯一入口是
     services.offload.run_blocking；
  3. run_blocking 真实卸载到工作线程（不阻塞事件循环）；
  4. sync_core 装饰器把同步核心变成自调度 async 函数（漏 await
     只得到 coroutine，不会同步阻塞，误用面结构性消失）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from src.services.offload import run_blocking, sync_core

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "src"
# 义务性契约短语（"调用方必须做 X 否则出事"）——必须为 0
_OBLIGATION_PHRASES = (
    "调用方须放线程池",
    "调用方须在工作线程",
    "须放线程池",
    "调用方必须放线程池",
)
# API 层模块（推理/重计算调用方）；services.offload 是唯一入口所在
_API_DIR = BACKEND_ROOT / "api"


def _py_sources(*dirs: Path) -> list[Path]:
    out: list[Path] = []
    for d in dirs:
        out.extend(d.rglob("*.py"))
    return [p for p in out if "__pycache__" not in p.parts]


# ── 契约 1：无注释级口头契约 ───────────────────────────────────────

def test_no_comment_level_threading_contracts():
    """后端源码不得残留'调用方须…'义务性契约（P1-06 验收标准）。"""
    sources = _py_sources(
        BACKEND_ROOT / "api",
        BACKEND_ROOT / "services",
        BACKEND_ROOT / "engines",
        BACKEND_ROOT / "data",
        BACKEND_ROOT / "middleware",
    )
    assert sources, "源码扫描范围为空——路径错误"
    offenders: list[str] = []
    for p in sources:
        text = p.read_text(encoding="utf-8", errors="replace")
        for phrase in _OBLIGATION_PHRASES:
            if phrase in text:
                offenders.append(f"{p.relative_to(BACKEND_ROOT)}: {phrase}")
    assert offenders == [], (
        f"发现注释级口头契约残留（应改为结构保证）:\n{chr(10).join(offenders)}")


# ── 契约 2：API 层零 to_thread 直调，入口唯一 ─────────────────────

def test_api_layer_no_direct_to_thread():
    """API 层不得直调 asyncio.to_thread——推理/重计算必经 run_blocking。

    豁免：services/ws_hub.py（毫秒级遥测读取，offload.py 策略明确
    允许）；services/scheduler（周期 tick 自调度循环）。
    """
    offenders = []
    for p in _py_sources(_API_DIR):
        text = p.read_text(encoding="utf-8", errors="replace")
        if "asyncio.to_thread" in text:
            offenders.append(str(p.relative_to(BACKEND_ROOT)))
    assert offenders == [], (
        f"API 层存在 asyncio.to_thread 直调（应收敛到 run_blocking）:\n"
        f"{chr(10).join(offenders)}")


def test_run_blocking_is_the_single_entry():
    """offload.run_blocking 存在且 API 层至少一处真实使用（入口活着）。"""
    from src.api import dialog  # noqa: F401 - 导入即验证无循环依赖
    src = (BACKEND_ROOT / "api" / "dialog.py").read_text(encoding="utf-8")
    assert "run_blocking(" in src, "dialog.py 未使用统一入口"


# ── 契约 3：run_blocking 真实卸载 ─────────────────────────────────

@pytest.mark.asyncio
async def test_run_blocking_offloads_to_worker_thread():
    """被调函数在工作线程执行（线程号 ≠ 事件循环线程），结果如实返回。"""
    main_tid = threading.get_ident()

    def probe(delay: float, *, tag: str) -> tuple[int, str]:
        time_sleep(delay)
        return threading.get_ident(), tag

    def time_sleep(s: float) -> None:
        threading.Event().wait(s)

    tid, tag = await run_blocking(probe, 0.0, tag="ok")
    assert tag == "ok"
    assert tid != main_tid, "run_blocking 未卸载——仍在事件循环线程执行"


@pytest.mark.asyncio
async def test_run_blocking_propagates_exception():
    """工作线程内的异常如实传播到 await 处（不被吞掉）。"""

    def boom():
        raise RuntimeError("worker boom")

    with pytest.raises(RuntimeError, match="worker boom"):
        await run_blocking(boom)


# ── 契约 4：sync_core 自调度（误用面结构性消失）───────────────────

def test_sync_core_returns_coroutine_function():
    """装饰后是协程函数：同步误用（漏 await）只会得到 coroutine 而非阻塞。"""

    @sync_core
    def _work() -> int:
        return 42

    import inspect
    assert inspect.iscoroutinefunction(_work)


@pytest.mark.asyncio
async def test_sync_core_executes_in_worker_thread():
    main_tid = threading.get_ident()

    @sync_core
    def _tid(a: int, b: int = 0) -> int:
        return threading.get_ident() + a + b

    coro = _tid(1, b=1)
    import inspect
    assert inspect.iscoroutine(coro), "漏 await 得到 coroutine（不阻塞）"
    tid = await coro
    assert (tid - 2) != main_tid, "sync_core 未卸载到工作线程"


@pytest.mark.asyncio
async def test_dialog_passive_helpers_are_self_scheduling():
    """dialog.py 被动补全两个辅助已是自调度 async——结构性杜绝误用。"""
    from src.api import dialog

    assert asyncio.iscoroutinefunction(dialog._quick_search_supplement)
    assert asyncio.iscoroutinefunction(dialog._passive_reinfer)


# ── 契约 5：async 函数零同步 SQLite 直调（2026-09-17 重构批 1）─────

_DB_METHODS = {"query", "execute", "execute_in_transaction", "insert",
               "update", "delete", "one", "sql", "executemany",
               "query_one", "query_value", "upsert"}
_DB_GETTERS = {"get_db_safe", "get_db"}


def test_async_functions_have_no_direct_sqlite_calls():
    """async def 体内不得直调 Database 同步方法——busy_timeout 竞争时
    会卡住整个事件循环（09-16 审计 P2，09-17 重构批 1 清偿 64 处）。

    合法形态：await run_blocking(lambda: db.xxx(...))（已包在
    run_blocking 参数内的调用不算违规）。同步内函数体内的调用随外层
    整体卸载，同样合规。
    """
    import ast as _ast

    offenders: list[str] = []

    class _V(_ast.NodeVisitor):
        def __init__(self) -> None:
            self.fn = ""
            self.db_names: set[str] = set()

        def visit_AsyncFunctionDef(self, node) -> None:
            prev_fn, prev_names = self.fn, set(self.db_names)
            self.fn, self.db_names = node.name, set()
            self.generic_visit(node)
            self.fn, self.db_names = prev_fn, prev_names

        def visit_FunctionDef(self, node) -> None:
            # 同步内函数：随外层卸载，不检查
            prev_fn, prev_names = self.fn, set(self.db_names)
            self.fn, self.db_names = "", set()
            self.generic_visit(node)
            self.fn, self.db_names = prev_fn, prev_names

        def visit_Assign(self, node) -> None:
            if (isinstance(node.value, _ast.Call)
                    and isinstance(node.value.func, _ast.Name)
                    and node.value.func.id in _DB_GETTERS):
                for t in node.targets:
                    if isinstance(t, _ast.Name):
                        self.db_names.add(t.id)
            self.generic_visit(node)

        def visit_Call(self, node) -> None:
            f = node.func
            if isinstance(f, _ast.Name) and f.id == "run_blocking":
                return  # 卸载面内合规
            if isinstance(f, _ast.Attribute) and f.attr in _DB_METHODS:
                v = f.value
                is_db = (
                    (isinstance(v, _ast.Name)
                     and (v.id == "db" or v.id in self.db_names))
                    or (isinstance(v, _ast.Call)
                        and isinstance(v.func, _ast.Name)
                        and v.func.id in _DB_GETTERS))
                if is_db and self.fn:
                    offenders.append(f"{self.fn}():{node.lineno} {f.attr}")
            self.generic_visit(node)

    for p in _py_sources(BACKEND_ROOT / "api", BACKEND_ROOT / "services"):
        tree = _ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        _V().visit(tree)

    assert offenders == [], (
        f"async 函数内存在同步 SQLite 直调（应包 await run_blocking）:\n"
        f"{chr(10).join(offenders)}\n"
        f"→ 改法见 scripts/codemod_async_sqlite.py（AST codemod，幂等）")
