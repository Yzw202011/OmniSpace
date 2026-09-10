"""漫画资产域路由：资产库 / 绑定采纳 / 参考图 / 历史与描述。

TASK-P2-01 自 manga.py 按路由域拆出（原文件 4521 行 → 包）。
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, File, Form, Query, UploadFile

from ...config import (
    API_PREFIX,
    DATA_DIR,
)
from ...data.database import Database, get_db_safe, parse_json
from ...data.models import (
    _UNSAFE_NAME_PAT,
    AssetAdoptRequest,
    AssetBatchGenerateRequest,
    AssetBindRequest,
    AssetGenerateRequest,
    AssetInferRequest,
    AssetRegenerateViewRequest,
    AssetTurnaroundRequest,
    AssetUpdateRequest,
)
from ...engines.vllm_service import get_vllm_service, pil_images_to_b64
from ...middleware.error_handler import ApiError, ok
from ...middleware.feature_lock import acquire_or_raise
from ...services.image_queue import get_image_queue
from ...services.inference.dialog_engine import DialogEngine, get_dialog_engine
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
    manga_dialog_model_id,
)

if TYPE_CHECKING:
    from ...services.flow_trace import Flow

router = APIRouter()
log = logging.getLogger("omnispace.api.manga.comic_asset")


def _start_asset_flow(feature: str, friendly: str, *,
                       input_summary: str = "") -> Flow | None:
    """漫剧生图执行流程追踪（2026-08-25 用户裁定补埋）：

    漫剧四视图/资产生成此前无 flow_trace 埋点，失败与取消任务在
    日志页「执行流程」面板完全不可见（实测 15:23 四视图连环失败
    零记录）。module 复用 "paint"（前端模块映射已覆盖），friendly
    以「漫剧·」前缀区分绘画模块直出任务。
    """
    try:
        from ...services.flow_trace import start_flow
        return start_flow("paint", feature, friendly,
                          trigger="用户提交漫剧生成任务",
                          input_summary=input_summary)
    except Exception:  # noqa: BLE001 - 追踪失败不影响业务
        return None


def _end_asset_flow(flow: Flow | None, status: str, *, error_code: str = "",
                    error_detail: str = "", output_summary: str = "") -> None:
    """结束漫剧生图追踪流程（幂等，失败吞掉不影响业务）。"""
    if flow is None:
        return
    try:
        flow.end(status, error_code=error_code,
                 error_detail=error_detail, output_summary=output_summary)
    except Exception:  # noqa: BLE001
        pass



def _asset_row_to_dict(r: dict) -> dict:
    return {"asset_id": r["id"], "project_id": r.get("project_id", ""),
            "kind": r.get("kind", "character"), "name": r.get("name", ""),
            "file_path": r.get("file_path", ""), "prompt": r.get("prompt", ""),
            "meta": parse_json(r.get("meta"), {}),
            "created_at": r.get("created_at", 0),
            "scope": r.get("scope") or "project",
            "face": r.get("face") or "manga"}


def _asset_kind_endpoint(kind: str) -> Callable[[AssetGenerateRequest], Awaitable[dict[str, Any]]]:
    """生成角色/场景/道具三个端点的公共实现工厂。

    2026-09-02 图像队列：submit_and_wait 在请求内排队（HTTP 契约不变），
    与绘画页/关键帧同队顺序消费（此前无锁直跑，靠引擎内部锁盲等）。
    """
    async def _handler(req: AssetGenerateRequest) -> dict[str, Any]:
        db = get_db_safe()
        if db is not None:
            _ensure_project(db, req.project_id)
        flow = _start_asset_flow(
            "comic_asset",
            f"漫剧·{_ASSET_KIND_CONF.get(kind, {}).get('subdir', kind)}"
            f"资产生成：{(req.name or '')[:20]}",
            input_summary=f"{req.width}x{req.height} "
                          f"{(req.prompt or '')[:60]}")
        # 云端路由（批2 云端API 2026-09-06）：资产工位绑定云端连接时
        # 任务走云端道（队列跳过本地锁）、生成核心换云端适配器；
        # 解析失败按本地（不阻断生成）
        try:
            from ...services.cloud_provider_service import get_image_endpoint
            _cloud_ep = get_image_endpoint("asset.image")
        except Exception as exc:  # noqa: BLE001
            log.warning("资产云端路由解析失败（按本地引擎）: %s", exc)
            _cloud_ep = None

        def _runner(task: dict, check_cancel: Callable[[], None]) -> dict:  # noqa: ARG001
            return _generate_asset_sync(req, kind,
                                        cloud_endpoint=_cloud_ep)

        try:
            data = await get_image_queue().submit_and_wait({
                "task_id": f"asset:{kind[:4]}:{req.name[:8]}:"
                           f"{time.time_ns():x}",
                "kind": "comic_asset", "runner": _runner,
                "loop": asyncio.get_running_loop(),
                "cloud": _cloud_ep is not None})
        except ApiError as exc:
            _end_asset_flow(flow, "error", error_code=str(exc.code),
                            error_detail=str(exc.message)[:300])
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("资产生成失败: %s", exc)
            _end_asset_flow(flow, "error",
                            error_code="PAINT_GENERATION_FAILED",
                            error_detail=str(exc)[:300])
            raise ApiError("PAINT_GENERATION_FAILED", str(exc)[:300]) from exc
        _end_asset_flow(flow, "success",
                        output_summary=str(data.get("file_path") or ""))
        return ok(data)
    return _handler


router.post("/comic/asset/generate-character")(
    _asset_kind_endpoint("character"))
router.post("/comic/asset/generate-scene")(
    _asset_kind_endpoint("scene"))
router.post("/comic/asset/generate-prop")(
    _asset_kind_endpoint("prop"))


@router.post("/comic/asset/generate-turnaround")
async def comic_asset_generate_turnaround(req: AssetTurnaroundRequest) -> dict[str, Any]:
    """角色多视图生成（COMIC-033~037，竞品对齐）：正面/侧面/背面/特写
    四张独立 16:9 图逐视图生成（每张可单独重生），同 seed 保一致性，
    自动落盘 portrait_views/ + canvas.png（2×2 拼图）→ 入库。

    诚实降级：FLUX.1-dev 未随包，SDXL 兜底，响应带 degraded 标记。
    2026-09-02 接入统一图像队列：此前无功能锁直跑——与队列任务
    （comfy 关键帧等）跨引擎叠载、且队列排空收尾 unload 不持
    _infer_lock 可砸中多视图循环（hazard 测试固化）。
    """
    db = get_db_safe()
    if db is not None:
        _ensure_project(db, req.project_id)
    flow = _start_asset_flow(
        "comic_turnaround",
        f"漫剧·角色四视图：{(req.name or '')[:20]}",
        input_summary=f"seed={req.seed} {(req.prompt or '')[:60]}")

    def _runner(task: dict, check_cancel: Callable[[], None]) -> dict:  # noqa: ARG001
        return _generate_turnaround_sync(req)

    try:
        data = await get_image_queue().submit_and_wait({
            "task_id": f"turnaround:{req.name[:8]}:{time.time_ns():x}",
            "kind": "comic_asset", "runner": _runner,
            "loop": asyncio.get_running_loop()})
    except ApiError as exc:
        _end_asset_flow(flow, "error", error_code=str(exc.code),
                        error_detail=str(exc.message)[:300])
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("多视图资产生成失败: %s", exc)
        _end_asset_flow(flow, "error",
                        error_code="PAINT_GENERATION_FAILED",
                        error_detail=str(exc)[:300])
        raise ApiError("PAINT_GENERATION_FAILED", str(exc)[:300]) from exc
    _end_asset_flow(
        flow, "success",
        output_summary=f"pipeline={data.get('pipeline')} "
                       f"views={len(data.get('views') or [])}")
    return ok(data)


@router.post("/comic/asset/batch-generate")
async def comic_asset_batch_generate(req: AssetBatchGenerateRequest) -> dict[str, Any]:
    """批量资产生成（COMIC-030）：逐项串行生成，聚合成功/失败明细。

    2026-09-02 图像队列：整批=一个队列任务（循环语义不变）。
    """
    kind = (req.kind or "character").strip()
    if kind not in _ASSET_KIND_CONF:
        raise ApiError(40008, "kind 必须是 character/scene/prop",
                       detail={"allowed": list(_ASSET_KIND_CONF)})
    if not req.items:
        raise ApiError(40008, "缺少 items 数组")
    db = get_db_safe()
    if db is not None:
        _ensure_project(db, req.project_id)
    # 2026-08-24：FLUX.2 中文直入为主路径后取消整批预译——FLUX 中文
    # 全文直送无需翻译（预译反而白载对话引擎）；SDXL 回退时由
    # _generate_asset_sync 按项现译（罕见降级路径，可接受换载）。
    raw_prompts = [str(item.get("prompt") or "").strip()
                   or str(item.get("name") or "asset") for item in req.items]
    flow = _start_asset_flow(
        "comic_batch",
        f"漫剧·批量{kind}资产生成（{len(req.items)} 项）",
        input_summary=", ".join(
            str(item.get("name") or "")[:12] for item in req.items[:8]))
    # 云端路由（批2）：整批共用一次绑定解析
    try:
        from ...services.cloud_provider_service import get_image_endpoint
        _cloud_ep = get_image_endpoint("asset.image")
    except Exception as exc:  # noqa: BLE001
        log.warning("资产云端路由解析失败（按本地引擎）: %s", exc)
        _cloud_ep = None

    def _batch_runner(task: dict, check_cancel: Callable[[], None]) -> dict:  # noqa: ARG001
        results: list[dict] = []
        failed: list[dict] = []
        for item, raw_prompt in zip(req.items, raw_prompts, strict=True):
            sub = AssetGenerateRequest(
                project_id=req.project_id,
                name=str(item.get("name") or "未命名资产")[:100],
                # DB/UI 保留用户原文；FLUX 直入用原文，SDXL 回退时现译
                prompt=raw_prompt,
                width=int(item.get("width", IMG_TARGET_W)),
                height=int(item.get("height", IMG_TARGET_H)),
                transparent=bool(item.get("transparent", False)))
            try:
                data = _generate_asset_sync(sub, kind, None,
                                            cloud_endpoint=_cloud_ep)
                results.append(data)
            except ApiError as exc:
                failed.append({"name": sub.name, "code": exc.code,
                               "message": exc.message})
            except Exception as exc:  # noqa: BLE001
                failed.append({"name": sub.name,
                               "code": "PAINT_GENERATION_FAILED",
                               "message": str(exc)[:300]})
        return {"project_id": req.project_id, "kind": kind,
                "succeeded": results, "failed": failed,
                "total": len(req.items), "success_count": len(results)}

    data = await get_image_queue().submit_and_wait({
        "task_id": f"assetbatch:{req.project_id[:10]}:{time.time_ns():x}",
        "kind": "comic_asset", "runner": _batch_runner,
        "loop": asyncio.get_running_loop(),
        "cloud": _cloud_ep is not None})
    _end_asset_flow(
        flow, "success" if not data["failed"] else "error",
        error_code="BATCH_PARTIAL_FAILED" if data["failed"] else "",
        error_detail="; ".join(
            f"{f['name']}: {f['message'][:80]}" for f in data["failed"][:5]),
        output_summary=f"成功 {data['success_count']}/{len(req.items)}")
    return ok(data)


@router.get("/comic/asset/library")
def comic_asset_library(project_id: str | None = Query(None),
                        kind: str | None = Query(None),
                        scope: str | None = Query(
                            None, description="project=项目资产（默认）；"
                            "global=全局资产库（跨项目，忽略 project_id）"),
                        face: str | None = Query(
                            "manga", description="全局资产归属面："
                            "manga(漫剧，缺省向后兼容) | comic(漫画页)"
                            "——2026-09-08 两产品面全局池隔离"),
                        limit: int = Query(100, ge=1, le=500,
                                           description="返回条数上限"),
                        offset: int = Query(0, ge=0,
                                            description="分页偏移")) -> dict[str, Any]:
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
        if face in ("manga", "comic"):
            cond.append("face=?")
            params.append(face)
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


def _replace_row_bindings(db: Database, project_id: str, old_id: str, new_id: str) -> int:
    """将项目内分镜行绑定中的 old_id 资产替换为 new_id（2026-08-26 修复）。

    跨项目「引入」此前只建副本不换旧绑定：行 asset_ids 新旧 id 并存
    （同一资产两份图，前端资产列对旧跨项目 id 渲染空按钮）。引入时把
    目标项目所有引用旧 id 的行绑定替换为新副本 id；asset_id 旧列指向
    旧 id 时同步改写；替换后去重（存量数据曾新旧并存）。只扫描目标
    项目的行——源项目内对源资产的绑定是合法引用，不动。返回替换行数。
    """
    if not old_id or old_id == new_id or not project_id:
        return 0
    rows = db.query(
        "SELECT r.id, r.asset_id, r.asset_ids FROM storyboard_rows r "
        "JOIN storyboards s ON r.storyboard_id = s.id WHERE s.project_id=?",
        (project_id,))
    replaced = 0
    for r in rows:
        if old_id not in _row_asset_ids(r):
            continue
        asset_ids = list(dict.fromkeys(
            new_id if a == old_id else a for a in _row_asset_ids(r)))
        asset_id = r.get("asset_id", "") or ""
        if asset_id == old_id:
            asset_id = new_id
        db.update("storyboard_rows",
                  {"asset_id": asset_id, "asset_ids": asset_ids},
                  "id=?", (r["id"],))
        replaced += 1
    return replaced


@router.put("/comic/asset/bind")
def comic_asset_bind(req: AssetBindRequest) -> dict[str, Any]:
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
def comic_asset_adopt(req: AssetAdoptRequest) -> dict[str, Any]:
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
    # 同时补换行内旧绑定（修复前引入的存量行仍指向源资产 id）。
    for r in db.query(
            f"SELECT {_ASSET_COLS} FROM comic_assets WHERE project_id=?",
            (req.project_id,)):
        existing = _asset_row_to_dict(r)
        if (existing["meta"] or {}).get("adopted_from") == req.asset_id:
            rebound = _replace_row_bindings(
                db, req.project_id, req.asset_id, existing["asset_id"])
            return ok({"asset": existing, "already_adopted": True,
                       "rebound_rows": rebound})
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
    # 引入即接管：目标项目行内对源资产的旧绑定同步替换为新副本 id
    rebound = _replace_row_bindings(db, req.project_id, req.asset_id, new_id)
    return ok({"asset": {**asset, "asset_id": new_id,
                         "project_id": req.project_id,
                         "file_path": new_rel, "meta": meta},
               "rebound_rows": rebound})


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


def _face_of_project(db: Database, project_id: str) -> str:
    """全局资产归属面：取项目 product_type（漫画项目→comic，其余→manga）。

    项目不存在（历史脏数据）时回落 manga（此前全局池只有漫剧在用）。
    """
    row = db.query_one(
        "SELECT project_type FROM projects WHERE id=?", (project_id,))
    return "comic" if row and row.get("project_type") == "comic" else "manga"


def assets_to_global(db: Database, project_id: str) -> int:
    """项目资产整体转全局域（删除项目时调用，用户裁定：不删除生成资产）。

    DB 行：scope='global'、project_id 置空；磁盘目录同步迁移到
    comic_assets/global/；迁移后项目资产根目录无文件残留时清理。
    返回转出的资产数。绑定关系（storyboard_rows.asset_ids）随分镜行
    一并由项目删除流程级联清理，此处不触碰。
    """
    import shutil

    rows = db.query(f"SELECT {_ASSET_COLS} FROM comic_assets"
                    " WHERE project_id=?", (project_id,))
    face = _face_of_project(db, project_id)
    for r in rows:
        asset = _asset_row_to_dict(r)
        new_rel, meta = _asset_move_to_global(asset, project_id)
        db.update("comic_assets",
                  {"project_id": "", "scope": "global", "face": face,
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
def comic_asset_to_global(asset_id: str) -> dict[str, Any]:
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
    face = _face_of_project(db, pid)
    db.update("comic_assets",
              {"project_id": "", "scope": "global", "face": face,
               "file_path": new_rel, "meta": meta},
              "id=?", (asset_id,))
    row = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    return ok({"asset": _asset_row_to_dict(row), "already_global": False})


@router.put("/comic/asset/unbind")
def comic_asset_unbind(req: AssetBindRequest) -> dict[str, Any]:
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
def comic_asset_update(asset_id: str, req: AssetUpdateRequest) -> dict[str, Any]:
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
        if "prompt" in fields:
            # P0 数据修复：手动接管描述词 → 解除 stale 门控（用户操作
            # 最高权限），标记来源为人工
            meta = dict(parse_json(row.get("meta"), {}))
            meta.pop("prompt_stale", None)
            meta["prompt_source"] = "manual"
            fields["meta"] = meta
        db.update("comic_assets", fields, "id=?", (asset_id,))
        row = db.query_one(
            f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    return ok({"asset": _asset_row_to_dict(row)})


@router.post("/comic/asset/{asset_id}/regenerate")
async def comic_asset_regenerate(asset_id: str,
                                 body: dict = Body(default_factory=dict)) -> dict[str, Any]:
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
    flow = _start_asset_flow(
        "comic_regenerate",
        f"漫剧·资产生成重生成：{(asset.get('name') or '')[:20]}",
        input_summary=f"kind={asset.get('kind')} "
                      f"{(asset.get('prompt') or '')[:60]}")

    def _runner(task: dict, check_cancel: Callable[[], None]) -> dict:  # noqa: ARG001
        return _regenerate_asset_sync(asset)

    try:
        data = await get_image_queue().submit_and_wait({
            "task_id": f"assetregen:{asset_id[:10]}:{time.time_ns():x}",
            "kind": "comic_asset", "runner": _runner,
            "loop": asyncio.get_running_loop()})
    except ApiError as exc:
        if exc.code == "PAINT_ENGINE_NOT_READY":
            _end_asset_flow(
                flow, "warning", error_code=str(exc.code),
                error_detail=f"引擎未就绪保留原图: {exc.message}")
            # degrade_reason 必须带上 suggestion（去哪加载/怎么处理），
            # 否则用户只看到"未就绪"却无出路（2026-08-31 漫剧生图诉求）
            reason = ("绘画引擎未就绪，已保留原图（非真实重生成）："
                      f"{exc.message}")
            if getattr(exc, "suggestion", ""):
                reason += f"。{exc.suggestion}"
            return ok({"asset": asset, "degraded": True,
                       "degrade_reason": reason})
        _end_asset_flow(flow, "error", error_code=str(exc.code),
                        error_detail=str(exc.message)[:300])
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("资产重生成失败: %s", exc)
        _end_asset_flow(flow, "error",
                        error_code="PAINT_GENERATION_FAILED",
                        error_detail=str(exc)[:300])
        raise ApiError("PAINT_GENERATION_FAILED", str(exc)[:300]) from exc
    _end_asset_flow(flow, "success",
                    output_summary=str(data.get("file_path") or ""))
    return ok({"asset": data, "degraded": False})


@router.post("/comic/asset/{asset_id}/regenerate-view")
async def comic_asset_regenerate_view(asset_id: str,
                                      req: AssetRegenerateViewRequest) -> dict[str, Any]:
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
    flow = _start_asset_flow(
        "comic_regenerate_view",
        f"漫剧·单视图重生：{(asset.get('name') or '')[:20]}·{req.view}",
        input_summary=f"view={req.view} {prompt_zh[:60]}")

    def _runner(task: dict, check_cancel: Callable[[], None]) -> dict:  # noqa: ARG001
        return _regenerate_view_sync(asset, req.view, prompt_zh)

    try:
        # 2026-09-02 接入统一图像队列（同 generate-turnaround：
        # 此前无功能锁直跑，跨引擎叠载/排空卸载竞态见 hazard 测试）
        data = await get_image_queue().submit_and_wait({
            "task_id": f"regenview:{asset_id[:10]}:{req.view}:"
                       f"{time.time_ns():x}",
            "kind": "comic_asset", "runner": _runner,
            "loop": asyncio.get_running_loop()})
    except ApiError as exc:
        _end_asset_flow(flow, "error", error_code=str(exc.code),
                        error_detail=str(exc.message)[:300])
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("单视图重生失败: %s", exc)
        _end_asset_flow(flow, "error",
                        error_code="PAINT_GENERATION_FAILED",
                        error_detail=str(exc)[:300])
        raise ApiError("PAINT_GENERATION_FAILED", str(exc)[:300]) from exc
    _end_asset_flow(flow, "success",
                    output_summary=str(data.get("file_path") or ""))
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
                                       file: UploadFile = File(...)) -> dict[str, Any]:
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
    _assert_image_magic(raw, file.filename)
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
def comic_asset_delete(asset_id: str) -> dict[str, Any]:
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
def comic_asset_reference_delete(asset_id: str) -> dict[str, Any]:
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
def comic_asset_history(asset_id: str) -> dict[str, Any]:
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


@router.get("/comic/image/tasks")
def comic_image_task_list(project_id: str = Query("", description="项目ID"),
                          category: str = Query(
                              "all", description="all/asset/keyframe"),
                          limit: int = Query(100, ge=1, le=500,
                                             description="返回条数上限"),
                          offset: int = Query(0, ge=0,
                                              description="分页偏移")) -> dict[str, Any]:
    """项目图片生成记录（2026-08-24 用户裁定：生成记录需含图片，具体详细）。

    聚合两类图片产物，统一 created_at 倒序：
    - asset：资产图生成留痕（meta.history，generate/regenerate/view
      各一条）；引擎/模型/seed/尺寸取资产当前 meta——历史条目无独立
      引擎留痕（meta 留痕仅 ts/kind/view/file），如实标注当前生成参数
    - keyframe：分镜关键帧（keyframes 表多版本，LEFT JOIN
      storyboard_rows 取镜号）
    """
    pid = (project_id or "").strip()
    if not pid:
        raise ApiError(40008, "缺少 project_id")
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法查询图片记录")
    records: list[dict] = []
    if category in ("all", "asset"):
        for r in db.query(
                f"SELECT {_ASSET_COLS} FROM comic_assets"
                " WHERE project_id=? ORDER BY created_at DESC", (pid,)):
            meta = parse_json(r.get("meta"), {})
            if not isinstance(meta, dict):
                meta = {}
            history = meta.get("history")
            if not isinstance(history, list):
                history = []
            for h in reversed(history):  # 最新在前
                if not isinstance(h, dict):
                    continue
                rel = str(h.get("file") or "").replace("\\", "/")
                ts = float(h.get("ts") or 0)
                records.append({
                    "record_id": f"{r['id']}:{int(ts)}",
                    "category": "asset",
                    "asset_id": r["id"],
                    "name": r.get("name", ""),
                    "kind": r.get("kind", ""),
                    "action": h.get("kind", ""),
                    "view": h.get("view") or "",
                    "file_path": rel,
                    "url": f"{API_PREFIX}/manga/media/{rel}" if rel else "",
                    "created_at": ts,
                    "engine": meta.get("engine", ""),
                    "model": meta.get("model", ""),
                    "seed": meta.get("seed"),
                    "width": meta.get("width"),
                    "height": meta.get("height"),
                })
    if category in ("all", "keyframe"):
        for r in db.query(
                "SELECT k.id, k.row_id, k.version, k.file_path, k.prompt,"
                " k.status, k.error, k.is_current, k.created_at,"
                " sr.shot_number AS shot_number"
                " FROM keyframes k"
                " LEFT JOIN storyboard_rows sr ON k.row_id = sr.id"
                " WHERE k.project_id=?", (pid,)):
            rel = str(r.get("file_path") or "").replace("\\", "/")
            records.append({
                "record_id": r["id"],
                "category": "keyframe",
                "row_id": r.get("row_id", ""),
                "shot_number": int(r.get("shot_number", 0) or 0),
                "version": int(r.get("version", 1)),
                "is_current": bool(r.get("is_current", 1)),
                "file_path": rel,
                "url": f"{API_PREFIX}/manga/media/{rel}" if rel else "",
                "prompt": (r.get("prompt") or "")[:80],
                "status": r.get("status", "done"),
                "created_at": float(r.get("created_at", 0) or 0),
            })
    records.sort(key=lambda x: x["created_at"], reverse=True)
    total = len(records)
    return ok({"items": records[offset:offset + limit], "total": total})


# 资产图片上传约束（竞品对齐）：png/jpg/jpeg/webp ≤10MB
_ASSET_UPLOAD_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def _assert_image_magic(raw: bytes, filename: str | None) -> None:
    """上传图片魔数校验（2026-09-02 B4 污染实弹修复）。

    扩展名白名单可被伪装绕过（<script> HTML / MZ EXE 改名 .png 落盘
    成可分发文件，经 /manga/media 回读）。PIL 解码必须成功且实际格式
    在允许集内；调用点必须在落盘之前。此前 upload/replace 两端点
    write_bytes 先写盘、解码失败被吞（「尺寸读取失败不阻断登记」），
    伪装文件会照常入库。
    """
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as probe:
            fmt = (probe.format or "").upper()
            if fmt not in ("PNG", "JPEG", "WEBP"):
                raise ValueError(f"实际内容格式 {fmt} 不在允许集内")
            probe.verify()
    except Exception as exc:  # noqa: BLE001 - 解码/格式失败即拒绝
        raise ApiError(
            40010, "文件内容不是有效图片（魔数校验失败），已拒绝上传",
            detail={"filename": filename or "",
                    "reason": str(exc)[:120]}) from exc
_ASSET_UPLOAD_MAX_BYTES = 10 * 1024 * 1024  # 10MB


@router.post("/comic/asset/{asset_id}/image")
async def comic_asset_image_replace(asset_id: str,
                                    file: UploadFile = File(...)) -> dict[str, Any]:
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
    _assert_image_magic(raw, file.filename)
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
    if kind == "character":
        # P0 数据修复：图已换、词仍旧 → 立 stale 标记（前端门控），
        # 后台 VLM 按新图重写描述词后解除（vLLM 未热备则保持 stale，
        # 由用户点「生成描述词」显式触发或手动编辑解除）
        meta["prompt_stale"] = True
    db.update("comic_assets",
              {"file_path": rel_path, "meta": meta}, "id=?", (asset_id,))
    if kind == "character":
        _spawn_prompt_rewrite(asset_id)
    nrow = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    return ok({"asset": _asset_row_to_dict(nrow)})


@router.post("/comic/asset/upload")
async def comic_asset_upload(project_id: str = Form(...),
                             kind: str = Form("character"),
                             name: str = Form(""),
                             file: UploadFile = File(...)) -> dict[str, Any]:
    """资产图片上传（竞品对齐）：本地图片登记为项目资产。

    multipart 字段：file / project_id / kind / name。
    校验扩展名 png/jpg/jpeg/webp 与大小 ≤10MB；落盘
    DATA_DIR/comic_assets/{project_id}/{subdir}/{name}/（与资产生成
    同目录约定，file_path 为 DATA_DIR 相对路径，/manga/media 白名单可回读）。
    """
    pid = (project_id or "").strip()
    if not pid:
        raise ApiError(40008, "缺少 project_id")
    # 路径安全（审计 09-10 P1-A）：pid 直拼落盘目录且会经 _ensure_project
    # 建项目行，与下方 name 同规矩拒路径元字符（另两处 pid 仅作 DB
    # 查询键，不碰文件系统，无需消毒）
    from ...data.models import _check_safe_name
    try:
        pid = _check_safe_name(pid)
    except ValueError as exc:
        raise ApiError(40010, f"project_id 不合法：{exc}") from None
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
    _assert_image_magic(raw, file.filename)
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法上传资产")
    _ensure_project(db, pid)
    asset_name = ((name or "").strip()[:100]
                  or Path(filename).stem[:100] or "未命名资产")
    # 路径安全（审计 P1-1 修复）：name 直拼落盘目录，拒路径元字符
    try:
        asset_name = _check_safe_name(asset_name)
    except ValueError as exc:
        raise ApiError(40010, f"资产名称不合法：{exc}") from None
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
    if kind == "character":
        # P0 数据修复：新上传角色描述词为空 → stale 标记 + 后台 VLM
        # 按图生词（未就绪保持 stale，交由显式触发/手动编辑解除）
        meta["prompt_stale"] = True
    db.insert("comic_assets", {
        "id": asset_id, "project_id": pid, "kind": kind,
        "name": asset_name, "file_path": rel_path, "prompt": "",
        "meta": meta, "created_at": _now()})
    if kind == "character":
        _spawn_prompt_rewrite(asset_id)
    row = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    return ok({"asset": _asset_row_to_dict(row)})


# 实体推断资产桩的自动描述词模板（竞品对齐）
_INFER_PROMPT_TPL = {
    "character": "{name}，角色立绘，全身像，白色背景",
}


def _infer_era_llm(engine: DialogEngine, db: Database, project_id: str) -> str:
    """从剧本原文提取项目整体时代背景（2026-08-24 用户裁定）。

    返回 ≤40 字短语（如"当代中国城市，21世纪20年代"/"架空古代王朝，
    类唐宋"）；LLM 不可用/失败回退固定文案 _INFER_ERA_LINE。
    """
    try:
        # 取分镜前 6 行（描述+台词）作为时代判断依据（开头通常含
        # 场景/环境交代，足够判定年代且不超上下文）
        ctx = ""
        sb = _find_storyboard(db, project_id)
        if sb is not None:
            rows = db.query(
                "SELECT description, original_dialogue FROM"
                " storyboard_rows WHERE storyboard_id=?"
                " ORDER BY sort_index ASC, shot_number ASC LIMIT 6",
                (sb["id"],))
            ctx = "；".join(
                f"{(r.get('description') or '')[:80]}"
                f"{(r.get('original_dialogue') or '')[:80]}".strip()
                for r in rows if (
                    (r.get("description") or "")
                    or (r.get("original_dialogue") or "")))
        if not ctx:
            return _INFER_ERA_LINE
        prompt = (
            "你是漫剧制片助理。依据以下剧本片段判断整部作品的时代"
            "背景，输出一个短语（≤30字），格式如：当代中国城市，21世"
            "纪20年代 / 架空古代王朝，类唐宋 / 近未来科幻都市。只输出"
            "短语本身，不输出任何解释。\n\n剧本片段：\n" + ctx[:1500])
        reply = engine.chat([{"role": "user", "content": prompt}],
                            temperature=0.2, max_new_tokens=64)
        era = (reply or "").strip().strip("。. \n")[:40]
        if era and len(era) <= 40 and "\n" not in era:
            return era
        return _INFER_ERA_LINE
    except Exception as exc:  # noqa: BLE001 - 失败回退固定文案
        log.warning("时代背景提取失败（回退固定文案）: %s", exc)
        return _INFER_ERA_LINE


# 实体名提取提示词（2026-08-24：AI 切分不填实体列，角色推理需从
# 原文直接提取实体名；行协议输出与分镜协议同理——比 JSON 稳定）
_ENTITY_NAMES_PROMPT = (
    "你是漫剧制片助理。从下列剧本原文中提取全部实体名称，严格"
    "忠于原文：\n"
    "1. 角色：出场人物的姓名或固定称呼\n"
    "2. 场景：故事发生的地点/场所\n"
    "3. 道具：角色可手持、携带或使用的可移动独立物件，且对剧情"
    "有实际作用（如武器、行李箱、手机、信件、钥匙）；以下不提取："
    "身体部位与服装部件（衣角、袖口、发丝）、植物花卉与自然物"
    "（花瓣、树叶、云朵、雨滴）、建筑构件与环境固定物（围栏、门窗、"
    "路灯）、场景的一部分（桌面、墙面、路面）、无特殊剧情作用的"
    "日常泛用品（白纸、筷子、水杯）\n"
    "要求：不虚构原文没有的实体；泛称（如：路人、人群）不提取。\n"
    "输出格式（严格遵守，三类各一行，多个用中文逗号分隔，没有则"
    "写“无”）：\n"
    "角色|名字1，名字2\n场景|名字1，名字2\n道具|名字1，名字2\n\n"
    "剧本原文：\n{chunk}"
)
# 实体名提取分块上限（块 1200 字 × 8 块 ≈ 万字剧本覆盖）
_ENTITY_CHUNK_CHARS = 1200
_ENTITY_CHUNK_MAX = 8
_ENTITY_NAMES_CAP = 40  # 每类实体名上限（防模型失控列举）

# 道具资产价值裁定提示词（2026-08-24 用户裁定：LLM 提取的道具噪声
# 大——衣角/花瓣/围栏/花卉被当作道具立绘污染资产库，需二次终审）
_PROP_VETTING_PROMPT = (
    "你是漫剧美术资产审核员。逐一判断下列候选道具是否值得制作成"
    "独立的道具资产立绘（用于后续分镜合成）。\n"
    "值得制作（保留）：可移动的独立物件，能被角色手持、携带或"
    "使用，且对剧情有实际作用（如武器、行李箱、手机、信件、钥匙、"
    "礼物盒、宠物用品）。\n"
    "不值得制作（否决）：\n"
    "- 身体部位或服装部件（衣角、袖口、发丝、鞋带）\n"
    "- 植物、花卉、自然物（花瓣、树叶、三角梅、云朵、雨滴）\n"
    "- 建筑构件与环境固定物（围栏、门窗、路灯、长椅）\n"
    "- 场景的一部分而非独立物件（桌面、墙面、地板）\n"
    "- 无特殊剧情作用的日常泛用品（白纸、筷子、水杯）\n"
    "- 抽象概念或不可绘制之物（声音、气味、回忆、气氛）\n"
    "输出格式（每行一条，严格遵守，覆盖全部候选）：\n"
    "道具名|保留\n道具名|否决\n\n"
    "候选道具：{names}"
)
# 启发式否决子串（LLM 终审前先丢明显垃圾：身体部位/服装部件/
# 自然物/建筑构件——零成本拦截高频噪声）
_PROP_REJECT_SUBSTRS = (
    "衣角", "袖口", "领口", "下摆", "衣襟", "发丝", "发梢", "指尖",
    "手心", "侧脸", "背影", "睫毛", "花瓣", "叶片", "叶子", "树荫",
    "光斑", "云朵", "云层", "雨滴", "雨丝", "微风", "空气", "阳光",
    "月光", "围栏", "栅栏", "栏杆", "门窗", "窗户", "屋檐", "房顶",
    "墙面", "地板", "地面", "桌面", "路面", "台阶", "天空", "远处",
)


def _vet_props_llm(engine: DialogEngine, names: list[str]) -> set[str]:
    """裁定 LLM 提取的候选道具的资产价值，返回保留集。

    两层闸门（2026-08-24 用户裁定：道具提取噪声大）：
    ① 启发式子串过滤（明显垃圾零成本丢弃）
    ② LLM 终审（行协议「道具名|保留/否决」；4B 模型做二元判断
       远比提取时顺手列举可靠）
    防幻觉：裁定输出只认候选内的名字（kept &= set(cand)）；
    未出现在裁定输出中的候选默认否决——宁可漏提不可错提，错提会
    生成立绘污染资产库。LLM 失败回退启发式结果（不阻断推理）。
    """
    import re
    cand = [n for n in names
            if len(n) >= 2 and not any(s in n for s in _PROP_REJECT_SUBSTRS)]
    dropped = len(names) - len(cand)
    if not cand:
        log.info("道具裁定: %d 候选全部命中启发式否决，丢弃",
                 len(names))
        return set()
    kept: set[str] = set()
    try:
        reply = engine.chat(
            [{"role": "user",
              "content": _PROP_VETTING_PROMPT.format(
                  names="，".join(cand))}],
            temperature=0.1, max_new_tokens=512)
        for line in (reply or "").splitlines():
            m = re.match(r"^(.+?)\s*[|｜]\s*(保留|否决)\s*$",
                         line.strip())
            if m and m.group(2) == "保留":
                kept.add(m.group(1).strip())
        kept &= set(cand)
    except Exception as exc:  # noqa: BLE001 - 裁定失败回退启发式结果
        log.warning("道具裁定 LLM 失败（回退启发式）: %s", exc)
        return set(cand)
    log.info("道具资产价值裁定: 候选 %d（启发式丢 %d）→ 保留 %d / 否决 %d",
             len(names), dropped, len(kept), len(cand) - len(kept))
    return kept


def _row_text_chunks(rows: list[dict],
                     limit: int = _ENTITY_CHUNK_CHARS) -> list[str]:
    """把分镜行文本（描述+台词）按行边界攒成 ≤limit 字的块。"""
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for r in rows:
        t = (f"{(r.get('description') or '').strip()}"
             f"{(r.get('original_dialogue') or '').strip()}").strip()
        if not t:
            continue
        if size + len(t) + 1 > limit and buf:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(t)
        size += len(t) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def _extract_entity_names_llm(engine: DialogEngine, rows: list[dict],
                              on_chunk: Callable[[int, int], None] | None = None) -> dict[str, set[str]]:
    """LLM 从分镜行原文提取实体名（2026-08-24 实体列空白兜底）。

    返回 {character/scene/prop: {名字}}；引擎失败/解析为空返回空集
    （调用方维持"未发现新实体"口径，不伪造实体）。
    on_chunk(done, total)：每块推理完成回调（进度上报用，可空）。
    """
    import re
    out: dict[str, set[str]] = {"character": set(), "scene": set(),
                                "prop": set()}
    chunks = _row_text_chunks(rows)
    if not chunks:
        return out
    key_map = {"角色": "character", "场景": "scene", "道具": "prop"}
    plan = chunks[:_ENTITY_CHUNK_MAX]
    total = len(plan)
    for i, chunk in enumerate(plan):
        try:
            reply = engine.chat(
                [{"role": "user",
                  "content": _ENTITY_NAMES_PROMPT.format(chunk=chunk)}],
                temperature=0.2, max_new_tokens=512)
        except Exception as exc:  # noqa: BLE001 - 单块失败继续下一块
            log.warning("实体名提取块失败（跳过）: %s", exc)
            if on_chunk:
                on_chunk(i + 1, total)
            continue
        for line in (reply or "").splitlines():
            m = re.match(r"^(角色|场景|道具)\s*[|｜]\s*(.+)$",
                         line.strip())
            if not m:
                continue
            key = key_map[m.group(1)]
            val = m.group(2).strip()
            if not val or val == "无":
                continue
            for nm in re.split(r"[，,、]", val):
                nm = nm.strip(" 。.\n")[:100]
                if nm and nm != "无":
                    out[key].add(nm)
        if on_chunk:
            on_chunk(i + 1, total)
    for k in out:
        if len(out[k]) > _ENTITY_NAMES_CAP:
            out[k] = set(sorted(out[k])[:_ENTITY_NAMES_CAP])
    log.info("实体名 LLM 提取: %d 块 → 角色 %d / 场景 %d / 道具 %d",
             len(chunks), len(out["character"]), len(out["scene"]),
             len(out["prop"]))
    return out


def _backfill_row_entities(db: Database, rows: list[dict],
                           names: dict[str, set[str]]) -> int:
    """按子串匹配回填分镜行实体列（仅填空列，不覆盖已有值）。

    rows 需含 id/description/original_dialogue/characters/scene/props。
    返回更新行数。db.update 内部把 list 序列化为 JSON（与分镜行
    update 路径同一约定）。
    """
    chars = sorted(names.get("character", set()))
    scenes = sorted(names.get("scene", set()))
    props = sorted(names.get("prop", set()))
    updated = 0
    for r in rows:
        text = (f"{r.get('description') or ''}"
                f"{r.get('original_dialogue') or ''}")
        fields: dict = {}
        if not (parse_json(r.get("characters"), []) or []):
            hit = [n for n in chars if n in text]
            if hit:
                fields["characters"] = hit
        if not (r.get("scene") or "").strip():
            hit_scene = next((n for n in scenes if n in text), "")
            if hit_scene:
                fields["scene"] = hit_scene
        if not (parse_json(r.get("props"), []) or []):
            hit_props = [n for n in props if n in text]
            if hit_props:
                fields["props"] = hit_props
        if fields:
            db.update("storyboard_rows", fields, "id=?", (r["id"],))
            updated += 1
    if updated:
        log.info("实体列回填: %d/%d 行", updated, len(rows))
    return updated


def _infer_character_settings_llm(db: Database, project_id: str,
                                  names: list[str],
                                  style_line: str = "",
                                  era_line: str = "") -> dict[str, str]:
    """批量生成角色设定段（角色推理 v2，2026-08-20；v3 2026-08-24）。

    一次 LLM 调用为全部新角色生成「角色设定」正文（剧本上下文注入
    到提示），返回 {name: setting}。v3 按用户四层提取规范升级：
    基础信息/服饰装备/姿态表情/时代背景逐层覆盖，忠实原文不虚构，
    并注入项目风格保持统一。LLM 不可用/失败/解析失败时返回
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
            "外观描述，系统性覆盖四层：①基础信息（姓名年龄、身高"
            "体态、面部细节、发型发色、气质关键词）；②服饰装备"
            "（上衣/下装/鞋履的款式、颜色、材质、细节设计与穿着"
            "状态）；③姿态表情（常态表情、视线方向、肢体特征）；"
            "④贴合时代背景。要求：与剧本上下文高度一致，原文没写"
            "的服装款式不得虚构（可用日常合理穿搭补全面料与颜色）；"
            f"贴合作品风格「{style_line or _INFER_STYLE_LINE}」；"
            f"时代背景为「{era_line or _INFER_ERA_LINE}」。"
            "结尾固定收口：空手，常态平静表情，眼睛平视镜头。\n"
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


