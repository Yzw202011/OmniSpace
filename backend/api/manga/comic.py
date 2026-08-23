"""漫画项目域路由：项目 CRUD / 剧本 DSL 导入 / 场景物件 / 导出包。

TASK-P2-01 自 manga.py 按路由域拆出（原文件 4521 行 → 包）。
"""
from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, File, Query, UploadFile

from ...config import (
    DATA_DIR,
)
from ...data.database import get_db_safe, parse_json
from ...data.models import (
    ProjectBatchDelete,
    ProjectCreate,
    ProjectUpdate,
    SceneObjectUpdate,
)
from ...middleware.error_handler import ApiError, ok
from ...services.inference.video_engine import VIDEO_OUT_DIR
from .common import (
    _ASSET_COLS,
    _KEYFRAME_DIR,
    _KF_COLS,
    _VIDEO_TASK_COLS,
    _ensure_storyboard,
    _find_storyboard,
    _load_rows,
    _make_row,
    _now,
    _public_row_to_db,
    _storyboards,
    _video_tasks,
)
from .storyboard import (
    storyboard_import,
)
from .voice import (
    _DSL_MAX_BYTES,
    _TEMPLATE_COMIC_DRAMA,
)
from .comic_asset import (
    assets_to_global,
)

router = APIRouter()
log = logging.getLogger("omnispace.api.manga.comic")



def _project_row_to_dict(r: dict) -> dict:
    return {"project_id": r["id"], "name": r.get("name", ""),
            "work_mode": r.get("work_mode", "regular"),
            "created_at": r.get("created_at", 0),
            "updated_at": r.get("updated_at", 0)}


def _is_within(path: Path, base: Path) -> bool:
    """resolve 后 path 是否严格位于 base 内（防路径穿越）。"""
    try:
        resolved = path.resolve()
        base_resolved = base.resolve()
    except OSError:
        return False
    return resolved != base_resolved and base_resolved in resolved.parents


def _safe_rmtree(path: Path, base: Path) -> None:
    """删除目录树：仅当 path resolve 后严格位于 base 内才执行。"""
    if not _is_within(path, base):
        log.warning("拒绝删除越界目录: %s", path)
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError as exc:
        log.warning("目录删除失败 %s: %s", path, exc)


def _safe_unlink(path: Path, base: Path) -> None:
    """删除单文件：仅当 path resolve 后严格位于 base 内才执行。"""
    if not _is_within(path, base):
        log.warning("拒绝删除越界文件: %s", path)
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("文件删除失败 %s: %s", path, exc)


def _cleanup_project_disk(project_id: str, row_ids: list[str],
                          video_files: list[str],
                          video_task_ids: list[str]) -> None:
    """清理项目磁盘产物（关键帧目录/视频文件）。

    资产目录不在此清理——删除项目时资产整体转全局域保留
    （assets_to_global 负责迁移与空目录收敛，用户裁定：不删除生成资产）。
    零信任：所有路径 resolve 后必须落在归属根目录内，否则拒绝删除；
    文件删除失败仅告警，不阻塞 DB 级联删除主流程。
    """
    try:
        for rid in row_ids:
            _safe_rmtree(_KEYFRAME_DIR / rid, _KEYFRAME_DIR)
        # 视频产物真实输出根为双目录（审计修复：级联删除漏清）：
        # 降级/AnimateLCM 管线 → VIDEO_OUT_DIR（data/generated/videos）；
        # 真实 diffusers 管线（video_engine.generate）→ DATA_DIR/videos。
        # file_path 落在任一根内均允许删除，根外路径拒绝（防路径穿越）。
        video_roots = (Path(VIDEO_OUT_DIR), DATA_DIR / "videos")
        for fp in video_files:
            if not fp:
                continue
            fp_path = Path(fp)
            for root in video_roots:
                if _is_within(fp_path, root):
                    _safe_unlink(fp_path, root)
                    break
            else:
                log.warning("拒绝删除越界文件: %s", fp)
        # file_path 未落库的完成任务：按命名约定兜底探测（双根）
        for tid in video_task_ids:
            for root in video_roots:
                _safe_unlink(root / f"{tid}.mp4", root)
    except Exception as exc:  # noqa: BLE001 - 磁盘清理不阻塞主流程
        log.warning("项目磁盘清理失败 %s: %s", project_id, exc)


