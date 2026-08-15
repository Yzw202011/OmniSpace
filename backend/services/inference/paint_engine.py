"""OmniSpace AI v2.3 绘画推理引擎（TASK-006 真实推理实现）。

使用 diffusers 加载本地 SDXL base 1.0（models/paint/sdxl-base-1.0）：

- StableDiffusionXLPipeline.from_pretrained(torch_dtype=float16, variant=fp16 若存在,
  use_safetensors=True)
- 16GB 显存安全: enable_model_cpu_offload()（accelerate 可用时）+ VAE slicing/tiling
- sampler 映射: euler_a / dpm++_2m / ddim
- seed=-1 时用 secrets 生成真随机种子并记录，可复现固定种子
- callback_on_step_end 进度回报（注入的 progress callback）
- img2img 复用 txt2img 组件（from_pipe，不重复占显存）
- upscale: 有 Real-ESRGAN 用真，无则 PIL LANCZOS 2x/4x 并标注 degraded
- 结果落盘 data/generated/images/（file_store），写 SQLite 表 paint_history
- 加载失败 → 状态 unavailable/error，API 层友好降级，绝不硬 OOM

单例用法::

    from backend.services.inference.paint_engine import get_paint_engine
    engine = get_paint_engine()
"""

from __future__ import annotations

import base64
import gc
import importlib
import io
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ...config import MODELS_DIR

logger = logging.getLogger("omnispace.inference.paint")


class PaintCancelledError(Exception):
    """绘画任务取消信号（API 层进度回调抛出，引擎层不吞、向上传播）。"""


def _try_import(name: str) -> Any:
    """容错导入可选依赖。"""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


# ── 绘画模型注册表 ────────────────────────────────────────────────
# model_id -> (相对 models/ 的目录, 需求显存 GB)
PAINT_MODEL_CANDIDATES: list[tuple[str, str, float]] = [
    ("sdxl-base-1.0", "paint/sdxl-base-1.0", 7.0),
]

# sampler 名称 -> (diffusers 调度器类名, 额外 kwargs)
SAMPLER_MAP: dict[str, tuple[str, dict]] = {
    "euler_a":   ("EulerAncestralDiscreteScheduler", {}),
    "euler":     ("EulerDiscreteScheduler", {}),
    "dpm++_2m":  ("DPMSolverMultistepScheduler", {}),
    "dpm++_2m_karras": ("DPMSolverMultistepScheduler", {"use_karras_sigmas": True}),
    "ddim":      ("DDIMScheduler", {}),
}
DEFAULT_SAMPLER = "euler_a"

DEFAULT_NEGATIVE = (
    "lowres, bad anatomy, bad hands, missing fingers, extra fingers, "
    "blurry, watermark, text, logo, cropped, worst quality, jpeg artifacts"
)

_MAX_SEED = 2 ** 31 - 1

# 低显存降级阈值（GB）：空闲显存低于理想需求但 ≥ 此值时，
# 使用 sequential_cpu_offload 加载（速度换可用性）。
LOW_VRAM_FALLBACK_GB = 4.0

# paint_history 建表 DDL（自建表，CREATE IF NOT EXISTS 幂等）
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


def resolve_seed(seed: int) -> int:
    """解析随机种子：-1（或负数）时用密码学随机源生成真随机种子。"""
    if seed is not None and seed >= 0:
        return int(seed)
    return secrets.randbelow(_MAX_SEED)


def _cuda_free_gb() -> float:
    """当前 GPU 空闲显存（GB）；无 CUDA 时返回 0。"""
    torch = _try_import("torch")
    if torch is None or not torch.cuda.is_available():
        return 0.0
    try:
        free, _total = torch.cuda.mem_get_info()
        return free / (1024 ** 3)
    except Exception:
        return 0.0


def _release_cuda_memory() -> None:
    """彻底释放 CUDA 显存：多轮 gc（拆引用环）+ 清空缓存 + 同步 + IPC 回收。"""
    for _ in range(3):
        gc.collect()
    torch = _try_import("torch")
    if torch is None:
        return
    try:
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            try:
                torch.cuda.ipc_collect()
            except Exception:
                pass
    except Exception:
        pass


