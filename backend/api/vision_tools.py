"""视觉工具 API 路由（文档E 附录B /art 前缀补齐，F-01~F-04 随包模型接线）。

端点清单：
- POST /art/image-to-3d   单图 3D 生成（TripoSR，输出 .glb）
- POST /art/segment       图像分割（SAM ViT-H，点/框提示 → mask PNG）
- POST /art/depth         深度估计（MiDaS-small ONNX → 伪彩色深度图）
- POST /art/detect        目标检测（YOLOv8-nano → 检测框 JSON）
- GET  /art/assets/3d/{filename}  3D 资产下载（.glb）
- GET  /art/tools/status  四引擎状态聚合（loaded/ready/degraded 如实上报）

输入约定：image 字段为 base64（允许 data:image/...;base64, 前缀）。
所有推理经 run_blocking（services/offload.py 唯一同步推理入口）防阻塞
事件循环；引擎权重缺失/加载失败如实返回 MODEL_FILE_NOT_FOUND /
MODEL_LOAD_FAILED，不伪造结果。
"""
from __future__ import annotations

import base64
import binascii
import io
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse

from ..config import DATA_DIR
from ..middleware.error_handler import ApiError, ok
from ..services.inference.depth_engine import get_depth_engine
from ..services.inference.detect_engine import get_detect_engine
from ..services.inference.segment_engine import get_segment_engine
from ..services.inference.triposr_engine import get_triposr_engine
from ..services.offload import run_blocking

router = APIRouter()
log = logging.getLogger("omnispace.api.vision_tools")

# 3D 资产输出目录（首用时创建）
ASSETS_3D_DIR = DATA_DIR / "assets" / "3d"


def _decode_image(data: str):
    """base64 / data-url → PIL.Image；解析失败返回 None。"""
    if not data or not isinstance(data, str):
        return None
    s = data.strip()
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    try:
        raw = base64.b64decode(s, validate=False)
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        img.load()
        return img.convert("RGB")
    except (binascii.Error, ValueError, OSError, Exception):
        return None


def _require_image(body: dict):
    img = _decode_image(str(body.get("image", "") or ""))
    if img is None:
        raise ApiError("SYSTEM_PARAM_INVALID", "缺少或无法解析 image 图像数据（base64）")
    return img


def _ensure_loaded(engine, name: str) -> None:
    """引擎就绪门控：未加载则尝试加载，失败如实抛语义码。"""
    if getattr(engine, "is_loaded", False):
        return
    if not engine.load_model():
        reason = getattr(engine, "unavailable_reason", "") or "未知原因"
        if "未找到" in reason or "不存在" in reason or "缺失" in reason:
            raise ApiError("MODEL_FILE_NOT_FOUND", f"{name}不可用：{reason}")
        raise ApiError("MODEL_LOAD_FAILED", f"{name}加载失败：{reason}")


# ── TripoSR 单图 3D 生成 ─────────────────────────────────────────
@router.post("/art/image-to-3d")
async def art_image_to_3d(body: dict = Body(default_factory=dict)):
    img = _require_image(body)
    mc_resolution = int(body.get("mc_resolution", 256) or 256)
    mc_resolution = max(64, min(mc_resolution, 512))
    engine = get_triposr_engine()
    _ensure_loaded(engine, "TripoSR 3D 生成引擎")
    ASSETS_3D_DIR.mkdir(parents=True, exist_ok=True)
    try:
        result = await run_blocking(
            engine.generate_3d, img, ASSETS_3D_DIR, mc_resolution)
    except RuntimeError as exc:
        raise ApiError("MODEL_INFERENCE_FAILED", f"3D 生成失败：{exc}") from exc
    glb_name = Path(result["glb_path"]).name
    return ok({
        "glb_url": f"/api/v1/art/assets/3d/{glb_name}",
        "vertices": result.get("vertices", 0),
        "faces": result.get("faces", 0),
        "elapsed_s": result.get("elapsed_s", 0.0),
        "backend": result.get("backend", "triposr"),
        "degraded": result.get("degraded", False),
    })


@router.get("/art/assets/3d/{filename}")
def art_asset_3d(filename: str):
    """3D 资产下载（防路径穿越：仅允许纯文件名）。"""
    safe = Path(filename).name
    if safe != filename or not safe.lower().endswith(".glb"):
        raise ApiError("SYSTEM_PARAM_INVALID", "非法资产文件名")
    path = ASSETS_3D_DIR / safe
    if not path.is_file():
        raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", "3D 资产不存在",
                       {"filename": safe})
    return FileResponse(str(path), media_type="model/gltf-binary",
                        filename=safe)


# ── SAM 分割 ─────────────────────────────────────────────────────
@router.post("/art/segment")
async def art_segment(body: dict = Body(default_factory=dict)):
    img = _require_image(body)
    points = body.get("points")  # [[x, y], ...]
    box = body.get("box")        # [x1, y1, x2, y2]
    engine = get_segment_engine()
    _ensure_loaded(engine, "SAM 分割引擎")
    try:
        result = await run_blocking(engine.segment, img, points, box)
    except RuntimeError as exc:
        raise ApiError("MODEL_INFERENCE_FAILED", f"图像分割失败：{exc}") from exc
    return ok(result)


# ── MiDaS 深度估计 ───────────────────────────────────────────────
@router.post("/art/depth")
async def art_depth(body: dict = Body(default_factory=dict)):
    img = _require_image(body)
    engine = get_depth_engine()
    _ensure_loaded(engine, "深度估计引擎")
    try:
        result = await run_blocking(engine.estimate, img)
    except RuntimeError as exc:
        raise ApiError("MODEL_INFERENCE_FAILED", f"深度估计失败：{exc}") from exc
    return ok(result)


# ── YOLOv8 目标检测 ──────────────────────────────────────────────
@router.post("/art/detect")
async def art_detect(body: dict = Body(default_factory=dict)):
    img = _require_image(body)
    conf = float(body.get("conf", 0.4) or 0.4)
    engine = get_detect_engine()
    _ensure_loaded(engine, "目标检测引擎")
    try:
        result = await run_blocking(engine.detect, img, conf)
    except RuntimeError as exc:
        raise ApiError("MODEL_INFERENCE_FAILED", f"目标检测失败：{exc}") from exc
    return ok(result)


# ── 状态聚合 ─────────────────────────────────────────────────────
@router.get("/art/tools/status")
def art_tools_status():
    """四引擎状态聚合（如实上报，不做可用性粉饰）。"""
    t0 = time.time()
    engines = {
        "triposr": get_triposr_engine(),
        "segment": get_segment_engine(),
        "depth": get_depth_engine(),
        "detect": get_detect_engine(),
    }
    return ok({
        "engines": {name: e.get_status() for name, e in engines.items()},
        "query_ms": round((time.time() - t0) * 1000, 1),
    })
