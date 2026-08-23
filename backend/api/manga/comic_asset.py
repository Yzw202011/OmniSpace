"""漫画资产域路由：资产库 / 绑定采纳 / 参考图 / 历史与描述。

TASK-P2-01 自 manga.py 按路由域拆出（原文件 4521 行 → 包）。
"""
from __future__ import annotations

import io
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, Query, UploadFile

from ...config import (
    API_PREFIX,
    DATA_DIR,
)
from ...data.database import get_db_safe, parse_json
from ...data.models import (
    AssetAdoptRequest,
    AssetBatchGenerateRequest,
    AssetBindRequest,
    AssetGenerateRequest,
    AssetInferRequest,
    AssetRegenerateViewRequest,
    AssetTurnaroundRequest,
    AssetUpdateRequest,
)
from ...middleware.error_handler import ApiError, ok
from ...middleware.feature_lock import acquire_or_raise
from ...services.inference.dialog_engine import get_dialog_engine
from ...services.inference.prompt_translator import translate_batch_zh2en
from ...services.offload import run_blocking
from .comic_gen import (
    _generate_turnaround_sync,
    _regenerate_asset_sync,
    _regenerate_view_sync,
)
from .common import (
    _ASSET_COLS,
    _ASSET_KIND_CONF,
    _COMIC_ASSET_DIR,
    IMG_TARGET_H,
    IMG_TARGET_W,
    _ensure_project,
    _find_storyboard,
    _generate_asset_sync,
    _now,
)

router = APIRouter()
log = logging.getLogger("omnispace.api.manga.comic_asset")



def _asset_row_to_dict(r: dict) -> dict:
    return {"asset_id": r["id"], "project_id": r.get("project_id", ""),
            "kind": r.get("kind", "character"), "name": r.get("name", ""),
            "file_path": r.get("file_path", ""), "prompt": r.get("prompt", ""),
            "meta": parse_json(r.get("meta"), {}),
            "created_at": r.get("created_at", 0),
            "scope": r.get("scope") or "project"}


def _asset_kind_endpoint(kind: str):
    """生成角色/场景/道具三个端点的公共实现工厂。"""
    async def _handler(req: AssetGenerateRequest):
        db = get_db_safe()
        if db is not None:
            _ensure_project(db, req.project_id)
        try:
            data = await run_blocking(_generate_asset_sync, req, kind)
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("资产生成失败: %s", exc)
            raise ApiError("PAINT_GENERATION_FAILED", str(exc)[:300]) from exc
        return ok(data)
    return _handler


router.post("/comic/asset/generate-character")(
    _asset_kind_endpoint("character"))
router.post("/comic/asset/generate-scene")(
    _asset_kind_endpoint("scene"))
router.post("/comic/asset/generate-prop")(
    _asset_kind_endpoint("prop"))


@router.post("/comic/asset/generate-turnaround")
async def comic_asset_generate_turnaround(req: AssetTurnaroundRequest):
    """角色多视图生成（COMIC-033~037，竞品对齐）：正面/侧面/背面/特写
    四张独立 16:9 图逐视图生成（每张可单独重生），同 seed 保一致性，
    自动落盘 portrait_views/ + canvas.png（2×2 拼图）→ 入库。

    诚实降级：FLUX.1-dev 未随包，SDXL 兜底，响应带 degraded 标记。
    """
    db = get_db_safe()
    if db is not None:
        _ensure_project(db, req.project_id)
    try:
        data = await run_blocking(_generate_turnaround_sync, req)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("多视图资产生成失败: %s", exc)
        raise ApiError("PAINT_GENERATION_FAILED", str(exc)[:300]) from exc
    return ok(data)


@router.post("/comic/asset/batch-generate")
async def comic_asset_batch_generate(req: AssetBatchGenerateRequest):
    """批量资产生成（COMIC-030）：逐项串行生成，聚合成功/失败明细。"""
    kind = (req.kind or "character").strip()
    if kind not in _ASSET_KIND_CONF:
        raise ApiError(40008, "kind 必须是 character/scene/prop",
                       detail={"allowed": list(_ASSET_KIND_CONF)})
    if not req.items:
        raise ApiError(40008, "缺少 items 数组")
    db = get_db_safe()
    if db is not None:
        _ensure_project(db, req.project_id)
    # 先整批中译英（对话引擎），再逐项生成（绘画引擎）：避免逐项
    # "翻译→生成"导致 dialog/paint 双模型反复换载（18s+/次）。
    raw_prompts = [str(item.get("prompt") or "").strip()
                   or str(item.get("name") or "asset") for item in req.items]
    en_prompts = await run_blocking(translate_batch_zh2en, raw_prompts)
    results: list[dict] = []
    failed: list[dict] = []
    for item, raw_prompt, prompt_en in zip(req.items, raw_prompts,
                                           en_prompts, strict=True):
        sub = AssetGenerateRequest(
            project_id=req.project_id,
            name=str(item.get("name") or "未命名资产")[:100],
            # DB/UI 保留用户原文；英文译文仅用于 SDXL 生成
            prompt=raw_prompt,
            width=int(item.get("width", IMG_TARGET_W)),
            height=int(item.get("height", IMG_TARGET_H)),
            transparent=bool(item.get("transparent", False)))
        try:
            data = await run_blocking(
                _generate_asset_sync, sub, kind, prompt_en)
            results.append(data)
        except ApiError as exc:
            failed.append({"name": sub.name, "code": exc.code,
                           "message": exc.message})
        except Exception as exc:  # noqa: BLE001
            failed.append({"name": sub.name, "code": "PAINT_GENERATION_FAILED",
                           "message": str(exc)[:300]})
    return ok({"project_id": req.project_id, "kind": kind,
               "succeeded": results, "failed": failed,
               "total": len(req.items), "success_count": len(results)})