def paint_model_dir_ready(model_dir: Path) -> bool:
    """SDXL diffusers 目录是否可加载（model_index.json + unet 权重齐全）。"""
    if not (model_dir / "model_index.json").is_file():
        return False
    unet = model_dir / "unet"
    for name in ("diffusion_pytorch_model.fp16.safetensors",
                 "diffusion_pytorch_model.safetensors"):
        f = unet / name
        if f.is_file() and f.stat().st_size > 1024 * 1024:
            return True
    # 单文件格式兜底（sd_xl_base_1.0.safetensors）
    single = model_dir / "sd_xl_base_1.0.safetensors"
    return single.is_file() and single.stat().st_size > 1024 * 1024


def get_sampler_class(name: str) -> tuple[Any, dict]:
    """根据 sampler 名返回 (调度器类, kwargs)；未知名回退 euler_a。

    diffusers 不可用时返回 (None, {})。
    """
    diffusers = _try_import("diffusers")
    if diffusers is None:
        return None, {}
    cls_name, kwargs = SAMPLER_MAP.get((name or "").lower(),
                                       SAMPLER_MAP[DEFAULT_SAMPLER])
    cls = getattr(diffusers, cls_name, None)
    if cls is None:  # 类缺失时回退默认
        cls_name, kwargs = SAMPLER_MAP[DEFAULT_SAMPLER]
        cls = getattr(diffusers, cls_name, None)
    return cls, dict(kwargs)


def apply_sampler(pipe: Any, name: str) -> str:
    """把指定采样器应用到 pipeline，返回实际生效的 sampler 名。"""
    cls, kwargs = get_sampler_class(name)
    if cls is None:
        return name or DEFAULT_SAMPLER
    try:
        pipe.scheduler = cls.from_config(pipe.scheduler.config, **kwargs)
    except Exception as exc:
        logger.warning("采样器 %s 应用失败，沿用默认: %s", name, exc)
        return DEFAULT_SAMPLER
    key = (name or "").lower()
    return key if key in SAMPLER_MAP else DEFAULT_SAMPLER


def _broadcast_quality_reduced(requested_steps: int, actual_steps: int) -> None:
    """WS 广播降参事件（quality_degraded，F-10：启动时降步数）。

    广播失败仅记日志，绝不影响生成主流程。
    """
    try:
        from ..ws_hub import get_ws_hub
        get_ws_hub().broadcast({
            "type": "quality_degraded",
            "data": {"module": "paint",
                     "reason": "gpu_util_sustained_critical",
                     "requested_steps": requested_steps,
                     "actual_steps": actual_steps},
        })
    except Exception as exc:  # noqa: BLE001
        logger.debug("降参事件广播失败（忽略）: %s", exc)


