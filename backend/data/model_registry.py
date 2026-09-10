"""模型注册表唯一程序化读者（ADR-003 P1：manifest v3 真源）。

背景（ADR-003 §1.2）：models/models_manifest.json 长期停留在 v2（9 条目，
引用磁盘已不存在的 qwen2-vl-2b），代码内 HARDWARE_TIER_TABLE 又引用一批
磁盘不存在的幽灵型号，manifest 的 capabilities/min_vram 字段不参与任何
路由裁决。本模块把「注册表读取」收敛为单一入口，供
startup_check / api/models / 测试锁定共同消费。

设计约束（诚实边界）：
  - 本模块只做「清单 ↔ 磁盘」一致性与 id 归属裁决；运行时显存预估的
    权威仍是 model_manager.estimate_vram_gb（人工实测覆盖 > 引擎候选表
    > 路由表 > 磁盘扫描），manifest 的 min_vram_gb 为索引级参考值，
    禁止用于放行/拒绝加载。
  - manifest 不可用（缺失/损坏）时全部查询回退为空，调用方自行降级，
    绝不阻断启动（对齐 startup_check 既有第 25 项 warning 语义）。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from ..config import MODELS_DIR

logger = logging.getLogger("omnispace.data.model_registry")

MANIFEST_PATH = MODELS_DIR / "models_manifest.json"

# v3 固化 schema：每条目必须具备的字段（测试锁定 test_model_registry.py）
REQUIRED_FIELDS = ("name", "type", "path", "capabilities", "lifecycle")
LIFECYCLE_VALUES = ("inproc", "subprocess")

# 顶层目录豁免表：不参与「孤儿目录」判定（非基础模型资产）
#   hf_cache   —— HuggingFace 动态缓存（all-MiniLM-L6-v2 等）
#   lora / style_lora —— LoRA 训练产物/适配器，由 learn 与 style 服务自管
#   image_gen  —— 遗留适配器目录（lcm-lora-sdxl）
#   _build / __pycache__ / 点开头 —— 非资产
_ORPHAN_EXEMPT_TOP_DIRS = {
    "hf_cache", "lora", "style_lora", "image_gen", "_build", "__pycache__",
}

# 模型目录「已下载」判定特征文件（与 model_manager._DIR_SIGNATURES 同口径）
_DIR_SIGNATURES = (
    "config.json", "model_index.json", "model.safetensors.index.json",
    "model.safetensors", "pytorch_model.bin", "modules.json",
)

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"mtime": -1.0, "data": None}


def load_manifest() -> dict[str, Any]:
    """读取 manifest v3（mtime 缓存）。缺失/损坏 → {}（调用方降级）。"""
    try:
        mtime = MANIFEST_PATH.stat().st_mtime
    except OSError:
        with _cache_lock:
            _cache.update({"mtime": -1.0, "data": None})
        return {}
    with _cache_lock:
        if _cache["mtime"] == mtime and _cache["data"] is not None:
            return _cache["data"]
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(
                data.get("models"), dict):
            raise ValueError("manifest 顶层结构非法（缺 models dict）")
    except Exception as exc:  # noqa: BLE001 - 损坏清单按缺失降级
        logger.warning("模型注册表解析失败，按缺失降级: %s", exc)
        data = {}
    with _cache_lock:
        _cache.update({"mtime": mtime, "data": data})
    return data


def entry(model_id: str) -> dict[str, Any]:
    """按 id 取注册表条目（无则 {}。api/models._manifest_entry 同契约）。"""
    models = load_manifest().get("models") or {}
    e = models.get(model_id)
    return e if isinstance(e, dict) else {}


def registry_ids() -> set[str]:
    """全部已登记模型 id 集合（manifest 不可用 → 空集，调用方跳过裁决）。"""
    return set((load_manifest().get("models") or {}).keys())


def remove_manifest_entry(model_id: str) -> bool:
    """从 manifest v3 移除登记条目并落盘（UAT 2026-09-10 缺陷⑨根修）。

    背景：当日三次「raw rm 删权重/正规入口删库表」都不同步 manifest →
    幽灵条目（登记有、磁盘无）连坏三回，靠提交闸兜底。本函数让删除
    正规通道具备登记簿同步能力：删盘（purge）必调；纯除名（权重仍在
    盘）不调（否则制造孤儿目录告警）。

    命中并移除返回 True；条目不存在/清单缺失损坏返回 False（损坏时
    不覆写原文件，对齐本模块「损坏按缺失降级」的诚实边界）。
    """
    with _cache_lock:
        try:
            raw = (MANIFEST_PATH.read_text(encoding="utf-8")
                   if MANIFEST_PATH.is_file() else None)
        except OSError:
            raw = None
    if raw is None:
        return False
    try:
        data = json.loads(raw)
        models = data.get("models")
        if not isinstance(models, dict) or model_id not in models:
            return False
        models.pop(model_id)
        data["models"] = models
    except Exception as exc:  # noqa: BLE001 - 损坏清单不覆写
        logger.warning("登记表移除失败（清单损坏，保留原文件）: %s", exc)
        return False
    try:
        MANIFEST_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
    except OSError as exc:
        logger.warning("登记表写盘失败: %s", exc)
        return False
    with _cache_lock:
        _cache.update({"mtime": -1.0, "data": None})  # 下次读取强制重载
    logger.info("登记表条目已移除: %s", model_id)
    return True


def _dir_has_signature(p: Path) -> bool:
    """目录（或其直接子目录）是否含模型特征文件。"""
    if any((p / f).is_file() for f in _DIR_SIGNATURES):
        return True
    return any(
        child.is_dir() and any((child / f).is_file() for f in _DIR_SIGNATURES)
        for child in p.iterdir())


def validate_against_disk() -> dict[str, list[str]]:
    """manifest ↔ 磁盘一致性校验（startup_check 第 25 项数据源）。

    Returns:
        required_missing: required=true 但磁盘 path 不存在的 id
        ghost_entries:    磁盘 path 不存在的条目（幽灵型号）
        orphan_dirs:      磁盘有模型特征文件但未被任何 manifest path
                          覆盖的顶层目录（登记缺失）
    """
    report: dict[str, list[str]] = {
        "required_missing": [], "ghost_entries": [], "orphan_dirs": []}
    manifest = load_manifest()
    models = manifest.get("models") or {}
    if not models:
        return report
    covered_top: set[str] = set()
    for mid, e in models.items():
        path = (MODELS_DIR / str(e.get("path", ""))).resolve()
        if path.exists():
            covered_top.add(path.relative_to(MODELS_DIR.resolve()).parts[0]
                            if len(path.relative_to(
                                MODELS_DIR.resolve()).parts) else "")
            continue
        report["ghost_entries"].append(mid)
        if e.get("required"):
            report["required_missing"].append(mid)
    if MODELS_DIR.is_dir():
        for child in MODELS_DIR.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name in _ORPHAN_EXEMPT_TOP_DIRS:
                continue
            if child.name in covered_top:
                continue
            if _dir_has_signature(child):
                report["orphan_dirs"].append(child.name)
    report["orphan_dirs"].sort()
    return report