@router.get("/comic/asset/library")
def comic_asset_library(project_id: str | None = Query(None),
                        kind: str | None = Query(None),
                        scope: str | None = Query(
                            None, description="project=项目资产（默认）；"
                            "global=全局资产库（跨项目，忽略 project_id）"),
                        limit: int = Query(100, ge=1, le=500,
                                           description="返回条数上限"),
                        offset: int = Query(0, ge=0,
                                            description="分页偏移")):
    """资产清单（COMIC-031）：按项目/类型过滤，返回缩略图信息。

    审计 R3-P3：增加 limit/offset 分页（默认 100、上限 500），
    total 维持「满足条件的记录总数」语义，向后兼容。
    全局资产域：scope=global 只返回全局资产（project_id 为空串）。
    """
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法查询资产库")
    cond, params = [], []
    if scope == "global":
        cond.append("scope='global'")
    elif project_id:
        cond.append("project_id=?")
        params.append(project_id)
    if kind:
        cond.append("kind=?")
        params.append(kind)
    where_sql = (" WHERE " + " AND ".join(cond)) if cond else ""
    total_row = db.query_one(
        f"SELECT COUNT(*) AS c FROM comic_assets{where_sql}",
        tuple(params))
    total = int(total_row["c"]) if total_row else 0
    sql = (f"SELECT {_ASSET_COLS} FROM comic_assets{where_sql}"
           " ORDER BY created_at DESC LIMIT ? OFFSET ?")
    items = [_asset_row_to_dict(r)
             for r in db.query(sql, tuple(params) + (limit, offset))]
    return ok({"items": items, "total": total})


def _row_asset_ids(row: dict) -> list[str]:
    """读取分镜行 asset_ids（JSON 数组），空时按旧列 asset_id 无缝升级。"""
    asset_ids = parse_json(row.get("asset_ids"), [])
    if not isinstance(asset_ids, list):
        asset_ids = []
    if not asset_ids and (row.get("asset_id") or ""):
        asset_ids = [row["asset_id"]]
    return [str(a) for a in asset_ids]


@router.put("/comic/asset/bind")
def comic_asset_bind(req: AssetBindRequest):
    """资产 ↔ 分镜行绑定（COMIC-032，竞品对齐多资产）。

    追加语义：读行 asset_ids → 去重追加 → 写回 asset_ids；
    asset_id 旧列同步置为该资产（兼容旧读取方）。
    """
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法绑定资产")
    asset = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (req.asset_id,))
    if asset is None:
        raise ApiError(40005, "资产不存在", detail={"asset_id": req.asset_id})
    row = db.query_one(
        "SELECT id, asset_id, asset_ids FROM storyboard_rows WHERE id=?",
        (req.row_id,))
    if row is None:
        raise ApiError(40005, "分镜行不存在", detail={"row_id": req.row_id})
    asset_ids = _row_asset_ids(row)
    if req.asset_id not in asset_ids:
        asset_ids.append(req.asset_id)
    db.update("storyboard_rows",
              {"asset_id": req.asset_id, "asset_ids": asset_ids},
              "id=?", (req.row_id,))
    return ok({"asset_id": req.asset_id, "row_id": req.row_id,
               "asset_ids": asset_ids})


@router.post("/comic/asset/adopt")
def comic_asset_adopt(req: AssetAdoptRequest):
    """资产库资产引入当前项目（竞品「全部可用角色」对齐）。

    复制 DB 行（新 id、目标项目）并复制资产目录文件；file_path /
    meta.views / meta.canvas / meta.history 中的旧项目路径前缀统一
    重写为新项目路径（JSON 级字符串替换，结构无需逐字段感知）。
    同名目录已存在时文件合并覆盖（dirs_exist_ok）。
    """
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法引入资产")
    row = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (req.asset_id,))
    if row is None:
        raise ApiError(40005, "资产不存在", detail={"asset_id": req.asset_id})
    asset = _asset_row_to_dict(row)
    if asset.get("project_id") == req.project_id:
        raise ApiError(40008, "该资产已在当前项目中",
                       detail={"asset_id": req.asset_id})
    _ensure_project(db, req.project_id)
    # 幂等：目标项目已有同一来源的引入副本时直接返回既有资产，
    # 避免重复点击「引入」产生重复角色（adopted_from 溯源标记）。
    for r in db.query(
            f"SELECT {_ASSET_COLS} FROM comic_assets WHERE project_id=?",
            (req.project_id,)):
        existing = _asset_row_to_dict(r)
        if (existing["meta"] or {}).get("adopted_from") == req.asset_id:
            return ok({"asset": existing, "already_adopted": True})
    kind = asset.get("kind", "character")
    conf = _ASSET_KIND_CONF.get(kind, _ASSET_KIND_CONF["character"])
    name = asset.get("name") or "asset"

    # ── 复制资产目录（old_dir_rel → new_dir_rel 前缀重写）─────────────
    old_rel = (asset.get("file_path") or "").strip()
    new_rel = ""
    if old_rel:
        old_dir_rel = old_rel.rsplit("/", 1)[0]
        new_dir = _COMIC_ASSET_DIR / req.project_id / conf["subdir"] / name
        new_dir.mkdir(parents=True, exist_ok=True)
        old_dir = DATA_DIR / old_dir_rel
        if old_dir.is_dir():
            import shutil
            shutil.copytree(old_dir, new_dir, dirs_exist_ok=True)
        new_dir_rel = str(new_dir.relative_to(DATA_DIR)).replace("\\", "/")
        new_rel = old_rel.replace(old_dir_rel, new_dir_rel, 1)
        # meta 内所有旧路径前缀统一重写（views/canvas/history 等）
        import json as _json
        meta = asset.get("meta") if isinstance(asset.get("meta"), dict) else {}
        meta = parse_json(
            _json.dumps(meta, ensure_ascii=False)
            .replace(old_dir_rel, new_dir_rel), {})
    else:
        meta = asset.get("meta") if isinstance(asset.get("meta"), dict) else {}
    meta = dict(meta)
    meta["adopted_from"] = req.asset_id
    meta.pop("history", None)  # 历史记录属于源资产生成过程，不带入新项目

    new_id = uuid.uuid4().hex
    db.insert("comic_assets", {
        "id": new_id, "project_id": req.project_id, "kind": kind,
        "name": name, "file_path": new_rel, "prompt": asset.get("prompt", ""),
        "meta": meta, "created_at": _now(), "scope": "project"})
    return ok({"asset": {**asset, "asset_id": new_id,
                         "project_id": req.project_id,
                         "file_path": new_rel, "meta": meta}})


