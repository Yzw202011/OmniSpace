"""资产编号服务（2026-10-05 用户令：人类可读编号体系）。

规则（用户拍板）：
- 编号格式：XM-0001（角色）/ CJ-0001（场景）/ DJ-0001（道具），
  每类独立递增
- 重图 = 换新编号：旧编号释放回池子可复用，新图拿新号（旧图不保留）
- 删除 = 编号释放：释放池最小号优先复用（XM-0001 删了，下一个新
  角色拿回 XM-0001）
- 存量补编：一次性脚本按创建顺序回填（code 列空=未编）

持久化：编号与释放池都落 settings_kv（asset.code.seq / asset.code.free），
崩溃窗口由"分配即持久化"收敛；释放池缺失时兜底重扫全表重建。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import logging
import threading

from ...data.settings_kv import kv_get, kv_set

log = logging.getLogger("omnispace.api.manga.asset_codes")

# kind → 编号前缀（用户指定中文拼音缩写）
_KIND_PREFIX = {"character": "XM", "scene": "CJ", "prop": "DJ"}
_PREFIX_KIND = {v: k for k, v in _KIND_PREFIX.items()}

_SEQ_KEY = "asset.code.seq"       # {"XM": 3, "CJ": 1, ...} 已发最大号
_FREE_KEY = "asset.code.free"     # {"XM": [1, 2], ...} 释放待复用号

# R3-M3 修复（2026-10-24）：分配/释放的 KV 读改写走模块级锁——
# 此前上传与队列 worker 并发读同一序号→写回同值→返回重号
_alloc_lock = threading.Lock()


def _seq_all() -> dict:
    v = kv_get(_SEQ_KEY, {})
    return v if isinstance(v, dict) else {}


def _free_all() -> dict:
    v = kv_get(_FREE_KEY, {})
    return v if isinstance(v, dict) else {}


def allocate(kind: str) -> str:
    """分配编号：释放池最小号优先，否则递增。分配即持久化（R3-M3 锁）。"""
    prefix = _KIND_PREFIX.get(kind)
    if not prefix:
        raise ValueError(f"未知资产类型: {kind}")
    with _alloc_lock:
        free = _free_all()
        pool = sorted(int(x) for x in (free.get(prefix) or []))
        if pool:
            num = pool[0]
            free[prefix] = [x for x in pool[1:]]
            kv_set(_FREE_KEY, free)
        else:
            seq = _seq_all()
            num = int(seq.get(prefix, 0)) + 1
            seq[prefix] = num
            kv_set(_SEQ_KEY, seq)
    return f"{prefix}-{num:04d}"


def release(code: str) -> None:
    """删除资产时回收编号（释放池去重入列；R3-M3 锁防并发丢号）。"""
    code = (code or "").strip()
    if not code or "-" not in code:
        return
    prefix, _, num_s = code.rpartition("-")
    if prefix not in _PREFIX_KIND or not num_s.isdigit():
        return
    with _alloc_lock:
        free = _free_all()
        pool = sorted(set(int(x) for x in (free.get(prefix) or []))
                      | {int(num_s)})
        free[prefix] = pool
        kv_set(_FREE_KEY, free)


def rebuild_if_missing(db) -> None:
    """释放池 kv 缺失时兜底重扫全表重建（崩溃窗口/手工清 kv 恢复）。

    判定"缺失"= kv 无 _FREE_KEY 键（空 list 与缺失区分）。
    """
    if kv_get(_FREE_KEY, None) is not None:
        return
    seq = _seq_all()
    free: dict[str, list[int]] = {p: [] for p in _PREFIX_KIND}
    used: dict[str, set[int]] = {p: set() for p in _PREFIX_KIND}
    try:
        for r in db.query("SELECT kind, code FROM comic_assets"
                          " WHERE COALESCE(code,'')<>''"):
            code = str(r.get("code") or "").strip()
            if "-" not in code:
                continue
            prefix, _, num_s = code.rpartition("-")
            if prefix in used and num_s.isdigit():
                used[prefix].add(int(num_s))
    except Exception as exc:  # noqa: BLE001 - 扫描失败保持空池不阻断
        log.warning("编号释放池重建扫描失败: %s", exc)
        return
    for prefix in _PREFIX_KIND:
        free[prefix] = sorted(
            n for n in range(1, int(seq.get(prefix, 0)) + 1)
            if n not in used[prefix])
    kv_set(_FREE_KEY, free)
    log.info("编号释放池已重建: %s", free)