def _infer_scene_prop_settings_llm(db: Database, project_id: str,
                                   scenes: list[str],
                                   props: list[str],
                                   style_line: str = "",
                                   era_line: str = "",
                                   on_stage: Callable[[str], None] | None = None) -> tuple[dict[str, str],
                                                          dict[str, str]]:
    """批量生成场景/道具设定段（角色推理 v3，2026-08-24 用户裁定）。

    场景层次：地理位置与时代→时间与天气→核心主体→周围环境元素→
    远景→光影与空气氛围；道具层次：尺寸规格与类型→外观材质与
    颜色→细节纹理与装饰→边角与造型特征→特殊设计细节→质感总结。
    忠实原文不虚构。
    返回 ({scene: desc}, {prop: desc})；LLM 不可用返回空 dict
    （调用方回退类型模板，如实记 meta.inferred_by='template'）。
    LLM 只产出「场景描述/道具描述」正文行，段式骨架（美术风格/
    时代背景/其他要求行）由调用方拼装（2026-08-24 用户格式规范）。
    on_stage(kind)：每段推理（scene/prop）开始前回调（进度上报用）。
    """
    if not scenes and not props:
        return {}, {}
    try:
        engine = get_dialog_engine()
        if not engine.ensure_loaded(None):
            log.warning("场景/道具提取 LLM 不可用（%s），回退模板",
                        engine.get_status().get("last_error", ""))
            return {}, {}
        import re
        scene_out: dict[str, str] = {}
        prop_out: dict[str, str] = {}

        def _extract(kind: str, names: list[str],
                     rules: dict, out: dict[str, str]) -> None:  # 注解修正：实参为 dict（Cython 编译期揪出）
            if not names:
                return
            if on_stage:
                on_stage(kind)
            label = rules["label"]  # 中文标签（【场景】/【道具】），输出解析同标签
            blocks = []
            for nm in names[:16]:  # 一次上限 16 个
                ctx = _entity_story_context(db, project_id, nm, kind)
                blocks.append(
                    f"【{label}名称】{nm}\n【剧本上下文】{ctx or '（无）'}")
            prompt = (
                f"你是漫剧美术设定师。为下列每个{label}生成"
                f"「{rules['desc_label']}」正文一段，系统性覆盖层次："
                f"{rules['layers']}。要求：与剧本上下文高度一致，"
                "原文没写的细节不得虚构情节性内容（可用常规视觉常识"
                "补全材质光影）；不要输出美术风格/时代背景/版式要求"
                "（我方另行拼装）。\n"
                "输出格式（严格遵守，每个一块，之间空行）：\n"
                f"【{label}】：名字\n{rules['desc_label']}：正文一段"
                f"{rules['len']}\n\n" + "\n\n".join(blocks))
            reply = engine.chat([{"role": "user", "content": prompt}],
                                temperature=0.7,
                                max_new_tokens=2800)
            desc_label = rules["desc_label"]
            for m in re.finditer(
                    rf"【{label}】[：:]\s*([^\n]{{1,100}})\n\s*"
                    rf"{desc_label}[：:]\s*([^\n【]+)", reply):
                name = m.group(1).strip()
                setting = m.group(2).strip()
                if name and setting:
                    out[name] = setting
            log.info("%s提取 LLM 批量生成: 请求 %d 成功 %d",
                     label, len(names), len(out))

        _extract("scene", scenes, {
            "label": "场景",
            "layers": ("①地理位置与时代（城市/地域/场所类型与年代感）；"
                       "②时间与天气（时段、季节、天气）；③画面中心"
                       "核心主体（主体建筑/物体及其外观细节）；④周围"
                       "环境元素（配景、植被、路面、设施等）；⑤远景"
                       "（天空、远处轮廓）；⑥光影与空气氛围（光线"
                       "方向、色调、空气感与氛围感受）"),
            "len": "100~180 字",
            "desc_label": "场景描述",
        }, scene_out)
        _extract("prop", props, {
            "label": "道具",
            "layers": ("①尺寸规格与道具类型（大小/种类，完整全貌）；"
                       "②外观材质与颜色（主材质、主色调）；③细节纹理"
                       "与装饰（纹样、压印、配件）；④边角与造型特征"
                       "（形状、圆角、轮廓）；⑤特殊设计细节（功能"
                       "部件、闭合/固定方式）；⑥整体质感总结"),
            "len": "80~150 字",
            "desc_label": "道具描述",
        }, prop_out)
        return scene_out, prop_out
    except Exception as exc:  # noqa: BLE001 - LLM 失败回退模板
        log.warning("场景/道具提取 LLM 异常（回退模板）: %s", exc)
        return {}, {}


