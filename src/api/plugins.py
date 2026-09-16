"""插件系统 API（OSP v1，插件系统 P1 2026-09-16）。

端点清单（/api/v1 前缀由 main.py 挂载）：
- GET   /plugins                 已登记插件清单（状态/信任级/能力/统计）
- POST  /plugins/{name}/load     加载（幂等；invoke 也会自动加载）
- POST  /plugins/{name}/unload   卸载（faulty 复位通道）
- POST  /plugins/{name}/invoke   执行插件任务（P1 = video-making 运镜预览：
                                  关键帧图片路径 + 镜头计划 → 统计摘要 +
                                  可选帧 PNG 落盘；帧本体绝不进 JSON——
                                  POC2-A 实测 1080p float64 帧列 1.2GB）

宿主侧前置校验（方案 v1.2 修正案 6，堵插件两缺陷）：
- 镜头数 > 关键帧数 → 拒（插件会静默截断多余镜头）；
- 运镜名不在插件 available_motions() → 拒（插件会静默按 static
  渲染但上报原名——报告与行为不符）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..middleware.error_handler import ApiError, ok
from ..services.offload import run_blocking
from ..services.plugin_runtime import get_plugin_runtime
from ..services.plugin_runtime.kernel_gateway import get_plugin_kernel
from ..services.plugin_runtime.loader import read_image_as_frame
from ..services.plugin_runtime.registry import (
    MIN_FREE_RAM_GB,
    PluginRuntimeError,
    _json_summary,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["plugins"])


class ShotSpec(BaseModel):
    """单镜头计划（对齐 video-making 插件 spec）。"""
    motion: str = "static"
    duration_s: float = Field(default=2.0, ge=0.1, le=60.0)
    transition: str | None = None   # cut/crossfade/dip_to_black
    easing: str | None = None       # smoothstep/linear/ease_out


class PluginInvokeRequest(BaseModel):
    """invoke 请求：video 形态（keyframes/shots）或通用形态（data）。

    keyframes/shots 给值走 video-making 规格校验；否则 data 原样作为
    插件 spec（如 rust-coding 的 {"code": ...}）。两态至少其一。
    """
    keyframes: list[str] = Field(
        default_factory=list,
        description="服务端图片路径列表（漫剧关键帧/资产图）")
    fps: int = Field(default=12, ge=1, le=60)
    shots: list[ShotSpec] = Field(default_factory=list)
    save_to: str | None = Field(
        default=None, description="帧输出目录名（仅名字，落 data/plugins/output 下）")
    timeout_s: float = Field(default=180.0, ge=1.0, le=600.0)
    data: dict[str, Any] | None = Field(
        default=None,
        description="通用形态插件 spec（非 video 插件用，如 {\"code\": ...}）")


def _translate(exc: PluginRuntimeError) -> ApiError:
    return ApiError(exc.code, exc.message, suggestion=exc.suggestion)


@router.get("/plugins")
async def list_plugins() -> dict[str, Any]:
    """已登记插件清单（轻量，无加载副作用）。"""
    return ok({"plugins": get_plugin_runtime().plugins_info()})


@router.post("/plugins/{name}/load")
async def load_plugin(name: str) -> dict[str, Any]:
    """加载插件（幂等；产品铁律=未加载自动加载，不要求用户点两次）。"""
    rt = get_plugin_runtime()
    try:
        instance = await run_blocking(rt.ensure_loaded, name)
    except PluginRuntimeError as exc:
        raise _translate(exc) from exc
    return ok({"name": name, "state": "loaded",
               "capability": instance.CAPABILITY})


@router.post("/plugins/{name}/unload")
async def unload_plugin(name: str) -> dict[str, Any]:
    """卸载插件（故障态复位通道：unload → load 即重建）。"""
    rt = get_plugin_runtime()
    try:
        await run_blocking(rt.unload, name)
    except PluginRuntimeError as exc:
        raise _translate(exc) from exc
    return ok({"name": name, "state": "unloaded"})


def _read_frames(paths: list[str]) -> list[Any]:
    """线程体：逐张读关键帧图（PIL 解码不在事件循环里）。"""
    frames = []
    for raw in paths:
        path = Path(raw)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_file():
            raise ApiError("PLUGIN_KEYFRAME_UNREADABLE",
                           f"关键帧图片不存在: {raw}",
                           suggestion="检查路径；漫剧关键帧可用其媒体文件路径")
        frames.append(read_image_as_frame(path))
    return frames


@router.post("/plugins/{name}/invoke")
async def invoke_plugin(name: str, req: PluginInvokeRequest
                        ) -> dict[str, Any]:
    """执行插件任务：video 形态（keyframes）或通用形态（data）。

    video 形态：spec 校验 → 自动加载 → 渲染 → 统计+可选落盘；
    通用形态：data 原样作 spec → 自动加载 → 结果键透传（label 等）。
    """
    rt = get_plugin_runtime()
    if not req.keyframes and req.data is None:
        raise ApiError("PLUGIN_SPEC_MISMATCH",
                       "keyframes 与 data 至少提供其一",
                       suggestion="video 形态传关键帧图片路径；"
                                  "通用形态传 data（如 {\"code\": ...}）")
    if req.data is not None and not req.keyframes:
        # 通用形态：data 原样作 spec（无 video 校验、无读图）
        try:
            result = await rt.invoke(name, dict(req.data),
                                     timeout_s=req.timeout_s)
        except PluginRuntimeError as exc:
            raise _translate(exc) from exc
        return ok(result)
    if not req.keyframes:
        raise ApiError("PLUGIN_SPEC_MISMATCH", "keyframes 不能为空",
                       suggestion="至少提供 1 张关键帧图片路径")
    if len(req.shots) > len(req.keyframes):
        raise ApiError(
            "PLUGIN_SPEC_MISMATCH",
            f"镜头数({len(req.shots)})超过关键帧数({len(req.keyframes)})",
            suggestion="插件按一镜一帧渲染，多余镜头会被静默丢弃——请对齐数量")
    try:
        # 登记检查最先（未登记插件不该走到读图/规格环节）
        instance = await run_blocking(rt.ensure_loaded, name)
        frames = await run_blocking(_read_frames, list(req.keyframes))
        # 运镜名白名单校验（探测式：非 video 形态插件无此方法则跳过）
        motions = getattr(instance, "available_motions", None)
        if callable(motions):
            legal = set(motions())
            bad = [s.motion for s in req.shots if s.motion not in legal]
            if bad:
                raise ApiError(
                    "PLUGIN_SPEC_MISMATCH",
                    f"未知运镜名: {sorted(set(bad))}",
                    suggestion=f"合法运镜: {sorted(legal)}")
        shots_spec = []
        for s in req.shots:
            item: dict[str, Any] = {"motion": s.motion,
                                    "duration_s": s.duration_s}
            if s.transition is not None:
                item["transition"] = s.transition
            if s.easing is not None:
                item["easing"] = s.easing
            shots_spec.append(item)
        spec = {"keyframes": frames, "fps": req.fps, "shots": shots_spec}
        save_dirname = Path(req.save_to).name if req.save_to else None
        result = await rt.invoke(name, spec, save_dirname=save_dirname,
                                 timeout_s=req.timeout_s)
    except PluginRuntimeError as exc:
        raise _translate(exc) from exc
    return ok(result)


# ── 内核接线（2026-09-16 拍板：CuteMamen 内核产品入口） ──────────

class KernelThinkRequest(BaseModel):
    """内核 think 请求：按主题路由到插件（未加载自动热加载）。"""
    topic: str = Field(min_length=1, max_length=64,
                       description="路由主题（如 rust / video）")
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="事件数据（如 rust 传 {\"code\": ...}）")


@router.get("/plugins/kernel")
async def kernel_status() -> dict[str, Any]:
    """内核状态：统计 + 已挂载/已登记插件（轻量，无加载副作用）。"""
    kernel = get_plugin_kernel()
    return ok({"kernel": kernel.stats(),
               "plugins": kernel.list_plugins()})


@router.post("/plugins/kernel/think")
async def kernel_think(req: KernelThinkRequest) -> dict[str, Any]:
    """内核路由一次事件 → 目标插件 on_think → 结果（JSON 安全化出线）。

    纯 CPU 轻量内核（numpy），帧本体等大数组压成形状摘要；
    RAM 闸口径与 registry.invoke 一致。
    """
    import psutil
    avail_gb = psutil.virtual_memory().available / (1 << 30)
    if avail_gb < MIN_FREE_RAM_GB:
        raise ApiError("PLUGIN_RAM_LOW",
                       f"系统可用内存不足（{avail_gb:.1f}GB < {MIN_FREE_RAM_GB}GB）",
                       suggestion="关闭其他大内存任务后重试")
    kernel = get_plugin_kernel()
    results = await run_blocking(
        kernel.think, {"topic": req.topic, "data": req.data})
    safe = [_json_summary(r) for r in results if r is not None]
    routed = len(safe) > 0
    return ok({"topic": req.topic, "routed": routed, "results": safe})