# ── 全局资产域（跨项目复用） ────────────────────────────────────────────

def _asset_move_to_global(asset: dict, project_id: str) -> tuple[str, dict]:
    """单个资产的磁盘迁移：comic_assets/{pid}/{subdir}/{name} →
    comic_assets/global/{subdir}/{name}（global 下同名目录已存在时
    逐文件合并覆盖）。返回 (new_rel, new_meta)；文件缺失时仅重写路径。"""
    import json as _json
    import shutil

    old_rel = (asset.get("file_path") or "").strip()
    meta = asset.get("meta") if isinstance(asset.get("meta"), dict) else {}
    if not old_rel:
        return old_rel, meta
    old_dir_rel = old_rel.rsplit("/", 1)[0]
    parts = old_dir_rel.split("/")
    if len(parts) < 3 or parts[0] != "comic_assets" or parts[1] != project_id:
        # 非标准布局（老数据/上传件）：不动磁盘，仅保留原路径
        return old_rel, meta
    new_dir_rel = "comic_assets/global/" + "/".join(parts[2:])
    old_dir = DATA_DIR / old_dir_rel
    new_dir = DATA_DIR / new_dir_rel
    if old_dir.is_dir():
        try:
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            if new_dir.exists():
                # 全局库已有同名资产：逐文件合并覆盖后移除源目录
                for f in old_dir.rglob("*"):
                    if f.is_file():
                        target = new_dir / f.relative_to(old_dir)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, target)
                shutil.rmtree(old_dir)
            else:
                shutil.move(str(old_dir), str(new_dir))
        except OSError as exc:
            log.warning("资产迁移全局失败 %s: %s", old_dir, exc)
    new_rel = old_rel.replace(old_dir_rel, new_dir_rel, 1)
    # meta 内所有旧路径前缀统一重写（views/canvas/history 等，与 adopt 同法）
    meta = parse_json(
        _json.dumps(meta, ensure_ascii=False).replace(old_dir_rel, new_dir_rel),
        {})
    return new_rel, meta


def assets_to_global(db, project_id: str) -> int:
    """项目资产整体转全局域（删除项目时调用，用户裁定：不删除生成资产）。

    DB 行：scope='global'、project_id 置空；磁盘目录同步迁移到
    comic_assets/global/；迁移后项目资产根目录无文件残留时清理。
    返回转出的资产数。绑定关系（storyboard_rows.asset_ids）随分镜行
    一并由项目删除流程级联清理，此处不触碰。
    """
    import shutil

    rows = db.query(f"SELECT {_ASSET_COLS} FROM comic_assets"
                    " WHERE project_id=?", (project_id,))
    for r in rows:
        asset = _asset_row_to_dict(r)
        new_rel, meta = _asset_move_to_global(asset, project_id)
        db.update("comic_assets",
                  {"project_id": "", "scope": "global",
                   "file_path": new_rel, "meta": meta},
                  "id=?", (asset["asset_id"],))
    if rows:
        proj_dir = _COMIC_ASSET_DIR / project_id
        if proj_dir.is_dir():
            try:
                if not any(p.is_file() for p in proj_dir.rglob("*")):
                    shutil.rmtree(proj_dir)
            except OSError as exc:
                log.warning("项目资产目录清理失败 %s: %s", proj_dir, exc)
    return len(rows)


@router.post("/comic/asset/{asset_id}/to-global")
def comic_asset_to_global(asset_id: str):
    """项目资产转为全局资产（跨项目复用）：单条迁移磁盘目录 + 置
    scope='global'、project_id=''。已是全局资产时幂等返回。"""
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法转换资产")
    row = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    if row is None:
        raise ApiError(40005, "资产不存在", detail={"asset_id": asset_id})
    asset = _asset_row_to_dict(row)
    if asset.get("scope") == "global":
        return ok({"asset": asset, "already_global": True})
    pid = asset.get("project_id", "")
    new_rel, meta = _asset_move_to_global(asset, pid)
    db.update("comic_assets",
              {"project_id": "", "scope": "global",
               "file_path": new_rel, "meta": meta},
              "id=?", (asset_id,))
    row = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    return ok({"asset": _asset_row_to_dict(row), "already_global": False})


@router.put("/comic/asset/unbind")
def comic_asset_unbind(req: AssetBindRequest):
    """资产 ↔ 分镜行解绑（竞品对齐多资产）。

    从 asset_ids 移除指定资产；asset_id 旧列若指向被解绑资产，
    回退为剩余首元素（无剩余则置空）。资产/行不存在仍抛 40005。
    """
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法解绑资产")
    asset = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (req.asset_id,))
    if asset is None:
        raise ApiError(40005, "资产不存在", detail={"asset_id": req.asset_id})
    row = db.query_one(
        "SELECT id, asset_id, asset_ids FROM storyboard_rows WHERE id=?",
        (req.row_id,))
    if row is None:
        raise ApiError(40005, "分镜行不存在", detail={"row_id": req.row_id})
    asset_ids = [a for a in _row_asset_ids(row) if a != req.asset_id]
    asset_id = row.get("asset_id", "") or ""
    if asset_id == req.asset_id:
        asset_id = asset_ids[0] if asset_ids else ""
    db.update("storyboard_rows",
              {"asset_id": asset_id, "asset_ids": asset_ids},
              "id=?", (req.row_id,))
    return ok({"asset_id": asset_id, "row_id": req.row_id,
               "asset_ids": asset_ids})


# ── 资产级端点（竞品对齐改造）────────────────────────────────────────────
# 路由注册顺序约束：字面量 PUT /comic/asset/bind、/comic/asset/unbind 已
# 注册于上方，路径参数路由 PUT /comic/asset/{asset_id} 必须在其后注册，
# 否则 bind/unbind 会被 {asset_id} 吞掉。

