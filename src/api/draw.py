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

import base64
import io
import logging
import pathlib
import threading
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import FileResponse

from ..data.database import get_db_safe, parse_json
from ..data.models import PAINT_ROUTING_TABLE
from ..middleware.error_handler import ApiError, ok
from ..middleware.feature_lock import acquire_or_raise
from ..services.image_queue import get_image_queue
from ..services.inference.paint_engine import (
    DEFAULT_SAMPLER,
    SAMPLER_MAP,
    PaintCancelledError,
    get_paint_engine,
)
from ..services.inference.prompt_translator import (
    contains_cjk,
    shrink_working_set,
    translate_prompt_zh2en,
)
from ..services.offload import run_blocking
from .manga.common import _paint_gen_engine_comfy  # W3-C 引擎档位门控

# ── W3-C 本地化落盘件（2026-09-13，自 legacy PaintEngine 抽出）────
# paint_history 建表 DDL（自建表，CREATE IF NOT EXISTS 幂等）——
# paint_engine 退役后由本模块自治
_PAINT_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS paint_history (
    task_id     TEXT PRIMARY KEY,
    prompt      TEXT NOT NULL DEFAULT '',
    negative    TEXT DEFAULT '',
    params_json TEXT DEFAULT '{}',
    file_path   TEXT DEFAULT '',
    seed        INTEGER DEFAULT -1,
    created_at  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_paint_history_time ON paint_history(created_at DESC);
"""


def _ensure_history_table() -> bool:
    """确保 paint_history 表存在（幂等；W3-C 本地化版）。"""
    try:
        db = get_db_safe()
        if db is None:
            return False
        db.executescript(_PAINT_HISTORY_DDL)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("paint_history 建表失败: %s", exc, exc_info=True)
        return False


def _pil_to_b64(image: Image.Image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _save_result_local(image: Image.Image, task_id: str, prompt: str,
                       negative: str, params: dict, seed: int) -> str:
    """生成图落盘（file_store）+ paint_history 写入（W3-C 本地化版）。"""
    import json as _json

    rel_path = ""
    try:
        from ..data.file_store import get_file_store
        store = get_file_store()
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        rel_path = store.save_file("image", buf.getvalue(),
                                   filename=f"{task_id}.png")
    except Exception as exc:  # noqa: BLE001
        log.warning("生成图落盘失败: %s", exc, exc_info=True)
    try:
        db = get_db_safe()
        if db is not None:
            db.executescript(_PAINT_HISTORY_DDL)
            db.insert("paint_history", {
                "task_id": task_id,
                "prompt": prompt,
                "negative": negative,
                "params_json": _json.dumps(params, ensure_ascii=False,
                                           default=str),
                "file_path": rel_path,
                "seed": seed,
                "created_at": time.time(),
            })
    except Exception as exc:  # noqa: BLE001
        log.warning("paint_history 写入失败: %s", exc, exc_info=True)
    return rel_path


def _upscale_lanczos(image: Image.Image, scale: int) -> dict:
    """PIL LANCZOS 放大（comfy 档的 upscale 实现；诚实标注 degraded）。"""
    w, h = image.size
    out = image.resize((w * scale, h * scale), Image.LANCZOS)  # type: ignore[attr-defined]
    return {"image": out, "scale": scale, "backend": "lanczos",
            "degraded": True}

if TYPE_CHECKING:
    from PIL import Image

    from ..services.cloud_provider_service import CloudEndpoint

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

# PAINT-041/042/044：协作式取消旗标 + 副作用表（等待队列/调度已迁移
# services/image_queue.py——2026-09-02 统一图像队列，绘画页与漫剧生图
# 全族单 worker 顺序消费、排空才释放 paint 锁）
_cancel_flags: set[str] = set()    # 取消旗标（进度回调检查并抛 _TaskCancelled）
_task_images: dict[str, tuple] = {}  # task_id -> (init_image, mask) 副作用表


# 协作式取消信号（引擎层 PaintCancelledError 的 API 侧别名；
# 进度回调抛出，引擎不吞、中断 diffusers 推理）
_TaskCancelled = PaintCancelledError


def _inject_style_pack(prompt: str, params: dict) -> tuple[str, str]:
    """风格包提示词注入（绘画链与漫剧关键帧链对齐，2026-09-06）。

    detect 对用户原文（中文风格词可嗅探），style_block/quality_block
    为英文词块（对编码器权重更稳）以 ", " 拼接；negative 用户自带
    优先、风格包 negative_hint 兜底（写入 params["negative"]）。

    Returns:
        (新 prompt, 注入说明——空串=无块可注入)。

    Raises:
        Exception: 上游嗅探异常（调用方兜底跳过，不阻断生成）。
    """
    from ..services.inference.gen_router import explain_style

    pack, verdict = explain_style(params.get("prompt") or prompt)
    if not (pack.style_block or pack.quality_block):
        return prompt, ""
    parts = [prompt.rstrip(" ,.。"), pack.style_block.strip(" ,.。"),
             pack.quality_block.strip(" ,.。")]
    new_prompt = ", ".join(p for p in parts if p)
    note = f"风格包 {pack.sid}（{pack.label}，{verdict}）"
    if not params.get("negative") and pack.negative_hint:
        params["negative"] = pack.negative_hint
        note += "，负面词兜底自风格包"
    return new_prompt, note


def _default_paint_model() -> str:
    """AI 绘画默认底座（2026-08-29 模型裁剪）：paint 槽 module_config
    默认模型；未配置回落 flux2-klein-9b（sdxl 缺省随裁剪移除）。
    """
    try:
        from .models import module_default_model
        return module_default_model("paint") or "flux2-klein-9b"
    except Exception:  # noqa: BLE001 - 配置读取失败不阻断任务创建
        return "flux2-klein-9b"


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
        "model": params.get("model") or _default_paint_model(),
        "elapsed_ms": 0,
        "degraded": False,
        "backend": "",
        "priority": max(0, min(priority, 9)),
        "params": params,
        "created_at": now,
        "updated_at": now,
    }
    # 执行流程追踪（2026-08-23）：触发时刻=任务创建（含排队时长），
    # Flow 对象随 task dict 跨线程显式传递到执行线程
    try:
        from ..services.flow_trace import start_flow
        prompt_brief = (params.get("prompt") or "")[:24]
        task["_flow"] = start_flow(
            "paint", task_type,
            f"AI 绘画（{'文生图' if task_type == 'txt2img' else task_type}）："
            f"{prompt_brief}{'…' if len(params.get('prompt') or '') > 24 else ''}",
            trigger="用户提交生成任务",
            input_summary=f"{params.get('width', 1024)}x{params.get('height', 1024)}"
                          f" {params.get('steps', 30)}步"
                          f" {task['model']} 优先级{task['priority']}",
            detail=f"task_id={task['task_id']}")
    except Exception:  # noqa: BLE001 - 追踪失败不影响业务
        log.debug("_task_create: 降级忽略", exc_info=True)
    with _tasks_lock:
        if len(_tasks) >= _TASK_KEEP:
            oldest = sorted(_tasks.values(),
                            key=lambda t: t["created_at"])[:len(_tasks) - _TASK_KEEP + 1]
            for t in oldest:
                _tasks.pop(t["task_id"], None)
        _tasks[task["task_id"]] = task
    return task


def _task_update(task_id: str, **fields: Any) -> None:
    with _tasks_lock:
        task = _tasks.get(task_id)
        if task is not None:
            task.update(fields)
            task["updated_at"] = time.time()


def _end_flow(task_id: str, status: str, *,
              error_code: str = "", error_detail: str = "") -> None:
    """结束任务携带的追踪流程（调度层取消/失败路径的收尾，幂等）。"""
    with _tasks_lock:
        task = _tasks.get(task_id)
        flow = task.pop("_flow", None) if task else None
    if flow is not None:
        try:
            flow.end(status, error_code=error_code,
                     error_detail=error_detail)
        except Exception:  # noqa: BLE001 - 追踪失败不影响业务
            log.debug("_end_flow: 降级忽略", exc_info=True)


def _task_get(task_id: str) -> dict | None:
    with _tasks_lock:
        task = _tasks.get(task_id)
        return dict(task) if task is not None else None


# ── 参数解析辅助 ─────────────────────────────────────────────────────

def _clamp_size(value: Any, default: int = 1024) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(512, min(v, 2688))


def _parse_common(body: dict) -> dict:
    """解析并归一化绘画公共参数。"""
    # 缺省 8 步（2026-09-16 拍板 balanced 档，与 DrawRequest/config 对齐；
    # 旧 30 为历史口径）
    try:
        steps = int(body.get("steps", 8))
    except (TypeError, ValueError):
        steps = 8
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


def _decode_b64_image(data: str) -> Image.Image | None:
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
                       init_image: Image.Image | None = None,
                       mask: Image.Image | None = None,
                       cloud_endpoint: CloudEndpoint | None = None) -> None:
    """后台线程：加载引擎 → 推理 → 落盘 → 更新任务/广播。

    mask 非空时走局部重绘（inpaint）链路；协作式取消：进度回调发现
    取消旗标即抛 _TaskCancelled 中断推理。
    cloud_endpoint（批2 云端API）：非 None 时跳过本地引擎链路，由云端
    适配器出图（绘画工位绑定云端连接时经 _make_paint_queue_runner 传入）。

    执行流程追踪（2026-08-23）：Flow 从 task dict 显式跨线程传入，
    节点链 排队等待→模型加载→提示词优化→图像生成→结果落盘，
    推理进度回调同时作为追踪心跳（stalled 卡住检测依据）。
    """
    from ..services.flow_trace import NULL_FLOW

    engine = get_paint_engine()
    _task_update(task_id, status="running")
    # 取出任务创建时启动的流程（排队时长回溯到触发时刻）；
    # _task_get 返回浅拷贝，须从原始字典 pop 防引用滞留
    with _tasks_lock:
        flow = (_tasks.get(task_id) or {}).pop("_flow", None) or NULL_FLOW
    flow.attach()

    gen_node = None  # 推理节点引用（progress 心跳写入）

    def progress(percent: int, step: int) -> None:
        if task_id in _cancel_flags:
            raise _TaskCancelled()
        _task_update(task_id, percent=percent, step=step)
        _broadcast_progress(task_id, percent, step)
        if gen_node is not None:
            gen_node.progress(f"{percent}%（{step}/{params.get('steps', 30)} 步）")

    try:
        if task_id in _cancel_flags:
            raise _TaskCancelled()

        # 节点1：排队等待（回溯到流程触发时刻，duration=排队时长）
        with flow.node("排队等待", start_at=flow.started_at,
                       input_summary=f"优先级 {params.get('priority', 5)}",
                       friendly="任务排队等待调度") as n:
            n.output(f"等待 {time.time() - flow.started_at:.1f} 秒后开始执行")

        # ── 云端出图（批2 云端API 2026-09-06）────────────────────
        # 绘画工位绑定云端连接：跳过本地选型/翻译/装载/优化（本地显卡
        # 零占用），提示词原样直送云端（多语言模型原生理解中文），结果
        # 契约与本地引擎对齐（images/seed/model/...）后走同一落盘链。
        if cloud_endpoint is not None:
            if mask is not None:
                raise RuntimeError(
                    "云端出图暂不支持局部重绘（蒙版）：请到设置解绑"
                    "「绘画页出图」工位后重试")
            _t0 = time.time()
            from ..services.inference.cloud_image_client import generate_image as _cloud_generate

            def _cloud_check() -> None:
                if task_id in _cancel_flags:
                    raise _TaskCancelled()

            def _cloud_progress(pct: int) -> None:
                _task_update(task_id, percent=max(1, min(99, pct)))
                _broadcast_progress(task_id, max(1, min(99, pct)), 0,
                                    status="running")

            with flow.node(
                    "云端出图",
                    input_summary=f"{params.get('width', 1024)}x"
                                  f"{params.get('height', 1024)} "
                                  f"{cloud_endpoint.provider_name}",
                    friendly=f"云端 API 出图（{cloud_endpoint.provider_name}"
                             f" · {cloud_endpoint.model or '默认模型'}）") as cn:
                cn.output("任务已提交云端，等待服务商出图…")
                image = _cloud_generate(
                    cloud_endpoint, params["prompt"],
                    width=int(params.get("width", 1024)),
                    height=int(params.get("height", 1024)),
                    negative=str(params.get("negative") or ""),
                    ref_images=[init_image] if init_image is not None
                    else None,
                    check_cancel=_cloud_check,
                    on_progress=_cloud_progress,
                    slot="paint.image")
                cn.output(f"用时 {time.time() - _t0:.1f}s")
            gen_node = None
            result = {"images": [image],
                      "seed": int(params.get("seed", -1)),
                      "model": f"cloud:{cloud_endpoint.provider_name}",
                      "sampler": params.get("sampler", ""),
                      "elapsed_ms": int((time.time() - _t0) * 1000),
                      "backend": "cloud", "actual_steps": 0}
            # 云端专属落盘尾链（与本地 节点5 同构；提前 return 跳过本地
            # 选型/翻译/装载/优化整段——本地链路代码零缩进改动）
            with flow.node("结果落盘",
                           friendly="保存图像并写入历史记录") as n:
                rel_path = engine.save_result(
                    image, task_id, params["prompt"],
                    params.get("negative", ""),
                    {k: v for k, v in params.items() if k != "optimize"},
                    result["seed"])
                image_b64 = engine.image_to_base64(image)
                n.output(rel_path)
            _task_update(task_id, status="done", percent=100,
                         file_path=rel_path, image_b64=image_b64,
                         seed=result["seed"], model=result["model"],
                         sampler=result.get("sampler", ""),
                         elapsed_ms=int(result["elapsed_ms"]),
                         backend="cloud")
            _broadcast_progress(task_id, 100,
                                int(params.get("steps", 0)), status="done")
            flow.end("success", output_summary=rel_path)
            return

        prompt = params["prompt"]
        model_hint = params.get("model")

        # ── comfy klein 出图分支（W3-C 2026-09-13）──────────────────
        # paint.gen_engine=comfy 时整段接管本地路径：klein 的 Qwen3
        # 编码器中文直入，SDXL 时代的语言感知路由/翻译兜底/RAM 折腾
        # 整段不需要；步数/cfg 走 paint.preset 档（fast=4 步）。
        # legacy 分支原样保留在下方（gate=legacy 可达），paint_engine
        # 退役时随删。img2img=ReferenceLatent 条件生成（非像素初始化）。
        if _paint_gen_engine_comfy():
            from ..services.inference.comfy_paint_engine import (
                comfy_paint_available,
                get_comfy_paint_engine,
            )
            from .manga.common import comfy_paint_generate

            if model_hint and model_hint not in (
                    "flux2-klein-9b", "flux2-klein-4b", "z-image-turbo"):
                # comfy 栈物理上只有 klein 系 + z-image-turbo 权重：其余
                # 点名如实记录后按 klein 出活（白名单拒绝已在上方执行）
                with flow.node("底座说明", friendly="底座口径说明") as n:
                    n.output(f"comfy 栈仅 klein 系与 z-image-turbo 可用，"
                             f"点名 {model_hint}"
                             " 按 flux2-klein-9b-fp8 执行")
            _z_mode = model_hint == "z-image-turbo"
            with flow.node(
                    "模型加载",
                    friendly=("ComfyUI Z-Image-Turbo 栈" if _z_mode
                              else "ComfyUI klein-9b-fp8 栈")) as n:
                if not comfy_paint_available():
                    raise RuntimeError(
                        "MODEL_LOAD_FAILED: ComfyUI klein 出图栈不可用"
                        "（便携版或权重缺失）")
                n.output("z-image-turbo（中文字渲染/写实，8 步蒸馏）"
                         if _z_mode else
                         "klein-9b-fp8（Qwen3 编码器中文直入，免翻译）")

            try:
                prompt, _style_note = _inject_style_pack(prompt, params)
                if _style_note:
                    with flow.node("风格注入",
                                   friendly="按画风注入风格与质量词") as n:
                        n.output(_style_note)
            except Exception as exc:  # noqa: BLE001 - 注入失败不阻断
                log.warning("风格包注入失败（跳过）: %s", exc, exc_info=True)
            params["prompt"] = prompt

            if params.get("optimize"):
                with flow.node("提示词优化", friendly="AI 优化提示词") as n:
                    n.output("comfy klein 栈不支持提示词优化，原样使用")

            with flow.node(
                    "图像生成",
                    input_summary=f"{params.get('width', 1024)}x"
                                  f"{params.get('height', 1024)} "
                                  f"paint.preset 档",
                    friendly="ComfyUI 模型推理生成图像") as gen_node:
                gen_node.output("已提交 ComfyUI（粗粒度进度）")
                # 合成心跳：ComfyUI /history 轮询无步级粒度，阶梯进度
                # 供 flow stalled 检测与前端进度条续命
                _hb_stop = threading.Event()

                def _hb() -> None:
                    _pct = 3
                    while not _hb_stop.wait(6.0):
                        _pct = min(90, _pct + 4)
                        progress(_pct, 0)

                _hb_t = threading.Thread(target=_hb, daemon=True,
                                         name="draw-comfy-hb")
                _hb_t.start()
                try:
                    if mask is not None and init_image is not None:
                        result = get_comfy_paint_engine().inpaint(
                            params, init_image, mask)
                    else:
                        result = comfy_paint_generate(
                            params, ref_image=init_image)
                finally:
                    _hb_stop.set()
                    _hb_t.join(timeout=1)
            gen_node = None

            with flow.node("结果落盘",
                           friendly="保存图像并写入历史记录") as n:
                image = result["images"][0]
                rel_path = _save_result_local(
                    image, task_id, prompt, params.get("negative", ""),
                    {k: v for k, v in params.items() if k != "optimize"},
                    result["seed"])
                image_b64 = _pil_to_b64(image)
                n.output(rel_path)

            _task_update(task_id, status="done", percent=100,
                         file_path=rel_path, image_b64=image_b64,
                         seed=result["seed"], model=result["model"],
                         sampler=params.get("sampler", "euler"),
                         elapsed_ms=int(result["elapsed_ms"]),
                         backend=result.get("engine") or "comfy-klein")
            _broadcast_progress(task_id, 100,
                                int(params.get("steps", 0)), status="done")
            flow.end("success", output_summary=rel_path)
            return

        # （prompt/model_hint 已在 comfy 分支前提取——W3-C）
        # ── 模块级选型配置生效（模型管理 → 功能模块模型配置）───────
        # ① 显式点名模型不在白名单 → 如实失败（精细化管控落地）
        # ② 未指定模型且配置了默认 → 采用模块默认（优先于智能路由
        #    ——与「显式点名尊重用户意图」同语义，管理员配置即意图）
        try:
            from ..api.models import get_module_model_scope
            _allowed, _default = get_module_model_scope("paint")
            if model_hint and _allowed is not None \
                    and model_hint not in _allowed:
                raise RuntimeError(
                    f"MODEL_NOT_ALLOWED: 模型 {model_hint} 不在 AI 绘画"
                    "模块的可用范围内，请在模型管理中调整配置")
            if not model_hint and _default:
                model_hint = _default
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - 配置读取失败不阻断生成
            log.warning("绘画模块选型配置读取失败（跳过）: %s", exc, exc_info=True)

        # ── 自动切换底座（生图路由引擎）：风格画像→底座×风格包组合 ──
        # auto 模式（未显式点名且无模块默认）时由路由引擎决策；
        # 白名单拒绝/模块默认优先级均高于路由（前置块已落地），
        # 此处仅在 model_hint 仍为空时介入，不触碰后续语言感知逻辑
        if not model_hint:
            try:
                from ..services.inference.gen_router import resolve_route
                current = (engine.get_status().get("model") or "") \
                    if engine.is_ready else ""
                route = resolve_route("paint", prompt, prefer_keep=current)
                if route.model_id:
                    model_hint = route.model_id
            except Exception as exc:  # noqa: BLE001 - 路由失败不阻断生成
                log.warning("生图路由决策失败（跳过）: %s", exc, exc_info=True)
        translated_fallback = False  # 走了翻译兜底（模型加载后需收缩 RAM）

        # ── 语言感知路由（2026-08-23 图文不符修复）─────────────────
        # SDXL 的 CLIP 文本编码器无中文语义能力，中文提示词直接送入
        # 会生成与文本无关的图像。缺省（未显式指定 model）且提示词
        # 含中文时：
        #   a. 冷启动 + 双语底座（qwen-image-2512）可用 → 加载双语底座
        #      （原生中文理解，零翻译损耗）；
        #   b. 双语底座不可用 或 sdxl 已驻留（避免反复换载）→
        #      提示词中译英后走英文底座（诚实降级，翻译失败回退原文）。
        # 显式点名 model 时尊重用户意图，不做改写。
        if not model_hint and contains_cjk(prompt):
            current = (engine.get_status().get("model") or "") \
                if engine.is_ready else ""
            if not current.startswith(("qwen-image", "flux2")):
                dual = engine.cjk_default_model()
                # RAM 预检（2026-08-23 后端 OOM 死亡事故）：qwen GGUF
                # 流式推理 12.31GB 权重常驻 RAM，叠加后台学习会话
                # （Chromium 数 GB）时 RAM 99% → 进程死亡。可用 RAM
                # 不足时放弃双语底座，走翻译兜底（sdxl 权重在显存，
                # 不占 RAM）
                ram_ok = False
                if dual and not current:
                    try:
                        import psutil

                        # 22GB 门槛（2026-08-23 实测定界）：qwen GGUF
                        # 峰值需求 17.3GB（权重 12.31 + 开销 3 + 生成
                        # 2），但 pageable 权重换页边界效应显著——
                        # 20/20.9GB 起步两次实测均 device mismatch 失败
                        # （部分层被换到 CPU），22.5GB 起步成功。22GB
                        # 是稳定下界。用户约束"RAM ≤85%；硬件不足时
                        # 确保质量"——低于 22GB 时质量最优解 = 翻译
                        # 兜底 + 四轮对照实验定论的提示词工程（主体
                        # 前置/鞋类污染剔除/场景锚定/风格护栏负面词，
                        # 实测 3/3 命中目标构图）。
                        # 2026-09-09 V9 尾款①：值搬至 services/
                        # vram_policy.py（对拍锁定），此处改引常量。
                        from ..services.vram_policy import PAINT_QWEN_RAM_FLOOR_GB
                        ram_ok = psutil.virtual_memory().available \
                            >= PAINT_QWEN_RAM_FLOOR_GB * 1024 ** 3
                    except Exception:  # noqa: BLE001 - psutil 缺失保守放行
                        ram_ok = True
                if dual and not current and ram_ok:
                    model_hint = dual
                elif (not current
                      and "flux2-klein-9b" in engine.available_models()):
                    # RAM 不足 qwen 时的次优解（2026-08-23 图文不符
                    # v2 排查定论）：SDXL 翻译兜底链路的 CLIP 是
                    # bag-of-words 弱语义，叠加 WDDM 桌面显存状态
                    # 噪声（GUI 应用占显存 → fp16 kernel 数值路径
                    # 变化 → 去噪轨迹混沌发散），同 prompt 同 seed
                    # 在干净态出写实脚特写、翻译态出黑白线条胸像
                    # ——出图对中文语义不可信。flux2-klein-9b 的
                    # Qwen3-8B 编码器原生中文，显存够时中文直入
                    # （零翻译损耗）；显存闸门由 ensure_loaded 内部
                    # check_vram 把守，失败走翻译兜底。
                    # 2026-08-29 模型裁剪：中文直入 hint 由 klein-4b
                    # 改为 klein-9b（绘画模块唯一 klein 底座）。
                    try:
                        flux_ok, _free = engine.check_vram(8.5)
                    except Exception:  # noqa: BLE001 - 查询失败保守放行
                        flux_ok = True
                    if flux_ok:
                        model_hint = "flux2-klein-9b"
                if not model_hint:
                    # 翻译须在模型加载前（调方约定：翻译用对话引擎，
                    # 翻译完 paint 加载按需腾显存卸载对话引擎）
                    with flow.node(
                            "提示词翻译",
                            input_summary=f"原文={prompt[:40]}",
                            friendly="中文提示词翻译为英文（当前底座"
                                     "不识中文）") as n:
                        translated = translate_prompt_zh2en(prompt)
                        n.output(f"译文={translated[:60]}"
                                 if translated != prompt else "翻译失败，原样使用")
                        if translated != prompt:
                            prompt = translated
                            translated_fallback = True
                            # SDXL 兜底风格护栏（2026-08-23 图文不符
                            # v2 实测教训）：CLIP 是 bag-of-words 弱语义，
                            # 负面提示词为空时译文中的风格词会失效
                            # （译出 realistic 仍生成动漫半身像、构图
                            # 焦点被 "beautiful girl" 权重淹没）。按译文
                            # 风格动态补负面词拉回目标风格 + 通用质量词；
                            # 用户已填 negative 时追加不覆盖。
                            tl = translated.lower()
                            if any(w in tl for w in
                                   ("realistic", "photo", "real beauty",
                                    "real skin", "real person",
                                    "real-life")):
                                style_neg = ("cartoon, anime, illustration,"
                                             " 3d render, painting")
                                # 硬过滤：剔除译文中矛盾的动漫词——
                                # 翻译器偶发混入 "anime style"（如把
                                # "美少女"联想成二次元），与负面词 anime
                                # 正负对冲后 CFG 互相抵消，护栏失效
                                # （2026-08-23 实测：含 anime style 的
                                # 译文仍生成动漫半身像）。代码级保证，
                                # 不依赖 LLM 遵从模板。
                                parts = [p.strip() for p in
                                         translated.split(",")]
                                kept = [p for p in parts
                                        if not any(b in p.lower() for b in
                                                   ("anime", "cartoon",
                                                    "manga", "illustration",
                                                    "chibi", "3d render"))]
                                if kept and kept != parts:
                                    translated = ", ".join(kept)
                                    prompt = translated
                                    tl = translated.lower()
                            elif any(w in tl for w in
                                     ("anime", "manga", "cartoon",
                                      "illustration", "chibi")):
                                style_neg = "photo, photorealistic, 3d"
                            else:
                                style_neg = ""
                            # 部位特写锚定（2026-08-23 五轮对照实验
                            # 定论）：成功写法（手动 3/3 写实脚特写）=
                            # "feet, foot focus, realistic photo,
                            # detailed toes, soft lighting, wooden
                            # floor" + 负面 "cartoon, anime,
                            # illustration, 3d render, painting"。
                            # d4d398ed 失败反证：token 集合一致但
                            # 顺序不同（detailed toes 第 6 位、
                            # wooden floor 重复）+ 负面词多
                            # watermark/text 即翻车——CLIP 77 token
                            # 内位置敏感。故核心词按成功顺序固定
                            # 重构，非核心词去重后尾部追加，负面词
                            # 与成功写法逐字对齐。
                            first = tl.split(",")[0].strip()
                            if first in ("feet", "foot", "barefoot"):
                                parts = [p.strip() for p in
                                         prompt.split(",")]
                                kept = [p for p in parts
                                        if p and not any(b in p.lower()
                                                         for b in (
                                                             "sneaker",
                                                             "shoe",
                                                             "boot",
                                                             "heel",
                                                             "sandal",
                                                             "sock",
                                                             "footwear",
                                                             "background"))]
                                core = ["feet", "foot focus",
                                        "realistic photo", "detailed toes",
                                        "soft lighting", "wooden floor"]
                                core_l = {c.lower() for c in core}
                                tail: list[str] = []
                                for p in kept:
                                    lp = p.lower()
                                    if lp in core_l or lp in tail \
                                            or any(lp == t.lower()
                                                   for t in tail):
                                        continue
                                    tail.append(p)
                                prompt = ", ".join(core + tail)
                                style_neg = ("cartoon, anime, illustration,"
                                             " 3d render, painting")
                            extra = [x for x in (style_neg,) if x]
                            user_neg = (params.get("negative") or "").strip()
                            params = {**params, "negative": ", ".join(
                                [user_neg] + extra if user_neg else extra)}

        # 节点2：模型加载（引擎已就绪时跳过——不记节点）
        if not engine.is_ready:
            with flow.node(
                    "模型加载", input_summary=f"model={model_hint}",
                    friendly="加载绘画模型到显存") as n:
                loaded = engine.ensure_loaded(model_hint)
                if not loaded:
                    raise RuntimeError(
                        "MODEL_LOAD_FAILED: "
                        + (engine.get_status().get("last_error")
                           or "绘画模型未就绪"))
                n.output(f"模型就绪: {engine.get_status().get('model')}")
        if translated_fallback:
            # 翻译兜底资源卫生：模型加载的腾挪已卸载对话引擎
            # （transformers 后端 ~9GB），但 torch/pymalloc 卸载后
            # 进程 RAM 不归还 OS——不收缩会把系统可用 RAM 永久压
            # 低 9GB，后续中文任务全部过不了 qwen 的 22GB 门槛
            # （恶性循环，2026-08-23 实测根因）。
            shrink_working_set()

        # 存量断链修复（2026-09-06 风格注入实现时发现）：翻译兜底与
        # 提示词优化改写的都是局部 prompt，此前从未回写 params——
        # engine.generate/img2img/inpaint 读 params["prompt"]，优化
        # 结果只进落盘记录不进生成链。回写后三分支（t2i/i2i/inpaint）
        # 统一生效。
        params["prompt"] = prompt

        # 节点2.5：风格包注入统一（2026-09-06 架构升级计划 B-阶段一，
        # 《AI计划》§3.1「便宜大赢」）：绘画链此前只用风格包选模型
        # （resolve_route）不注入提示词，与漫剧关键帧链（完整注入
        # style_block+quality_block）不对齐。移植同款注入——detect
        # 对用户原文（中文风格词可嗅探），注入块为英文（对编码器
        # 权重更稳），负面词用户自带优先、风格包 negative_hint 兜底；
        # explain_style 裁决凭据进 flow 可追溯。
        try:
            prompt, _style_note = _inject_style_pack(prompt, params)
            if _style_note:
                with flow.node("风格注入",
                               friendly="按画风注入风格与质量词") as n:
                    n.output(_style_note)
        except Exception as exc:  # noqa: BLE001 - 注入失败不阻断生成
            log.warning("风格包注入失败（跳过）: %s", exc, exc_info=True)

        # 节点3：提示词优化（仅启用时）
        if params.get("optimize"):
            with flow.node("提示词优化", input_summary=f"prompt={prompt[:40]}",
                           friendly="AI 优化提示词") as n:
                prompt, used = engine.optimize_prompt(prompt)
                n.output("已优化" if used else "原样保留（无需优化）")
                if used:
                    _task_update(task_id, optimized_prompt=prompt)

        # 节点4：图像生成（推理主链路，progress 心跳续命）
        with flow.node(
                "图像生成",
                input_summary=f"{params.get('width', 1024)}x"
                              f"{params.get('height', 1024)} "
                              f"{params.get('steps', 30)}步 "
                              f"cfg={params.get('cfg', 7.5)}",
                friendly="模型推理生成图像") as gen_node:
            if mask is not None and init_image is not None:
                result = engine.inpaint(params, init_image, mask,
                                        progress_cb=progress)
            elif init_image is not None:
                result = engine.img2img(params, init_image, progress_cb=progress)
            else:
                result = engine.generate(params, progress_cb=progress)
            gen_node.output(
                f"seed={result['seed']} 实际{result.get('actual_steps', params.get('steps', 30))}步"
                f" 用时 {result['elapsed_ms'] / 1000:.1f}s"
                + ("（质量降参）" if result.get("quality_reduced") else ""))
        gen_node = None

        # 节点5：结果落盘
        with flow.node("结果落盘", friendly="保存图像并写入历史记录") as n:
            image = result["images"][0]
            rel_path = engine.save_result(
                image, task_id, prompt, params.get("negative", ""),
                {k: v for k, v in params.items() if k != "optimize"},
                result["seed"])
            image_b64 = engine.image_to_base64(image)
            n.output(rel_path)

        _task_update(task_id, status="done", percent=100,
                     file_path=rel_path, image_b64=image_b64,
                     seed=result["seed"], model=result["model"],
                     sampler=result.get("sampler", params["sampler"]),
                     elapsed_ms=int(result["elapsed_ms"]),
                     degraded=bool(result.get("degraded", False)),
                     backend=str(result.get("backend", "")))
        _broadcast_progress(task_id, 100, params["steps"], status="done")
        if translated_fallback:
            # 兜底任务后的 RAM 让路（恶性循环最后一环）：sdxl 的
            # cpu_offload 在 host RAM 常驻 ~7.8GB 权重副本（is_ready
            # 常驻设计）。RAM 紧张时（<22GB，即 qwen 进不来的场景）
            # 任务结束卸载引擎并收缩进程内存，让下一次中文任务恢复
            # qwen 双语底座（质量优先）；RAM 充足时保持常驻（性能
            # 优先，免重载）。2026-08-23 实测：不卸载时可用 RAM 卡
            # 在 12GB，永远过不了 22GB 门槛。
            try:
                import psutil
                if psutil.virtual_memory().available < 22 * 1024 ** 3:
                    engine.unload_model()
                    shrink_working_set()
                    log.info("RAM 紧张（<22GB），兜底任务后已卸载绘画"
                             "引擎 host 副本，为下次 qwen 路由让路")
            except Exception as exc:  # noqa: BLE001 - 让路失败不影响结果
                log.warning("兜底任务后引擎让路失败（忽略）: %s", exc, exc_info=True)
        flow.end("success", output_summary=rel_path)
    except _TaskCancelled:
        _cancel_flags.discard(task_id)
        log.info("绘画任务已取消: %s", task_id)
        _task_update(task_id, status="cancelled", error="用户取消")
        _broadcast_progress(task_id, 0, 0, status="cancelled")
        flow.end("cancelled", error_detail="用户取消")
    except Exception as exc:  # noqa: BLE001 - 任务失败收敛为状态
        log.exception("绘画任务失败: %s", task_id)
        msg = str(exc)
        code = "PAINT_GENERATION_FAILED"
        flow_code = "GENERATE_FAILED"
        if msg.startswith("MODEL_LOAD_FAILED"):
            code = "MODEL_LOAD_FAILED"   # 保持既有错误码语义
            flow_code = "MODEL_LOAD_FAILED"
            msg = msg.split(":", 1)[-1].strip()
        _task_update(task_id, status="error", code=code, error=msg)
        _broadcast_progress(task_id, 0, 0, status="error")
        flow.end("error", error_code=flow_code,
                 error_detail=str(exc)[:500])


def _submit_precheck() -> None:
    """提交期快速预检（仅热保护；2026-09-02 图像队列改造）。

    此前其他功能（视频/对话）持锁时直接 40007 拒绝——现一律受理入队，
    由 services/image_queue.py 的 worker 等锁让位顺序执行（与视频队列
    同语义）；热保护仍保持提交期拒绝（用户立即得到反馈）。
    """
    try:  # 热保护（与 acquire_or_raise 同源逻辑，失败不阻断）
        from ..services.thermal_guard import get_thermal_guard
        guard = get_thermal_guard()
        if guard.is_paused():
            status = guard.get_status()
            raise ApiError("HARDWARE_THERMAL_THROTTLE",
                f"GPU 温度过高（{status['last_temp_celsius']:.0f}°C），"
                f"已强制暂停生成任务，请等待散热后重试")
    except ApiError:
        raise
    except Exception:  # noqa: BLE001
        log.debug("_submit_precheck: 降级忽略", exc_info=True)


def _make_paint_queue_runner(
        task_id: str,
        cloud_endpoint: CloudEndpoint | None = None,
) -> Callable[[dict, Callable[[], None]], None]:
    """绘画任务的图像队列 runner（2026-09-02 迁移自 _dispatch_loop）。

    功能锁/vLLM 协商由队列统一编排；此处仅取副作用图 + 跑推理。
    运行中取消 = draw 自己的 _cancel_flags（progress 回调协作中断，
    引擎抛 PaintCancelledError，由 _run_generate_task 内部收敛终态）。
    cloud_endpoint（批2 云端API 2026-09-06）：绘画工位绑定云端时由
    提交点解析传入——任务走云端道（队列跳过本地准入），推理换云端
    适配器（_run_generate_task 内分支）。
    """

    def runner(task: dict, check_cancel: Callable[[], None]) -> None:  # noqa: ARG001
        imgs = _task_images.pop(task_id, None)
        init_image = imgs[0] if imgs else None
        mask = imgs[1] if imgs and len(imgs) > 1 else None
        params = (_task_get(task_id) or {}).get("params", {})
        _run_generate_task(task_id, params, init_image, mask,
                           cloud_endpoint=cloud_endpoint)

    return runner


def _resolve_paint_cloud_endpoint():
    """解析绘画工位云端端点（未绑定/连接停用返回 None=本地旧行为）。"""
    try:
        from ..services.cloud_provider_service import get_image_endpoint
        return get_image_endpoint("paint.image")
    except Exception as exc:  # noqa: BLE001 - 解析失败按本地（不阻断生图）
        log.warning("绘画云端路由解析失败（按本地引擎）: %s", exc, exc_info=True)
        return None


def _submit_task(task: dict, init_image=None, mask=None) -> None:
    """提交任务：预检 → 副作用登记 → 入统一图像队列。

    云端绑定（批2）：跳过热保护预检（云任务不占本地 GPU 温度无关），
    任务打 cloud 标记走云端道。
    """
    cloud_ep = _resolve_paint_cloud_endpoint()
    if cloud_ep is None:
        _submit_precheck()
    if init_image is not None or mask is not None:
        _task_images[task["task_id"]] = (init_image, mask)
    import asyncio as _asyncio
    get_image_queue().submit({
        "task_id": task["task_id"], "kind": "paint",
        "runner": _make_paint_queue_runner(task["task_id"], cloud_ep),
        "loop": _asyncio.get_running_loop(),
        "priority": task.get("priority", 5),
        "cloud": cloud_ep is not None,
    })


# ── 文生图 ──────────────────────────────────────────────────────────

@router.post("/draw/generate")
@router.post("/paint/generate")
async def draw_generate(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """文生图（异步任务）。

    请求: {"prompt", "negative"?, "steps"=30, "cfg"=7.5, "width"=1024,
           "height"=1024, "sampler"="euler_a", "seed"=-1, "model"?,
           "optimize"?, "batch_size"=1(1~4)}
    返回: {"task_id"(首个), "task_ids"(批量时全量)}；进度经
    ws_broadcaster 推送，结果走 /result/{task_id}。

    批量（P2 复核修复 2026-09-02）：此前 batch_size 前端有控件、
    后端静默丢弃——批量=2 实生 1 张。现钳 1~4 派发 N 个队列任务
    （统一图像队列 FIFO 串行）；固定种子按序派生 seed+i 防同图，
    随机种子(-1)各任务独立随机。
    """
    params = _parse_common(body)
    if not params["prompt"]:
        raise ApiError("PAINT_GENERATION_FAILED", "生成失败，请检查提示词是否为空")

    try:
        batch = int(body.get("batch_size") or 1)
    except (TypeError, ValueError):
        batch = 1
    batch = max(1, min(batch, 4))
    base_seed = params.get("seed", -1)
    task_ids: list[str] = []
    for i in range(batch):
        task_params = dict(params)  # 每任务独立（防 params 别名共享）
        if isinstance(base_seed, int) and base_seed >= 0:
            task_params["seed"] = base_seed + i  # 定种子按序派生防同图
        task = _task_create("txt2img", task_params)
        task_ids.append(task["task_id"])
        _submit_task(task)
    return ok({"task_id": task_ids[0], "task_ids": task_ids,
               "priority": 5, "batch": batch})


# ── 图生图 ──────────────────────────────────────────────────────────

@router.post("/draw/img2img")
async def draw_img2img(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """图生图（异步任务）。额外参数: init_image(base64), strength(0.05~1.0)。"""
    params = _parse_common(body)
    if not params["prompt"]:
        raise ApiError("PAINT_GENERATION_FAILED", "生成失败，请检查提示词是否为空")

    raw = body.get("init_image") or body.get("image") or ""
    if not raw:
        raise ApiError("SYSTEM_PARAM_INVALID", "缺少 init_image 图像数据")
    init_image = _decode_b64_image(str(raw))
    if init_image is None:
        raise ApiError("SYSTEM_PARAM_INVALID", "init_image 图像数据无法解析")

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
async def art_inpaint(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
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
        raise ApiError("SYSTEM_PARAM_INVALID", "缺少 image 原图数据")
    image = _decode_b64_image(str(raw_img))
    if image is None:
        raise ApiError("SYSTEM_PARAM_INVALID", "image 原图数据无法解析")

    raw_mask = body.get("mask") or ""
    if not raw_mask:
        raise ApiError("SYSTEM_PARAM_INVALID", "缺少 mask 遮罩数据（白色=待重绘区域）")
    mask = _decode_b64_image(str(raw_mask))
    if mask is None:
        raise ApiError("SYSTEM_PARAM_INVALID", "mask 遮罩数据无法解析")
    if mask.size != image.size:
        mask = mask.resize(image.size)

    # 遮罩空/过大校验（亮度 ≥128 视为重绘区）
    binmask = mask.convert("L").point(lambda v: 255 if v >= 128 else 0)
    if binmask.getbbox() is None:
        raise ApiError("SYSTEM_PARAM_INVALID", "遮罩为空，请涂抹需要重绘的区域")
    hist = binmask.histogram()
    white = sum(hist[128:])
    ratio = white / float(image.size[0] * image.size[1])
    if ratio > _MASK_MAX_RATIO:
        raise ApiError("SYSTEM_PARAM_INVALID",
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
async def draw_upscale(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """图像超分。{"image": base64, "scale": 2|4}

    Real-ESRGAN 可用时真超分；否则 PIL LANCZOS 并标注 degraded=true。
    """
    raw = body.get("image") or ""
    if not raw:
        raise ApiError("SYSTEM_PARAM_INVALID", "缺少 image 图像数据")
    image = _decode_b64_image(str(raw))
    if image is None:
        raise ApiError("SYSTEM_PARAM_INVALID", "image 图像数据无法解析")
    try:
        scale = int(body.get("scale", 2))
    except (TypeError, ValueError):
        scale = 2
    scale = 4 if scale >= 4 else 2

    engine = get_paint_engine()
    lock = await acquire_or_raise("paint")
    try:
        if _paint_gen_engine_comfy():
            # W3-C：comfy 档无超分模型——PIL LANCZOS 诚实降级
            result = await run_blocking(_upscale_lanczos, image, scale)
        else:
            result = await run_blocking(engine.upscale, image, scale)
    finally:
        await lock.release("paint")

    out_b64 = _pil_to_b64(result["image"])
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
def draw_result(task_id: str) -> dict[str, Any]:
    """查询任务状态与结果。done 时返回图片 base64 + 文件路径。"""
    task = _task_get(task_id)
    if task is None:
        # 内存任务表没有时查 paint_history（例如服务重启后）
        db = get_db_safe()
        if db is not None:
            try:
                _ensure_history_table()
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
                log.warning("paint_history 查询失败: %s", exc, exc_info=True)
        raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", "任务不存在", detail={"task_id": task_id})

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
    if task["status"] == "pending":
        pos = get_image_queue().position(task_id)
        if pos is not None:
            resp["queue_position"] = pos
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
def paint_task_cancel(task_id: str) -> dict[str, Any]:
    """取消绘画任务（协作式）。

    - 排队中：直接从图像队列剔除，状态 → cancelled
    - 运行中：置取消旗标，下一推理步进度回调抛中断（状态 → cancelled）
    - 已结束（done/error/cancelled）：40008
    """
    task = _task_get(task_id)
    if task is None:
        raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", "任务不存在", detail={"task_id": task_id})
    if task["status"] in ("done", "error", "cancelled"):
        raise ApiError("SYSTEM_PARAM_INVALID", f"任务已结束（{task['status']}），无法取消")

    outcome = get_image_queue().cancel(task_id)
    _task_images.pop(task_id, None)

    if outcome == "queued":
        _task_update(task_id, status="cancelled", error="排队中被取消")
        _broadcast_progress(task_id, 0, 0, status="cancelled")
        # 追踪收尾：排队中被取消（流程对象随任务字典丢弃前结束）
        _end_flow(task_id, "cancelled", error_detail="排队中被用户取消")
    else:
        _cancel_flags.add(task_id)  # 运行中：进度回调协作中断

    return ok({"task_id": task_id, "status": "cancelled",
               "was_pending": outcome == "queued"})


@router.post("/paint/task/{task_id}/priority")
def paint_task_priority(task_id: str, body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """调整排队任务优先级（PAINT-044）：{"priority": 0~9}。

    仅对仍在等待队列中的任务生效（运行中/已结束任务返回
    effective=false 如实告知）。
    """
    task = _task_get(task_id)
    if task is None:
        raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", "任务不存在", detail={"task_id": task_id})
    try:
        priority = int(body.get("priority", 5))
    except (TypeError, ValueError):
        raise ApiError("SYSTEM_PARAM_INVALID", "priority 必须是 0~9 的整数") from None
    priority = max(0, min(priority, 9))

    effective = get_image_queue().set_priority(task_id, priority)
    _task_update(task_id, priority=priority)
    return ok({"task_id": task_id, "priority": priority,
               "effective": effective,
               "status": (_task_get(task_id) or {}).get("status", "")})


@router.get("/paint/queue")
@router.get("/draw/queue")
def paint_queue() -> dict[str, Any]:
    """绘画任务队列快照（PAINT-042）：等待队列（按调度顺序）+ 运行中。

    2026-09-02：等待队列真源 = services/image_queue.py（统一图像队列，
    与漫剧关键帧/资产图同队）。
    """
    snap = get_image_queue().snapshot()
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
               (_task_get(str(q.get("task_id"))) for q in snap["queued"])
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
        log.warning("paint_history 列迁移失败: %s", exc, exc_info=True)


@router.get("/draw/history")
@router.get("/paint/history")
def draw_history(page: int = Query(1, ge=1),
                 page_size: int = Query(20, ge=1, le=100),
                 favorite: int | None = Query(None),
                 start: float | None = Query(None),
                 end: float | None = Query(None),
                 width: int | None = Query(None),
                 height: int | None = Query(None),
                 keyword: str = Query("")) -> dict[str, Any]:
    """生成历史（paint_history 表，按时间倒序分页）。

    PAINT-046 画廊筛选：
    - favorite=0/1   只看收藏 / 只看未收藏
    - start/end      创建时间范围（epoch 秒）
    - width/height   按生成尺寸筛选（params_json 内 JSON1 提取）
    - keyword        提示词模糊匹配
    """
    _ensure_history_table()
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
        log.warning("paint_history 查询失败: %s", exc, exc_info=True)
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
        # 同步清缩略图缓存（#5 缩略图端点配套，防孤儿堆积）
        thumb = (GENERATED_DIR / "images" / _THUMB_DIR_NAME
                 / f"{target.stem}_{_THUMB_SIZE}.jpg")
        if thumb.is_file():
            thumb.unlink(missing_ok=True)
        if target.is_file():
            target.unlink()
            return True
    except Exception as exc:  # noqa: BLE001
        log.debug("历史图文件删除失败（忽略）: %s", exc)
    return False


@router.post("/paint/history/{task_id}/favorite")
@router.post("/draw/history/{task_id}/favorite")
def paint_history_favorite(task_id: str,
                           body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """切换/设置收藏（PAINT-048）。body.favorite 缺省时取反。"""
    _ensure_history_table()
    _ensure_history_columns()

    row = _history_row(task_id)
    if row is None:
        raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", "历史记录不存在",
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


@router.post("/paint/history/batch-delete")
@router.post("/draw/history/batch-delete")
def paint_history_batch_delete(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """批量删除历史（PAINT-051）：{"ids": ["task_id", ...]}（上限 200）。"""
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        raise ApiError("SYSTEM_PARAM_INVALID", "缺少必填参数: ids（task_id 数组）")
    ids = [str(i) for i in ids[:200]]

    _ensure_history_table()

    db = get_db_safe()
    if db is None:
        raise ApiError("PAINT_GENERATION_FAILED", "数据库不可用")
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

_THUMB_DIR_NAME = ".thumbs"
_THUMB_SIZE = 512


def _paint_thumbnail(src: str | pathlib.Path) -> pathlib.Path | None:
    """512px JPEG 缩略图（首访生成 + 磁盘缓存，原子替换）。

    #5 画廊卡顿修复：历史图为 2560×1440 PNG（单张 2-4MB），画廊
    每卡解码 3.7MP → 滚动几十张即百 MB 级解码抖动。网格用缩略图
    （~30KB/张），灯箱仍回原图。生成失败回落 None（调用方回原图）。
    """
    try:
        from pathlib import Path as _P

        tdir = _P(src).parent / _THUMB_DIR_NAME
        tpath = tdir / f"{_P(src).stem}_{_THUMB_SIZE}.jpg"
        if tpath.is_file():
            return tpath
        from PIL import Image

        tdir.mkdir(parents=True, exist_ok=True)
        tmp = tpath.with_suffix(".tmp")
        with Image.open(src) as im:
            rgb = im.convert("RGB")
        rgb.thumbnail((_THUMB_SIZE, _THUMB_SIZE))
        rgb.save(tmp, "JPEG", quality=82)
        tmp.replace(tpath)
        return tpath
    except Exception as exc:  # noqa: BLE001 - PIL 缺失/解码失败回落原图
        log.debug("缩略图生成失败（回落原图）: %s", exc)
        return None


@router.get("/draw/image/{filename}")
def draw_image(filename: str, thumb: int = 0) -> FileResponse:
    """按文件名回读生成图片（前端历史画廊/预览用）。

    历史记录只存相对路径 generated/images/<task_id>.png，
    本端点按文件名安全回读（Path.name 防路径穿越）。
    thumb=1 → 512px JPEG 缩略图（首访生成落盘 .thumbs/ 缓存）；
    生成失败诚实回落原图。
    """
    from pathlib import Path

    from fastapi.responses import FileResponse

    from ..data.file_store import GENERATED_DIR

    safe = Path(filename).name  # 防路径穿越：仅取文件名部分
    path = GENERATED_DIR / "images" / safe
    if not path.is_file():
        raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", "图片不存在或已被清理",
                       detail={"filename": safe})
    if thumb:
        thumb_path = _paint_thumbnail(path)
        if thumb_path is not None:
            return FileResponse(str(thumb_path), media_type="image/jpeg",
                                headers={
                                    "Cache-Control": "private, max-age=86400"})
    return FileResponse(str(path), media_type="image/png",
                        headers={"Cache-Control": "private, max-age=3600"})


# ── 模型与状态 ──────────────────────────────────────────────────────

@router.get("/draw/models")
def draw_models() -> dict[str, Any]:
    """绘画模型列表：路由表 + 本地实际可用状态。"""
    engine = get_paint_engine()
    available = set(engine.available_models())
    items = []
    for m in PAINT_ROUTING_TABLE:
        model_id = m["model"]
        # 本地实际就绪判定：sdxl 系列映射到 sdxl-base-1.0 目录；
        # 其余候选表模型（flux2-klein-4b / qwen-image-2512 等）按
        # 磁盘就绪判定（available_models 列 PAINT_MODEL_CANDIDATES
        # 中目录可加载的模型）
        if model_id.startswith("sdxl"):
            ready = "sdxl-base-1.0" in available
            status = "ready" if ready else "not_installed"
            local_id = "sdxl-base-1.0" if ready else ""
        else:
            ready = model_id in available
            status = "ready" if ready else "not_installed"
            local_id = model_id if ready else ""
        items.append({
            "id": model_id,
            "name": model_id,
            "category": "vision",
            "min_vram_gb": m["min_vram_gb"],
            "status": status,
            "local_model": local_id,
        })
    # 用户导入的绘画模型（登记表兜底，2026-09-01 完整接入）：家族探测
    # 命中才入列——陌生家族明确不支持，不提供点选
    try:
        from ..services.inference.paint_engine import imported_paint_models
        for mid, info in imported_paint_models().items():
            if any(m["id"] == mid for m in items):
                continue
            items.append({
                "id": mid,
                "name": mid,
                "category": "vision",
                "min_vram_gb": info["min_vram_gb"],
                "status": "ready",
                "local_model": mid,
            })
    except Exception as exc:  # noqa: BLE001 - 清单失败不阻断主流程
        log.warning("导入绘画模型清单并入跳过: %s", exc, exc_info=True)
    # 模块级选型配置（模型管理 → 功能模块模型配置）：
    # 白名单过滤 + 默认模型下发。allowed 为空 = 不限制（兼容存量）。
    default_model = ""
    try:
        from ..api.models import get_module_model_scope
        allowed, default_model = get_module_model_scope("paint")
        if allowed is not None:
            items = [m for m in items if m["id"] in allowed]
    except Exception as exc:  # noqa: BLE001 - 配置读取失败不阻断清单
        log.warning("绘画模块白名单过滤跳过: %s", exc, exc_info=True)
    return ok({
        "items": items,
        "total": len(items),
        "engine": engine.get_status(),
        "default_model": default_model or "",
    })


@router.get("/draw/status")
def draw_status() -> dict[str, Any]:
    """绘画引擎状态。"""
    return ok(get_paint_engine().get_status())


# ── ControlNet 预览 ─────────────────────────────────────────────────

@router.post("/draw/controlnet/preview")
async def controlnet_preview(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """预览 ControlNet 条件图（审计 BK-012 诚实降级版）。

    ControlNet 模型未随包 / 预处理管线未接入时，不再抛恒定的 501 空壳
    错误，而是返回 degraded:true + degrade_reason 中文字段的成功信封，
    如实告知前端该能力处于降级不可用状态。
    """
    ctype = str(body.get("type") or "").strip()
    image = body.get("image")
    if not ctype:
        raise ApiError("SYSTEM_PARAM_INVALID", "缺少 ControlNet type 参数")
    if not image:
        raise ApiError("CONTROLNET_CONDITION_INVALID", "ControlNet条件图格式错误")

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
# 本项目仅供学习使用，商业授权请+Q 3559331368