class PaintEngine:
    """绘画推理引擎——本地 SDXL base 1.0（diffusers 后端）。

    状态机: unavailable -> unloaded -> ready / error（同 DialogEngine）。
    """

    def __init__(self) -> None:
        self._pipe: Any = None            # txt2img 管线
        self._pipe_i2i: Any = None        # img2img 管线（from_pipe 共享组件）
        self._model_id: str = ""
        self._model_dir: Optional[Path] = None
        self._sampler: str = DEFAULT_SAMPLER
        self._state: str = "unavailable"
        self._last_error: str = ""
        self._lock = threading.Lock()
        # 推理串行锁：单 GPU 单管线实例，并发生成会叠加显存导致颠簸，
        # 功能锁同功能可重入（规格 §6.1），故在引擎层串行化推理。
        self._infer_lock = threading.Lock()

        # 推理统计
        self.last_seed: int = -1
        self.last_elapsed_ms: float = 0.0

        self._refresh_availability()

    # ── 可用性探测 ────────────────────────────────────────────────

    def _refresh_availability(self) -> None:
        if self._state == "ready":
            return
        for _mid, rel, _vram in PAINT_MODEL_CANDIDATES:
            if paint_model_dir_ready(MODELS_DIR / rel):
                self._state = "unloaded"
                return
        self._state = "unavailable"

    def available_models(self) -> list[str]:
        return [mid for mid, rel, _v in PAINT_MODEL_CANDIDATES
                if paint_model_dir_ready(MODELS_DIR / rel)]

    def _pick_model(self, model_id: Optional[str]) -> Optional[tuple[str, Path, float]]:
        for mid, rel, vram in PAINT_MODEL_CANDIDATES:
            if model_id and model_id != mid and not model_id.startswith("sdxl"):
                continue
            path = MODELS_DIR / rel
            if paint_model_dir_ready(path):
                return mid, path, vram
        return None

    # ── 显存协调 ──────────────────────────────────────────────────

    def _try_free_vram(self, required_gb: float) -> float:
        """显存不足时尝试腾挪：先 model_manager 契约，再直接卸载对话引擎。"""
        free = _cuda_free_gb()
        if free >= required_gb:
            return free

        try:
            from ..model_manager import get_model_manager  # type: ignore
            mgr = get_model_manager()
            # 卸载管理器记账中的冲突类别模型（dialog/video_gen 与 paint 互斥）
            for entry in mgr.get_loaded_models():
                if entry.get("category") in ("dialog", "language",
                                             "video", "video_gen"):
                    logger.info("经 model_manager 卸载冲突模型: %s",
                                entry.get("model_id"))
                    mgr.unload_model(entry["model_id"])
            # 审计 R2-C01：循环驱逐最低优先级直到满足或无可驱逐
            # （多类别驻留时驱逐一个可能仍不足）。
            free = _cuda_free_gb()
            while free < required_gb and mgr.get_loaded_models():
                if not mgr.evict_lowest_priority():
                    break
                free = _cuda_free_gb()
        except Exception as exc:
            logger.debug("model_manager 腾显存不可用: %s", exc)

        try:
            from .dialog_engine import get_dialog_engine
            dialog = get_dialog_engine()
            if getattr(dialog, "is_loaded", False):
                logger.info("显存不足（%.1fGB < %.1fGB），卸载对话引擎腾挪",
                            free, required_gb)
                dialog.unload_model()
        except Exception as exc:
            logger.debug("对话引擎卸载不可用: %s", exc)

        _release_cuda_memory()
        return _cuda_free_gb()

    def check_vram(self, required_gb: float) -> tuple[bool, float]:
        free = _cuda_free_gb()
        if free >= required_gb:
            return True, free
        free = self._try_free_vram(required_gb)
        return free >= required_gb, free

    # ── 加载 / 卸载 ───────────────────────────────────────────────

    def load_model(self, model_id: Optional[str] = None) -> bool:
        """加载 SDXL 绘画管线。

        流程: 选模型 → 显存预检（不足尝试腾挪）→ diffusers 加载 →
        cpu offload / vae slicing 显存保护。失败收敛为状态，不抛异常。
        """
        with self._lock:
            if self._state == "ready":
                return True

            torch = _try_import("torch")
            diffusers = _try_import("diffusers")
            if torch is None or diffusers is None:
                self._last_error = "torch/diffusers 依赖不可用"
                self._state = "unavailable"
                logger.warning("绘画引擎不可用: %s", self._last_error)
                return False

            pick = self._pick_model(model_id)
            if pick is None:
                self._last_error = "绘画模型未找到（models/paint/sdxl-base-1.0），请先下载模型"
                self._state = "unavailable"
                logger.warning(self._last_error)
                return False

            mid, path, required_gb = pick

            if not torch.cuda.is_available():
                self._last_error = "未检测到 CUDA GPU，无法加载绘画模型"
                self._state = "error"
                logger.warning(self._last_error)
                return False

            ok_vram, free_gb = self.check_vram(required_gb)
            low_vram_mode = False
            if not ok_vram:
                # 显存不满足理想值时，若仍有 ≥4GB 空闲，允许以
                # sequential_cpu_offload 降级加载（速度换可用性）。
                if free_gb >= LOW_VRAM_FALLBACK_GB:
                    low_vram_mode = True
                    logger.info(
                        "显存偏紧（空闲 %.1fGB < 理想 %.0fGB），"
                        "使用 sequential_cpu_offload 低显存模式加载",
                        free_gb, required_gb)
                else:
                    self._last_error = (
                        f"显存不足：空闲 {free_gb:.1f}GB，需求约 {required_gb:.0f}GB"
                    )
                    self._state = "error"
                    logger.warning(self._last_error)
                    return False

            try:
                logger.info("开始加载绘画模型 %s <- %s", mid, path)
                sdxl_cls = diffusers.StableDiffusionXLPipeline

                # 优先 fp16 variant（component 级回退由 diffusers 处理）
                fp16_unet = (path / "unet"
                             / "diffusion_pytorch_model.fp16.safetensors").is_file()
                kwargs: dict[str, Any] = {
                    "torch_dtype": torch.float16,
                    "use_safetensors": True,
                }
                if fp16_unet:
                    kwargs["variant"] = "fp16"

                if (path / "model_index.json").is_file():
                    pipe = sdxl_cls.from_pretrained(str(path), **kwargs)
                else:
                    # 单文件兜底
                    pipe = sdxl_cls.from_single_file(
                        str(path / "sd_xl_base_1.0.safetensors"),
                        torch_dtype=torch.float16,
                        use_safetensors=True,
                    )

                # 16GB 显存保护策略：
                # - 低显存模式：sequential_cpu_offload（最低 ~4GB 可跑，较慢）
                # - 常规模式：model_cpu_offload（accelerate 可用时）
                # - 兜底：整管线上 GPU + vae slicing
                offload_done = False
                try:
                    if _try_import("accelerate") is not None:
                        if low_vram_mode:
                            pipe.enable_sequential_cpu_offload()
                        else:
                            pipe.enable_model_cpu_offload()
                        offload_done = True
                except Exception as exc:
                    logger.warning("offload 启用失败，尝试整管线上 GPU: %s", exc)
                if not offload_done:
                    try:
                        pipe = pipe.to("cuda")
                    except Exception:
                        pass
                for meth in ("enable_vae_slicing", "enable_vae_tiling"):
                    try:
                        getattr(pipe, meth)()
                    except Exception:
                        pass

                self._pipe = pipe
                self._pipe_i2i = None  # 懒加载
                self._model_id = mid
                self._model_dir = path
                self._state = "ready"
                self._last_error = ""
                logger.info("绘画模型加载成功: %s", mid)
                return True
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"绘画模型加载失败: {exc}"
                self._state = "error"
                self._pipe = None
                self._pipe_i2i = None
                logger.exception("绘画模型加载失败")
                gc.collect()
                try:
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass
                return False

    def unload_model(self) -> bool:
        """卸载绘画管线并释放显存。返回是否有模型被卸载。"""
        with self._lock:
            had = self._pipe is not None
            self._pipe = None
            self._pipe_i2i = None
            self._model_id = ""
            self._model_dir = None
            if had:
                self._state = "unloaded"
            _release_cuda_memory()
            if had:
                logger.info("绘画模型已卸载，显存已释放（空闲 %.1fGB）",
                            _cuda_free_gb())
            return had

    def ensure_loaded(self, model_id: Optional[str] = None) -> bool:
        """确保绘画模型已加载（先走 model_manager 契约协调）。"""
        if self._state == "ready":
            return True
        try:
            from ..model_manager import get_model_manager  # type: ignore
            mgr = get_model_manager()
            ensure = getattr(mgr, "ensure_loaded", None)
            if callable(ensure):
                try:
                    ensure("paint", model_id or "sdxl-base-1.0")
                except Exception as exc:
                    logger.debug("model_manager.ensure_loaded 调用失败: %s", exc)
        except Exception:
            pass
        return self.load_model(model_id)

    def _get_img2img_pipe(self) -> Any:
        """懒加载 img2img 管线：from_pipe 复用 txt2img 全部组件（零额外显存）。"""
        if self._pipe_i2i is None:
            diffusers = _try_import("diffusers")
            i2i_cls = getattr(diffusers, "StableDiffusionXLImg2ImgPipeline", None)
            if i2i_cls is None:
                raise RuntimeError("diffusers 缺少 StableDiffusionXLImg2ImgPipeline")
            self._pipe_i2i = i2i_cls.from_pipe(self._pipe)
        return self._pipe_i2i

    # ── 进度回调 ──────────────────────────────────────────────────

    @staticmethod
    def _make_step_callback(progress_cb: Optional[Callable[[int, int], None]],
                            total_steps: int, watch: dict):
        """构造 diffusers callback_on_step_end 回调。

        progress_cb(percent, step) —— percent 0~100，step 为当前步（1 起）。
        watch 为本次生成的可变观测 dict（last_step）。
        F-10 降参在任务入口按旗标降步数（见 _apply_governor_steps），
        回调只做进度上报，不做在途中断。
        """

        def _cb(pipe, step_index: int, timestep, callback_kwargs):
            current = step_index + 1
            watch["last_step"] = current
            # F-10 降参已上移至任务启动时降步数（generate/img2img 入口），
            # 采样中途绝不腰斩——euler_a 祖先采样器中途停止时 latent 残留
            # 大量注入噪声，解码即全屏噪点（2026-08-14 实测确认）。
            if progress_cb is not None:
                percent = int(min(99, current * 100 / max(1, total_steps)))
                try:
                    progress_cb(percent, current)
                except PaintCancelledError:
                    raise  # 取消信号不吞：中断推理向上传播
                except Exception:
                    pass
            return callback_kwargs

        return _cb

    # ── 质量总督（F-10）入口降步 ─────────────────────────────────

    @staticmethod
    def _apply_governor_steps(steps: int, watch: dict) -> int:
        """F-10 降参（启动时）：命中旗标则 steps ×= steps_factor（≥12 步）。

        设计依据：SDXL 利用率 >95% 持续 10s 的旗标在"自身就是高负载
        生成"时必然误触发；且 euler_a 祖先采样器每步注入新噪声，中途
        腰斩必产全屏噪点。故降参改为任务启动时减少步数——采样器跑
        完整调度（sigma→0），图像收敛，仅细节量略减（诚实降级，
        元数据标注 quality_reduced/requested_steps/actual_steps）。
        """
        try:
            from ..quality_governor import get_quality_governor
            gov = get_quality_governor()
            if not gov.should_reduce():
                return steps
            reduced = max(12, int(round(steps * gov.steps_factor)))
            if reduced >= steps:
                return steps
            watch["reduced"] = True
            watch["requested_steps"] = steps
            logger.info("质量总督降步: 原 %d 步 -> %d 步（GPU 持续高负载）",
                        steps, reduced)
            _broadcast_quality_reduced(steps, reduced)
            return reduced
        except Exception:
            return steps

    # ── 生成 ──────────────────────────────────────────────────────

    def generate(self, params: dict,
                 progress_cb: Optional[Callable[[int, int], None]] = None) -> dict:
        """文生图。

        Args:
            params: {prompt, negative, steps=30, cfg=7.5, width=1024, height=1024,
                     sampler="euler_a", seed=-1, batch_size=1}
            progress_cb: 进度回调 (percent, step)

        Returns:
            {images: [PIL.Image], seed, sampler, model, elapsed_ms}

        Raises:
            RuntimeError: 引擎未就绪
        """
        if self._state != "ready" or self._pipe is None:
            raise RuntimeError(self._last_error or "绘画模型未就绪")

        torch = _try_import("torch")

        prompt = (params.get("prompt") or "").strip()
        negative = params.get("negative") or params.get("negative_prompt") \
            or DEFAULT_NEGATIVE
        steps = int(params.get("steps") or 30)
        cfg = float(params.get("cfg") or params.get("cfg_scale")
                    or params.get("guidance_scale") or 7.5)
        width = int(params.get("width") or 1024)
        height = int(params.get("height") or 1024)
        batch = max(1, min(int(params.get("batch_size") or 1), 4))
        sampler = params.get("sampler") or DEFAULT_SAMPLER
        seed = resolve_seed(int(params.get("seed", -1)))

        with self._infer_lock:
            self._sampler = apply_sampler(self._pipe, sampler)

            generator = torch.Generator(device="cuda").manual_seed(seed)
            # F-10 降参（启动时）：命中旗标降步数，采样跑完整调度
            watch: dict = {"reduced": False, "last_step": 0,
                           "requested_steps": steps}
            steps = self._apply_governor_steps(steps, watch)
            cb = self._make_step_callback(progress_cb, steps, watch)

            call_kwargs: dict[str, Any] = dict(
                prompt=prompt,
                negative_prompt=negative,
                width=width,
                height=height,
                num_inference_steps=steps,
                guidance_scale=cfg,
                num_images_per_prompt=batch,
                generator=generator,
            )
            call_kwargs["callback_on_step_end"] = cb
            call_kwargs["callback_on_step_end_tensor_inputs"] = ["latents"]

            start = time.perf_counter()
            result = self._pipe(**call_kwargs)
            self.last_elapsed_ms = (time.perf_counter() - start) * 1000
            self.last_seed = seed

        if progress_cb is not None:
            try:
                progress_cb(100, steps)
            except Exception:
                pass

        if watch["reduced"]:
            logger.info("文生图降参完成: 原 %d 步实际 %d 步 seed=%d",
                        watch.get("requested_steps", steps), steps, seed)
        else:
            logger.info("文生图完成: %dx%d %d步 seed=%d %.0fms",
                        width, height, steps, seed, self.last_elapsed_ms)
        return {
            "images": list(result.images),
            "seed": seed,
            "sampler": self._sampler,
            "model": self._model_id,
            "elapsed_ms": self.last_elapsed_ms,
            # F-10 降参元数据：命中标注 quality_reduced + 原/实际步数
            "quality_reduced": bool(watch["reduced"]),
            "requested_steps": watch.get("requested_steps") or steps,
            "actual_steps": steps,
        }

    def img2img(self, params: dict, init_image: Any,
                progress_cb: Optional[Callable[[int, int], None]] = None) -> dict:
        """图生图。

        Args:
            params: 同 generate，另支持 strength/denoising_strength（默认 0.75）
            init_image: PIL 图片
            progress_cb: 进度回调
        """
        if self._state != "ready" or self._pipe is None:
            raise RuntimeError(self._last_error or "绘画模型未就绪")

        torch = _try_import("torch")
        pipe = self._get_img2img_pipe()

        prompt = (params.get("prompt") or "").strip()
        negative = params.get("negative") or params.get("negative_prompt") \
            or DEFAULT_NEGATIVE
        steps = int(params.get("steps") or 30)
        cfg = float(params.get("cfg") or params.get("cfg_scale")
                    or params.get("guidance_scale") or 7.5)
        strength = float(params.get("strength")
                         or params.get("denoising_strength") or 0.75)
        strength = max(0.05, min(strength, 1.0))
        sampler = params.get("sampler") or DEFAULT_SAMPLER
        seed = resolve_seed(int(params.get("seed", -1)))

        apply_sampler(pipe, sampler)
        generator = torch.Generator(device="cuda").manual_seed(seed)
        # F-10 降参（启动时）：命中旗标降步数，采样跑完整调度
        watch: dict = {"reduced": False, "last_step": 0,
                       "requested_steps": steps}
        steps = self._apply_governor_steps(steps, watch)
        cb = self._make_step_callback(progress_cb, steps, watch)

        call_kwargs: dict[str, Any] = dict(
            prompt=prompt,
            negative_prompt=negative,
            image=init_image.convert("RGB"),
            strength=strength,
            num_inference_steps=steps,
            guidance_scale=cfg,
            generator=generator,
        )
        call_kwargs["callback_on_step_end"] = cb
        call_kwargs["callback_on_step_end_tensor_inputs"] = ["latents"]

        start = time.perf_counter()
        result = pipe(**call_kwargs)
        self.last_elapsed_ms = (time.perf_counter() - start) * 1000
        self.last_seed = seed

        if progress_cb is not None:
            try:
                progress_cb(100, steps)
            except Exception:
                pass

        logger.info("图生图完成: strength=%.2f seed=%d %.0fms%s",
                    strength, seed, self.last_elapsed_ms,
                    f"（降参: 原 {watch.get('requested_steps', steps)} 步"
                    f"实际 {steps} 步）" if watch["reduced"] else "")
        return {
            "images": list(result.images),
            "seed": seed,
            "sampler": self._sampler,
            "model": self._model_id,
            "strength": strength,
            "elapsed_ms": self.last_elapsed_ms,
            # F-10 降参元数据：命中标注 quality_reduced + 原/实际步数
            "quality_reduced": bool(watch["reduced"]),
            "requested_steps": watch.get("requested_steps") or steps,
            "actual_steps": steps,
        }

    # ── 局部重绘（PAINT-027/028/030/031、COMIC-039）───────────────

    def inpaint(self, params: dict, image: Any, mask: Any,
                progress_cb: Optional[Callable[[int, int], None]] = None
                ) -> dict:
        """局部重绘（遮罩区域重绘后按遮罩回贴）。

        随包仅有 SDXL base（UNet 4 通道），无 inpaint 专用 9 通道权重，
        采用诚实降级实现：遮罩外扩 bbox 裁剪 → img2img 高强度重绘 →
        软边遮罩 composite 回贴原图。返回 dict 带 degraded=True +
        degrade_reason + backend="masked_img2img"。

        Args:
            params: 同 img2img，另支持 mask_margin（外扩像素，默认 48）、
                    strength（默认 1.0，局部重绘需高强度）
            image: PIL 原图（RGB）
            mask:  PIL 遮罩（任意模式，亮度 ≥128 视为待重绘区域）
        """
        if self._state != "ready" or self._pipe is None:
            raise RuntimeError(self._last_error or "绘画模型未就绪")

        from PIL import Image, ImageFilter

        t0 = time.perf_counter()
        binmask = mask.convert("L").point(lambda v: 255 if v >= 128 else 0)
        bbox = binmask.getbbox()
        if bbox is None:  # API 层已校验，双保险
            raise ValueError("遮罩为空，请涂抹需要重绘的区域")

        margin = int(params.get("mask_margin", 48) or 48)
        W, H = image.size
        l, t, r, b = bbox
        l = max(0, l - margin)
        t = max(0, t - margin)
        r = min(W, r + margin)
        b = min(H, b + margin)
        # VAE 要求尺寸对齐 8 像素；下限 64 防止过小裁片推理失败
        w = max(64, (r - l) // 8 * 8)
        h = max(64, (b - t) // 8 * 8)
        r = min(W, l + w)
        b = min(H, t + h)
        w, h = r - l, b - t

        crop = image.convert("RGB").crop((l, t, r, b))
        mcrop = binmask.crop((l, t, r, b))

        sub = dict(params)
        try:
            strength = float(sub.get("strength", 1.0) or 1.0)
        except (TypeError, ValueError):
            strength = 1.0
        sub["strength"] = max(0.05, min(strength, 1.0))

        res = self.img2img(sub, crop, progress_cb=progress_cb)
        out = res["images"][0].convert("RGB")
        if out.size != (w, h):
            out = out.resize((w, h), resample=1)  # 1 = LANCZOS

        soft = mcrop.filter(ImageFilter.GaussianBlur(3))
        blended = Image.composite(out, crop, soft)
        final = image.convert("RGB").copy()
        final.paste(blended, (l, t))

        elapsed = (time.perf_counter() - t0) * 1000
        logger.info("局部重绘完成: 区域=(%d,%d,%d,%d) seed=%d %.0fms",
                    l, t, r, b, res["seed"], elapsed)
        return {
            "images": [final],
            "seed": res["seed"],
            "sampler": res.get("sampler", self._sampler),
            "model": self._model_id,
            "elapsed_ms": elapsed,
            "region": [l, t, r, b],
            "degraded": True,
            "backend": "masked_img2img",
            "degrade_reason": (
                "未随包 SDXL-inpaint 专用权重（9 通道 UNet），"
                "采用遮罩区域 img2img 重绘并按软边遮罩回贴"),
        }

    # ── 超分 ──────────────────────────────────────────────────────

    def upscale(self, image: Any, scale: int = 2) -> dict:
        """图像超分。

        有 Real-ESRGAN 用真模型；无则 PIL LANCZOS 放大并标注 degraded。

        Returns:
            {image: PIL.Image, scale, backend: "realesrgan"|"lanczos",
             degraded: bool}
        """
        scale = 4 if int(scale) >= 4 else 2
        realesrgan = _try_import("realesrgan")
        if realesrgan is not None:
            try:
                torch = _try_import("torch")
                from basicsr.archs.rrdbnet_arch import RRDBNet  # type: ignore
                upsampler = realesrgan.RealESRGANer(
                    scale=scale,
                    model_path=None,
                    model=RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                                  num_block=23, num_grow_ch=32, scale=scale),
                    tile=256, pre_pad=10, half=torch.cuda.is_available(),
                )
                import numpy as np
                out, _ = upsampler.enhance(np.array(image.convert("RGB")),
                                           outscale=scale)
                from PIL import Image
                return {"image": Image.fromarray(out), "scale": scale,
                        "backend": "realesrgan", "degraded": False}
            except Exception as exc:
                logger.warning("Real-ESRGAN 超分失败，降级 LANCZOS: %s", exc)

        # 降级：PIL LANCZOS
        w, h = image.size
        resized = image.convert("RGB").resize((w * scale, h * scale),
                                              resample=1)  # 1 = LANCZOS
        logger.info("LANCZOS 降级超分: %dx%d -> %dx%d", w, h, w * scale, h * scale)
        return {"image": resized, "scale": scale,
                "backend": "lanczos", "degraded": True}

    # ── Prompt 优化（v2.3 知识反哺）──────────────────────────────

    def optimize_prompt(self, prompt: str) -> tuple[str, bool]:
        """知识库 Prompt 优化。

        尝试用 vector_db 检索与 prompt 相关的风格知识，提取描述词补充；
        任何失败都原样返回，不影响主流程。

        Returns:
            (optimized_prompt, used_knowledge)
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return prompt, False
        try:
            from ...data.vector_db import get_vector_db
            vdb = get_vector_db()
            results = vdb.search(prompt, n_results=3)
            extras: list[str] = []
            for r in results or []:
                score = r.get("score", r.get("distance", 1.0))
                try:
                    if score is not None and float(score) < 0.5 \
                            and "distance" in r:
                        continue
                except (TypeError, ValueError):
                    pass
                text = (r.get("text") or r.get("content") or "").strip()
                if text:
                    extras.append(text[:120])
            if not extras:
                return prompt, False
            suffix = ", ".join(dict.fromkeys(extras))  # 去重保序
            optimized = f"{prompt}, {suffix}"
            logger.info("Prompt 知识优化: +%d 条风格描述", len(extras))
            return optimized, True
        except Exception as exc:
            logger.debug("Prompt 优化跳过（知识库不可用）: %s", exc)
            return prompt, False

    # ── 落盘与历史 ────────────────────────────────────────────────

    @staticmethod
    def image_to_png_bytes(image: Any) -> bytes:
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()

    @staticmethod
    def image_to_base64(image: Any) -> str:
        return base64.b64encode(
            PaintEngine.image_to_png_bytes(image)).decode("utf-8")

    def save_result(self, image: Any, task_id: str, prompt: str,
                    negative: str, params: dict, seed: int) -> str:
        """生成图落盘（file_store）并写 paint_history 表。返回相对路径。"""
        import json as _json

        rel_path = ""
        try:
            from ...data.file_store import get_file_store
            store = get_file_store()
            rel_path = store.save_file("image", self.image_to_png_bytes(image),
                                       filename=f"{task_id}.png")
        except Exception as exc:  # noqa: BLE001
            logger.warning("生成图落盘失败: %s", exc)

        try:
            from ...data.database import get_db_safe
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
            logger.warning("paint_history 写入失败: %s", exc)

        return rel_path

    @staticmethod
    def ensure_history_table() -> bool:
        """确保 paint_history 表存在（供 API 层启动/查询前调用）。"""
        try:
            from ...data.database import get_db_safe
            db = get_db_safe()
            if db is None:
                return False
            db.executescript(_PAINT_HISTORY_DDL)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("paint_history 建表失败: %s", exc)
            return False

    # ── 状态 ──────────────────────────────────────────────────────

    @property
    def is_loaded(self) -> bool:
        return self._state == "ready"

    @property
    def is_ready(self) -> bool:
        return self._state == "ready"

    @property
    def model_name(self) -> str:
        return self._model_id or "none"

    def get_status(self) -> dict:
        return {
            "engine": "paint",
            "state": self._state,
            "loaded": self._state == "ready",
            "model": self._model_id,
            "model_dir": str(self._model_dir) if self._model_dir else "",
            "available_models": self.available_models(),
            "sampler": self._sampler,
            "samplers": sorted(SAMPLER_MAP.keys()),
            "last_error": self._last_error,
            "vram_free_gb": round(_cuda_free_gb(), 2),
            "last_seed": self.last_seed,
        }


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

_engine_instance: Optional[PaintEngine] = None
_engine_lock = threading.Lock()


def get_paint_engine() -> PaintEngine:
    """获取绘画引擎全局单例（线程安全双重检查）。"""
    global _engine_instance
    if _engine_instance is None:
        with _engine_lock:
            if _engine_instance is None:
                _engine_instance = PaintEngine()
    return _engine_instance