@router.post("/comic/project/create")
def comic_project_create(req: ProjectCreate):
    """创建漫剧项目（COMIC-001/002/003）。

    template=comic_drama 时预置 5 行模板分镜；项目重名 →
    COMIC_PROJECT_NAME_DUPLICATED。
    """
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法创建项目")
    dup = db.query_one("SELECT id FROM projects WHERE name=?", (req.name,))
    if dup is not None:
        raise ApiError("COMIC_PROJECT_NAME_DUPLICATED",
                       detail={"name": req.name})
    pid = req.project_id or uuid.uuid4().hex
    now = _now()
    db.insert("projects", {"id": pid, "name": req.name, "path": "",
                           "work_mode": req.work_mode.value,
                           "created_at": now, "updated_at": now})
    rows: list[dict] = []
    if (req.template or "").strip() == "comic_drama":
        sb = _ensure_storyboard(db, pid)
        for i, tpl in enumerate(_TEMPLATE_COMIC_DRAMA):
            row = _make_row(i + 1, scene=tpl["scene"],
                            description=tpl["description"])
            db.insert("storyboard_rows",
                      _public_row_to_db(row, sb["id"], sort_index=i))
            rows.append(row)
        db.update("storyboards", {"updated_at": _now()}, "id=?", (sb["id"],))
    return ok({"project_id": pid, "name": req.name,
               "template": req.template or "", "rows": rows,
               "created_at": now})


@router.get("/comic/project/list")
def comic_project_list():
    """项目列表（COMIC-004），按更新时间倒序。"""
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法列出项目")
    rows = db.query(
        "SELECT id, name, work_mode, created_at, updated_at FROM projects"
        " ORDER BY updated_at DESC, created_at DESC")
    items = [_project_row_to_dict(r) for r in rows]
    return ok({"items": items, "total": len(items)})


@router.put("/comic/project/{project_id}")
def comic_project_update(project_id: str, req: ProjectUpdate):
    """重命名项目（COMIC-004）；新名与他项目重名 → 名称重复错误。"""
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法更新项目")
    row = db.query_one("SELECT id FROM projects WHERE id=?", (project_id,))
    if row is None:
        raise ApiError(40005, "项目不存在", detail={"project_id": project_id})
    dup = db.query_one("SELECT id FROM projects WHERE name=? AND id<>?",
                       (req.name, project_id))
    if dup is not None:
        raise ApiError("COMIC_PROJECT_NAME_DUPLICATED",
                       detail={"name": req.name})
    db.update("projects", {"name": req.name, "updated_at": _now()},
              "id=?", (project_id,))
    return ok({"project_id": project_id, "name": req.name})


def _delete_project_cascade(db, project_id: str) -> bool:
    """级联删除单个项目：分镜表/分镜行/视频任务/资产/关键帧/场景对象
    + 磁盘产物清理。项目不存在返回 False（供单删报错、批删跳过复用）。"""
    row = db.query_one("SELECT id FROM projects WHERE id=?", (project_id,))
    if row is None:
        return False
    sb = db.query_one("SELECT id FROM storyboards WHERE project_id=?",
                      (project_id,))
    row_ids: list[str] = []
    video_files: list[str] = []
    video_task_ids: list[str] = []
    if sb is not None:
        # 先收集行 id / 视频任务（删行后无从关联），供级联与磁盘清理
        row_ids = [r["id"] for r in db.query(
            "SELECT id FROM storyboard_rows WHERE storyboard_id=?",
            (sb["id"],))]
        # video_tasks 无 project_id 列，经分镜行关联项目
        vt_rows = db.query(
            "SELECT vt.id AS id, vt.file_path AS file_path"
            " FROM video_tasks vt"
            " JOIN storyboard_rows sr ON vt.storyboard_row_id = sr.id"
            " WHERE sr.storyboard_id=?", (sb["id"],))
        video_task_ids = [str(r["id"]) for r in vt_rows]
        video_files = [str(r.get("file_path") or "") for r in vt_rows]
        # 先删 video_tasks（引用分镜行），再删分镜行/分镜表
        db.delete("video_tasks",
                  "storyboard_row_id IN"
                  " (SELECT id FROM storyboard_rows WHERE storyboard_id=?)",
                  (sb["id"],))
        db.delete("storyboard_rows", "storyboard_id=?", (sb["id"],))
        db.delete("storyboards", "id=?", (sb["id"],))
    # 用户裁定：删除项目不删除生成资产——项目资产整体转全局域
    # （scope='global' + 磁盘迁移 comic_assets/global/），跨项目保留可复用
    assets_to_global(db, project_id)
    db.delete("keyframes", "project_id=?", (project_id,))
    db.delete("scene_objects", "project_id=?", (project_id,))
    db.delete("projects", "id=?", (project_id,))
    _storyboards.pop(project_id, None)
    for tid in video_task_ids:
        _video_tasks.pop(tid, None)
    # 磁盘产物清理（失败仅告警，不阻塞 DB 删除主流程）
    _cleanup_project_disk(project_id, row_ids, video_files, video_task_ids)
    return True


