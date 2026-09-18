# 本项目仅供学习使用，商业授权请+Q 3559331368
"""训练中心聚合 API（P1 训练中心，2026-09-17 用户拍板 2A）。

GET /training/tasks —— 统一训练队列：聚合**知识训练**（/learn，对话模型
QLoRA）、**风格训练**（/style，视频风格 LoRA）与**人物训练**
（/character_lora，P3 增补）三套任务源。三套服务本体
不动，本层只做薄聚合（拍板 2A 的稳妥路线）：

- 逐源 fail-soft：单侧故障不拖垮整队，sources 字段如实披露各侧健康度
  （诚实降级：坏一侧的 items 缺席但标 ok:false，不伪造空列表冒充无任务）；
- 归一化：三套任务的公共字段（id/kind/name/status/progress/时间戳）
  统一形状，created_at 缺失者沉底；
- 排序：按 created_at 倒序（与各源自身列表口径一致）。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter

from ..middleware.error_handler import ApiError, ok
from .character_lora import character_lora_tasks
from .learn import learn_tasks
from .style import style_tasks

router = APIRouter()
log = logging.getLogger("omnispace.api.training")

_KIND_LABELS: dict[str, str] = {"knowledge": "知识训练", "style": "风格训练",
                           "character": "人物训练"}


def _norm(item: dict[str, Any], kind: str) -> dict[str, Any]:
    """两源任务 → 统一队列条目（公共字段；源字段原样保留在 raw 里）。"""
    name = (item.get("name") or item.get("style_prompt")
            or item.get("base_model") or item.get("id") or "?")
    return {
        "id": str(item.get("id", "")),
        "kind": kind,
        "kind_label": _KIND_LABELS.get(kind, kind),
        "name": str(name)[:80],
        "status": str(item.get("status", "unknown")),
        "progress": float(item.get("progress") or 0.0),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def _collect(kind: str, fn: Callable[[], dict[str, Any]],
             sources: dict[str, Any]) -> list[dict[str, Any]]:
    """拉取一源任务列表；失败记入 sources（fail-soft 不抛）。"""
    try:
        env = fn()
        items = (env.get("data") or {}).get("items") or []
        sources[kind] = {"ok": True, "count": len(items), "error": None}
        return [_norm(it, kind) for it in items if isinstance(it, dict)]
    except (ApiError, Exception) as exc:  # noqa: BLE001 - 单源失败不拖垮整队
        log.warning("训练队列聚合: %s 源失败: %s", kind, exc, exc_info=True)
        sources[kind] = {"ok": False, "count": 0, "error": str(exc)}
        return []


@router.get("/training/tasks")
def training_tasks() -> dict[str, Any]:
    """统一训练任务队列（知识 + 风格两源聚合，按创建时间倒序）。"""
    sources: dict[str, Any] = {}
    merged = (_collect("knowledge", learn_tasks, sources)
              + _collect("style", style_tasks, sources)
              + _collect("character", character_lora_tasks, sources))
    # created_at 缺失（None）者沉底，避免 None 比较炸排序
    merged.sort(key=lambda t: (t["created_at"] is None,
                               -(t["created_at"] or 0)))
    return ok({"items": merged, "total": len(merged), "sources": sources})
