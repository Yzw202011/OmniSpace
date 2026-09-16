"""FeatureLock 跨线程安全回归测试（2026-09-16 批3 P1 根修）。

背景：acquire_sync/release_sync 由训练工作线程调用，与事件循环的
async acquire/release 共享 _holders/_hold_counts——此前两路 read-
modify-write 无 threading 互斥（asyncio.Lock 挡不住别的线程），
计数漂移会导致假释放/永久卡锁。修复=全部读写临界区过 _state_lock。
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.middleware.feature_lock import FeatureLockManager  # noqa: E402

_N = 300


def test_cross_thread_hammer_counts_consistent() -> None:
    """双线程（sync + async）对同一功能交错 acquire/release 各 300 次。

    修复前：read-modify-write 竞态丢增量 → 收尾后计数 >0（锁卡死）
    或中途本该重入成功的 acquire 误判失败。修复后：干净归零。
    """
    m = FeatureLockManager()
    errors: list[str] = []

    def worker_sync() -> None:
        for _ in range(_N):
            if not m.acquire_sync("training", task_id="t-sync"):
                errors.append("sync acquire unexpectedly denied")
                return
            m.release_sync("training")

    async def worker_async() -> None:
        for _ in range(_N):
            if not await m.acquire("training", task_id="t-async"):
                errors.append("async acquire unexpectedly denied")
                return
            await m.release("training")

    t = threading.Thread(target=worker_sync)
    t.start()
    asyncio.run(worker_async())
    t.join()

    assert errors == []
    assert m.status()["active_feature"] is None
    assert m.holders == {}


def test_sync_holds_block_async_same_domain() -> None:
    """训练线程持锁期间，事件循环侧同域 acquire 必须被拒（互斥语义保持）。"""
    m = FeatureLockManager()
    assert m.acquire_sync("training", task_id="t1") is True

    async def _try_dialog() -> bool:
        return await m.acquire("dialog")

    assert asyncio.run(_try_dialog()) is False
    m.release_sync("training")

    assert asyncio.run(_try_dialog()) is True
