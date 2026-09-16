"""视觉工具 API 路由（B3 幻影清理 2026-09-13：/art 收敛为单端点）。

端点清单（收敛后）：
- POST /art/segment       图像分割（SAM ViT-H，点/框提示 → mask PNG）
                          ——漫画分格链唯一在役消费方（comic_gen 遮罩）

历史端点与收敛理由（零前端消费 + 权重不在盘，B3 调用面对账见
docs/全量技术评估总汇总与修复总方案-v4 §八）：
- /art/image-to-3d、/art/assets/3d/{filename}  TripoSR 3D（3D 已裁定剔除，
  权重已入隔离区）；引擎文件已随 B3 摘除
- /art/depth、/art/detect                      MiDaS/YOLO（权重目录不存在，
  一调必 MODEL_FILE_NOT_FOUND；引擎文件已随 B3 摘除）
- /art/tools/status                            随四引擎聚合一并移除

输入约定：image 字段为 base64（允许 data:image/...;base64, 前缀）。
推理经 run_blocking（services/offload.py 唯一同步推理入口）防阻塞
事件循环；权重缺失/加载失败如实返回语义码，不伪造结果。
"""
from __future__ import annotations

import base64
import binascii
import io
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body

from ..middleware.error_handler import ApiError, ok
from ..services.inference.segment_engine import get_segment_engine
from ..services.offload import run_blocking

if TYPE_CHECKING:
    from PIL import Image

router = APIRouter()
log = logging.getLogger("omnispace.api.vision_tools")


def _decode_image(data: str) -> Image | None:
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


def _require_image(body: dict) -> Image:
    img = _decode_image(str(body.get("image", "") or ""))
    if img is None:
        raise ApiError("SYSTEM_PARAM_INVALID", "缺少或无法解析 image 图像数据（base64）")
    return img


def _ensure_loaded(engine: Any, name: str) -> None:
    """引擎就绪门控：未加载则尝试加载，失败如实抛语义码。"""
    if getattr(engine, "is_loaded", False):
        return
    if not engine.load_model():
        reason = getattr(engine, "unavailable_reason", "") or "未知原因"
        if "未找到" in reason or "不存在" in reason or "缺失" in reason:
            raise ApiError("MODEL_FILE_NOT_FOUND", f"{name}不可用：{reason}")
        raise ApiError("MODEL_LOAD_FAILED", f"{name}加载失败：{reason}")


# ── SAM 分割（唯一在役）──────────────────────────────────────────
@router.post("/art/segment")
async def art_segment(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
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
# 本项目仅供学习使用，商业授权请+Q 3559331368