@router.delete("/comic/project/{project_id}")
def comic_project_delete(project_id: str):
    """删除项目（COMIC-004）：级联删除分镜表/分镜行/视频任务/资产/关键帧/
    场景对象，并清理项目磁盘产物（资产目录/关键帧目录/视频文件）。"""
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法删除项目")
    if not _delete_project_cascade(db, project_id):
        raise ApiError(40005, "项目不存在", detail={"project_id": project_id})
    return ok({"project_id": project_id, "deleted": True})


@router.post("/comic/project/batch-delete")
def comic_project_batch_delete(req: ProjectBatchDelete):
    """批量删除项目（COMIC-004 扩展）：逐个复用级联删除，不存在的跳过。
    返回 {deleted, deleted_ids, missing_ids}；至少删掉 1 个即视为成功。"""
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法删除项目")
    deleted_ids: list[str] = []
    missing_ids: list[str] = []
    for pid in req.project_ids:
        if _delete_project_cascade(db, pid):
            deleted_ids.append(pid)
        else:
            missing_ids.append(pid)
    if not deleted_ids:
        raise ApiError(40005, "项目不存在", detail={"project_ids": missing_ids})
    return ok({"deleted": len(deleted_ids), "deleted_ids": deleted_ids,
               "missing_ids": missing_ids})


# ═══════════════════════════════════════════════════════════════════
#  批 1.2 DSL 文件上传（COMIC-005/009/010）
# ═══════════════════════════════════════════════════════════════════

@router.post("/comic/script/import-dsl")
async def comic_script_import_dsl(project_id: str = Query(...),
                                  strict: bool = Query(False),
                                  file: UploadFile = File(...)):
    """DSL 剧本文件上传导入（multipart，.txt/.dsl ≤10MB）。

    strict=true 时要求文本含 ``shot:`` 分镜标记，否则
    COMIC_DSL_FORMAT_INVALID；解析复用 storyboard_import 管线。
    """
    filename = (file.filename or "").lower()
    if not filename.endswith((".txt", ".dsl")):
        raise ApiError(40010, "仅支持 .txt/.dsl 剧本文件",
                       detail={"filename": file.filename})
    raw = await file.read()
    if not raw:
        raise ApiError("SCRIPT_FORMAT_UNSUPPORTED", "剧本文件为空")
    if len(raw) > _DSL_MAX_BYTES:
        raise ApiError("OPERATION_LIMIT_EXCEEDED", "剧本文件超过 10MB 上限",
                       detail={"max_bytes": _DSL_MAX_BYTES,
                               "given": len(raw)})
    try:
        text = raw.decode("utf-8", errors="ignore").strip()
    except Exception:  # noqa: BLE001
        raise ApiError("FILE_PARSE_FAILED", "剧本文件解码失败") from None
    if not text:
        raise ApiError("SCRIPT_FORMAT_UNSUPPORTED", "剧本文件内容为空")
    if strict and "shot:" not in text.lower():
        raise ApiError("COMIC_DSL_FORMAT_INVALID",
                       detail={"hint": "每镜头以 'shot:' 行开头"})
    # 有 shot: 标记时按标记切分，否则按行切分（与 storyboard_import 一致）
    if "shot:" in text.lower():
        import re as _re
        segments = [s.strip() for s in _re.split(r"(?im)^\s*shot:\s*", text)
                    if s.strip()]
    else:
        segments = [s.strip() for s in text.splitlines() if s.strip()]
    return await storyboard_import({"project_id": project_id,
                                    "script": "\n".join(segments)})


@router.put("/comic/scene/object/update")
def comic_scene_object_update(req: SceneObjectUpdate):
    """3D 场景对象 Transform 持久化（COMIC-090，scene_objects 表 upsert）。"""
    import json as _json
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法保存场景对象")
    sid = f"{req.project_id}:{req.object_id}"
    fields: dict = {"updated_at": _now()}
    if req.name is not None:
        fields["name"] = req.name[:100]
    for key in ("position", "rotation", "scale"):
        val = getattr(req, key)
        if val is not None:
            fields[key] = _json.dumps(val, ensure_ascii=False)
    existing = db.query_one("SELECT id FROM scene_objects WHERE id=?", (sid,))
    if existing is None:
        db.insert("scene_objects", {
            "id": sid, "project_id": req.project_id,
            "object_id": req.object_id,
            "name": fields.get("name", ""),
            "position": fields.get("position", "{}"),
            "rotation": fields.get("rotation", "{}"),
            "scale": fields.get("scale", "{}"),
            "updated_at": fields["updated_at"]})
    else:
        db.update("scene_objects", fields, "id=?", (sid,))
    return ok({"project_id": req.project_id, "object_id": req.object_id,
               "updated": True})


