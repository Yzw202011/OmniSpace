"""OmniSpace AI v2.3 SAM 图像分割推理引擎。

使用 transformers 内置 SamModel / SamProcessor 加载本地 SAM ViT-H 权重
（models/sam-vit-h，含 model.safetensors + config.json + preprocessor_config.json），
支持点提示 / 框提示的交互式分割（真实模型推理，绝不伪造分割结果）。

降级策略（诚实降级）：
  - torch / transformers 依赖缺失、权重文件缺失或加载失败 → 状态 unavailable/error，
    load_model() 返回 False，unavailable_reason 给出中文原因
  - segment() 在引擎未就绪或推理异常时抛 RuntimeError（带清晰原因），
    由 API 层捕获并友好降级，绝不返回伪造 mask

单例用法::

    from src.services.inference.segment_engine import get_segment_engine
    engine = get_segment_engine()
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368

from __future__ import annotations

import base64
import importlib
import io
import logging
import threading
import time
from pathlib import Path
from typing import Any

from ...config import MODELS_DIR

log = logging.getLogger("omnispace.inference.segment")


def _try_import(name: str) -> Any:
    """容错导入可选依赖。"""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


# SAM 模型目录（相对 models/）
SAM_MODEL_REL_DIR = "sam-vit-h"
SAM_BACKEND_ID = "sam-vit-h"


def sam_model_dir_ready(model_dir: Path) -> bool:
    """SAM 权重目录是否可加载（config + preprocessor + 权重齐全）。"""
    if not (model_dir / "config.json").is_file():
        return False
    if not (model_dir / "preprocessor_config.json").is_file():
        return False
    for name in ("model.safetensors", "pytorch_model.bin"):
        f = model_dir / name
        if f.is_file() and f.stat().st_size > 1024 * 1024:
            return True
    return False


class SegmentEngine:
    """SAM 图像分割引擎——本地 SAM ViT-H（transformers 后端）。

    状态机: unavailable -> unloaded -> ready / error（同 PaintEngine）。
    懒加载：实例化不加载权重，须显式调用 load_model()。
    """

    def __init__(self) -> None:
        self._model: Any = None
        self._processor: Any = None
        self._device: str = "cpu"
        self._dtype: Any = None
        self._state: str = "unavailable"
        self._unavailable_reason: str = ""
        self._lock = threading.Lock()
        # 推理串行锁：单 GPU 单模型实例，并发推理叠加显存导致颠簸
        self._infer_lock = threading.Lock()

        self._refresh_availability()

    # ── 可用性探测 ────────────────────────────────────────────────

    def _refresh_availability(self) -> None:
        if self._state == "ready":
            return
        if sam_model_dir_ready(MODELS_DIR / SAM_MODEL_REL_DIR):
            self._state = "unloaded"
        else:
            self._state = "unavailable"
            self._unavailable_reason = (
                "SAM 分割模型未找到（models/sam-vit-h），请先下载模型"
            )

    # ── 加载 / 卸载 ───────────────────────────────────────────────

    def load_model(self) -> bool:
        """加载 SAM ViT-H 分割模型（SamModel + SamProcessor）。

        device auto：CUDA 可用时 float16 + cuda，否则 float32 + cpu。
        失败收敛为状态 + unavailable_reason，返回 False，不抛异常。
        """
        with self._lock:
            if self._state == "ready":
                return True

            torch = _try_import("torch")
            transformers = _try_import("transformers")
            if torch is None:
                self._unavailable_reason = "torch 依赖不可用"
                self._state = "unavailable"
                log.warning("分割引擎不可用: %s", self._unavailable_reason)
                return False
            SamModel = getattr(transformers, "SamModel", None) if transformers else None
            SamProcessor = getattr(transformers, "SamProcessor", None) \
                if transformers else None
            if SamModel is None or SamProcessor is None:
                self._unavailable_reason = (
                    "transformers 缺少 SamModel/SamProcessor，请检查 transformers 版本"
                )
                self._state = "unavailable"
                log.warning("分割引擎不可用: %s", self._unavailable_reason)
                return False

            path = MODELS_DIR / SAM_MODEL_REL_DIR
            if not sam_model_dir_ready(path):
                self._unavailable_reason = (
                    "SAM 分割模型未找到（models/sam-vit-h），请先下载模型"
                )
                self._state = "unavailable"
                log.warning(self._unavailable_reason)
                return False

            cuda_ok = bool(torch.cuda.is_available())
            self._device = "cuda" if cuda_ok else "cpu"
            self._dtype = torch.float16 if cuda_ok else torch.float32

            try:
                log.info("开始加载分割模型 %s <- %s (device=%s, dtype=%s)",
                            SAM_BACKEND_ID, path, self._device, self._dtype)
                processor = SamProcessor.from_pretrained(str(path))
                model = SamModel.from_pretrained(
                    str(path), torch_dtype=self._dtype
                ).to(self._device)
                model.eval()

                self._processor = processor
                self._model = model
                self._state = "ready"
                self._unavailable_reason = ""
                log.info("分割模型加载成功: %s", SAM_BACKEND_ID)
                return True
            except Exception as exc:  # noqa: BLE001
                self._unavailable_reason = f"SAM 分割模型加载失败: {exc}"
                self._state = "error"
                self._model = None
                self._processor = None
                log.exception("分割模型加载失败")
                try:
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    log.debug("load_model: 降级忽略", exc_info=True)
                return False

    def unload_model(self) -> bool:
        """卸载分割模型并释放显存。返回是否有模型被卸载。"""
        with self._lock:
            had = self._model is not None
            self._model = None
            self._processor = None
            if had:
                self._state = "unloaded"
            torch = _try_import("torch")
            if torch is not None:
                try:
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    log.debug("unload_model: 降级忽略", exc_info=True)
            if had:
                log.info("分割模型已卸载，显存已释放")
            return had

    # ── 推理 ──────────────────────────────────────────────────────

    def segment(self, image: Any,
                points: list[list[int]] | None = None,
                box: list[int] | None = None) -> dict:
        """对图像执行 SAM 交互式分割，返回得分最高的 mask。

        Args:
            image: PIL.Image 输入图像
            points: 前景点提示 [[x, y], ...]（labels 全 1）；可选
            box: 框提示 [x1, y1, x2, y2]；可选（与 points 可同时给出）
            两者都缺省时自动使用图像中心点

        Returns:
            {"mask_png_b64": str（单通道 PNG base64）, "score": float,
             "elapsed_s": float, "backend": "sam-vit-h", "degraded": False}

        Raises:
            RuntimeError: 引擎未就绪或推理失败（带清晰原因）
        """
        if self._state != "ready" or self._model is None \
                or self._processor is None:
            raise RuntimeError(
                self._unavailable_reason or "SAM 分割模型未就绪，请先调用 load_model()"
            )

        torch = _try_import("torch")
        start = time.perf_counter()
        try:
            with self._infer_lock:
                w, h = image.size
                if not points and not box:
                    points = [[w // 2, h // 2]]
                    log.debug("未提供提示，使用图像中心点 (%d, %d)", w // 2, h // 2)

                prompt_kwargs: dict[str, Any] = {}
                if points:
                    # (batch=1, point_batch=1, num_points, 2)，labels 全 1（前景点）
                    prompt_kwargs["input_points"] = [
                        [[int(p[0]), int(p[1])] for p in points]
                    ]
                    prompt_kwargs["input_labels"] = [[[1] * len(points)]]
                if box:
                    if len(box) != 4:
                        raise RuntimeError(
                            f"框提示格式错误：期望 [x1, y1, x2, y2]，实际 {box}"
                        )
                    # (batch=1, num_boxes=1, 4)
                    prompt_kwargs["input_boxes"] = [[list(box)]]

                inputs = self._processor(
                    image.convert("RGB"), return_tensors="pt", **prompt_kwargs
                )
                original_sizes = inputs["original_sizes"]
                reshaped_input_sizes = inputs["reshaped_input_sizes"]
                # 上设备：浮点张量随模型 dtype，整型张量保持整型
                inputs = {
                    k: (v.to(self._device, self._dtype)
                        if v.is_floating_point() else v.to(self._device))
                    for k, v in inputs.items()
                }

                with torch.no_grad():
                    outputs = self._model(**inputs)

                # (batch, point_batch, num_masks, H, W) → 原图尺寸
                masks = self._processor.image_processor.post_process_masks(
                    outputs.pred_masks.cpu(),
                    original_sizes,
                    reshaped_input_sizes,
                )
                # iou_scores: (batch, point_batch, num_masks) → 取得分最高的 mask
                scores = outputs.iou_scores[0, 0]
                best_idx = int(scores.argmax().item())
                score = float(scores[best_idx].item())
                mask_tensor = masks[0][0, best_idx]  # (H, W) bool

            # 单通道 PNG（0/255）→ base64
            mask_bytes = (mask_tensor.to(torch.uint8) * 255).numpy().tobytes()
            mh, mw = mask_tensor.shape[0], mask_tensor.shape[1]
            from PIL import Image as _PILImage
            mask_img = _PILImage.frombytes("L", (mw, mh), mask_bytes)
            buf = io.BytesIO()
            mask_img.save(buf, format="PNG")
            mask_png_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

            elapsed = time.perf_counter() - start
            log.info("分割完成: %dx%d score=%.4f %.2fs（prompt: points=%d, box=%s）",
                        w, h, score, elapsed,
                        len(points) if points else 0, "yes" if box else "no")
            return {
                "mask_png_b64": mask_png_b64,
                "score": score,
                "elapsed_s": round(elapsed, 3),
                "backend": SAM_BACKEND_ID,
                "degraded": False,
            }
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("分割推理失败")
            raise RuntimeError(f"分割推理失败: {exc}") from exc
        finally:
            if self._device == "cuda":
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    log.debug("segment: 降级忽略", exc_info=True)

    # ── 状态 ──────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        """引擎是否就绪。"""
        return self._state == "ready"

    @property
    def unavailable_reason(self) -> str:
        """不可用/失败的中文原因（就绪时为空串）。"""
        return self._unavailable_reason

    @property
    def model_name(self) -> str:
        """当前加载的模型名。"""
        return SAM_BACKEND_ID if self._state == "ready" else "none"

    def get_status(self) -> dict:
        """返回引擎状态。"""
        return {
            "engine": "segment",
            "backend": SAM_BACKEND_ID,
            "state": self._state,
            "loaded": self._state == "ready",
            "model_dir": str(MODELS_DIR / SAM_MODEL_REL_DIR),
            "model_ready": sam_model_dir_ready(MODELS_DIR / SAM_MODEL_REL_DIR),
            "device": self._device,
            "dtype": str(self._dtype) if self._dtype is not None else "",
            "unavailable_reason": self._unavailable_reason,
            "has_torch": _try_import("torch") is not None,
            "has_transformers": _try_import("transformers") is not None,
        }


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

_engine_instance: SegmentEngine | None = None
_engine_lock = threading.Lock()


def get_segment_engine() -> SegmentEngine:
    """获取分割引擎全局单例（线程安全双重检查）。"""
    global _engine_instance
    if _engine_instance is None:
        with _engine_lock:
            if _engine_instance is None:
                _engine_instance = SegmentEngine()
    return _engine_instance