@router.put("/comic/asset/{asset_id}")
def comic_asset_update(asset_id: str, req: AssetUpdateRequest):
    """资产元信息更新（竞品对齐）：仅更新非 None 的 name/prompt 字段。"""
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法更新资产")
    row = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    if row is None:
        raise ApiError(40005, "资产不存在", detail={"asset_id": asset_id})
    fields = req.model_dump(exclude_none=True)
    if fields:
        db.update("comic_assets", fields, "id=?", (asset_id,))
        row = db.query_one(
            f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    return ok({"asset": _asset_row_to_dict(row)})


@router.post("/comic/asset/{asset_id}/regenerate")
async def comic_asset_regenerate(asset_id: str,
                                 body: dict = Body(default_factory=dict)):
    """资产图重生成（竞品对齐）：按资产现有 prompt 重新出图并覆盖 file_path。

    prompt 为空 → 40008「请先填写描述词」；绘画引擎不可用 →
    degraded:true + degrade_reason 诚实降级（保留原图，不伪造产物）。
    功能锁语义与现有资产生成端点一致（不持有 paint 锁，由引擎自调度）。

    body.mode="four_views"（竞品对齐）：角色资产首次升级为四视图资产
    （meta.turnaround=True），随后走 one-pass 单图四视图管线（FLUX.2
    Klein 中文直入；不可用回退 SDXL 逐视图，诚实降级）。
    """
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法重生成资产")
    row = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    if row is None:
        raise ApiError(40005, "资产不存在", detail={"asset_id": asset_id})
    asset = _asset_row_to_dict(row)
    # 角色资产首次四视图化：置 turnaround 标记，后续按四视图路径重生成
    if str(body.get("mode") or "") == "four_views" \
            and asset.get("kind") == "character":
        meta = asset.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        meta["turnaround"] = True
        asset["meta"] = meta
    if not (asset.get("prompt") or "").strip():
        raise ApiError(40008, "请先填写描述词",
                       detail={"asset_id": asset_id})
    try:
        data = await run_blocking(_regenerate_asset_sync, asset)
    except ApiError as exc:
        if exc.code == "PAINT_ENGINE_NOT_READY":
            return ok({"asset": asset, "degraded": True,
                       "degrade_reason": (
                           "绘画引擎未就绪，已保留原图（非真实重生成）："
                           f"{exc.message}")})
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("资产重生成失败: %s", exc)
        raise ApiError("PAINT_GENERATION_FAILED", str(exc)[:300]) from exc
    return ok({"asset": data, "degraded": False})


@router.post("/comic/asset/{asset_id}/regenerate-view")
async def comic_asset_regenerate_view(asset_id: str,
                                      req: AssetRegenerateViewRequest):
    """单视图重生（竞品对齐）：仅重生成四视图资产的指定视图并覆盖
    portrait_views/{view}.png，同步重建 canvas.png 拼图。

    仅 meta.turnaround=True 的资产可用；prompt 缺省沿用资产描述词。
    """
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法重生成视图")
    row = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    if row is None:
        raise ApiError(40005, "资产不存在", detail={"asset_id": asset_id})
    asset = _asset_row_to_dict(row)
    if not (asset.get("meta") or {}).get("turnaround"):
        raise ApiError(40008, "该资产不是四视图资产，无法单视图重生",
                       detail={"asset_id": asset_id})
    prompt_zh = ((req.prompt or "").strip()
                 or (asset.get("prompt") or "").strip())
    if not prompt_zh:
        raise ApiError(40008, "请先填写描述词",
                       detail={"asset_id": asset_id})
    try:
        data = await run_blocking(
            _regenerate_view_sync, asset, req.view, prompt_zh)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("单视图重生失败: %s", exc)
        raise ApiError("PAINT_GENERATION_FAILED", str(exc)[:300]) from exc
    return ok(data)


def _asset_dir_for(asset: dict) -> Path:
    """定位资产落盘目录：优先取 file_path 父目录，缺省按目录约定推导。"""
    rel_path = (asset.get("file_path") or "").strip()
    if rel_path:
        return (DATA_DIR / rel_path).parent
    kind = asset.get("kind", "character")
    conf = _ASSET_KIND_CONF.get(kind, _ASSET_KIND_CONF["character"])
    return (_COMIC_ASSET_DIR / asset.get("project_id", "")
            / conf["subdir"] / (asset.get("name") or "asset"))


@router.post("/comic/asset/{asset_id}/reference")
async def comic_asset_reference_upload(asset_id: str,
                                       file: UploadFile = File(...)):
    """上传 AI 参考图（竞品对齐 img2img）：保存为资产目录 reference.png
    并置 meta.reference_image=True。

    校验扩展名 png/jpg/jpeg/webp 与大小 ≤10MB（与 /comic/asset/upload
    同口径）；统一转 PNG 落盘，四视图生成路径自动按 img2img 使用。
    """
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法上传参考图")
    row = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    if row is None:
        raise ApiError(40005, "资产不存在", detail={"asset_id": asset_id})
    asset = _asset_row_to_dict(row)
    filename = (file.filename or "").lower()
    ext = Path(filename).suffix
    if ext not in _ASSET_UPLOAD_EXTS:
        raise ApiError(40010, "仅支持 png/jpg/jpeg/webp 图片文件",
                       detail={"filename": file.filename})
    raw = await file.read()
    if not raw:
        raise ApiError(40008, "图片文件为空")
    if len(raw) > _ASSET_UPLOAD_MAX_BYTES:
        raise ApiError("OPERATION_LIMIT_EXCEEDED", "图片文件超过 10MB 上限",
                       detail={"max_bytes": _ASSET_UPLOAD_MAX_BYTES,
                               "given": len(raw)})
    out_dir = _asset_dir_for(asset)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "reference.png"
    try:
        # 统一转 PNG（jpg/webp 归一化，后续 img2img 直接 Image.open）
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as im:
            im.convert("RGB").save(out_path, "PNG")
    except Exception as exc:  # noqa: BLE001 - 解码失败即非法图片
        raise ApiError(40010, "参考图解码失败，请上传有效图片文件",
                       detail={"error": str(exc)[:200]}) from exc
    rel = str(out_path.relative_to(DATA_DIR)).replace("\\", "/")
    meta = asset.get("meta") or {}
    meta["reference_image"] = True
    meta["reference_path"] = rel
    db.update("comic_assets", {"meta": meta}, "id=?", (asset_id,))
    asset = {**asset, "meta": meta}
    return ok({"asset": asset, "reference": rel})


@router.delete("/comic/asset/{asset_id}")
def comic_asset_delete(asset_id: str):
    """删除单个资产（2026-08-20 用户裁定）：DB 行 + 分镜行绑定引用
    清理 + 磁盘目录清理。

    - 全项目扫描 storyboard_rows：asset_id 列置空、asset_ids JSON
      数组剔除该 ID（全局资产可能被多项目绑定）
    - 磁盘清理以 _asset_dir_for 定位目录，rmtree 前校验 resolve 后
      严格位于 _COMIC_ASSET_DIR 内且深度 ≥3（comic_assets/{owner}/
      {kind}/{name}），防路径穿越与误删父目录；失败仅告警不阻断
    """
    import shutil
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法删除资产")
    row = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    if row is None:
        raise ApiError(40005, "资产不存在", detail={"asset_id": asset_id})
    asset = _asset_row_to_dict(row)

    # 1) 清分镜行绑定引用（asset_id 兼容列 + asset_ids JSON 数组）
    for r in db.query(
            "SELECT id, asset_id, asset_ids FROM storyboard_rows"
            " WHERE asset_id=? OR asset_ids LIKE ?",
            (asset_id, f"%{asset_id}%")):
        ids = [i for i in (parse_json(r.get("asset_ids"), []) or [])
               if i != asset_id]
        db.update("storyboard_rows",
                  {"asset_id": "" if r.get("asset_id") == asset_id
                   else (r.get("asset_id") or ""),
                   "asset_ids": ids}, "id=?", (r["id"],))

    # 2) 删 DB 行
    db.delete("comic_assets", "id=?", (asset_id,))

    # 3) 磁盘目录清理（越界防护：必须在 comic_assets 下且深度 ≥3）
    out_dir = _asset_dir_for(asset)
    try:
        resolved = out_dir.resolve()
        base = _COMIC_ASSET_DIR.resolve()
        depth_ok = len(resolved.relative_to(base).parts) >= 3
        if resolved != base and base in resolved.parents and depth_ok:
            shutil.rmtree(resolved, ignore_errors=True)
        else:
            log.warning("拒绝删除越界/浅层资产目录: %s", out_dir)
    except (OSError, ValueError) as exc:  # noqa: BLE001 - 清理失败不阻断
        log.warning("资产目录删除失败 %s: %s", out_dir, exc)
    return ok({"asset_id": asset_id, "deleted": True,
               "name": asset.get("name", ""), "kind": asset.get("kind", "")})


@router.delete("/comic/asset/{asset_id}/reference")
def comic_asset_reference_delete(asset_id: str):
    """删除 AI 参考图：移除资产目录 reference.png 并清 meta 标记。"""
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法删除参考图")
    row = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    if row is None:
        raise ApiError(40005, "资产不存在", detail={"asset_id": asset_id})
    asset = _asset_row_to_dict(row)
    out_dir = _asset_dir_for(asset)
    ref_path = out_dir / "reference.png"
    if ref_path.is_file():
        try:
            ref_path.unlink()
        except OSError as exc:
            log.warning("参考图删除失败 %s: %s", ref_path, exc)
    meta = asset.get("meta") or {}
    meta.pop("reference_image", None)
    meta.pop("reference_path", None)
    db.update("comic_assets", {"meta": meta}, "id=?", (asset_id,))
    asset = {**asset, "meta": meta}
    return ok({"asset": asset, "reference": None})


@router.get("/comic/asset/{asset_id}/history")
def comic_asset_history(asset_id: str):
    """资产生成历史（竞品对齐）：meta.history 留痕（最新在前），
    每项补 /manga/media 可回读 url。"""
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法查询历史")
    row = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    if row is None:
        raise ApiError(40005, "资产不存在", detail={"asset_id": asset_id})
    asset = _asset_row_to_dict(row)
    meta = asset.get("meta") or {}
    history = meta.get("history")
    if not isinstance(history, list):
        history = []
    items: list[dict] = []
    for h in reversed(history):  # 最新在前
        if not isinstance(h, dict):
            continue
        item = dict(h)
        rel = str(h.get("file") or "").replace("\\", "/")
        item["url"] = f"{API_PREFIX}/manga/media/{rel}" if rel else ""
        items.append(item)
    return ok({"items": items, "total": len(items)})


# 资产图片上传约束（竞品对齐）：png/jpg/jpeg/webp ≤10MB
_ASSET_UPLOAD_EXTS = (".png", ".jpg", ".jpeg", ".webp")
_ASSET_UPLOAD_MAX_BYTES = 10 * 1024 * 1024  # 10MB


@router.post("/comic/asset/{asset_id}/image")
async def comic_asset_image_replace(asset_id: str,
                                    file: UploadFile = File(...)):
    """替换资产图片（2026-08-20 用户裁定：上传 = 替换当前资产，非新建）。

    落盘到资产专属子目录 {资产目录}/{asset_id}/portrait|image{ext}——
    按 asset_id 隔离，杜绝历史按名称约定落盘（{name}/portrait.png）
    导致同名资产共享同一文件、一次上传全部被替换的问题；同时不
    insert 新资产行。旧共享文件不动（其他同名行仍在引用）；本资产
    历史四视图产物引用（views/canvas/master）失效移除，防详情面板
    混显新旧图。
    """
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法替换图片")
    arow = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    if arow is None:
        raise ApiError(40005, "资产不存在", detail={"asset_id": asset_id})
    asset = _asset_row_to_dict(arow)
    filename = (file.filename or "").lower()
    ext = Path(filename).suffix
    if ext not in _ASSET_UPLOAD_EXTS:
        raise ApiError(40010, "仅支持 png/jpg/jpeg/webp 图片文件",
                       detail={"filename": file.filename})
    raw = await file.read()
    if not raw:
        raise ApiError(40008, "图片文件为空")
    if len(raw) > _ASSET_UPLOAD_MAX_BYTES:
        raise ApiError("OPERATION_LIMIT_EXCEEDED", "图片文件超过 10MB 上限",
                       detail={"max_bytes": _ASSET_UPLOAD_MAX_BYTES,
                               "given": len(raw)})
    kind = asset.get("kind", "character")
    # 资产专属子目录：固定按名称约定推导基础目录（勿用 _asset_dir_for
    # ——它优先取 file_path 父目录，首次替换后路径已含 /{asset_id}/，
    # 反复替换会层层嵌套）；路径恒为 {pid}/{subdir}/{name}/{asset_id}/
    conf = _ASSET_KIND_CONF.get(kind, _ASSET_KIND_CONF["character"])
    own_dir = (_COMIC_ASSET_DIR / (asset.get("project_id") or "")
               / conf["subdir"] / (asset.get("name") or "asset") / asset_id)
    own_dir.mkdir(parents=True, exist_ok=True)
    out_path = own_dir / (
        ("portrait" if kind == "character" else "image") + ext)
    # 清本资产专属子目录内旧替换文件（仅本目录，不越界）
    for old in own_dir.iterdir():
        if old.is_file() and old != out_path:
            try:
                old.unlink()
            except OSError as exc:
                log.warning("旧替换图删除失败 %s: %s", old, exc)
    out_path.write_bytes(raw)
    rel_path = str(out_path.relative_to(DATA_DIR)).replace("\\", "/")
    meta = dict(asset.get("meta") or {})
    # 四视图产物引用失效（旧文件可能被同名资产共享，不删磁盘只摘引用）
    for k in ("views", "canvas", "master", "turnaround", "onepass",
              "prompt_zh", "layout_verified", "bg_verified",
              "match_verified", "consistency"):
        meta.pop(k, None)
    meta["source"] = "upload_replace"
    meta["orig_filename"] = file.filename or ""
    try:
        from PIL import Image
        with Image.open(out_path) as im:
            meta["width"], meta["height"] = im.size
    except Exception:  # noqa: BLE001 - 尺寸读取失败不阻断替换
        pass
    db.update("comic_assets",
              {"file_path": rel_path, "meta": meta}, "id=?", (asset_id,))
    nrow = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    return ok({"asset": _asset_row_to_dict(nrow)})


@router.post("/comic/asset/upload")
async def comic_asset_upload(project_id: str = Form(...),
                             kind: str = Form("character"),
                             name: str = Form(""),
                             file: UploadFile = File(...)):
    """资产图片上传（竞品对齐）：本地图片登记为项目资产。

    multipart 字段：file / project_id / kind / name。
    校验扩展名 png/jpg/jpeg/webp 与大小 ≤10MB；落盘
    DATA_DIR/comic_assets/{project_id}/{subdir}/{name}/（与资产生成
    同目录约定，file_path 为 DATA_DIR 相对路径，/manga/media 白名单可回读）。
    """
    pid = (project_id or "").strip()
    if not pid:
        raise ApiError(40008, "缺少 project_id")
    kind = (kind or "character").strip()
    if kind not in _ASSET_KIND_CONF:
        raise ApiError(40008, "kind 必须是 character/scene/prop",
                       detail={"allowed": list(_ASSET_KIND_CONF)})
    filename = (file.filename or "").lower()
    ext = Path(filename).suffix
    if ext not in _ASSET_UPLOAD_EXTS:
        raise ApiError(40010, "仅支持 png/jpg/jpeg/webp 图片文件",
                       detail={"filename": file.filename})
    raw = await file.read()
    if not raw:
        raise ApiError(40008, "图片文件为空")
    if len(raw) > _ASSET_UPLOAD_MAX_BYTES:
        raise ApiError("OPERATION_LIMIT_EXCEEDED", "图片文件超过 10MB 上限",
                       detail={"max_bytes": _ASSET_UPLOAD_MAX_BYTES,
                               "given": len(raw)})
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法上传资产")
    _ensure_project(db, pid)
    asset_name = ((name or "").strip()[:100]
                  or Path(filename).stem[:100] or "未命名资产")
    asset_id = uuid.uuid4().hex
    conf = _ASSET_KIND_CONF[kind]
    out_dir = _COMIC_ASSET_DIR / pid / conf["subdir"] / asset_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (
        ("portrait" if kind == "character" else "image") + ext)
    out_path.write_bytes(raw)
    rel_path = str(out_path.relative_to(DATA_DIR)).replace("\\", "/")
    meta: dict = {"source": "upload", "orig_filename": file.filename or ""}
    try:
        from PIL import Image
        with Image.open(out_path) as im:
            meta["width"], meta["height"] = im.size
    except Exception:  # noqa: BLE001 - 尺寸读取失败不阻断登记
        pass
    db.insert("comic_assets", {
        "id": asset_id, "project_id": pid, "kind": kind,
        "name": asset_name, "file_path": rel_path, "prompt": "",
        "meta": meta, "created_at": _now()})
    row = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    return ok({"asset": _asset_row_to_dict(row)})


# 实体推断资产桩的自动描述词模板（竞品对齐）
_INFER_PROMPT_TPL = {
    "character": "{name}，角色立绘，全身像，白色背景",
    "scene": "{name}，场景图，横屏16:9",
    "prop": "{name}，道具特写，透明背景",
}


def _infer_character_settings_llm(db, project_id: str,
                                  names: list[str]) -> dict[str, str]:
    """批量生成角色设定段（角色推理 v2，2026-08-20）。

    一次 LLM 调用为全部新角色生成「角色设定」正文（剧本上下文注入
    到提示），返回 {name: setting}。LLM 不可用/失败/解析失败时返回
    空 dict（调用方回退死模板，如实记 meta.inferred_by='template'）。
    常驻对话模型优先（ensure_loaded(None) 自动腾显存）。
    """
    if not names:
        return {}
    try:
        engine = get_dialog_engine()
        if not engine.ensure_loaded(None):
            log.warning("角色推理 LLM 不可用（%s），描述词回退模板",
                        engine.get_status().get("last_error", ""))
            return {}
        blocks = []
        for nm in names[:12]:  # 一次上限 12 个，防上下文超长
            ctx = _entity_story_context(db, project_id, nm, "character")
            blocks.append(f"【角色名称】{nm}\n【剧本上下文】{ctx or '（无）'}")
        prompt = (
            "你是漫剧美术设定师。为下列每个角色生成「角色设定」人设"
            "外观描述。每段按顺序覆盖：年龄段与身份、身高体态、脸型"
            "骨相、眉眼鼻唇五官、发型发色与细节、气质关键词、上装"
            "（款式/颜色/面料/细节）、下装、鞋子、空手、常态平静表情、"
            "眼睛平视镜头。服装为日常真实穿搭。\n"
            "输出格式（严格遵守，每个角色一块，之间空行）：\n"
            "【角色】：名字\n角色设定：正文一段 120~200 字\n\n"
            + "\n\n".join(blocks))
        reply = engine.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.7, max_new_tokens=3200)
        import re
        out: dict[str, str] = {}
        for m in re.finditer(
                r"【角色】[：:]\s*([^\n]{1,100})\n\s*角色设定[：:]\s*"
                r"([^\n【]+)", reply):
            name = m.group(1).strip()
            setting = m.group(2).strip()
            if name and setting:
                out[name] = setting
        log.info("角色推理 LLM 批量生成: 请求 %d 成功 %d",
                 len(names), len(out))
        return out
    except Exception as exc:  # noqa: BLE001 - LLM 失败回退模板
        log.warning("角色推理 LLM 异常（回退模板）: %s", exc)
        return {}


