# 本项目仅供学习使用，商业授权请+Q 3559331368
"""人物 LoRA API（P3 训练中心·人物页签，2026-09-17）。

端点：
- GET  /character/lora/assets     可训练角色资产列表（含图片数/当前版本）
- POST /character/lora/train      入队训练 {asset_id}
- GET  /character/lora/tasks      任务列表
- POST /character/lora/tasks/{id}/cancel  取消
- GET  /character/lora/versions   版本列表 ?asset_id=
- POST /character/lora/rollback   回滚 {asset_id, version}
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body

from ..data.database import get_db_safe
from ..middleware.error_handler import ApiError, ok
from ..services.character_lora_service import (
    CharacterTrainingFailed,
    get_character_lora_service,
)

router = APIRouter()
log = logging.getLogger("omnispace.api.character_lora")

_ASSET_COLS = ("id, project_id, kind, name, file_path, prompt, meta,"
               " created_at, scope, face")


def _load_asset(asset_id: str) -> dict[str, Any]:
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用")
    row = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    if row is None:
        raise ApiError(40005, "资产不存在", detail={"asset_id": asset_id})
    a = dict(row)
    try:
        import json as _json
        a["meta"] = _json.loads(a.get("meta") or "{}")
    except Exception:  # noqa: BLE001 - meta 损坏按空处理
        a["meta"] = {}
    a["asset_id"] = a["id"]
    return a


@router.get("/character/lora/assets")
def character_lora_assets() -> dict[str, Any]:
    """可训练角色资产（kind=character，含图片数与已部署版本）。"""
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用")
    svc = get_character_lora_service()
    rows = db.query(
        f"SELECT {_ASSET_COLS} FROM comic_assets"
        " WHERE kind='character' ORDER BY created_at DESC")
    import json as _json
    items = []
    for r in rows:
        a = dict(r)
        try:
            a["meta"] = _json.loads(a.get("meta") or "{}")
        except Exception:  # noqa: BLE001
            a["meta"] = {}
        a["asset_id"] = a["id"]
        n_imgs = len(svc.collect_images(a))
        vers = svc.versions(a["asset_id"])
        current = next((v for v in vers if v.get("is_current")), None)
        items.append({
            "asset_id": a["asset_id"], "name": a.get("name"),
            "prompt": (a.get("prompt") or "")[:120],
            "image_count": n_imgs,
            "trainable": n_imgs >= 4,
            "current_version": current.get("version") if current else None,
        })
    return ok({"items": items, "total": len(items)})


@router.post("/character/lora/train")
def character_lora_train(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """入队人物 LoRA 训练（素材不足/互斥占用即时拒绝并给原因）。"""
    asset_id = str(body.get("asset_id") or "").strip()
    if not asset_id:
        raise ApiError("CHARACTER_LORA_PARAM", "asset_id 不能为空")
    asset = _load_asset(asset_id)
    if asset.get("kind") != "character":
        raise ApiError("CHARACTER_LORA_KIND", "仅角色资产可训练人物 LoRA")
    svc = get_character_lora_service()
    try:
        task = svc.submit(asset)
    except CharacterTrainingFailed as exc:
        raise ApiError("CHARACTER_LORA_REJECTED", str(exc)) from exc
    return ok(task)


@router.get("/character/lora/tasks")
def character_lora_tasks() -> dict[str, Any]:
    return ok({"items": get_character_lora_service().list_tasks(),
               "total": 0})


@router.post("/character/lora/tasks/{task_id}/cancel")
def character_lora_cancel(task_id: str) -> dict[str, Any]:
    try:
        return ok(get_character_lora_service().cancel(task_id))
    except CharacterTrainingFailed as exc:
        raise ApiError("CHARACTER_LORA_TASK", str(exc)) from exc


@router.get("/character/lora/versions")
def character_lora_versions(asset_id: str) -> dict[str, Any]:
    if not asset_id:
        raise ApiError("CHARACTER_LORA_PARAM", "asset_id 不能为空")
    return ok({"items": get_character_lora_service().versions(asset_id)})


@router.post("/character/lora/rollback")
def character_lora_rollback(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    asset_id = str(body.get("asset_id") or "").strip()
    version = str(body.get("version") or "").strip()
    if not asset_id or not version:
        raise ApiError("CHARACTER_LORA_PARAM", "asset_id 与 version 必填")
    asset = _load_asset(asset_id)
    try:
        return ok(get_character_lora_service().rollback(asset, version))
    except CharacterTrainingFailed as exc:
        raise ApiError("CHARACTER_LORA_ROLLBACK", str(exc)) from exc
