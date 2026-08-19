"""OmniSpace AI v2.3.1 TripoSR 单图 3D 生成推理引擎。

使用 pydeps/triposr_src 内的 TripoSR 官方源码（包名 ``tsr``）加载本地权重
（models/3d/TripoSR，含 config.yaml + model.ckpt）：

- TSR.from_pretrained(权重目录, config_name="config.yaml", weight_name="model.ckpt")
- device 自动选择 cuda/cpu，CUDA 时 float16 半精度（16GB 显存安全）
- 推理流程：PIL 图像 → model([image]) 生成 scene_codes →
  model.extract_mesh(marching cubes) → trimesh 导出 .glb（文件名带时间戳）
- DINO-ViT 图像 tokenizer 仅需 facebook/dino-vitb16 的 config.json（权重
  来自 model.ckpt 的 state_dict）；离线环境下自动回退到本地缓存的等价配置，
  绝不因无法访问 HuggingFace 而加载失败
- 依赖（torch/tsr/omegaconf/einops/trimesh/transformers）或权重缺失时，
  load_model() 返回 False，unavailable_reason 给出中文原因，API 层友好降级

单例用法::

    from backend.services.inference.triposr_engine import get_triposr_engine
    engine = get_triposr_engine()
"""

from __future__ import annotations

import gc
import importlib
import json
import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ... import config as _config
from ...config import MODELS_DIR

logger = logging.getLogger("omnispace.inference.triposr")


def _try_import(name: str) -> Any:
    """容错导入可选依赖。"""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


# ── 路径推导（勿硬编码绝对路径）──────────────────────────────────
# config 模块位于 <root>/backend/config.py，其上一级目录即项目根。
_ROOT_DIR = Path(_config.__file__).resolve().parent.parent
TRIPOSR_SRC_DIR = _ROOT_DIR / "pydeps" / "triposr_src"
TRIPOSR_MODEL_DIR = MODELS_DIR / "3d" / "TripoSR"
TRIPOSR_CONFIG_NAME = "config.yaml"
TRIPOSR_WEIGHT_NAME = "model.ckpt"

# marching cubes 分块评估块大小（显存保护，同官方默认）
_MC_CHUNK_SIZE = 8192

# TripoSR 运行时必需的三方依赖（用于可用性探测与中文原因报告）
_REQUIRED_DEPS = ("torch", "omegaconf", "einops", "trimesh", "transformers",
                  "torchmcubes", "skimage")

# facebook/dino-vitb16 标准 ViT 配置（DINOSingleImageTokenizer 仅需
# config.json 构建 ViTModel 结构，权重由 model.ckpt 的 state_dict 覆盖）。
# 离线环境下写入本地缓存目录供 hf_hub_download 回退使用。
_DINO_REPO_ID = "facebook/dino-vitb16"
_DINO_CONFIG: dict[str, Any] = {
    "architectures": ["ViTModel"],
    "attention_probs_dropout_prob": 0.0,
    "hidden_act": "gelu",
    "hidden_dropout_prob": 0.0,
    "hidden_size": 768,
    "image_size": 224,
    "initializer_range": 0.02,
    "intermediate_size": 3072,
    "layer_norm_eps": 1e-12,
    "model_type": "vit",
    "num_attention_heads": 12,
    "num_channels": 3,
    "num_hidden_layers": 12,
    "patch_size": 16,
    "qkv_bias": True,
}


def ensure_triposr_src_on_path() -> bool:
    """把 pydeps/triposr_src 加入 sys.path（若不在）。返回目录是否存在。"""
    if not TRIPOSR_SRC_DIR.is_dir():
        return False
    p = str(TRIPOSR_SRC_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)
    return True


def triposr_weights_ready(model_dir: Path) -> bool:
    """TripoSR 权重目录是否可加载（config.yaml + model.ckpt 齐全）。"""
    if not (model_dir / TRIPOSR_CONFIG_NAME).is_file():
        return False
    ckpt = model_dir / TRIPOSR_WEIGHT_NAME
    return ckpt.is_file() and ckpt.stat().st_size > 1024 * 1024