@router.post("/comic/asset/infer-entities")
def comic_asset_infer_entities(req: AssetInferRequest):
    """从分镜行推断实体资产桩（竞品对齐；角色推理 v2 接入 LLM）。

    聚合项目全部分镜行的 characters（JSON 数组）/scene（字符串）/
    props（JSON 数组），与 comic_assets 现有 (kind, name) 去重后，
    为缺失实体插入资产桩（file_path=''）。角色描述词为五段式 AI
    描述词（【角色】/绘图提示词/美术风格/时代背景/角色设定，LLM
    依据剧本上下文批量生成角色设定段）；场景/道具为类型模板。
    LLM 不可用时角色回退简单模板并如实记 meta.inferred_by。
    """
    project_id = (req.project_id or "").strip()
    if not project_id:
        raise ApiError(40008, "缺少 project_id")
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法推断实体")
    sb = _find_storyboard(db, project_id)
    rows: list[dict] = []
    if sb is not None:
        rows = db.query(
            "SELECT characters, scene, props FROM storyboard_rows"
            " WHERE storyboard_id=?", (sb["id"],))
    wanted: dict[str, set[str]] = {"character": set(), "scene": set(),
                                   "prop": set()}
    for r in rows:
        for nm in parse_json(r.get("characters"), []) or []:
            nm = str(nm).strip()[:100]
            if nm:
                wanted["character"].add(nm)
        scene = (r.get("scene") or "").strip()[:100]
        if scene:
            wanted["scene"].add(scene)
        for nm in parse_json(r.get("props"), []) or []:
            nm = str(nm).strip()[:100]
            if nm:
                wanted["prop"].add(nm)
    existing = db.query(
        "SELECT kind, name FROM comic_assets WHERE project_id=?",
        (project_id,))
    existing_keys = {(r.get("kind", ""), (r.get("name") or "").strip())
                     for r in existing}
    new_characters = sorted(
        nm for nm in wanted["character"]
        if ("character", nm) not in existing_keys)
    # 角色推理 v2：LLM 批量生成角色设定段（失败回退模板）
    settings = _infer_character_settings_llm(db, project_id, new_characters)
    created = {"character": 0, "scene": 0, "prop": 0}
    new_ids: list[str] = []
    for kind in ("character", "scene", "prop"):
        for nm in sorted(wanted[kind]):
            if (kind, nm) in existing_keys:
                continue
            if kind == "character" and nm in settings:
                prompt = _build_character_prompt_v2(nm, settings[nm])
                meta = {"source": "infer_entities", "inferred_by": "llm"}
            else:
                prompt = _INFER_PROMPT_TPL[kind].format(name=nm)
                meta = {"source": "infer_entities",
                        "inferred_by": "template"}
            asset_id = uuid.uuid4().hex
            db.insert("comic_assets", {
                "id": asset_id, "project_id": project_id, "kind": kind,
                "name": nm, "file_path": "", "prompt": prompt,
                "meta": meta, "created_at": _now()})
            created[kind] += 1
            new_ids.append(asset_id)
    items: list[dict] = []
    if new_ids:
        placeholders = ",".join("?" for _ in new_ids)
        items = [_asset_row_to_dict(r) for r in db.query(
            f"SELECT {_ASSET_COLS} FROM comic_assets"
            f" WHERE id IN ({placeholders})"
            " ORDER BY created_at ASC", tuple(new_ids))]
    return ok({"created": created, "items": items})