# ── 角色推理进度上报（2026-08-24 用户需求：弹窗 + 进度条 + 预计时间）──
# project_id -> 状态 dict；run_blocking 线程写、轮询端点读，单键
# 赋值原子（同 common._video_eta 模式，免锁）
_INFER_PROGRESS: dict[str, dict] = {}


def _infer_prog_start(project_id: str, rows_count: int) -> None:
    _INFER_PROGRESS[project_id] = {
        "running": True, "stage": "aggregate",
        "stage_label": "聚合分镜实体列", "percent": 1.0,
        "detail": f"{rows_count} 行分镜", "started_at": time.time(),
        "eta_hint": None,
    }


def _infer_prog_set(project_id: str, stage: str, label: str,
                    percent: float, detail: str = "") -> None:
    """推进进度（单调不回退，推理中封顶 99；100 由 finish 写入）。"""
    st = _INFER_PROGRESS.get(project_id)
    if st is None:
        return
    st["stage"] = stage
    st["stage_label"] = label
    st["percent"] = round(max(st["percent"], min(percent, 99.0)), 1)
    st["detail"] = detail


def _infer_prog_finish(project_id: str, success: bool,
                       message: str = "") -> None:
    st = _INFER_PROGRESS.get(project_id)
    if st is None:
        return
    st["running"] = False
    st["success"] = success
    st["message"] = message
    st["elapsed_seconds"] = round(time.time() - st["started_at"], 1)
    if success:
        st["percent"] = 100.0