def _write_dino_config_cache() -> str | None:
    """把 dino-vitb16 等价 config.json 写入权重目录下的本地缓存，返回路径。"""
    cache_dir = TRIPOSR_MODEL_DIR / "dino-vitb16-config"
    cfg_path = cache_dir / "config.json"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if not cfg_path.is_file():
            cfg_path.write_text(
                json.dumps(_DINO_CONFIG, indent=2), encoding="utf-8"
            )
        return str(cfg_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("DINO 本地配置缓存写入失败: %s", exc)
        return None


def _patch_dino_offline_fallback() -> None:
    """为 tsr 的 DINO tokenizer 打离线回退补丁。

    DINOSingleImageTokenizer.configure 通过 hf_hub_download 拉取
    facebook/dino-vitb16 的 config.json；离线环境下网络请求失败后，
    回退到本地缓存的等价配置（仅定义 ViT 结构，权重来自 model.ckpt）。
    """
    try:
        from tsr.models.tokenizers import image as _img_tok  # type: ignore
    except Exception:
        return
    original = _img_tok.hf_hub_download

    def _fallback_hf_hub_download(repo_id: str, filename: str, **kwargs):
        try:
            return original(repo_id=repo_id, filename=filename, **kwargs)
        except Exception:  # noqa: BLE001
            if repo_id == _DINO_REPO_ID and filename == "config.json":
                local = _write_dino_config_cache()
                if local:
                    logger.info("离线模式：DINO 配置使用本地缓存 %s", local)
                    return local
            raise

    _img_tok.hf_hub_download = _fallback_hf_hub_download


class TripoSREngine:
    """TripoSR 单图 3D 生成引擎（懒加载单例）。

    状态机: unavailable -> unloaded -> ready / error（同 PaintEngine）。
    __init__ 不加载权重，load_model() 显式加载。
    """

    def __init__(self) -> None:
        self._model: Any = None
        self._device: str = "cpu"
        self._dtype: Any = None
        self._state: str = "unavailable"
        self._last_error: str = ""
        self._lock = threading.Lock()
        # 推理串行锁：单 GPU 单模型实例，并发生成会叠加显存
        self._infer_lock = threading.Lock()

        self.last_elapsed_s: float = 0.0

        self._refresh_availability()

    # ── 可用性探测 ────────────────────────────────────────────────

    def _refresh_availability(self) -> None:
        if self._state == "ready":
            return
        if triposr_weights_ready(TRIPOSR_MODEL_DIR):
            self._state = "unloaded"
        else:
            self._state = "unavailable"

    def _missing_deps(self) -> list[str]:
        return [d for d in _REQUIRED_DEPS if _try_import(d) is None]

    # ── 加载 / 卸载 ───────────────────────────────────────────────

    def load_model(self) -> bool:
        """加载 TripoSR 模型。失败收敛为状态，不抛异常。"""
        with self._lock:
            if self._state == "ready":
                return True

            torch = _try_import("torch")
            missing = self._missing_deps()
            if missing:
                self._last_error = (
                    f"TripoSR 依赖缺失（{', '.join(missing)}），"
                    "请检查运行时环境"
                )
                self._state = "unavailable"
                logger.warning("TripoSR 引擎不可用: %s", self._last_error)
                return False

            if not ensure_triposr_src_on_path():
                self._last_error = (
                    "TripoSR 推理源码未找到（pydeps/triposr_src），"
                    "请检查安装包完整性"
                )
                self._state = "unavailable"
                logger.warning(self._last_error)
                return False

            if _try_import("tsr.system") is None:
                self._last_error = "TripoSR 源码包（tsr）导入失败，依赖可能不完整"
                self._state = "unavailable"
                logger.warning(self._last_error)
                return False

            if not triposr_weights_ready(TRIPOSR_MODEL_DIR):
                self._last_error = (
                    "TripoSR 权重未找到（models/3d/TripoSR/model.ckpt），"
                    "请先下载模型"
                )
                self._state = "unavailable"
                logger.warning(self._last_error)
                return False

            try:
                from tsr.system import TSR  # type: ignore

                self._device = "cuda" if torch.cuda.is_available() else "cpu"
                self._dtype = torch.float16 if self._device == "cuda" \
                    else torch.float32

                # DINO tokenizer 离线回退（仅 config.json，权重来自 ckpt）
                _patch_dino_offline_fallback()

                logger.info("开始加载 TripoSR 模型 <- %s (device=%s)",
                            TRIPOSR_MODEL_DIR, self._device)
                # 离线环境：from_pretrained 期间禁用 HF 网络重试（否则每次
                # 加载白等 ~40s 超时才走本地回退），完成后恢复原值
                import os as _os
                _prev_offline = _os.environ.get("HF_HUB_OFFLINE")
                _os.environ["HF_HUB_OFFLINE"] = "1"
                try:
                    model = TSR.from_pretrained(
                        str(TRIPOSR_MODEL_DIR),
                        config_name=TRIPOSR_CONFIG_NAME,
                        weight_name=TRIPOSR_WEIGHT_NAME,
                    )
                finally:
                    if _prev_offline is None:
                        _os.environ.pop("HF_HUB_OFFLINE", None)
                    else:
                        _os.environ["HF_HUB_OFFLINE"] = _prev_offline
                try:
                    model.renderer.set_chunk_size(_MC_CHUNK_SIZE)
                except Exception as exc:
                    logger.debug("chunk_size 设置跳过: %s", exc)
                model.to(self._device)
                if self._device == "cuda":
                    model = model.half()

                self._model = model
                self._state = "ready"
                self._last_error = ""
                logger.info("TripoSR 模型加载成功 (device=%s, dtype=%s)",
                            self._device, self._dtype)
                return True
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"TripoSR 模型加载失败: {exc}"
                self._state = "error"
                self._model = None
                logger.exception("TripoSR 模型加载失败")
                gc.collect()
                try:
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass
                return False

    def unload_model(self) -> bool:
        """卸载模型并释放显存。返回是否有模型被卸载。"""
        with self._lock:
            had = self._model is not None
            self._model = None
            if had:
                self._state = "unloaded"
            for _ in range(3):
                gc.collect()
            torch = _try_import("torch")
            if torch is not None:
                try:
                    if torch.cuda.is_available():
                        torch.cuda.synchronize()
                        torch.cuda.empty_cache()
                except Exception:
                    pass
            if had:
                logger.info("TripoSR 模型已卸载，显存已释放")
            return had

    # ── 推理 ──────────────────────────────────────────────────────

    def generate_3d(self, image: Any, output_dir: Path,
                    mc_resolution: int = 256) -> dict:
        """单图生成 3D 网格并导出 .glb。

        Args:
            image: PIL.Image 输入图像（任意模式，内部转 RGB）
            output_dir: .glb 输出目录（自动创建）
            mc_resolution: marching cubes 网格分辨率（默认 256）

        Returns:
            {glb_path, vertices, faces, elapsed_s,
             backend: "triposr", degraded: False}

        Raises:
            RuntimeError: 引擎未就绪或推理失败（带清晰原因，由 API 层
                          捕获转 4xx/5xx 信封）
        """
        if self._state != "ready" or self._model is None:
            raise RuntimeError(self._last_error or "TripoSR 模型未就绪")

        torch = _try_import("torch")
        mc_resolution = max(32, min(int(mc_resolution), 512))

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / (
            f"triposr_{datetime.now().strftime('%Y%m%d_%H%M%S')}.glb"
        )

        with self._infer_lock:
            start = time.perf_counter()
            try:
                rgb = image.convert("RGB")

                # CUDA 下半精度模型：tokenizer 输出为 float32，需 autocast
                # 统一混合精度上下文，避免 "expected Half but found Float"
                if self._device == "cuda":
                    with torch.no_grad(), torch.autocast(
                            "cuda", dtype=torch.float16):
                        scene_codes = self._model([rgb], device=self._device)
                        meshes = self._model.extract_mesh(
                            scene_codes, has_vertex_color=True,
                            resolution=mc_resolution,
                        )
                else:
                    with torch.no_grad():
                        scene_codes = self._model([rgb], device=self._device)
                        meshes = self._model.extract_mesh(
                            scene_codes, has_vertex_color=True,
                            resolution=mc_resolution,
                        )
                mesh = meshes[0]
                mesh.export(str(out_path))

                self.last_elapsed_s = time.perf_counter() - start
                logger.info(
                    "TripoSR 生成完成: %d 顶点 %d 面 res=%d %.1fs -> %s",
                    len(mesh.vertices), len(mesh.faces),
                    mc_resolution, self.last_elapsed_s, out_path)
                return {
                    "glb_path": str(out_path),
                    "vertices": int(len(mesh.vertices)),
                    "faces": int(len(mesh.faces)),
                    "elapsed_s": round(self.last_elapsed_s, 3),
                    "backend": "triposr",
                    "degraded": False,
                }
            except Exception as exc:  # noqa: BLE001
                logger.exception("TripoSR 3D 生成失败")
                raise RuntimeError(f"TripoSR 3D 生成失败: {exc}") from exc
            finally:
                try:
                    if torch is not None and torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass

    # ── 状态 ──────────────────────────────────────────────────────

    @property
    def is_loaded(self) -> bool:
        return self._state == "ready"

    @property
    def is_ready(self) -> bool:
        return self._state == "ready"

    @property
    def unavailable_reason(self) -> str:
        """引擎不可用时的中文原因（可用时为空串）。"""
        if self._state in ("ready", "unloaded"):
            return ""
        return self._last_error or "TripoSR 引擎不可用"

    def get_status(self) -> dict:
        return {
            "engine": "triposr",
            "state": self._state,
            "loaded": self._state == "ready",
            "device": self._device,
            "model_dir": str(TRIPOSR_MODEL_DIR),
            "weights_ready": triposr_weights_ready(TRIPOSR_MODEL_DIR),
            "src_ready": TRIPOSR_SRC_DIR.is_dir(),
            "missing_deps": self._missing_deps(),
            "last_error": self._last_error,
            "unavailable_reason": self.unavailable_reason,
            "last_elapsed_s": self.last_elapsed_s,
        }


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

_engine: TripoSREngine | None = None
_engine_lock = threading.Lock()


def get_triposr_engine() -> TripoSREngine:
    """获取 TripoSR 引擎全局单例（线程安全双重检查）。"""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = TripoSREngine()
    return _engine