# 资产描述词扩写提示词（沿用 ai-describe 的分镜师风格定位）
_ASSET_DESCRIBE_PROMPT = """你是漫剧美术设定师。请为以下{kind_label}资产扩写一段可直接用于 AI 绘图的中文描述词。
要求：
1. 80 字以内，具体描述外观、风格、配色与氛围；
2. 不要输出解释或标题，只输出描述词本身。

【{kind_label}名称】{name}"""

# 角色描述词扩写模板（2026-08-20 用户裁定五段式格式）：【角色】/
# 绘图提示词（四视图版式+中文标注指令，固定文案）/美术风格（韩国
# 网漫风，固定）/时代背景/角色设定（LLM 生成）。生成时由
# _sanitize_character_prompt_zh 在中文阶段剥版式与固定段，生图模板
# 统一注入网漫风格段，故固定文案不与生图冲突。
_INFER_DRAW_PROMPT_TPL = (
    "生成角色4视图：正面全身、侧面全身、背面全身、上半身特写。"
    "纯白色背景，禁止纹理，全局光照，禁止投影。"
    "图片左上角标注中文角色名：{name}；各视图下方标注中文："
    "正面全身、侧面全身、背面全身、上半身特写；"
    "禁止出现任何其他文字；禁止外语字符。"
)
_INFER_STYLE_LINE = "韩国网漫风、干净线稿、清晰上色、表情表现力强"
_INFER_ERA_LINE = "当代中国城市，21世纪1020年代"


