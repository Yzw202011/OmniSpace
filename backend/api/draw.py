"""绘画 API 路由（TASK-006 真实推理实现，兼容规格 §4.3 端点路径）。

端点清单：
- POST /draw/generate            文生图（异步，返回 {"task_id"}）
- POST /draw/img2img             图生图（init_image base64）
- POST /art/inpaint              局部重绘（image+mask base64，遮罩空/过大校验，
                                 masked_img2img 实现并如实 degraded 标记）
- POST /draw/upscale             图像超分（Real-ESRGAN 缺失时 LANCZOS 降级）
- GET  /draw/result/{task_id}    查询任务状态/结果（含图片 base64 或文件路径）
- POST /paint/task/{id}/cancel   取消任务（排队剔除 / 运行中协作中断）
- POST /paint/task/{id}/priority 调整排队任务优先级（0~9）
- GET  /paint/queue              任务队列快照（pending 按调度序 + running）
- GET  /draw/history             生成历史（favorite/start/end/width/height/keyword 筛选）
- POST /paint/history/{id}/favorite  收藏切换/设置
- DELETE /paint/history/{id}     删除单条历史（含图文件）
- POST /paint/history/batch-delete   批量删除历史（ids 数组）
- GET  /draw/models              绘画模型路由表 + 实际可用状态
- POST /draw/controlnet/preview  ControlNet 预览（无模型时 degraded 如实告知）
- GET  /draw/status              绘画引擎状态
- 别名: /paint/generate /paint/img2img /paint/upscale /paint/history
       /paint/result/{task_id}（文档 TASK-006）

真实推理链路：
  参数校验 → 创建任务 → 后台线程 PaintEngine(SDXL) 推理 →
  进度经注入的 ws_broadcaster 推送 {"type":"progress","module":"paint",...} →
  结果落盘 data/generated/images/ 并写 paint_history 表。

错误约定：
  - 50001 提示词为空；50002 尺寸超限；50003 ControlNet 条件图错误
  - MODEL_LOAD_FAILED 绘画模型加载失败/显存不足（友好降级，绝不硬 OOM；
    审计 BK-008：原 50005 未登记进 _LEGACY_CODE_MAP，改用语义码）
  - 40007 功能互斥；40008 缺少必填参数
生成期间持有 "paint" 功能锁（规格 §6.1）。
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import threading
import time
import uuid
from collections.abc import Callable

from fastapi import APIRouter, Body, Query

from ..data.database import get_db_safe, parse_json
from ..data.models import PAINT_ROUTING_TABLE
from ..middleware.error_handler import ApiError, ok
from ..middleware.feature_lock import acquire_or_raise, get_feature_lock
from ..services.inference.paint_engine import (
    DEFAULT_SAMPLER,
    SAMPLER_MAP,
    PaintCancelledError,
    get_paint_engine,
)
from ..services.offload import run_blocking

router = APIRouter()
log = logging.getLogger("omnispace.api.draw")

# ── 模块级注入点：WebSocket 广播器 ──────────────────────────────────
# 由上层（如 main/websocket 服务）注入 callable(dict)，把进度推给前端。
ws_broadcaster: Callable[[dict], None] | None = None


def set_ws_broadcaster(fn: Callable[[dict], None] | None) -> None:
    """注入/替换进度广播器。fn(payload: dict) -> None。"""
    global ws_broadcaster
    ws_broadcaster = fn


def _broadcast(payload: dict) -> None:
    """安全广播（广播器异常不影响推理主流程）。"""
    fn = ws_broadcaster
    if fn is None:
        return
    try:
        fn(payload)
    except Exception as exc:
        log.debug("进度广播失败（忽略）: %s", exc)


def _broadcast_progress(task_id: str, percent: int, step: int,
                        status: str = "running") -> None:
    _broadcast({
        "type": "progress",
        "module": "paint",
        "data": {"task_id": task_id, "percent": percent,
                 "step": step, "status": status},
    })


# ── 任务注册表（进程内，优先级队列调度执行）─────────────────────────

_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()
_TASK_KEEP = 200  # 最多保留的任务数（超出淘汰最旧）

# PAINT-041/042/044：等待队列（优先级调度）+ 协作式取消旗标
_pending: list[str] = []           # 等待调度的 task_id
_pending_lock = threading.Lock()
_dispatcher_running = False
_cancel_flags: set[str] = set()    # 取消旗标（进度回调检查并抛 _TaskCancelled）
_task_images: dict[str, tuple] = {}  # task_id -> (init_image, mask) 副作用表


# 协作式取消信号（引擎层 PaintCancelledError 的 API 侧别名；
# 进度回调抛出，引擎不吞、中断 diffusers 推理）
_TaskCancelled = PaintCancelledError


def _task_create(task_type: str, params: dict) -> dict:
    now = time.time()
    try:
        priority = int(params.get("priority", 5))
    except (TypeError, ValueError):
        priority = 5
    task = {
        "task_id": uuid.uuid4().hex,
        "type": task_type,
        "status": "pending",     # pending/running/done/error/cancelled
        "percent": 0,
        "step": 0,
        "error": "",
        "code": 0,
        "file_path": "",
        "image_b64": "",
        "seed": -1,
        "sampler": params.get("sampler") or DEFAULT_SAMPLER,
        "model": params.get("model") or "sdxl-base-1.0",
        "elapsed_ms": 0,
        "degraded": False,
        "backend": "",
        "priority": max(0, min(priority, 9)),
        "params": params,
        "created_at": now,
        "updated_at": now,
    }
    with _tasks_lock:
        if len(_tasks) >= _TASK_KEEP:
            oldest = sorted(_tasks.values(),
                            key=lambda t: t["created_at"])[:len(_tasks) - _TASK_KEEP + 1]
            for t in oldest:
                _tasks.pop(t["task_id"], None)
        _tasks[task["task_id"]] = task
    return task


def _task_update(task_id: str, **fields) -> None:
    with _tasks_lock:
        task = _tasks.get(task_id)
        if task is not None:
            task.update(fields)
            task["updated_at"] = time.time()


def _task_get(task_id: str) -> dict | None:
    with _tasks_lock:
        task = _tasks.get(task_id)
        return dict(task) if task is not None else None


# ── 参数解析辅助 ─────────────────────────────────────────────────────

def _clamp_size(value, default: int = 1024) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(512, min(v, 2048))


def _parse_common(body: dict) -> dict:
    """解析并归一化绘画公共参数。"""
    try:
        steps = int(body.get("steps", 30))
    except (TypeError, ValueError):
        steps = 30
    try:
        cfg = float(body.get("cfg", body.get("cfg_scale",
                                             body.get("guidance_scale", 7.5))))
    except (TypeError, ValueError):
        cfg = 7.5
    try:
        seed = int(body.get("seed", -1))
    except (TypeError, ValueError):
        seed = -1
    sampler = str(body.get("sampler") or DEFAULT_SAMPLER).lower()
    if sampler not in SAMPLER_MAP:
        sampler = DEFAULT_SAMPLER
    try:
        priority = int(body.get("priority", 5))
    except (TypeError, ValueError):
        priority = 5
    return {
        "prompt": str(body.get("prompt") or "").strip(),
        "negative": str(body.get("negative") or body.get("negative_prompt")
                      or "").strip(),
        "steps": max(1, min(steps, 50)),
        "cfg": max(1.0, min(cfg, 20.0)),
        "width": _clamp_size(body.get("width"), 1024),
        "height": _clamp_size(body.get("height"), 1024),
        "sampler": sampler,
        "seed": seed,
        "model": body.get("model") or None,
        "optimize": bool(body.get("optimize", False)),
        "priority": max(0, min(priority, 9)),
    }


def _decode_b64_image(data: str):
    """base64 -> PIL.Image；失败返回 None。"""
    try:
        from PIL import Image
        if "," in data and data.split(",", 1)[0].startswith("data:"):
            data = data.split(",", 1)[1]
        return Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB")
    except Exception:
        return None


# ── 后台执行任务 ─────────────────────────────────────────────────────

def _run_generate_task(task_id: str, params: dict,
                       init_image=None, mask=None) -> None:
    """后台线程：加载引擎 → 推理 → 落盘 → 更新任务/广播。

    mask 非空时走局部重绘（inpaint）链路；协作式取消：进度回调发现
    取消旗标即抛 _TaskCancelled 中断推理。
    """
    engine = get_paint_engine()
    _task_update(task_id, status="running")

    def progress(percent: int, step: int) -> None:
        if task_id in _cancel_flags:
            raise _TaskCancelled()
        _task_update(task_id, percent=percent, step=step)
        _broadcast_progress(task_id, percent, step)

    try:
        if task_id in _cancel_flags:
            raise _TaskCancelled()

        if not engine.is_ready and not engine.ensure_loaded(params.get("model")):
            status = engine.get_status()
            _task_update(task_id, status="error", code="MODEL_LOAD_FAILED",
                         error=status["last_error"] or "绘画模型未就绪")
            _broadcast_progress(task_id, 0, 0, status="error")
            return

        prompt = params["prompt"]
        if params.get("optimize"):
            prompt, used = engine.optimize_prompt(prompt)
            if used:
                _task_update(task_id, optimized_prompt=prompt)

        if mask is not None and init_image is not None:
            result = engine.inpaint(params, init_image, mask,
                                    progress_cb=progress)
        elif init_image is not None:
            result = engine.img2img(params, init_image, progress_cb=progress)
        else:
            result = engine.generate(params, progress_cb=progress)

        image = result["images"][0]
        rel_path = engine.save_result(
            image, task_id, prompt, params.get("negative", ""),
            {k: v for k, v in params.items() if k != "optimize"},
            result["seed"])
        image_b64 = engine.image_to_base64(image)

        _task_update(task_id, status="done", percent=100,
                     file_path=rel_path, image_b64=image_b64,
                     seed=result["seed"], model=result["model"],
                     sampler=result.get("sampler", params["sampler"]),
                     elapsed_ms=int(result["elapsed_ms"]),
                     degraded=bool(result.get("degraded", False)),
                     backend=str(result.get("backend", "")))
        _broadcast_progress(task_id, 100, params["steps"], status="done")
    except _TaskCancelled:
        _cancel_flags.discard(task_id)
        log.info("绘画任务已取消: %s", task_id)
        _task_update(task_id, status="cancelled", error="用户取消")
        _broadcast_progress(task_id, 0, 0, status="cancelled")
    except Exception as exc:  # noqa: BLE001 - 任务失败收敛为状态
        log.exception("绘画任务失败: %s", task_id)
        _task_update(task_id, status="error", code=50001, error=str(exc))
        _broadcast_progress(task_id, 0, 0, status="error")


def _submit_precheck() -> None:
    """提交期快速预检（保持 40007/20004 提交期语义，不入队即拒）。"""
    try:  # 热保护（与 acquire_or_raise 同源逻辑，失败不阻断）
        from ..services.thermal_guard import get_thermal_guard
        guard = get_thermal_guard()
        if guard.is_paused():
            status = guard.get_status()
            raise ApiError(
                20004,
                f"GPU 温度过高（{status['last_temp_celsius']:.0f}°C），"
                f"已强制暂停生成任务，请等待散热后重试")
    except ApiError:
        raise
    except Exception:  # noqa: BLE001
        pass
    mgr = get_feature_lock()
    reason = mgr.get_block_reason("paint")
    if reason:
        raise ApiError(40007, reason,
                       detail={"active_feature": mgr.active_feature,
                               "feature": "paint"})


def _enqueue_task(task_id: str) -> None:
    """入队并按需唤醒调度线程（PAINT-044 优先级队列）。"""
    global _dispatcher_running
    with _pending_lock:
        _pending.append(task_id)
        if not _dispatcher_running:
            _dispatcher_running = True
            threading.Thread(target=_dispatch_loop, daemon=True).start()


def _dispatch_loop() -> None:
    """调度线程：按优先级串行执行等待任务（单 GPU 显存保护）。

    每轮从等待队列取（priority 降序，同优先级按提交先后）队首执行；
    队空即退出。功能锁逐任务在独立事件循环内获取/释放。
    """
    global _dispatcher_running
    try:
        while True:
            with _pending_lock:
                if not _pending:
                    return
                pick = min(
                    _pending,
                    key=lambda t: (-(_task_get(t) or {}).get("priority", 5),
                                   (_task_get(t) or {}).get("created_at", 0)))
                _pending.remove(pick)

            task = _task_get(pick)
            if task is None:
                continue
            if pick in _cancel_flags:
                _cancel_flags.discard(pick)
                _task_images.pop(pick, None)
                _task_update(pick, status="cancelled", error="排队中被取消")
                _broadcast_progress(pick, 0, 0, status="cancelled")
                continue

            async def _run_locked(tid: str = pick, t: dict = task) -> None:
                lock = await acquire_or_raise("paint", task_id=tid)
                try:
                    imgs = _task_images.pop(tid, None)
                    init_image = imgs[0] if imgs else None
                    mask = imgs[1] if imgs and len(imgs) > 1 else None
                    await run_blocking(
                        _run_generate_task, tid, t["params"],
                        init_image, mask)
                finally:
                    await lock.release("paint")

            try:
                asyncio.run(_run_locked())
            except Exception as exc:  # noqa: BLE001 - 锁竞争/热保护等
                log.warning("任务调度失败 %s: %s", pick, exc)
                _task_images.pop(pick, None)
                _task_update(pick, status="error", code=50001,
                             error=str(exc))
                _broadcast_progress(pick, 0, 0, status="error")
    finally:
        with _pending_lock:
            _dispatcher_running = False
            restart = bool(_pending)
            if restart:  # 退出竞态：finally 期间又有任务入队
                _dispatcher_running = True
        if restart:
            threading.Thread(target=_dispatch_loop, daemon=True).start()


def _submit_task(task: dict, init_image=None, mask=None) -> None:
    """提交任务：预检 → 副作用登记 → 入队。"""
    _submit_precheck()
    if init_image is not None or mask is not None:
        _task_images[task["task_id"]] = (init_image, mask)
    _enqueue_task(task["task_id"])


# ── 文生图 ──────────────────────────────────────────────────────────

@router.post("/draw/generate")
@router.post("/paint/generate")
async def draw_generate(body: dict = Body(default_factory=dict)):
    """文生图（异步任务）。

    请求: {"prompt", "negative"?, "steps"=30, "cfg"=7.5, "width"=1024,
           "height"=1024, "sampler"="euler_a", "seed"=-1, "model"?,
           "optimize"?}
    返回: {"task_id"}；进度经 ws_broadcaster 推送，结果走 /result/{task_id}。
    """
    params = _parse_common(body)
    if not params["prompt"]:
        raise ApiError(50001, "生成失败，请检查提示词是否为空")

    task = _task_create("txt2img", params)
    _submit_task(task)
    return ok({"task_id": task["task_id"], "priority": task["priority"]})


# ── 图生图 ──────────────────────────────────────────────────────────

@router.post("/draw/img2img")
@router.post("/paint/img2img")
async def draw_img2img(body: dict = Body(default_factory=dict)):
    """图生图（异步任务）。额外参数: init_image(base64), strength(0.05~1.0)。"""
    params = _parse_common(body)
    if not params["prompt"]:
        raise ApiError(50001, "生成失败，请检查提示词是否为空")

    raw = body.get("init_image") or body.get("image") or ""
    if not raw:
        raise ApiError(40008, "缺少 init_image 图像数据")
    init_image = _decode_b64_image(str(raw))
    if init_image is None:
        raise ApiError(40008, "init_image 图像数据无法解析")

    try:
        strength = float(body.get("strength",
                                  body.get("denoising_strength", 0.75)))
    except (TypeError, ValueError):
        strength = 0.75
    params["strength"] = max(0.05, min(strength, 1.0))

    task = _task_create("img2img", params)
    _submit_task(task, init_image=init_image)
    return ok({"task_id": task["task_id"], "priority": task["priority"]})


# ── 局部重绘（PAINT-027/028/030/031、COMIC-039）──────────────────────

# 遮罩覆盖面积上限（占比），超出判定为"过大"拒绝（PAINT-031）
_MASK_MAX_RATIO = 0.95


@router.post("/art/inpaint")
@router.post("/paint/inpaint")
async def art_inpaint(body: dict = Body(default_factory=dict)):
    """局部重绘（异步任务）。

    请求: {"image": base64 原图, "mask": base64 遮罩（白色=待重绘区）,
           "prompt"?, "negative"?, "strength"=1.0, "mask_margin"=48,
           "steps"/"cfg"/"seed"/"sampler"/"priority" 同文生图}
    返回: {"task_id"}；结果走 /paint/result/{task_id}。

    校验（PAINT-030/031）：遮罩缺失/为空 → 40008；遮罩覆盖面积
    占比 > 95% → 40008（等效全图重绘，应改用 img2img）。
    实现：随包无 SDXL-inpaint 专用 9 通道权重，采用遮罩区域 img2img
    重绘 + 软边回贴，结果如实标注 degraded=true / backend 字段。
    """
    raw_img = body.get("image") or body.get("init_image") or ""
    if not raw_img:
        raise ApiError(40008, "缺少 image 原图数据")
    image = _decode_b64_image(str(raw_img))
    if image is None:
        raise ApiError(40008, "image 原图数据无法解析")

    raw_mask = body.get("mask") or ""
    if not raw_mask:
        raise ApiError(40008, "缺少 mask 遮罩数据（白色=待重绘区域）")
    mask = _decode_b64_image(str(raw_mask))
    if mask is None:
        raise ApiError(40008, "mask 遮罩数据无法解析")
    if mask.size != image.size:
        mask = mask.resize(image.size)

    # 遮罩空/过大校验（亮度 ≥128 视为重绘区）
    binmask = mask.convert("L").point(lambda v: 255 if v >= 128 else 0)
    if binmask.getbbox() is None:
        raise ApiError(40008, "遮罩为空，请涂抹需要重绘的区域")
    hist = binmask.histogram()
    white = sum(hist[128:])
    ratio = white / float(image.size[0] * image.size[1])
    if ratio > _MASK_MAX_RATIO:
        raise ApiError(
            40008,
            f"遮罩覆盖面积过大（{ratio * 100:.0f}% > "
            f"{int(_MASK_MAX_RATIO * 100)}%），等效全图重绘，请改用图生图")

    params = _parse_common(body)
    params["strength"] = max(0.05, min(
        float(body.get("strength", 1.0) or 1.0), 1.0))
    try:
        params["mask_margin"] = max(0, min(
            int(body.get("mask_margin", 48)), 256))
    except (TypeError, ValueError):
        params["mask_margin"] = 48

    task = _task_create("inpaint", params)
    _submit_task(task, init_image=image, mask=mask)
    return ok({"task_id": task["task_id"], "priority": task["priority"]})


# ── 超分 ────────────────────────────────────────────────────────────

@router.post("/draw/upscale")
@router.post("/paint/upscale")
async def draw_upscale(body: dict = Body(default_factory=dict)):
    """图像超分。{"image": base64, "scale": 2|4}

    Real-ESRGAN 可用时真超分；否则 PIL LANCZOS 并标注 degraded=true。
    """
    raw = body.get("image") or ""
    if not raw:
        raise ApiError(40008, "缺少 image 图像数据")
    image = _decode_b64_image(str(raw))
    if image is None:
        raise ApiError(40008, "image 图像数据无法解析")
    try:
        scale = int(body.get("scale", 2))
    except (TypeError, ValueError):
        scale = 2
    scale = 4 if scale >= 4 else 2

    engine = get_paint_engine()
    lock = await acquire_or_raise("paint")
    try:
        result = await run_blocking(engine.upscale, image, scale)
    finally:
        await lock.release("paint")

    out_b64 = get_paint_engine().image_to_base64(result["image"])
    return ok({
        "image": out_b64,
        "scale": result["scale"],
        "backend": result["backend"],
        "degraded": result["degraded"],
        "width": result["image"].size[0],
        "height": result["image"].size[1],
    })


# ── 结果 / 历史 ─────────────────────────────────────────────────────

@router.get("/draw/result/{task_id}")
@router.get("/paint/result/{task_id}")
def draw_result(task_id: str):
    """查询任务状态与结果。done 时返回图片 base64 + 文件路径。"""
    task = _task_get(task_id)
    if task is None:
        # 内存任务表没有时查 paint_history（例如服务重启后）
        db = get_db_safe()
        if db is not None:
            try:
                from ..services.inference.paint_engine import PaintEngine
                PaintEngine.ensure_history_table()
                row = db.query_one(
                    "SELECT task_id, prompt, negative, params_json, file_path,"
                    " seed, created_at FROM paint_history WHERE task_id=?",
                    (task_id,))
                if row is not None:
                    return ok({
                        "task_id": task_id, "status": "done",
                        "file_path": row.get("file_path", ""),
                        "seed": row.get("seed", -1),
                        "prompt": row.get("prompt", ""),
                        "params": parse_json(row.get("params_json"), {}),
                        "created_at": row.get("created_at", 0),
                    })
            except Exception as exc:  # noqa: BLE001
                log.warning("paint_history 查询失败: %s", exc)
        raise ApiError(40005, "任务不存在", detail={"task_id": task_id})

    resp = {
        "task_id": task_id,
        "type": task["type"],
        "status": task["status"],
        "percent": task["percent"],
        "step": task["step"],
        "seed": task["seed"],
        "model": task["model"],
        "sampler": task["sampler"],
        "priority": task.get("priority", 5),
        "elapsed_ms": task["elapsed_ms"],
        "created_at": task["created_at"],
    }
    if task["status"] in ("error", "cancelled"):
        resp["error"] = task["error"]
        resp["code"] = task["code"]
    if task["status"] == "done":
        resp["file_path"] = task["file_path"]
        resp["image"] = task["image_b64"]
        resp["degraded"] = bool(task.get("degraded", False))
        resp["backend"] = task.get("backend", "")
        if task.get("optimized_prompt"):
            resp["optimized_prompt"] = task["optimized_prompt"]
    return ok(resp)


# ── 任务控制（PAINT-041/042/044）─────────────────────────────────────

@router.post("/paint/task/{task_id}/cancel")
@router.post("/draw/task/{task_id}/cancel")
def paint_task_cancel(task_id: str):
    """取消绘画任务（协作式）。

    - 排队中：直接从等待队列剔除，状态 → cancelled
    - 运行中：置取消旗标，下一推理步进度回调抛中断（状态 → cancelled）
    - 已结束（done/error/cancelled）：40008
    """
    task = _task_get(task_id)
    if task is None:
        raise ApiError(40005, "任务不存在", detail={"task_id": task_id})
    if task["status"] in ("done", "error", "cancelled"):
        raise ApiError(40008, f"任务已结束（{task['status']}），无法取消")

    was_pending = False
    with _pending_lock:
        if task_id in _pending:
            _pending.remove(task_id)
            was_pending = True
    _task_images.pop(task_id, None)

    if was_pending:
        _task_update(task_id, status="cancelled", error="排队中被取消")
        _broadcast_progress(task_id, 0, 0, status="cancelled")
    else:
        _cancel_flags.add(task_id)  # 运行中：进度回调协作中断

    return ok({"task_id": task_id, "status": "cancelled",
               "was_pending": was_pending})


@router.post("/paint/task/{task_id}/priority")
def paint_task_priority(task_id: str, body: dict = Body(default_factory=dict)):
    """调整排队任务优先级（PAINT-044）：{"priority": 0~9}。

    仅对仍在等待队列中的任务生效（运行中/已结束任务返回
    effective=false 如实告知）。
    """
    task = _task_get(task_id)
    if task is None:
        raise ApiError(40005, "任务不存在", detail={"task_id": task_id})
    try:
        priority = int(body.get("priority", 5))
    except (TypeError, ValueError):
        raise ApiError(40008, "priority 必须是 0~9 的整数") from None
    priority = max(0, min(priority, 9))

    with _pending_lock:
        effective = task_id in _pending
    _task_update(task_id, priority=priority)
    return ok({"task_id": task_id, "priority": priority,
               "effective": effective,
               "status": (_task_get(task_id) or {}).get("status", "")})


@router.get("/paint/queue")
@router.get("/draw/queue")
def paint_queue():
    """绘画任务队列快照（PAINT-042）：等待队列（按调度顺序）+ 运行中。"""
    with _pending_lock:
        pending_ids = list(_pending)
    with _tasks_lock:
        running = [dict(t) for t in _tasks.values()
                   if t["status"] == "running"]

    def _brief(t: dict) -> dict:
        return {
            "task_id": t["task_id"], "type": t["type"],
            "status": t["status"], "priority": t.get("priority", 5),
            "percent": t.get("percent", 0),
            "prompt": (t.get("params") or {}).get("prompt", "")[:60],
            "created_at": t.get("created_at", 0),
        }

    pending = [_brief(t) for t in
               sorted((_task_get(tid) for tid in pending_ids),
                      key=lambda t: (-(t or {}).get("priority", 5),
                                     (t or {}).get("created_at", 0)))
               if t]
    return ok({
        "pending": pending,
        "running": [_brief(t) for t in running],
        "pending_count": len(pending),
        "running_count": len(running),
    })


def _ensure_history_columns() -> None:
    """paint_history 增量列迁移（PAINT-048：favorite 收藏列）。"""
    db = get_db_safe()
    if db is None:
        return
    try:
        cols = {r["name"] for r in db.query("PRAGMA table_info(paint_history)")}
        if cols and "favorite" not in cols:
            db.executescript(
                "ALTER TABLE paint_history "
                "ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0;")
            log.info("paint_history 迁移：新增 favorite 列")
    except Exception as exc:  # noqa: BLE001
        log.warning("paint_history 列迁移失败: %s", exc)


@router.get("/draw/history")
@router.get("/paint/history")
def draw_history(page: int = Query(1, ge=1),
                 page_size: int = Query(20, ge=1, le=100),
                 favorite: int | None = Query(None),
                 start: float | None = Query(None),
                 end: float | None = Query(None),
                 width: int | None = Query(None),
                 height: int | None = Query(None),
                 keyword: str = Query("")):
    """生成历史（paint_history 表，按时间倒序分页）。

    PAINT-046 画廊筛选：
    - favorite=0/1   只看收藏 / 只看未收藏
    - start/end      创建时间范围（epoch 秒）
    - width/height   按生成尺寸筛选（params_json 内 JSON1 提取）
    - keyword        提示词模糊匹配
    """
    from ..services.inference.paint_engine import PaintEngine
    PaintEngine.ensure_history_table()
    _ensure_history_columns()

    db = get_db_safe()
    if db is None:
        return ok({"items": [], "total": 0, "page": page,
                   "page_size": page_size})

    where: list[str] = []
    args: list = []
    if favorite is not None:
        where.append("favorite = ?")
        args.append(1 if favorite else 0)
    if start is not None:
        where.append("created_at >= ?")
        args.append(float(start))
    if end is not None:
        where.append("created_at <= ?")
        args.append(float(end))
    if width is not None:
        where.append("json_extract(params_json, '$.width') = ?")
        args.append(int(width))
    if height is not None:
        where.append("json_extract(params_json, '$.height') = ?")
        args.append(int(height))
    if keyword.strip():
        where.append("prompt LIKE ?")
        args.append(f"%{keyword.strip()}%")
    cond = (" WHERE " + " AND ".join(where)) if where else ""

    try:
        total_row = db.query_one(
            f"SELECT COUNT(*) AS c FROM paint_history{cond}", tuple(args))
        total = int(total_row["c"]) if total_row else 0
        rows = db.query(
            "SELECT task_id, prompt, negative, params_json, file_path,"
            " seed, created_at, favorite FROM paint_history"
            f"{cond} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            tuple(args) + (page_size, (page - 1) * page_size))
        items = [{
            "task_id": r["task_id"],
            "prompt": r.get("prompt", ""),
            "negative": r.get("negative", ""),
            "params": parse_json(r.get("params_json"), {}),
            "file_path": r.get("file_path", ""),
            "seed": r.get("seed", -1),
            "favorite": bool(r.get("favorite", 0)),
            "created_at": r.get("created_at", 0),
        } for r in rows]
        return ok({"items": items, "total": total,
                   "page": page, "page_size": page_size})
    except Exception as exc:  # noqa: BLE001
        log.warning("paint_history 查询失败: %s", exc)
        return ok({"items": [], "total": 0, "page": page,
                   "page_size": page_size})


# ── 画廊管理：收藏 / 删除（PAINT-048/050/051）─────────────────────────

def _history_row(task_id: str) -> dict | None:
    db = get_db_safe()
    if db is None:
        return None
    try:
        return db.query_one(
            "SELECT task_id, file_path, favorite FROM paint_history"
            " WHERE task_id=?", (task_id,))
    except Exception:  # noqa: BLE001
        return None


def _delete_history_file(file_path: str) -> bool:
    """按历史记录相对路径删除生成图文件（防路径穿越，best-effort）。"""
    if not file_path:
        return False
    try:
        from pathlib import Path as _P

        from ..data.file_store import GENERATED_DIR
        name = _P(file_path).name
        target = GENERATED_DIR / "images" / name
        if target.is_file():
            target.unlink()
            return True
    except Exception as exc:  # noqa: BLE001
        log.debug("历史图文件删除失败（忽略）: %s", exc)
    return False


@router.post("/paint/history/{task_id}/favorite")
@router.post("/draw/history/{task_id}/favorite")
def paint_history_favorite(task_id: str,
                           body: dict = Body(default_factory=dict)):
    """切换/设置收藏（PAINT-048）。body.favorite 缺省时取反。"""
    from ..services.inference.paint_engine import PaintEngine
    PaintEngine.ensure_history_table()
    _ensure_history_columns()

    row = _history_row(task_id)
    if row is None:
        raise ApiError(40005, "历史记录不存在",
                       detail={"task_id": task_id})
    explicit = body.get("favorite")
    if explicit is None:
        new_val = 0 if row.get("favorite") else 1
    else:
        new_val = 1 if bool(explicit) else 0
    db = get_db_safe()
    db.update("paint_history", {"favorite": new_val}, "task_id=?",
              (task_id,))
    return ok({"task_id": task_id, "favorite": bool(new_val)})


@router.delete("/paint/history/{task_id}")
@router.delete("/draw/history/{task_id}")
def paint_history_delete(task_id: str):
    """删除单条历史（PAINT-050）：记录 + 图文件（best-effort）。"""
    from ..services.inference.paint_engine import PaintEngine
    PaintEngine.ensure_history_table()

    row = _history_row(task_id)
    if row is None:
        raise ApiError(40005, "历史记录不存在",
                       detail={"task_id": task_id})
    db = get_db_safe()
    db.delete("paint_history", "task_id=?", (task_id,))
    file_deleted = _delete_history_file(row.get("file_path", ""))
    return ok({"task_id": task_id, "deleted": True,
               "file_deleted": file_deleted})


@router.post("/paint/history/batch-delete")
@router.post("/draw/history/batch-delete")
def paint_history_batch_delete(body: dict = Body(default_factory=dict)):
    """批量删除历史（PAINT-051）：{"ids": ["task_id", ...]}（上限 200）。"""
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        raise ApiError(40008, "缺少必填参数: ids（task_id 数组）")
    ids = [str(i) for i in ids[:200]]

    from ..services.inference.paint_engine import PaintEngine
    PaintEngine.ensure_history_table()

    db = get_db_safe()
    if db is None:
        raise ApiError(50001, "数据库不可用")
    deleted = 0
    files_deleted = 0
    missing: list[str] = []
    for tid in ids:
        row = _history_row(tid)
        if row is None:
            missing.append(tid)
            continue
        db.delete("paint_history", "task_id=?", (tid,))
        deleted += 1
        if _delete_history_file(row.get("file_path", "")):
            files_deleted += 1
    return ok({"deleted": deleted, "files_deleted": files_deleted,
               "missing": missing})


# ── 生成图回读 ────────────────────────────────────────────────────

@router.get("/draw/image/{filename}")
def draw_image(filename: str):
    """按文件名回读生成图片（前端历史画廊/预览用）。

    历史记录只存相对路径 generated/images/<task_id>.png，
    本端点按文件名安全回读（Path.name 防路径穿越）。
    """
    from pathlib import Path

    from fastapi.responses import FileResponse

    from ..data.file_store import GENERATED_DIR

    safe = Path(filename).name  # 防路径穿越：仅取文件名部分
    path = GENERATED_DIR / "images" / safe
    if not path.is_file():
        raise ApiError(40005, "图片不存在或已被清理",
                       detail={"filename": safe})
    return FileResponse(str(path), media_type="image/png")


# ── 模型与状态 ──────────────────────────────────────────────────────

@router.get("/draw/models")
def draw_models():
    """绘画模型列表：路由表 + 本地实际可用状态。"""
    engine = get_paint_engine()
    available = set(engine.available_models())
    items = []
    for m in PAINT_ROUTING_TABLE:
        model_id = m["model"]
        # 本地实际就绪判定：sdxl 系列映射到 sdxl-base-1.0 目录
        if model_id.startswith("sdxl"):
            ready = "sdxl-base-1.0" in available
            status = "ready" if ready else "not_installed"
            local_id = "sdxl-base-1.0" if ready else ""
        else:
            status = "not_installed"
            local_id = ""
        items.append({
            "id": model_id,
            "name": model_id,
            "category": "vision",
            "min_vram_gb": m["min_vram_gb"],
            "status": status,
            "local_model": local_id,
        })
    return ok({
        "items": items,
        "total": len(items),
        "engine": engine.get_status(),
    })


@router.get("/draw/status")
def draw_status():
    """绘画引擎状态。"""
    return ok(get_paint_engine().get_status())


# ── ControlNet 预览 ─────────────────────────────────────────────────

@router.post("/draw/controlnet/preview")
async def controlnet_preview(body: dict = Body(default_factory=dict)):
    """预览 ControlNet 条件图（审计 BK-012 诚实降级版）。

    ControlNet 模型未随包 / 预处理管线未接入时，不再抛恒定的 501 空壳
    错误，而是返回 degraded:true + degrade_reason 中文字段的成功信封，
    如实告知前端该能力处于降级不可用状态。
    """
    ctype = str(body.get("type") or "").strip()
    image = body.get("image")
    if not ctype:
        raise ApiError(40008, "缺少 ControlNet type 参数")
    if not image:
        raise ApiError(50003, "ControlNet条件图格式错误")

    from ..config import MODELS_DIR
    controlnet_dir = MODELS_DIR / "controlnet"
    has_model = controlnet_dir.is_dir() and any(
        p.suffix in (".safetensors", ".bin", ".pth")
        for p in controlnet_dir.rglob("*") if p.is_file()
    )
    if not has_model:
        return ok({
            "type": ctype, "url": "", "method": "",
            "degraded": True,
            "degrade_reason": (
                f"ControlNet 模型未随包安装，无法生成 {ctype} 预览；"
                "请将 controlnet-sdxl 系列模型放入 models/controlnet/ 后重试"),
        })
    # 有模型时的真实预处理尚未接入（ControlNet 管线为后续版本能力）
    return ok({
        "type": ctype, "url": "", "method": "",
        "degraded": True,
        "degrade_reason": "ControlNet 预处理管线尚未接入（后续版本能力），预览暂不可用",
    })