def _safe_entity_name(nm: str) -> str:
    """实体名消毒（审计 09-10 P2-2）：分镜列/LLM 提取的实体名事后会
    拼进落盘路径（_asset_dir_for/图替换/adopt copytree），与资产名同
    规矩剔路径元字符；剔后为空或含「..」返回空串（调用方丢弃该实体，
    不炸整次推理）。"""
    cleaned = _UNSAFE_NAME_PAT.sub("", str(nm or "").strip()[:100])
    return "" if not cleaned or ".." in cleaned else cleaned


def _infer_entities_sync(project_id: str) -> dict:
    """infer-entities 同步主体（在线程池执行，持 dialog 锁期间调用）。

    各阶段向 _INFER_PROGRESS 上报进度（前端弹窗 1s 轮询展示）：
    aggregate → extract_names（逐块）→ era → char_settings →
    scene_settings / prop_settings → save。percent 按推理耗时占比
    加权（块推理 ~38s、设定段 ~30s 量级），供端点线性外推 ETA。
    """
    _infer_prog_start(project_id, 0)
    db = get_db_safe()
    sb = _find_storyboard(db, project_id)
    rows: list[dict] = []
    if sb is not None:
        rows = db.query(
            "SELECT id, characters, scene, props, description,"
            " original_dialogue FROM storyboard_rows"
            " WHERE storyboard_id=?", (sb["id"],))
    _INFER_PROGRESS[project_id]["detail"] = f"{len(rows)} 行分镜"
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
    # v3.1（2026-08-24）：AI 切分不填实体列 → 列全空时 LLM 从分镜
    # 原文提取实体名并回填行（分镜表可见，下次推理免二次提取）
    if not any(wanted.values()) and rows:
        n_chunks = min(len(_row_text_chunks(rows)), _ENTITY_CHUNK_MAX)
        _infer_prog_set(project_id, "extract_names", "提取实体名称",
                        4.0, f"0/{n_chunks} 块 · 逐块推理中")
        # ETA 初始估算（块推理 ~38s + 道具裁定 ~10s + 三段设定
        # ~32s×3 + 加载 6s），外推法 percent≥10 后接管
        _INFER_PROGRESS[project_id]["eta_hint"] = 6 + n_chunks * 38 + 106
        engine = get_dialog_engine()
        if engine.ensure_loaded(None):

            def _on_chunk(done: int, total: int) -> None:
                _infer_prog_set(
                    project_id, "extract_names", "提取实体名称",
                    4 + 52 * done / max(total, 1),
                    f"第 {done}/{total} 块 · 逐块推理中")

            names = _extract_entity_names_llm(engine, rows,
                                              on_chunk=_on_chunk)
            # 道具资产价值裁定（2026-08-24 用户裁定：提取噪声大——
            # 衣角/花瓣/围栏等被当作道具立绘；只裁 LLM 提取的，
            # 用户手填 props 列不过滤）
            if names.get("prop"):
                _infer_prog_set(project_id, "vet_props",
                                "裁定道具资产价值", 56.0,
                                f"{len(names['prop'])} 个候选道具")
                names["prop"] = _vet_props_llm(
                    engine, sorted(names["prop"]))
            if any(names.values()):
                _backfill_row_entities(db, rows, names)
                wanted = {k: set(v) for k, v in names.items()}
    # 审计 09-10 P2-2：两类来源（分镜列/LLM 提取）统一在此消毒，
    # 下游 settings 字典键与入库名全部以消毒后为准；消毒后为空的
    # 实体直接丢弃
    for _kind in wanted:
        wanted[_kind] = {
            safe for raw_nm in wanted[_kind]
            if (safe := _safe_entity_name(raw_nm))}
    existing = db.query(
        "SELECT kind, name FROM comic_assets WHERE project_id=?",
        (project_id,))
    existing_keys = {(r.get("kind", ""), (r.get("name") or "").strip())
                     for r in existing}
    new_characters = sorted(
        nm for nm in wanted["character"]
        if ("character", nm) not in existing_keys)
    new_scenes = sorted(
        nm for nm in wanted["scene"]
        if ("scene", nm) not in existing_keys)
    new_props = sorted(
        nm for nm in wanted["prop"]
        if ("prop", nm) not in existing_keys)
    # 角色推理 v3：引擎一次预热，时代背景提取 + 三类设定生成共用；
    # 美术风格行联动项目 art_style（未选回退网漫风）
    style_line = _project_style_line(db, project_id)
    era_line = ""
    if new_characters or new_scenes or new_props:
        _infer_prog_set(project_id, "era", "提取时代背景", 58.0,
                        "从剧本前文分析")
        engine = get_dialog_engine()
        if engine.ensure_loaded(None):
            era_line = _infer_era_llm(engine, db, project_id)
    settings = {}
    if new_characters:
        _infer_prog_set(project_id, "char_settings", "生成角色四层设定",
                        64.0, f"{len(new_characters)} 个角色")
        settings = _infer_character_settings_llm(
            db, project_id, new_characters, style_line, era_line)
    scene_settings: dict[str, str] = {}
    prop_settings: dict[str, str] = {}
    if new_scenes or new_props:

        def _on_sp_stage(kind: str) -> None:
            if kind == "scene":
                _infer_prog_set(project_id, "scene_settings",
                                "生成场景四层设定", 82.0,
                                f"{len(new_scenes)} 个场景")
            else:
                _infer_prog_set(project_id, "prop_settings",
                                "生成道具四层设定", 90.0,
                                f"{len(new_props)} 个道具")

        scene_settings, prop_settings = _infer_scene_prop_settings_llm(
            db, project_id, new_scenes, new_props, style_line, era_line,
            on_stage=_on_sp_stage)
    created = {"character": 0, "scene": 0, "prop": 0}
    new_ids: list[str] = []
    total_new = (len(new_characters) + len(new_scenes)
                 + len(new_props))
    if total_new:
        _infer_prog_set(project_id, "save", "写入资产库", 97.0,
                        f"{total_new} 个新实体")
    kind_settings = {"character": settings, "scene": scene_settings,
                     "prop": prop_settings}
    for kind in ("character", "scene", "prop"):
        for nm in sorted(wanted[kind]):
            if (kind, nm) in existing_keys:
                continue
            setting = kind_settings[kind].get(nm)
            if kind == "character" and setting:
                prompt = _build_character_prompt_v2(
                    nm, setting, style_line, era_line)
                meta = {"source": "infer_entities", "inferred_by": "llm"}
            elif kind == "scene" and setting:
                # 场景/道具：段式骨架（2026-08-24 用户裁定格式规范，
                # 美术风格/时代背景行联动项目，结尾固定其他要求行）
                desc = _strip_setting_prefix(setting, "场景描述")
                prompt = _build_scene_prompt_v2(
                    nm, desc, style_line, era_line)[:2000]
                meta = {"source": "infer_entities", "inferred_by": "llm"}
            elif kind == "prop" and setting:
                desc = _strip_setting_prefix(setting, "道具描述")
                prompt = _build_prop_prompt_v2(
                    nm, desc, style_line, era_line)[:2000]
                meta = {"source": "infer_entities", "inferred_by": "llm"}
            elif kind == "scene":
                prompt = _build_scene_prompt_v2(
                    nm, f"{nm}，场景全景图", style_line, era_line)
                meta = {"source": "infer_entities",
                        "inferred_by": "template"}
            elif kind == "prop":
                prompt = _build_prop_prompt_v2(
                    nm, f"{nm}，道具完整全貌", style_line, era_line)
                meta = {"source": "infer_entities",
                        "inferred_by": "template"}
            else:
                prompt = _INFER_PROMPT_TPL["character"].format(name=nm)
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
    _infer_prog_finish(
        project_id, True,
        f"新增 {total_new} 个实体"
        f"（角色 {created['character']} · 场景 {created['scene']}"
        f" · 道具 {created['prop']}）")
    return {"created": created, "items": items}


