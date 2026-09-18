# 本项目仅供学习使用，商业授权请+Q 3559331368
"""训练服务公共基座（批3，2026-09-18）。

三训练服务（lora_training/style_lora/character_lora）同构面抽出：
- TrainingLockGuard：training 功能锁获取/释放（事件循环回投+同步降级
  双路径，两服务逐字节同文复制 60 行 → 单源）
- VersionStore：版本目录管理（next/current 指针/修剪/列表，三服务
  同构逻辑 → 参数化 root 的通用件）

各服务域逻辑（训练核心/数据集/底座）不动——本模块只收编机械同构面。
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("omnispace.services.training_common")


# ═══════════════════════════════════════════════════════════════
#  TrainingLockGuard——training 功能锁（三服务共用一把语义）
# ═══════════════════════════════════════════════════════════════

class TrainingLockGuard:
    """training 功能锁守卫：获取/释放成对，worker 线程上下文专用。

    双路径：事件循环可用时回投协程（异步语义），否则 acquire_sync
    同步降级（批1 多卡地基：共享同一份按域状态）。
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._via_fallback = False

    def set_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """宿主 API 层传入事件循环引用（None=纯线程上下文）。"""
        self._loop = loop

    def acquire(self, task_id: str) -> bool:
        """获取 training 锁；失败返回 False（调用方拒绝任务）。"""
        from ..middleware.feature_lock import get_feature_lock
        mgr = get_feature_lock()
        self._via_fallback = False
        loop = self._loop
        if loop is not None:
            try:
                if not loop.is_closed() and loop.is_running():
                    fut = asyncio.run_coroutine_threadsafe(
                        mgr.acquire("training", task_id=task_id), loop)
                    return bool(fut.result(timeout=10))
            except Exception as exc:  # noqa: BLE001
                log.warning("事件循环获取 training 锁失败，同步降级: %s",
                               exc, exc_info=True)
        if not mgr.acquire_sync("training", task_id=task_id):
            return False
        self._via_fallback = True
        return True

    def release(self) -> None:
        """释放 training 锁（与获取路径对称）。"""
        from ..middleware.feature_lock import get_feature_lock
        mgr = get_feature_lock()
        if not self._via_fallback:
            loop = self._loop
            if loop is not None and not loop.is_closed() \
                    and loop.is_running():
                try:
                    asyncio.run_coroutine_threadsafe(
                        mgr.release("training"), loop)
                    return
                except Exception as exc:  # noqa: BLE001
                    log.warning("事件循环释放 training 锁失败: %s", exc,
                                   exc_info=True)
        mgr.release_sync("training")


# ═══════════════════════════════════════════════════════════════
#  VersionStore——版本目录管理（三服务同构件）
# ═══════════════════════════════════════════════════════════════

class VersionStore:
    """版本目录：next→写入→current 指针→修剪（参数化 root）。

    目录形态：root/v1/、root/v2/…（vN 递增）+ root/current.json 指针。
    """

    def __init__(self, root: Path, keep: int = 3) -> None:
        self.root = root
        self.keep = keep

    def next_version_dir(self) -> tuple[Path, str]:
        """分配下一个版本目录（root mkdir + vN 递增）。"""
        self.root.mkdir(parents=True, exist_ok=True)
        n = 1
        while (self.root / f"v{n}").exists():
            n += 1
        d = self.root / f"v{n}"
        d.mkdir(parents=True)
        return d, f"v{n}"

    def set_current(self, version: str) -> None:
        """写 current 指针（版本名 + 时间戳）。"""
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "current.json").write_text(
            json.dumps({"version": version, "updated_at": time.time()},
                       ensure_ascii=False), encoding="utf-8")

    def current(self) -> str:
        """读 current 指针（无/损坏返回空串）。"""
        cj = self.root / "current.json"
        if not cj.is_file():
            return ""
        try:
            return str(json.loads(
                cj.read_text(encoding="utf-8")).get("version", ""))
        except Exception:  # noqa: BLE001
            return ""

    def versions(self) -> list[dict[str, Any]]:
        """版本列表（含 meta 合并 + is_current 标记；倒序）。"""
        cur = self.current()
        out: list[dict[str, Any]] = []
        if not self.root.is_dir():
            return out
        for d in sorted(self.root.iterdir(), reverse=True):
            if not (d.is_dir() and d.name.startswith("v")):
                continue
            meta: dict[str, Any] = {"version": d.name}
            mf = d / "meta.json"
            if mf.is_file():
                try:
                    meta.update(json.loads(mf.read_text(encoding="utf-8")))
                except Exception:  # noqa: BLE001
                    pass
            meta["is_current"] = d.name == cur
            out.append(meta)
        return out

    def prune(self) -> None:
        """修剪旧版本（keep 之外且非 current 的删除）。"""
        cur = self.current()
        dirs = sorted(
            [d for d in self.root.iterdir()
             if d.is_dir() and d.name.startswith("v")],
            reverse=True) if self.root.is_dir() else []
        for d in dirs[self.keep:]:
            if d.name != cur:
                shutil.rmtree(d, ignore_errors=True)

    def version_dir(self, version: str) -> Path | None:
        """指定版本的目录（不存在返回 None）。"""
        d = self.root / version
        return d if d.is_dir() else None

    @staticmethod
    def write_meta(version_dir: Path, meta: dict[str, Any]) -> None:
        """写版本 meta.json。"""
        (version_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8")