@router.get("/comic/scene/object/list")
def comic_scene_object_list(project_id: str = Query(...)):
    """项目 3D 场景对象列表（scene_objects 表）。"""
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法查询场景对象")
    rows = db.query(
        "SELECT object_id, name, position, rotation, scale, updated_at"
        " FROM scene_objects WHERE project_id=?", (project_id,))
    items = [{"object_id": r["object_id"], "name": r.get("name", ""),
              "position": parse_json(r.get("position"), {}),
              "rotation": parse_json(r.get("rotation"), {}),
              "scale": parse_json(r.get("scale"), {}),
              "updated_at": r.get("updated_at", 0)} for r in rows]
    return ok({"project_id": project_id, "items": items,
               "total": len(items)})


@router.post("/comic/export/bundle")
def comic_export_bundle(body: dict = Body(default_factory=dict)):
    """项目内容合并打包（COMIC-139）：分镜 json/csv + 资产 zip + 视频 → 单 zip。"""
    import json as _json
    import zipfile
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        raise ApiError(40008, "缺少 project_id")
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法打包导出")
    rows: list[dict] = []
    sb = _find_storyboard(db, project_id)
    if sb is not None:
        rows = _load_rows(db, sb["id"])
    out_dir = DATA_DIR / "generated" / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"bundle_{project_id}_{int(_now())}.zip"
    counts = {"storyboard_rows": len(rows), "assets": 0, "videos": 0,
              "keyframes": 0}
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 分镜表
        zf.writestr("storyboard.json", _json.dumps(
            {"project_id": project_id, "rows": rows},
            ensure_ascii=False, indent=2))
        # 资产文件
        for r in db.query(
                f"SELECT {_ASSET_COLS} FROM comic_assets WHERE project_id=?",
                (project_id,)):
            fp = DATA_DIR / (r.get("file_path") or "")
            if fp.is_file():
                zf.write(fp, f"assets/{r.get('kind', 'misc')}/{fp.name}")
                counts["assets"] += 1
        # 关键帧
        for r in db.query(
                f"SELECT {_KF_COLS} FROM keyframes WHERE project_id=?",
                (project_id,)):
            fp = DATA_DIR / (r.get("file_path") or "")
            if fp.is_file():
                zf.write(fp, f"keyframes/{r['row_id']}/v{r['version']}.png")
                counts["keyframes"] += 1
        # 已生成视频（video_tasks 经分镜行关联项目）
        if sb is not None:
            _vt_cols = ", ".join(f"vt.{c}" for c in
                                 _VIDEO_TASK_COLS.split(", "))
            for r in db.query(
                    f"SELECT {_vt_cols} FROM video_tasks vt"
                    " JOIN storyboard_rows sr ON vt.storyboard_row_id = sr.id"
                    " WHERE sr.storyboard_id=? AND vt.status='done'",
                    (sb["id"],)):
                fp = Path(r.get("file_path") or "")
                if fp.is_file():
                    zf.write(fp, f"videos/{r['id']}.mp4")
                    counts["videos"] += 1
        zf.writestr("manifest.json", _json.dumps(
            {"project_id": project_id, "contents": counts},
            ensure_ascii=False, indent=2))
    return ok({"project_id": project_id,
               "file_path": str(zip_path.relative_to(DATA_DIR)).replace("\\", "/"),
               "contents": counts})


# ═══════════════════════════════════════════════════════════════════
#  G2 — 可用模型列表（工序弹窗模型选择数据源）
# ═══════════════════════════════════════════════════════════════════

# 任务类型 → ModelCategory 映射（绘画模型注册在 vision 分类下）
_TASK_CATEGORY_MAP = {
    "dialog": "dialog",
    "paint": "vision",
    "video": "video",
}

# 速度标签基准（显存需求越大 → 推理越慢，本地经验值）
_SPEED_TABLE = [
    (0, 4, "极速"),
    (4, 8, "快速"),
    (8, 16, "标准"),
    (16, 999, "慢速"),
]