@router.post("/comic/asset/infer-entities")
async def comic_asset_infer_entities(req: AssetInferRequest) -> dict[str, Any]:
    """从分镜行系统性提取实体资产（角色推理 v3，2026-08-24 用户裁定）。

    从分镜行的 characters/scene/props 列聚合实体；AI 切分不填实体列，
    列全空时 LLM 直接从分镜原文提取实体名（角色/场景/道具）并回填
    行实体列（仅填空列）。新实体生成四层规范设定（角色：基础信息/
    服饰装备/姿态表情/时代背景；场景：环境/视觉/氛围/时代；道具：
    形态/材质/设计/时代），忠实原文不虚构；描述词美术风格行联动
    项目 art_style，时代背景行从原文提取。角色为五段式（【角色】/
    绘图提示词/美术风格/时代背景/角色设定），场景/道具为一段式。
    LLM 不可用时回退类型模板并如实记 meta.inferred_by。
    推理全程持有 "dialog" 功能锁（防 scheduler 卸载推理中模型）。
    """
    project_id = (req.project_id or "").strip()
    if not project_id:
        raise ApiError(40008, "缺少 project_id")
    if get_db_safe() is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法推断实体")
    lock = await acquire_or_raise("dialog", task_id=f"infer:{project_id}")
    try:
        try:
            data = await run_blocking(_infer_entities_sync, project_id)
        except Exception as exc:  # noqa: BLE001 - 进度标失败后原样上抛
            _infer_prog_finish(project_id, False, str(exc)[:200])
            raise
        return ok(data)
    finally:
        await lock.release("dialog")


