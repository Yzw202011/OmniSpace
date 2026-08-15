"""OmniSpace AI v2.3.1 YOLOv8 目标检测推理引擎。

使用 ultralytics 加载本地 YOLOv8n（models/detect/yolov8n.pt）：

- 推理 device 自动选择：CUDA 可用则 0，否则 cpu
- detect() 经 model.predict 推理并解析 boxes →
  class_id / class_name / conf / bbox[x1,y1,x2,y2]
- 依赖缺失 / 模型缺失 / 加载失败 → 状态 unavailable/error，
  unavailable_reason 给出中文原因，detect() 失败抛 RuntimeError

单例用法::

    from backend.services.inference.detect_engine import get_detect_engine
    engine = get_detect_engine()
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from typing import Any, Optional

from ...config import MODELS_DIR

logger = logging.getLogger("omnispace.inference.detect")


def _try_import(name: str) -> Any:
    """容错导入可选依赖。"""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


_torch = _try_import("torch")
_ultralytics = _try_import("ultralytics")

DETECT_MODEL_PATH = MODELS_DIR / "detect" / "yolov8n.pt"

# 模型权重最小有效字节数（防 0 字节占位文件）
_MIN_MODEL_BYTES = 1024 * 1024


class DetectEngine:
    """目标检测推理引擎——YOLOv8n（ultralytics 后端）。

    状态机: unavailable -> unloaded -> ready / error（同 PaintEngine）。
    """

    def __init__(self) -> None:
        self._model: Any = None
        self._device: str = "cpu"
        self._state: str = "unavailable"
        self._last_error: str = ""
        self._lock = threading.Lock()
        # 推理串行锁：单模型实例，避免并发推理互相干扰
        self._infer_lock = threading.Lock()

        self._refresh_availability()

    # ── 可用性探测 ────────────────────────────────────────────────

    def _refresh_availability(self) -> None:
        if self._state == "ready":
            return
        if _ultralytics is None:
            self._state = "unavailable"
            return
        if DETECT_MODEL_PATH.is_file() \
                and DETECT_MODEL_PATH.stat().st_size > _MIN_MODEL_BYTES:
            self._state = "unloaded"
        else:
            self._state = "unavailable"

    @property
    def unavailable_reason(self) -> str:
        """引擎不可用的中文原因（就绪时为空串）。"""
        if self._state == "ready":
            return ""
        if _ultralytics is None:
            return "ultralytics 依赖不可用"
        if not (DETECT_MODEL_PATH.is_file()
                and DETECT_MODEL_PATH.stat().st_size > _MIN_MODEL_BYTES):
            return f"检测模型未找到（{DETECT_MODEL_PATH}），请先下载模型"
        if self._last_error:
            return self._last_error
        return "检测模型尚未加载"

    # ── 加载 / 卸载 ───────────────────────────────────────────────

    def load_model(self) -> bool:
        """加载 YOLOv8n 检测模型。

        推理 device 自动选择：CUDA 可用则 0，否则 cpu。
        失败收敛为状态，不抛异常。
        """
        with self._lock:
            if self._state == "ready":
                return True

            if _ultralytics is None:
                self._last_error = "ultralytics 依赖不可用"
                self._state = "unavailable"
                logger.warning("检测引擎不可用: %s", self._last_error)
                return False

            if not (DETECT_MODEL_PATH.is_file()
                    and DETECT_MODEL_PATH.stat().st_size > _MIN_MODEL_BYTES):
                self._last_error = (
                    f"检测模型未找到（{DETECT_MODEL_PATH}），请先下载模型"
                )
                self._state = "unavailable"
                logger.warning(self._last_error)
                return False

            if _torch is not None and _torch.cuda.is_available():
                self._device = "0"
            else:
                self._device = "cpu"

            try:
                logger.info("开始加载检测模型 <- %s (device=%s)",
                            DETECT_MODEL_PATH, self._device)
                self._model = _ultralytics.YOLO(str(DETECT_MODEL_PATH))
                self._state = "ready"
                self._last_error = ""
                logger.info("检测模型加载成功: %s (device=%s)",
                            DETECT_MODEL_PATH.name, self._device)
                return True
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"检测模型加载失败: {exc}"
                self._state = "error"
                self._model = None
                logger.exception("检测模型加载失败")
                return False

    def unload_model(self) -> bool:
        """卸载检测模型。返回是否有模型被卸载。"""
        with self._lock:
            had = self._model is not None
            self._model = None
            if had:
                self._state = "unloaded"
                logger.info("检测模型已卸载")
            return had

    # ── 推理 ──────────────────────────────────────────────────────

    def detect(self, image: Any, conf: float = 0.4) -> dict:
        """目标检测。

        Args:
            image: PIL.Image 输入图像
            conf: 置信度阈值（默认 0.4）

        Returns:
            {detections: [{class_id, class_name, conf,
                           bbox: [x1, y1, x2, y2]}],
             count, elapsed_s, backend: "yolov8n", degraded: False}

        Raises:
            RuntimeError: 引擎未就绪或推理失败
        """
        if self._state != "ready" or self._model is None:
            raise RuntimeError(self.unavailable_reason or "检测模型未就绪")

        conf = max(0.05, min(float(conf), 1.0))
        try:
            with self._infer_lock:
                start = time.perf_counter()
                results = self._model.predict(
                    source=image.convert("RGB"),
                    conf=conf,
                    device=self._device,
                    verbose=False,
                )
                elapsed = time.perf_counter() - start

            detections: list[dict] = []
            if results:
                result = results[0]
                names = result.names or {}
                boxes = getattr(result, "boxes", None)
                if boxes is not None and len(boxes) > 0:
                    xyxy = boxes.xyxy.tolist()
                    cls_ids = boxes.cls.tolist()
                    confs = boxes.conf.tolist()
                    for (x1, y1, x2, y2), cls_id, score in zip(
                            xyxy, cls_ids, confs):
                        cid = int(cls_id)
                        detections.append({
                            "class_id": cid,
                            "class_name": str(names.get(cid, cid)),
                            "conf": round(float(score), 4),
                            "bbox": [round(float(x1), 1), round(float(y1), 1),
                                     round(float(x2), 1), round(float(y2), 1)],
                        })
        except Exception as exc:  # noqa: BLE001
            logger.exception("目标检测推理失败")
            raise RuntimeError(f"目标检测推理失败: {exc}") from exc

        logger.info("目标检测完成: %d 个目标 device=%s %.3fs",
                    len(detections), self._device, elapsed)
        return {
            "detections": detections,
            "count": len(detections),
            "elapsed_s": round(elapsed, 4),
            "backend": "yolov8n",
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
            "engine": "detect",
            "state": self._state,
            "loaded": self._state == "ready",
            "model": str(DETECT_MODEL_PATH),
            "model_exists": DETECT_MODEL_PATH.is_file(),
            "device": self._device,
            "cuda_available": bool(_torch is not None
                                   and _torch.cuda.is_available()),
            "has_ultralytics": _ultralytics is not None,
            "unavailable_reason": self.unavailable_reason,
            "last_error": self._last_error,
        }


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

_engine_instance: Optional[DetectEngine] = None
_engine_lock = threading.Lock()


def get_detect_engine() -> DetectEngine:
    """获取目标检测引擎全局单例（线程安全双重检查）。"""
    global _engine_instance
    if _engine_instance is None:
        with _engine_lock:
            if _engine_instance is None:
                _engine_instance = DetectEngine()
    return _engine_instance
