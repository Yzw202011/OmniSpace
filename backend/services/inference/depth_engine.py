"""OmniSpace AI v2.3.1 MiDaS 深度估计推理引擎。

使用 onnxruntime 加载本地 MiDaS small（models/depth/midas_small.onnx）：

- providers 优先 CUDAExecutionProvider，不可用时回退 CPUExecutionProvider
- 预处理遵循 midas_small 输入约定：ImageNet mean/std、NCHW float32，
  输入尺寸在加载时读取 ONNX 模型 input shape 决定（通常 256x256）
- 深度图归一化 0-255 → cv2.applyColorMap MAGMA 伪彩色（cv2 缺失时灰度兜底）→ PNG base64
- 依赖缺失 / 模型缺失 / 加载失败 → 状态 unavailable/error，
  unavailable_reason 给出中文原因，estimate() 失败抛 RuntimeError

单例用法::

    from backend.services.inference.depth_engine import get_depth_engine
    engine = get_depth_engine()
"""

from __future__ import annotations

import base64
import importlib
import io
import logging
import threading
import time
from typing import Any

from ...config import MODELS_DIR

logger = logging.getLogger("omnispace.inference.depth")


def _try_import(name: str) -> Any:
    """容错导入可选依赖。"""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


_ort = _try_import("onnxruntime")
_cv2 = _try_import("cv2")
_np = _try_import("numpy")

DEPTH_MODEL_PATH = MODELS_DIR / "depth" / "midas_small.onnx"

# ImageNet 归一化参数（MiDaS small ONNX 导出约定）
_IMAGENET_MEAN = (0.485, 0.456, 0.406)
_IMAGENET_STD = (0.229, 0.224, 0.225)

# 模型权重最小有效字节数（防 0 字节占位文件）
_MIN_MODEL_BYTES = 1024 * 1024