@router.get("/comic/asset/infer-progress")
def comic_asset_infer_progress(project_id: str = Query(
        "", description="项目ID")) -> dict[str, Any]:
    """角色推理进度轮询（前端弹窗 1s 拉取，2026-08-24 用户需求）。

    percent 按推理耗时占比加权 → 线性外推 ETA：
    percent≥10 用外推（自适应实际速度），此前用进入提取阶段时的
    静态估算 eta_hint（块推理 ~38s + 三段设定），无任务返回 idle。
    """
    pid = (project_id or "").strip()
    if not pid:
        raise ApiError(40008, "缺少 project_id")
    st = _INFER_PROGRESS.get(pid)
    if st is None:
        return ok({"running": False, "idle": True})
    out = dict(st)
    if st.get("running"):
        elapsed = time.time() - st["started_at"]
        pct = st["percent"]
        out["elapsed_seconds"] = round(elapsed, 1)
        if pct >= 10:
            out["eta_seconds"] = round(elapsed * (100 - pct) / pct, 1)
        elif st.get("eta_hint"):
            out["eta_seconds"] = round(max(st["eta_hint"] - elapsed, 5), 1)
        else:
            out["eta_seconds"] = None
    return ok(out)


# 资产描述词扩写提示词（场景/道具：层次参数化对齐用户格式规范，
# 2026-08-24；扩写正文由调用方拼装段式骨架）
_ASSET_DESCRIBE_PROMPT = """你是漫剧美术设定师。请为以下{kind_label}资产扩写「{kind_label}描述」段正文（我方会拼装成完整绘图描述词）。
要求：
1. 按层次覆盖：{layers}；
2. 100~180 字一段，忠实名称与资产语境，不虚构情节性细节；
3. 只输出正文本身，不输出解释、标题或"描述："前缀。

【{kind_label}名称】{name}"""

_DESCRIBE_LAYERS = {
    "scene": ("地理位置与时代、时间与天气、画面中心核心主体、"
              "周围环境元素、远景、光影与空气氛围"),
    "prop": ("尺寸规格与类型、外观材质与颜色、细节纹理与装饰、"
             "边角与造型特征、特殊设计细节、整体质感总结"),
}

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