def _build_character_prompt_v2(name: str, setting_text: str) -> str:
    """拼装五段式角色描述词（2026-08-20 用户裁定格式）。

    绘图提示词/美术风格/时代背景为固定文案；角色设定来自 LLM。
    生图端由 _sanitize_character_prompt_zh 剥版式与固定段。
    """
    setting = (setting_text or "").strip()
    setting = setting.removeprefix("角色设定：").strip()
    return (f"【角色】：{name}\n"
            f"绘图提示词：{_INFER_DRAW_PROMPT_TPL.format(name=name)}\n"
            f"美术风格：{_INFER_STYLE_LINE}\n"
            f"时代背景：{_INFER_ERA_LINE}\n"
            f"角色设定：{setting}")


def _entity_story_context(db, project_id: str, name: str,
                          kind: str) -> str:
    """收集实体在分镜行中的上下文（描述+台词摘要，前 4 行）。"""
    sb = _find_storyboard(db, project_id)
    if sb is None:
        return ""
    rows = db.query(
        "SELECT description, original_dialogue, characters, scene, props"
        " FROM storyboard_rows WHERE storyboard_id=?", (sb["id"],))
    hits: list[str] = []
    for r in rows:
        blob = " ".join(str(r.get(k) or "") for k in
                        ("description", "original_dialogue", "characters",
                         "scene", "props"))
        if name in blob:
            snippet = f"{r.get('description') or ''}{r.get('original_dialogue') or ''}"
            snippet = snippet.strip()[:120]
            if snippet:
                hits.append(snippet)
        if len(hits) >= 4:
            break
    return "；".join(hits)