class DepthEngine:
    """深度估计推理引擎——MiDaS small（onnxruntime 后端）。

    状态机: unavailable -> unloaded -> ready / error（同 PaintEngine）。
    """

    def __init__(self) -> None:
        self._session: Any = None
        self._provider: str = ""
        self._input_name: str = ""
        self._input_size: tuple[int, int] = (256, 256)  # (height, width)
        self._state: str = "unavailable"
        self._last_error: str = ""
        self._lock = threading.Lock()
        # 推理串行锁：单会话实例，避免并发推理互相干扰
        self._infer_lock = threading.Lock()

        self._refresh_availability()

    # ── 可用性探测 ────────────────────────────────────────────────

    def _refresh_availability(self) -> None:
        if self._state == "ready":
            return
        if _ort is None or _np is None:
            self._state = "unavailable"
            return
        if DEPTH_MODEL_PATH.is_file() \
                and DEPTH_MODEL_PATH.stat().st_size > _MIN_MODEL_BYTES:
            self._state = "unloaded"
        else:
            self._state = "unavailable"

    @property
    def unavailable_reason(self) -> str:
        """引擎不可用的中文原因（就绪时为空串）。"""
        if self._state == "ready":
            return ""
        if _ort is None:
            return "onnxruntime 依赖不可用"
        if _np is None:
            return "numpy 依赖不可用"
        if not (DEPTH_MODEL_PATH.is_file()
                and DEPTH_MODEL_PATH.stat().st_size > _MIN_MODEL_BYTES):
            return f"深度模型未找到（{DEPTH_MODEL_PATH}），请先下载模型"
        if self._last_error:
            return self._last_error
        return "深度模型尚未加载"

    # ── 加载 / 卸载 ───────────────────────────────────────────────

    def load_model(self) -> bool:
        """加载 MiDaS small ONNX 推理会话。

        providers 优先 CUDAExecutionProvider，回退 CPUExecutionProvider。
        输入尺寸从模型 input shape 读取。失败收敛为状态，不抛异常。
        """
        with self._lock:
            if self._state == "ready":
                return True

            if _ort is None or _np is None:
                self._last_error = "onnxruntime/numpy 依赖不可用"
                self._state = "unavailable"
                logger.warning("深度引擎不可用: %s", self._last_error)
                return False

            if not (DEPTH_MODEL_PATH.is_file()
                    and DEPTH_MODEL_PATH.stat().st_size > _MIN_MODEL_BYTES):
                self._last_error = (
                    f"深度模型未找到（{DEPTH_MODEL_PATH}），请先下载模型"
                )
                self._state = "unavailable"
                logger.warning(self._last_error)
                return False

            try:
                available = _ort.get_available_providers()
                providers = [p for p in ("CUDAExecutionProvider",
                                         "CPUExecutionProvider")
                             if p in available]
                if not providers:
                    providers = ["CPUExecutionProvider"]

                logger.info("开始加载深度模型 <- %s (providers=%s)",
                            DEPTH_MODEL_PATH, providers)
                session = _ort.InferenceSession(str(DEPTH_MODEL_PATH),
                                                providers=providers)

                inp = session.get_inputs()[0]
                self._input_name = inp.name
                shape = inp.shape
                # 固定输入形状 [1, 3, H, W] 时按模型约定确定预处理尺寸
                if len(shape) == 4 and isinstance(shape[2], int) \
                        and isinstance(shape[3], int):
                    self._input_size = (int(shape[2]), int(shape[3]))

                self._session = session
                self._provider = session.get_providers()[0]
                self._state = "ready"
                self._last_error = ""
                logger.info("深度模型加载成功: %s (provider=%s, input=%s)",
                            DEPTH_MODEL_PATH.name, self._provider, shape)
                return True
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"深度模型加载失败: {exc}"
                self._state = "error"
                self._session = None
                logger.exception("深度模型加载失败")
                return False

    def unload_model(self) -> bool:
        """卸载推理会话。返回是否有模型被卸载。"""
        with self._lock:
            had = self._session is not None
            self._session = None
            self._provider = ""
            if had:
                self._state = "unloaded"
                logger.info("深度模型已卸载")
            return had

    # ── 推理 ──────────────────────────────────────────────────────

    def _preprocess(self, image: Any) -> Any:
        """PIL.Image → NCHW float32（按模型输入尺寸缩放 + ImageNet 归一化）。"""
        in_h, in_w = self._input_size
        img = image.convert("RGB").resize((in_w, in_h))
        arr = _np.asarray(img, dtype=_np.float32) / 255.0
        mean = _np.asarray(_IMAGENET_MEAN, dtype=_np.float32)
        std = _np.asarray(_IMAGENET_STD, dtype=_np.float32)
        arr = (arr - mean) / std
        return arr.transpose(2, 0, 1)[None, ...]  # HWC -> NCHW

    def _depth_to_png(self, depth: Any, out_wh: tuple[int, int]) -> bytes:
        """深度图 → 归一化 0-255 → MAGMA 伪彩色（或灰度）→ PNG bytes。"""
        d = depth.squeeze().astype(_np.float32)
        d_min, d_max = float(d.min()), float(d.max())
        span = d_max - d_min
        if span < 1e-6:
            norm = _np.zeros_like(d, dtype=_np.uint8)
        else:
            norm = ((d - d_min) / span * 255.0).astype(_np.uint8)

        if _cv2 is not None:
            resized = _cv2.resize(norm, out_wh,
                                  interpolation=_cv2.INTER_LINEAR)
            colored = _cv2.applyColorMap(resized, _cv2.COLORMAP_MAGMA)
            ok, buf = _cv2.imencode(".png", colored)
            if not ok:
                raise RuntimeError("深度图 PNG 编码失败")
            return buf.tobytes()

        # cv2 不可用：PIL 灰度兜底
        from PIL import Image
        gray = Image.fromarray(norm).resize(out_wh)
        out = io.BytesIO()
        gray.save(out, format="PNG")
        return out.getvalue()

    def estimate(self, image: Any) -> dict:
        """深度估计。

        Args:
            image: PIL.Image 输入图像

        Returns:
            {depth_png_b64, width, height, provider, elapsed_s,
             backend: "midas-small-onnx", degraded: False}

        Raises:
            RuntimeError: 引擎未就绪或推理失败
        """
        if self._state != "ready" or self._session is None:
            raise RuntimeError(self.unavailable_reason or "深度模型未就绪")

        orig_w, orig_h = image.size
        try:
            with self._infer_lock:
                blob = self._preprocess(image)
                start = time.perf_counter()
                outputs = self._session.run(None, {self._input_name: blob})
                elapsed = time.perf_counter() - start
            png_bytes = self._depth_to_png(outputs[0], (orig_w, orig_h))
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("深度估计推理失败")
            raise RuntimeError(f"深度估计推理失败: {exc}") from exc

        logger.info("深度估计完成: %dx%d provider=%s %.3fs",
                    orig_w, orig_h, self._provider, elapsed)
        return {
            "depth_png_b64": base64.b64encode(png_bytes).decode("utf-8"),
            "width": orig_w,
            "height": orig_h,
            "provider": self._provider,
            "elapsed_s": round(elapsed, 4),
            "backend": "midas-small-onnx",
            "degraded": False,
        }

    # ── 状态 ──────────────────────────────────────────────────────

    @property
    def is_loaded(self) -> bool:
        return self._state == "ready"

    @property
    def is_ready(self) -> bool:
        return self._state == "ready"

    def get_status(self) -> dict:
        return {
            "engine": "depth",
            "state": self._state,
            "loaded": self._state == "ready",
            "model": str(DEPTH_MODEL_PATH),
            "model_exists": DEPTH_MODEL_PATH.is_file(),
            "provider": self._provider,
            "available_providers":
                _ort.get_available_providers() if _ort is not None else [],
            "input_size": list(self._input_size),
            "has_onnxruntime": _ort is not None,
            "has_cv2": _cv2 is not None,
            "unavailable_reason": self.unavailable_reason,
            "last_error": self._last_error,
        }


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

_engine_instance: DepthEngine | None = None
_engine_lock = threading.Lock()


def get_depth_engine() -> DepthEngine:
    """获取深度估计引擎全局单例（线程安全双重检查）。"""
    global _engine_instance
    if _engine_instance is None:
        with _engine_lock:
            if _engine_instance is None:
                _engine_instance = DepthEngine()
    return _engine_instance