# 预置作品风格 key → 中文风格描述（与前端 ART_STYLES 对齐；
# 2026-08-24 用户裁定：资产描述词的美术风格行与项目 art_style 联动。
# 2026-08-29 市场调研裁定：前端预置收敛为 11 种（剔除 healing/manga/
# vintage/live/cartoon，新增 manhwa/thickpaint，国风两向标签强化为
# 国风2D/3D国风）——下线 key 的映射**保留**，历史项目 art_style 仍
# 存旧 key，删映射会静默回退网漫风）
_ART_STYLE_ZH: dict[str, str] = {
    "pixar": "3D卡通、皮克斯质感、黏土材质渲染、柔和电影光、干净明亮",
    "anime": "日系动漫风、干净线稿、色彩明快、五官精致",
    "chibi": "Q版卡通风、2-3头身、五官夸张可爱、高饱和配色",
    # 国风两向（2026-08-29 强化：标签与描述词对齐调研口径）
    "guofeng": "国风2D、工笔重彩、东方美学、水墨淡彩、介于2D与3D之间",
    "xuanhuan": "3D国风玄幻、华丽古装、法术特效、宏大建筑、国风3D渲染",
    # 2026-08-29：描述词去「国风」字——gen_router 国风2D 判据含「国风」，
    # 水墨描述须避免被国风2D 包抢占（水墨归 inkwash 判据「水墨|丹青」）
    "inkwash": "水墨丹青、写意笔触、意境留白、氤氲朦胧",
    # 2026-08-29 新增（爆款主力画风）
    "manhwa": "韩漫半写实、半写实比例、分层上色、精致五官、潮流色调",
    "thickpaint": "厚涂CG玄幻、立体光影、材质肌理、层次堆叠、史诗氛围",
    "cyberpunk": "赛博朋克、霓虹光影、机械结构、未来都市",
    "real3d": "3D写实、电影级CG、质感细腻、光影层次丰富",
    "battle": "热血战斗漫风、高对比度、动态张力、戏剧光影",
    # ── 2026-08-31 新增 12 主流包（中文行含包族关键词，供预置卡
    #    _project_style_pack 定族与 A 段画风锚复用）──
    "comic_en": "美式漫画、硬朗墨线、网点排线、超英分镜、高饱和原色",
    "manga_bw": "黑白漫画、网点纸阴影、锐利墨线、日漫分镜、高对比单色",
    "ghibli": "吉卜力手绘动画、水彩背景、柔和自然光、温暖怀旧手绘质感",
    "pixel": "像素风、16位复古游戏、锐利像素网格、有限色板、抖动上色",
    "uscartoon": "美式卡通、扁平夸张造型、粗描边、明快玩乐配色",
    "steampunk": "蒸汽朋克、维多利亚黄铜机械、齿轮发条、蒸汽管道、复古暖褐",
    "flat": "扁平插画、几何简洁造型、矢量色块、无渐变现代设计感",
    "claymation": "黏土定格动画、黏土手工质感、指痕纹理、柔和棚拍光",
    "popart": "波普复古、复古印刷海报、网点波普、双色调撞色",
    "storybook": "童话绘本、儿童插画、水粉蜡笔质感、暖粉彩、圆润可爱",
    "gothic": "哥特暗黑奇幻、巴洛克阴影、深红炭黑色调、戏剧明暗",
    "lowpoly": "低多边形3D、几何切面、平面着色多边形、风格化简约造型",
    # ── 2026-09-02 新增 4 包预置卡（市场调研爆款赛道；中文行含
    #    各包判据复合词，供 _project_style_pack 定族与 A 段画风锚）。
    #    悬疑行由 mystery3d 复合词「悬疑」先中（mystery3d 在 donghua3d
    #    之前），「3D国漫悬疑」不会被 donghua3d 的「国漫」抢走 ──
    "xianxia_cg": (
        "次世代二次元古风仙侠CG、卡通渲染干净面部阴影、边缘光、"
        "PBR写实场景、游戏CG质感"
    ),
    "mystery3d": (
        "3D国漫悬疑暗黑、谋杀推理、低饱和冷色调、单一暖光源、光影切割"
    ),
    "donghua3d": "3D国漫写实、真人感建模、UE5电影级渲染、朴实辨识脸型",
    "guofeng_hist": "3D古风历史、纯历史质感、汉服写实、宫殿烛光、无仙法光效",
    # ── 已下线预设（前端列表不再展示，映射保留兼容历史项目）──
    "healing": "日系治愈风、柔和粉彩、温暖阳光、清新日常",
    "manga": "黑白漫画风、网点纸阴影、高对比墨线、日漫分镜",
    "vintage": "美式复古卡通风、粗犷线条、夸张变形、胶片颗粒",
    "live": "真人写实、照片级质感、自然皮肤纹理、浅景深",
    "cartoon": "可爱卡通风、明亮配色、圆润造型、全龄友好",
}

# 预置卡 key → 风格包 sid 静态映射（2026-09-02 词序坑治理第二步）。
# 此前预置卡定族靠「拿 _ART_STYLE_ZH 中文行跑 STYLE_PACKS 正则」的
# 间接方式——包该属于谁是显性事实，不该靠文本猜。本表 = 旧正则行为
# 的实测快照（style_routing_golden.json zh_lines 口径），等价性由
# test_style_pack_routing 双向钉死（静态值 ⇄ detect_style(中文行)）。
# healing 旧正则无命中（→默认包），故不在表内（回落嗅探，行为同旧）。
_PRESET_STYLE_PACK: dict[str, str] = {
    "pixar": "claymation", "anime": "anime", "chibi": "anime",
    "guofeng": "guofeng2d", "xuanhuan": "guofeng3d", "inkwash": "inkwash",
    "manhwa": "manhwa", "thickpaint": "thickpaint", "cyberpunk": "cyberpunk",
    "real3d": "cg3d", "battle": "battle", "comic_en": "comic_en",
    "manga_bw": "manga_bw", "ghibli": "ghibli", "pixel": "pixel",
    "uscartoon": "uscartoon", "steampunk": "steampunk", "flat": "flat",
    "claymation": "claymation", "popart": "popart", "storybook": "storybook",
    "gothic": "gothic", "lowpoly": "lowpoly",
    "xianxia_cg": "xianxia_cg", "mystery3d": "mystery3d",
    "donghua3d": "donghua3d", "guofeng_hist": "guofeng_hist",
    # 已下线预设（历史项目仍带旧 key，绑定保持旧正则行为）
    "manga": "manga_bw", "vintage": "anime", "live": "photoreal",
    "cartoon": "anime",
}


def _project_style_line(db: Database, project_id: str) -> str:
    """项目作品风格 → 描述词美术风格行（未选/未知回退网漫风）。

    projects.art_style：预置 key 直查映射；custom:{id} 查 art_styles
    表（name + prompt 拼接）；空串回退 _INFER_STYLE_LINE。
    """
    fallback = _INFER_STYLE_LINE
    if db is None or not project_id:
        return fallback
    try:
        proj = db.query_one(
            "SELECT art_style FROM projects WHERE id=?", (project_id,))
        key = ((proj or {}).get("art_style") or "").strip()
        if not key:
            return fallback
        if key.startswith("custom:"):
            row = db.query_one(
                "SELECT name, prompt FROM art_styles WHERE id=?",
                (key[7:],))
            if row is None:
                return fallback
            parts = [p for p in (row.get("name", "").strip(),
                                 row.get("prompt", "").strip()) if p]
            return "、".join(parts)[:120] or fallback
        return _ART_STYLE_ZH.get(key, fallback)
    except Exception as exc:  # noqa: BLE001 - 查询失败回退不阻塞提取
        log.warning("项目风格查询失败（回退网漫风）: %s", exc)
        return fallback


def _project_style_pack(db: Database, project_id: str) -> tuple[str, dict | None]:
    """项目作品风格 → 显式风格包（2026-08-31 卡片↔包绑定）。

    返回 (pack_id, pack_def)：预置卡读 art_styles.pack（族回填写入）；
    自定义卡读 pack（custom:xxx）+ pack_def（导入 JSON 解析）。无绑定
    返回 ("", None)——路由回落正则嗅探，行为与既往一致。
    """
    if db is None or not project_id:
        return "", None
    try:
        proj = db.query_one(
            "SELECT art_style FROM projects WHERE id=?", (project_id,))
        key = ((proj or {}).get("art_style") or "").strip()
        if not key:
            return "", None
        style_id = key[7:] if key.startswith("custom:") else key
        try:
            row = db.query_one(
                "SELECT pack, pack_def FROM art_styles WHERE id=?", (style_id,))
        except Exception:  # noqa: BLE001 - 旧库无 pack 列（回填前）
            row = None
        if not row:
            # 预置卡（semantic key 如 pixar/manhwa，不在 art_styles 表
            # ——该表只存库卡 hex id + 自定义卡）：2026-09-02 起走
            # _PRESET_STYLE_PACK 静态映射（等价性由金标准测试钉死），
            # 不再用「中文行跑正则」的间接定族
            sid = _PRESET_STYLE_PACK.get(key, "")
            return (sid, None) if sid else ("", None)
        pack_id = (row.get("pack") or "").strip()
        pack_def = None
        if pack_id.startswith("custom:") and row.get("pack_def"):
            import json as _json
            try:
                parsed = _json.loads(row["pack_def"])
                if isinstance(parsed, dict):
                    pack_def = parsed
            except Exception:  # noqa: BLE001 - 损坏 JSON 走嗅探兜底
                pack_def = None
        return pack_id, pack_def
    except Exception as exc:  # noqa: BLE001
        log.warning("项目风格包查询失败（回落嗅探）: %s", exc)
        return "", None


def _build_character_prompt_v2(name: str, setting_text: str,
                               style_line: str = "",
                               era_line: str = "") -> str:
    """拼装五段式角色描述词（2026-08-20 用户裁定格式；v3 2026-08-24
    美术风格/时代背景行动态化）。

    绘图提示词为固定文案；美术风格=项目 art_style 映射（空回退网漫
    风）；时代背景=LLM 原文提取（空回退固定文案）；角色设定来自 LLM。
    生图端由 _sanitize_character_prompt_zh 剥版式与固定段。
    """
    setting = (setting_text or "").strip()
    setting = setting.removeprefix("角色设定：").strip()
    return (f"【角色】：{name}\n"
            f"绘图提示词：{_INFER_DRAW_PROMPT_TPL.format(name=name)}\n"
            f"美术风格：{style_line or _INFER_STYLE_LINE}\n"
            f"时代背景：{era_line or _INFER_ERA_LINE}\n"
            f"角色设定：{setting}")


# ── 场景/道具段式骨架（2026-08-24 用户裁定格式规范，取代旧一段式——
#    旧约束是 SDXL 中译英时代防段标签污染，FLUX 中文直入后段式安全）──
_SCENE_OTHER_REQ = "画面中不要出现人物；禁止出现文字、水印、UI；纯场景输出。"
_PROP_OTHER_REQ = ("画面中不要出现人物；道具居中；完整呈现道具全貌；"
                   "四周留白；禁止裁切与局部特写；纯白背景；禁止纹理；"
                   "全局光照；禁止投影；禁止出现文字、水印、UI。")


def _strip_setting_prefix(text: str, *labels: str) -> str:
    """剥设定正文前的段名前缀（如"场景描述："）。"""
    out = (text or "").strip()
    for lb in labels:
        out = out.removeprefix(f"{lb}：").removeprefix(f"{lb}:").strip()
    return out


def _build_scene_prompt_v2(name: str, desc: str,
                           style_line: str = "",
                           era_line: str = "") -> str:
    """拼装段式场景描述词（美术风格行联动项目风格，结尾固定其他要求）。"""
    return (f"【场景】：{name}\n"
            f"美术风格：{style_line or _INFER_STYLE_LINE}\n"
            f"场景描述：{desc}\n"
            f"时代背景：{era_line or _INFER_ERA_LINE}\n"
            f"其他要求：{_SCENE_OTHER_REQ}")


def _build_prop_prompt_v2(name: str, desc: str,
                          style_line: str = "",
                          era_line: str = "") -> str:
    """拼装段式道具描述词（同场景骨架，其他要求为道具版固定文案）。"""
    return (f"【道具】：{name}\n"
            f"美术风格：{style_line or _INFER_STYLE_LINE}\n"
            f"道具描述：{desc}\n"
            f"时代背景：{era_line or _INFER_ERA_LINE}\n"
            f"其他要求：{_PROP_OTHER_REQ}")