_ASSET_DESCRIBE_CHARACTER_PROMPT = """你是漫剧美术设定师。请为以下角色扩写「角色设定」段（人设外观描述），我方会拼装成完整绘图描述词。
要求：
1. 按顺序覆盖：年龄段与身份、身高体态、脸型骨相、眉眼鼻唇五官、发型发色与细节、气质关键词、上装（款式/颜色/面料/细节）、下装、鞋子、空手、常态平静表情、眼睛平视镜头；
2. 只输出「角色设定：」开头的正文一段，120~200 字，不输出解释与标题；
3. 贴合剧本上下文与角色名气质；服装为日常真实穿搭。

【角色名称】{name}
【剧本上下文】{context}"""


@router.post("/comic/asset/{asset_id}/describe")
async def comic_asset_describe(asset_id: str):
    """资产描述词 AI 扩写（竞品对齐）：对话引擎把 name+kind 扩写为绘图
    描述词并写回 prompt。

    对话引擎未就绪 → DIALOG_NOT_READY 诚实错误（与 ai-describe 一致，
    不伪造描述词）；推理期间持有 "dialog" 功能锁（规格 §6.1 互斥）。
    """
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法扩写描述词")
    row = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    if row is None:
        raise ApiError(40005, "资产不存在", detail={"asset_id": asset_id})
    asset = _asset_row_to_dict(row)
    kind_label = {"character": "角色", "scene": "场景",
                  "prop": "道具"}.get(asset.get("kind", ""), "资产")
    engine = get_dialog_engine()
    lock = await acquire_or_raise("dialog", task_id=asset_id)
    try:
        if not engine.is_ready:
            status = engine.get_status()
            raise ApiError(
                "DIALOG_NOT_READY",
                "对话模型未加载，无法扩写描述词，请先在对话模块加载模型",
                detail={"engine_state": status["state"],
                        "last_error": status["last_error"]})
        # 角色：五段式格式（LLM 只生成角色设定段，代码拼装固定段，
        # 2026-08-20 用户裁定）；场景/道具保持原结构
        if asset.get("kind") == "character":
            context = _entity_story_context(
                db, asset.get("project_id") or "",
                asset.get("name", ""), "character")
            prompt = _ASSET_DESCRIBE_CHARACTER_PROMPT.format(
                name=asset.get("name", ""), context=context or "（无）")
            max_tokens = 512
        else:
            prompt = _ASSET_DESCRIBE_PROMPT.format(
                kind_label=kind_label, name=asset.get("name", ""))
            max_tokens = 256
        try:
            description = (await run_blocking(
                engine.chat, [{"role": "user", "content": prompt}],
                temperature=0.7, max_new_tokens=max_tokens)).strip()
        except Exception as exc:  # noqa: BLE001 - 推理失败收敛为语义错误码
            raise ApiError("MODEL_INFERENCE_FAILED",
                           f"资产描述词扩写失败：{exc}") from exc
        if not description:
            raise ApiError("MODEL_INFERENCE_FAILED",
                           "资产描述词扩写失败：模型返回为空")
        if asset.get("kind") == "character":
            description = _build_character_prompt_v2(
                asset.get("name", ""), description)
        db.update("comic_assets", {"prompt": description[:2000]},
                  "id=?", (asset_id,))
        row = db.query_one(
            f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
        return ok({"asset": _asset_row_to_dict(row)})
    finally:
        await lock.release("dialog")


@router.post("/comic/asset/export-pack")
def comic_asset_export_pack(body: dict = Body(default_factory=dict)):
    """项目资产打包导出（COMIC-138）：zip 含 manifest.json 与全部资产文件。"""
    import zipfile
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        raise ApiError(40008, "缺少 project_id")
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法导出资产包")
    rows = db.query(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE project_id=?",
        (project_id,))
    if not rows:
        raise ApiError(40005, "项目无资产可导出",
                       detail={"project_id": project_id})
    out_dir = DATA_DIR / "generated" / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"assets_{project_id}_{int(_now())}.zip"
    manifest = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in rows:
            item = _asset_row_to_dict(r)
            manifest.append(item)
            fp = DATA_DIR / item["file_path"]
            if fp.is_file():
                zf.write(fp, f"{item['kind']}/{item['name']}/{fp.name}")
        zf.writestr("manifest.json",
                    __import__("json").dumps(
                        {"project_id": project_id, "assets": manifest},
                        ensure_ascii=False, indent=2))
    return ok({"project_id": project_id,
               "file_path": str(zip_path.relative_to(DATA_DIR)).replace("\\", "/"),
               "asset_count": len(rows)})
