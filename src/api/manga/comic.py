"""漫画项目域路由：项目 CRUD / 剧本 DSL 导入 / 场景物件 / 导出包。

TASK-P2-01 自 manga.py 按路由域拆出（原文件 4521 行 → 包）。
"""
from __future__ import annotations

import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, File, Query, UploadFile

from ...config import (
    DATA_DIR,
)
from ...data.database import get_db_safe, parse_json
from ...data.models import (
    ArtStyleCreate,
    ProjectBatchDelete,
    ProjectCreate,
    ProjectUpdate,
    SceneObjectUpdate,
)
from ...middleware.error_handler import ApiError, ok
from ...services.inference.video_engine import VIDEO_OUT_DIR
from ...services.offload import run_blocking
from .comic_asset import (
    assets_to_global,
)
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

if TYPE_CHECKING:
    from ...data.database import Database

router = APIRouter()
log = logging.getLogger("omnispace.api.manga.comic")



def _project_row_to_dict(r: dict) -> dict:
    return {"project_id": r["id"], "name": r.get("name", ""),
            "work_mode": r.get("work_mode", "regular"),
            "art_style": r.get("art_style", ""),
            "project_type": r.get("project_type", "manga"),
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
        log.warning("目录删除失败 %s: %s", path, exc, exc_info=True)


def _safe_unlink(path: Path, base: Path) -> None:
    """删除单文件：仅当 path resolve 后严格位于 base 内才执行。"""
    if not _is_within(path, base):
        log.warning("拒绝删除越界文件: %s", path)
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("文件删除失败 %s: %s", path, exc, exc_info=True)


def _cleanup_comfy_run_dirs(task_ids: list[str]) -> None:
    """ComfyUI 侧链式视频工作目录回收（2026-08-31 删除机制补全）。

    链式引擎收片是 move（成片 MP4 不留 ComfyUI），但 output/h3_chains/
    h3chain_<task_id>/ 下的链式中间帧工作目录从不清理，无限累积
    （08-31 实测积压 5.4G）。删除记录/删行/删项目时一并回收；
    零信任守卫同源：resolve 后必须在 _COMFY_OUTPUT 内。
    """
    try:
        # 延迟导入：services 层符号，避免 api 层模块装载顺序耦合
        from ...services.inference.h3_engine import _COMFY_OUTPUT
        for tid in task_ids:
            _safe_rmtree(_COMFY_OUTPUT / "h3_chains" / f"h3chain_{tid}",
                         _COMFY_OUTPUT)
    except Exception as exc:  # noqa: BLE001 - 清理失败不阻塞主流程
        log.warning("ComfyUI 工作目录清理失败: %s", exc, exc_info=True)


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
        # ComfyUI 侧链式中间帧工作目录（成片已 move 走，此处只剩死重）
        # （task_ids 为未定义名——Cython 编译期静态检查揪出的潜伏 NameError，
        #   实际应为 video_task_ids，2026-09-01 修复）
        _cleanup_comfy_run_dirs(video_task_ids)
    except Exception as exc:  # noqa: BLE001 - 磁盘清理不阻塞主流程
        log.warning("项目磁盘清理失败 %s: %s", project_id, exc, exc_info=True)


@router.post("/comic/project/create")
def comic_project_create(req: ProjectCreate) -> dict[str, Any]:
    """创建漫剧/漫画项目（COMIC-001/002/003）。

    template=comic_drama 时预置 5 行模板分镜；项目重名 →
    COMIC_PROJECT_NAME_DUPLICATED。project_type 区分产品面
    （manga=漫剧库 / comic=漫画页），共用全部生成底座。
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
                           "art_style": (req.art_style or "").strip(),
                           "project_type": req.project_type,
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
               "art_style": (req.art_style or "").strip(),
               "project_type": req.project_type,
               "created_at": now})


@router.get("/comic/project/list")
def comic_project_list(type: str | None = None) -> dict[str, Any]:
    """项目列表（COMIC-004），按更新时间倒序。

    type 过滤产品面（manga/comic）：漫剧库与漫画页各自只看自己的项目；
    缺省不过滤=全部（兼容旧调用）。
    """
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法列出项目")
    if type in ("manga", "comic"):
        rows = db.query(
            "SELECT id, name, work_mode, art_style, project_type,"
            " created_at, updated_at FROM projects WHERE project_type=?"
            " ORDER BY updated_at DESC, created_at DESC", (type,))
    else:
        rows = db.query(
            "SELECT id, name, work_mode, art_style, project_type,"
            " created_at, updated_at FROM projects"
            " ORDER BY updated_at DESC, created_at DESC")
    items = [_project_row_to_dict(r) for r in rows]
    return ok({"items": items, "total": len(items)})


# ══ 自定义作品风格 CRUD（2026-08-24：预置之外可自定义画风）══════════

@router.get("/comic/art-style/list")
def art_style_list() -> dict[str, Any]:
    """自定义风格列表（预置风格由前端 ART_STYLES 常量提供，不落库）。

    返回 key=custom:{id}（创建项目时直接写入 projects.art_style）。
    """
    db = get_db_safe()
    if db is not None:
        _ensure_pack_columns(db)
    items: list[dict] = []
    if db is not None:
        try:
            rows = db.query(
                "SELECT id, name, prompt, created_at, pack, pack_def "
                "FROM art_styles ORDER BY created_at DESC")
            items = [{"style_id": r["id"], "key": f"custom:{r['id']}",
                      "name": r.get("name", ""),
                      "prompt": r.get("prompt", ""),
                      "created_at": r.get("created_at", 0),
                      "pack": r.get("pack", "") or "",
                      "has_pack_def": bool(r.get("pack_def", ""))}
                     for r in rows]
        except Exception as exc:  # noqa: BLE001
            log.warning("自定义风格列表读取失败: %s", exc, exc_info=True)
    return ok({"items": items, "total": len(items)})


@router.post("/comic/art-style/create")
def art_style_create(req: ArtStyleCreate) -> dict[str, Any]:
    """新增自定义风格（重名拒绝；名称≤30字，提示词≤500字）。

    2026-08-31 用户裁定：自定义风格**必须导入风格包**（pack_def JSON，
    parse_custom_pack 校验）——此前纯文本自定义只影响提示词、引擎
    行为与默认无异；现在导入的包定义（底座偏好 + 后处理档位 +
    风格词块）随生成真实生效。
    """
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法保存风格")
    name = req.name.strip()
    if not name:
        raise ApiError("SYSTEM_PARAM_INVALID", "风格名称不能为空")
    dup = db.query_one("SELECT id FROM art_styles WHERE name=?", (name,))
    if dup is not None:
        raise ApiError("COMIC_ART_STYLE_NAME_DUPLICATED", detail={"name": name})
    pack_raw = (req.pack_def or "").strip()
    if not pack_raw:
        raise ApiError("SYSTEM_PARAM_INVALID", "必须导入风格包（.json 文件）——自定义风格需携带"
            "底座偏好/后处理档位/风格词块定义，模板见弹窗下载示例")
    from ...services.inference.gen_router import parse_custom_pack
    _pack, pack_err = parse_custom_pack(pack_raw)
    if _pack is None:
        raise ApiError("SYSTEM_PARAM_INVALID", f"风格包校验失败：{pack_err}")
    sid = uuid.uuid4().hex[:7]
    prompt = req.prompt.strip()
    _ensure_pack_columns(db)
    db.insert("art_styles", {"id": sid, "name": name, "prompt": prompt,
                             "created_at": _now(), "pack": _pack.sid,
                             "pack_def": pack_raw})
    return ok({"style_id": sid, "key": f"custom:{sid}", "name": name,
               "prompt": prompt, "pack": _pack.sid,
               "pack_label": _pack.label})


def _ensure_pack_columns(db: Database) -> None:
    """art_styles 补 pack / pack_def 列（幂等）+ 首次全量族回填。

    回填（2026-08-31 卡片↔包显式绑定）：按 detect_style 对 name+
    prompt 命中写 pack 列（2026-09-02 起与嗅探/定族同口径收口，
    三处不再各写一份匹配循环）——大族卡（韩漫/3D/水墨…）生成时
    确定切换底座/后处理，不再依赖 A 段措辞嗅探。仅在 pack
    列全空时执行一次（单事务批量提交）。
    """
    cols = {r["name"] for r in db.query("PRAGMA table_info(art_styles)")}
    if "pack" not in cols:
        db.sql("ALTER TABLE art_styles ADD COLUMN pack TEXT DEFAULT ''")
    if "pack_def" not in cols:
        db.sql("ALTER TABLE art_styles ADD COLUMN pack_def TEXT DEFAULT ''")
    try:
        tagged = db.query_one(
            "SELECT COUNT(*) AS n FROM art_styles WHERE pack!=''") or {}
        if int(tagged.get("n") or 0) > 0:
            return
        from ...services.inference.gen_router import detect_style
        rows = db.query("SELECT id, name, prompt FROM art_styles")
        updates = []
        for row in rows:
            text = f"{row.get('name', '')}、{row.get('prompt', '')}"
            sid = detect_style(text).sid
            if sid != "default":  # 未命中不绑（生成时回落嗅探，同旧）
                updates.append((sid, row["id"]))
        if updates:
            db.execute_in_transaction(lambda conn: [
                conn.execute("UPDATE art_styles SET pack=? WHERE id=?", u)
                for u in updates])
            log.info("art_styles 族回填完成: %d/%d 张卡绑定风格包",
                     len(updates), len(rows))
    except Exception as exc:  # noqa: BLE001 - 回填失败不阻断接口
        log.warning("art_styles 族回填失败（不影响现有行为）: %s", exc, exc_info=True)


@router.delete("/comic/art-style/{style_id}")
def art_style_delete(style_id: str) -> dict[str, Any]:
    """删除自定义风格（引用该风格的项目不做级联清理，展示层兜底）。"""
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用")
    row = db.query_one("SELECT id FROM art_styles WHERE id=?", (style_id,))
    if row is None:
        raise ApiError("COMIC_ART_STYLE_NOT_FOUND", detail={"style_id": style_id})
    db.delete("art_styles", "id=?", (style_id,))
    return ok({"deleted": style_id})


@router.put("/comic/project/{project_id}")
def comic_project_update(project_id: str, req: ProjectUpdate) -> dict[str, Any]:
    """重命名项目（COMIC-004）；新名与他项目重名 → 名称重复错误。"""
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法更新项目")
    row = db.query_one("SELECT id FROM projects WHERE id=?", (project_id,))
    if row is None:
        raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", "项目不存在", detail={"project_id": project_id})
    dup = db.query_one("SELECT id FROM projects WHERE name=? AND id<>?",
                       (req.name, project_id))
    if dup is not None:
        raise ApiError("COMIC_PROJECT_NAME_DUPLICATED",
                       detail={"name": req.name})
    # art_style 可选随行更新（漫画页「换风格重生成」：关键帧生成时
    # 经 _project_style_pack 实时读项目画风，改完对后续生成即生效）
    patch: dict[str, Any] = {"name": req.name, "updated_at": _now()}
    if req.art_style is not None:
        patch["art_style"] = req.art_style.strip()
    db.update("projects", patch, "id=?", (project_id,))
    return ok({"project_id": project_id, "name": req.name,
               "art_style": (req.art_style or "").strip() or None})


def _delete_project_cascade(db: Database, project_id: str) -> bool:
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
def comic_project_delete(project_id: str) -> dict[str, Any]:
    """删除项目（COMIC-004）：级联删除分镜表/分镜行/视频任务/资产/关键帧/
    场景对象，并清理项目磁盘产物（资产目录/关键帧目录/视频文件）。"""
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法删除项目")
    if not _delete_project_cascade(db, project_id):
        raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", "项目不存在", detail={"project_id": project_id})
    return ok({"project_id": project_id, "deleted": True})


@router.post("/comic/project/batch-delete")
def comic_project_batch_delete(req: ProjectBatchDelete) -> dict[str, Any]:
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
        raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", "项目不存在", detail={"project_ids": missing_ids})
    return ok({"deleted": len(deleted_ids), "deleted_ids": deleted_ids,
               "missing_ids": missing_ids})


# ── 插件技能（技能插座批3，方案 docs/插件技能层接线方案-2026-09-17）──

_COMIC_SKILL_MAX_IMAGES = 4
_COMIC_SKILL_MAX_SIDE = 4096
_COMIC_SKILL_TIMEOUT_S = 180.0
# 技能可读入的图片目录（与 /manga/media 回读白名单同口径）
_COMIC_SKILL_INPUT_DIRS = ("comic_assets", "keyframes")


def _read_skill_images(relpaths: list[str]) -> list[Any]:
    """按 DATA_DIR 相对路径读图为 RGB uint8 ndarray（零信任闸同 media 口径）。"""
    import numpy as np
    from PIL import Image
    out: list[Any] = []
    for rel in relpaths[:_COMIC_SKILL_MAX_IMAGES]:
        p = Path(rel)
        if p.is_absolute() or ".." in p.parts:
            raise ApiError("SYSTEM_PARAM_INVALID", "非法图片路径", detail={"path": rel[:200]})
        base = (DATA_DIR / p).resolve()
        allowed = [(DATA_DIR / d).resolve()
                   for d in _COMIC_SKILL_INPUT_DIRS]
        if not any(base.is_relative_to(a) for a in allowed):
            raise ApiError("SYSTEM_PARAM_INVALID", "图片路径不在允许目录内",
                           detail={"allowed": list(_COMIC_SKILL_INPUT_DIRS)})
        if not base.is_file():
            raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", "图片不存在", detail={"path": rel[:200]})
        try:
            img = Image.open(base).convert("RGB")
        except OSError as exc:
            raise ApiError("PLUGIN_INPUT_INVALID",
                           f"图片解码失败: {base.name}") from exc
        if max(img.size) > _COMIC_SKILL_MAX_SIDE:
            ratio = _COMIC_SKILL_MAX_SIDE / max(img.size)
            img = img.resize((max(1, round(img.width * ratio)),
                              max(1, round(img.height * ratio))))
        out.append(np.asarray(img, dtype=np.uint8))
    return out


@router.post("/comic/skill")
async def comic_skill(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """AI 漫画插件技能（批3）：图像进图像出（后处理，不进图像队列）。

    请求: {"plugin": str, "skill_id": str, "image_paths": [DATA_DIR 相对
    路径（comic_assets/keyframes）], "text"?: str}
    产出帧落 data/plugins/output/&lt;plugin&gt;/&lt;run&gt;/，返回
    /manga/media 可回读的相对 URL 列表——前端作「新版本」显示，不覆盖
    原图。沙箱档（user_source）不支持图像输入：子进程通道 JSON-only
    （方案 B 兜底，如实报错不硬撑）。
    """
    plugin = str(body.get("plugin") or "").strip()
    skill_id = str(body.get("skill_id") or "").strip()
    if not plugin or not skill_id:
        raise ApiError("PLUGIN_SPEC_MISMATCH", "plugin 与 skill_id 必填",
                       suggestion="先经 GET /plugins/skills?feature=comic "
                                  "获取可用技能清单")
    relpaths = body.get("image_paths")
    if not isinstance(relpaths, list) or not relpaths:
        raise ApiError("SYSTEM_PARAM_INVALID", "image_paths 必须是非空列表")
    from ...services.plugin_runtime import get_plugin_runtime
    from ...services.plugin_runtime.registry import PluginRuntimeError, sandbox_enabled
    rt = get_plugin_runtime()
    try:
        skills = rt.skills_info("comic")
    except PluginRuntimeError as exc:
        raise ApiError(exc.code, exc.message,
                       suggestion=exc.suggestion) from exc
    match = next((s for s in skills
                  if s["plugin"] == plugin and s["id"] == skill_id), None)
    if match is None:
        raise ApiError("PLUGIN_SKILL_NOT_FOUND",
                       f"漫画技能不存在或已停用: {plugin}/{skill_id}",
                       suggestion="刷新插件技能清单后重试")
    entry_trust = match.get("trust", "")
    if entry_trust == "user_source" and sandbox_enabled():
        raise ApiError(
            "PLUGIN_IMAGE_SANDBOX_UNSUPPORTED",
            f"含源码档（沙箱）插件暂不支持图像技能: {plugin}",
            suggestion="图像技能需出厂或纯数据档插件；或等沙箱 IPC "
                       "升级后开放")
    images = await run_blocking(_read_skill_images,
                                [str(p) for p in relpaths])
    run_dir = f"skill-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    try:
        result = await rt.invoke(
            plugin,
            {"kind": "skill", "skill_id": skill_id, "feature": "comic",
             "text": str(body.get("text") or ""),
             "images": images},
            save_dirname=run_dir, timeout_s=_COMIC_SKILL_TIMEOUT_S)
    except PluginRuntimeError as exc:
        raise ApiError(exc.code, exc.message,
                       suggestion=exc.suggestion) from exc
    saved = result.get("saved_files") if isinstance(result, dict) else None
    out_dir = result.get("output_dir") if isinstance(result, dict) else None
    urls = []
    if isinstance(saved, list) and out_dir:
        for name in saved:
            rel = Path(out_dir).resolve().relative_to(
                DATA_DIR.resolve()) / str(name)
            urls.append(str(rel).replace("\\", "/"))
    if not urls:
        # 插件未产出帧：文本结果如实透出（如纯分析类技能）
        data = result.get("data") if isinstance(result, dict) else None
        data = data if isinstance(data, dict) else {}
        text_out = data.get("text") or data.get("output")
        return ok({"plugin": plugin, "skill_id": skill_id,
                   "title": match.get("title", ""),
                   "image_urls": [],
                   "text": str(text_out or "")[:8000] or "（无输出）"})
    return ok({"plugin": plugin, "skill_id": skill_id,
               "title": match.get("title", ""),
               "image_urls": urls, "text": ""})


# ═══════════════════════════════════════════════════════════════════
#  批 1.2 DSL 文件上传（COMIC-005/009/010）
# ═══════════════════════════════════════════════════════════════════

@router.post("/comic/script/import-dsl")
async def comic_script_import_dsl(project_id: str = Query(...),
                                  strict: bool = Query(False),
                                  file: UploadFile = File(...)) -> dict[str, Any]:
    """DSL 剧本文件上传导入（multipart，.txt/.dsl ≤10MB）。

    strict=true 时要求文本含 ``shot:`` 分镜标记，否则
    COMIC_DSL_FORMAT_INVALID；解析复用 storyboard_import 管线。
    """
    filename = (file.filename or "").lower()
    if not filename.endswith((".txt", ".dsl")):
        raise ApiError("UNSUPPORTED_FORMAT", "仅支持 .txt/.dsl 剧本文件",
                       detail={"filename": file.filename})
    # 有界读（2026-09-15 审计 P2-4 收尾）：旧实现先全量 read 后验大小，
    # 超大文件会先整包进内存才被拒——与 comic_asset.py 三端点同款修法
    raw = await file.read(_DSL_MAX_BYTES + 1)
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
def comic_scene_object_update(req: SceneObjectUpdate) -> dict[str, Any]:
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
def comic_scene_object_list(project_id: str = Query(...)) -> dict[str, Any]:
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
def comic_export_bundle(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """项目内容合并打包（COMIC-139）：分镜 json/csv + 资产 zip + 视频 → 单 zip。"""
    import json as _json
    import zipfile
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        raise ApiError("SYSTEM_PARAM_INVALID", "缺少 project_id")
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
# 本项目仅供学习使用，商业授权请+Q 3559331368