def _entity_story_context(db: Database, project_id: str, name: str,
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


# ── P0 数据修复（2026-08-27）：角色描述词以资产实际图像为准 ─────────
# 根因（V41 事故）：上传/替换图片后 DB prompt 保持旧值，/describe 又是
# 纯文本 LLM 凭资产名「臆想」外貌——描述词与资产照片脱节，生图链路
# B 段注入错误人设、评审判据同步失真。修复：VLM（Qwen3-VL）直读资产
# 图写「角色设定」段；上传/替换后自动后台重写；未完成期 meta
# .prompt_stale=true 供前端卡控（AI 生图门控，手动编辑/重写完成解除）。
_ASSET_VLM_DESCRIBE_CHAR_PROMPT = (
    "你是漫剧美术设定师。观察这张角色设定图，写出「角色设定」段正文"
    "（人设外观描述，我方会拼装成完整绘图描述词）。\n"
    "要求：\n"
    "1. 严格以图片实际可见内容为准，禁止虚构与图中不符的外貌、发型、"
    "服装细节；\n"
    "2. 按顺序覆盖：年龄段、脸型骨相、眉眼鼻唇五官、发型发色与细节"
    "（含刘海/发长/扎法）、气质关键词、上装（款式/颜色/细节）、下装、"
    "鞋子、常态表情；\n"
    "3. 图中未展示的部位按可见信息一句泛化（如仅半身图则下装从简），"
    "不得编造具体款式颜色；\n"
    "4. 只输出设定正文一段，100~180 字，不输出解释、标题或"
    "「角色设定：」前缀。\n"
    "【角色名称】{name}")

_VLM_REWRITE_WAIT_S = 300.0   # 显式触发（点按钮）：允许点火冷启动等就绪
_VLM_AUTO_WAIT_S = 20.0       # 后台自动重写：不点火，仅短窗搭常驻热备便车


def _vlm_describe_char_image_sync(img_path: Path, name: str) -> str | None:
    """VLM 直读角色资产图写设定正文（阻塞，经 run_blocking 调用）。

    Returns:
        设定正文（已去段前缀）；vLLM 未就绪/推理失败返回 None。
    """
    try:
        from PIL import Image
        img = Image.open(img_path).convert("RGB")
        img.thumbnail((960, 960), Image.LANCZOS)  # 省 token / 加速理解
        svc = get_vllm_service()
        chunks: list[str] = []
        for ch in svc.chat_stream(
                [{"role": "user",
                  "content": _ASSET_VLM_DESCRIBE_CHAR_PROMPT.format(
                      name=name or "角色")}],
                images_b64=pil_images_to_b64([img]),
                temperature=0.2, max_tokens=384):
            chunks.append(ch)
        desc = _strip_setting_prefix("".join(chunks).strip(), "角色设定")
        return desc or None
    except Exception as exc:  # noqa: BLE001
        log.warning("VLM 看图写设定失败 %s: %s", img_path, exc)
        return None


async def _wait_vllm_for_rewrite(timeout_s: float, *, ignite: bool) -> bool:
    """等 vLLM 就绪。ignite=False 不点火冷启动——后台自动重写不抢
    显存（vLLM 冷启 ~157s 且与绘画管线互斥，仅搭常驻热备便车）。"""
    svc = get_vllm_service()
    if not svc.runtime_ready():
        return False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    ignited = False
    while loop.time() < deadline:
        try:
            if await run_blocking(svc.is_healthy):
                return True
            if ignite and not ignited:
                ignited = True
                svc.start_async()  # 幂等：已在运行/启动中则复用
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(3.0)
    return False


def _mark_prompt_stale(db: Database, asset_id: str, meta: dict, stale: bool,
                       *, source: str | None = None) -> dict:
    """更新 meta.prompt_stale / prompt_source 并落库（best-effort）。"""
    out = dict(meta)
    if stale:
        out["prompt_stale"] = True
    else:
        out.pop("prompt_stale", None)
        out["prompt_rewritten_at"] = _now()
    if source:
        out["prompt_source"] = source
    try:
        db.update("comic_assets", {"meta": out}, "id=?", (asset_id,))
    except Exception as exc:  # noqa: BLE001
        log.warning("prompt_stale 落库失败: %s", exc)
    return out


async def _rewrite_char_prompt_from_image(asset_id: str, *,
                                          wait_s: float,
                                          ignite: bool) -> bool:
    """VLM 按资产实际图像重写角色描述词（五段式拼装落库）。

    Returns:
        True=重写成功；False=vLLM 未就绪/推理失败/非角色或无图
        （保持 prompt_stale 标记，交由用户显式触发或手动编辑解除）。
    """
    db = get_db_safe()
    if db is None:
        return False
    row = db.query_one(
        f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
    if row is None:
        return False
    asset = _asset_row_to_dict(row)
    if asset.get("kind") != "character":
        return False
    fp = (asset.get("file_path") or "").strip()
    img_path = DATA_DIR / fp if fp else None
    if img_path is None or not img_path.is_file():
        return False
    if not await _wait_vllm_for_rewrite(wait_s, ignite=ignite):
        log.info("描述词自动重写跳过（vLLM 未就绪）: asset=%s", asset_id)
        return False
    desc = await run_blocking(
        _vlm_describe_char_image_sync, img_path, asset.get("name", ""))
    if not desc:
        return False
    pid = asset.get("project_id") or ""
    engine = get_dialog_engine()
    era_line = (await run_blocking(_infer_era_llm, engine, db, pid)
                if engine.is_ready else _INFER_ERA_LINE)
    prompt = _build_character_prompt_v2(
        asset.get("name", ""), desc, _project_style_line(db, pid), era_line)
    db.update("comic_assets", {"prompt": prompt[:2000]}, "id=?", (asset_id,))
    _mark_prompt_stale(db, asset_id, asset.get("meta") or {}, False,
                       source="vlm_image")
    log.info("描述词已按资产图重写: asset=%s len=%d", asset_id, len(prompt))
    return True


def _spawn_prompt_rewrite(asset_id: str) -> None:
    """上传/替换后后台自动重写（fire-and-forget，不点火冷启动）。"""
    try:
        asyncio.get_running_loop().create_task(
            _rewrite_char_prompt_from_image(
                asset_id, wait_s=_VLM_AUTO_WAIT_S, ignite=False))
    except RuntimeError:  # 无运行中事件循环（端点内不应发生）
        log.warning("描述词自动重写任务创建失败: asset=%s", asset_id)


@router.post("/comic/asset/{asset_id}/describe")
async def comic_asset_describe(asset_id: str) -> dict[str, Any]:
    """资产描述词 AI 扩写（竞品对齐）：对话引擎把 name+kind 扩写为绘图
    描述词并写回 prompt。

    对话引擎未就绪 → 自动加载漫剧·文字槽默认模型后继续（2026-09-02
    未加载必须全自动铁律，对齐 ai-describe）；真加载失败才报
    DIALOG_NOT_READY（不伪造描述词）；推理期间持有 "dialog" 功能锁
    （规格 §6.1 互斥）。
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
    # P0 数据修复（2026-08-27）：角色资产已有图 → VLM 按图重写（图像
    # 锚），不再凭名字文本臆想；无图才回退旧文本扩写路径
    if asset.get("kind") == "character":
        fp = (asset.get("file_path") or "").strip()
        img_path = DATA_DIR / fp if fp else None
        if img_path is not None and img_path.is_file():
            if not await _wait_vllm_for_rewrite(
                    _VLM_REWRITE_WAIT_S, ignite=True):
                raise ApiError(
                    "VLLM_NOT_READY",
                    "视觉模型未就绪，无法按图重写描述词，"
                    "请在对话模块触发模型加载后重试")
            if not await _rewrite_char_prompt_from_image(
                    asset_id, wait_s=1.0, ignite=False):
                raise ApiError("MODEL_INFERENCE_FAILED",
                               "VLM 按图重写描述词失败，请重试")
            row = db.query_one(
                f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?",
                (asset_id,))
            return ok({"asset": _asset_row_to_dict(row)})
    engine = get_dialog_engine()
    lock = await acquire_or_raise("dialog", task_id=asset_id)
    try:
        if not engine.is_ready:
            # 未加载必须全自动（2026-09-02 用户铁律，对齐 storyboard
            # ai-describe / video 描述词端点同款）：按需自动加载漫剧·
            # 文字槽默认模型（manga-dialog 槽 module_config 管控），
            # 真失败才报错且带出路指引——不再甩给用户手动去对话模块
            if not await run_blocking(engine.ensure_loaded,
                                      manga_dialog_model_id()):
                status = engine.get_status()
                raise ApiError(
                    "DIALOG_NOT_READY",
                    "对话模型未加载且自动加载失败，请先在对话模块加载模型",
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
                kind_label=kind_label,
                layers=_DESCRIBE_LAYERS.get(
                    asset.get("kind", ""), "外观、风格、配色与氛围"),
                name=asset.get("name", ""))
            max_tokens = 384
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
            # 时代背景 LLM 提取走 run_blocking（engine.chat 为阻塞
            # 调用，禁止在事件循环内直调——F-008）
            pid = asset.get("project_id") or ""
            era_line = await run_blocking(
                _infer_era_llm, engine, db, pid)
            description = _build_character_prompt_v2(
                asset.get("name", ""), description,
                _project_style_line(db, pid), era_line)
        elif asset.get("kind") in ("scene", "prop"):
            # 场景/道具：段式骨架（2026-08-24 用户裁定格式规范，
            # 与 infer-entities 产出格式一致，防覆盖回一段式）
            pid = asset.get("project_id") or ""
            era_line = await run_blocking(
                _infer_era_llm, engine, db, pid)
            style_line = _project_style_line(db, pid)
            desc = _strip_setting_prefix(
                description, "场景描述", "道具描述")
            description = (
                _build_scene_prompt_v2(
                    asset.get("name", ""), desc, style_line, era_line)
                if asset.get("kind") == "scene"
                else _build_prop_prompt_v2(
                    asset.get("name", ""), desc, style_line, era_line))
        db.update("comic_assets", {"prompt": description[:2000]},
                  "id=?", (asset_id,))
        _mark_prompt_stale(db, asset_id, asset.get("meta") or {}, False,
                           source="llm_text")
        row = db.query_one(
            f"SELECT {_ASSET_COLS} FROM comic_assets WHERE id=?", (asset_id,))
        return ok({"asset": _asset_row_to_dict(row)})
    finally:
        await lock.release("dialog")


@router.post("/comic/asset/export-pack")
def comic_asset_export_pack(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
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
