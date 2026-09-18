"""OmniSpace AI 视频兜底引擎（批0-c 2026-09-12 docstring 勘误）。

定位勘误：真实成片主力 = ComfyUI H3 链（h3_engine / h3_chain_engine），
VIDEO_ROUTING_TABLE 仅 minimax-h3 一条——本文件的 LTX-2 / Wan2.1 /
CogVideoX diffusers 生成路径已无路由可达（文档 §5.2 旧口径），当前
职责仅为：目录常量（VIDEO_OUT_DIR）+ AnimateLCM/Ken Burns 探测链 +
PIL+FFmpeg「降级真实管线」兜底成片（TASK-010）+ 状态台账
（get_status，switch_engine 读）。
"""

from __future__ import annotations

import base64
import gc
import hashlib
import importlib
import io
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...config import DATA_DIR, MODELS_DIR
from ...data.models import (
    VideoGenerateRequest,
    VideoGenResult,
    VideoModel,
)
from ...middleware.error_handler import ApiError
from ..scheduler.video_router import VideoRouter
from .base_engine import BaseEngine

if TYPE_CHECKING:  # 仅注解用（_generate_h3 首帧参考图签名），运行时各函数内局部导入
    from PIL import Image

logger = logging.getLogger("omnispace.inference.video")


def _try_import(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except Exception:
        return None


_torch = _try_import("torch")
_diffusers = _try_import("diffusers")


# ═══════════════════════════════════════════════════════════════════
#  降级真实管线（TASK-010）：真实视频模型未下载时仍产出真实视频文件
# ═══════════════════════════════════════════════════════════════════

# 视频产物输出目录
VIDEO_OUT_DIR = DATA_DIR / "generated" / "videos"

# ═══════════════════════════════════════════════════════════════════
#  AnimateLCM 图生视频（F-07）：探测常量与状态上报
# ═══════════════════════════════════════════════════════════════════

# AnimateLCM 运动模块单文件 ckpt（diffusers 格式 state dict，随包附带）
ANIMATELCM_CKPT_PATH = (
    MODELS_DIR / "video_gen" / "AnimateLCM" / "AnimateLCM_sd15_t2v.ckpt")

# SD1.5 底座探测候选路径（AnimateLCM 必须搭配 SD1.5 base 才能组成
# AnimateDiffPipeline；按 model_index.json 存在判定为有效 diffusers 目录）
SD15_BASE_CANDIDATES = (
    MODELS_DIR / "sd15",
    MODELS_DIR / "stable-diffusion-v1-5",
    MODELS_DIR / "paint" / "sd1.5",
    MODELS_DIR / "paint" / "sd15",
    MODELS_DIR / "paint" / "stable-diffusion-v1-5",
    MODELS_DIR / "video_gen" / "sd15",
    MODELS_DIR / "video_gen" / "stable-diffusion-v1-5",
)

# AnimateLCM 推理常量：运动模块 motion_max_seq_length=32，LCM 少步数采样
ANIMATELCM_MODEL_LABEL = "animatelcm-sd15-t2v"
ANIMATELCM_MAX_FRAMES = 24       # AI 片段帧数上限（<=32，质量/显存折中）
ANIMATELCM_CLIP_SECONDS = 3.0    # 目标 AI 片段时长（受帧数上限截断）
ANIMATELCM_INFER_STEPS = 6       # LCM 少步采样
ANIMATELCM_GUIDANCE = 1.5        # LCM 低引导
_ANIMATELCM_NEG_PROMPT = "low quality, worst quality, blurry, watermark, text"


def _find_sd15_base() -> Path | None:
    """在 SD15_BASE_CANDIDATES 中探测有效 SD1.5 diffusers 目录。"""
    for path in SD15_BASE_CANDIDATES:
        if (path / "model_index.json").is_file():
            return path
    return None


def _load_text_encoder_int8(model_dir: Path, dtype: Any) -> Any:
    """文本编码器 int8 量化加载（bitsandbytes），显存不足时调用。

    LTX/Wan 系管线的 T5-XXL 编码器约占整管线 2/3 显存（fp16 ~9.4GB），
    int8 后约减半，常可让 16GB 卡免 CPU offload 整卡运行。
    按 text_encoder/config.json 的 model_type 分派编码器类：
    Wan 系是 UMT5（每层独立 relative_attention_bias，误用 T5EncoderModel
    会静默丢弃层 1-23 的 bias 键导致嵌入语义漂移）；LTX 系是标准 T5。
    目录缺失 / bitsandbytes 未装 / 加载失败均返回 None（回退原路径）。
    """
    if _try_import("bitsandbytes") is None:
        return None
    te_dir = Path(model_dir) / "text_encoder"
    if not (te_dir / "config.json").is_file():
        return None
    try:
        import json as _json
        with open(te_dir / "config.json", encoding="utf-8") as fh:
            model_type = _json.load(fh).get("model_type", "t5")
        from transformers import BitsAndBytesConfig
        if model_type.startswith("umt5"):
            from transformers import UMT5EncoderModel as _TECls
        else:
            from transformers import T5EncoderModel as _TECls
        qcfg = BitsAndBytesConfig(load_in_8bit=True)
        te = _TECls.from_pretrained(
            str(te_dir), quantization_config=qcfg, torch_dtype=dtype)
        logger.info("文本编码器 int8 量化加载成功: %s (%s)",
                    te_dir, _TECls.__name__)
        return te
    except Exception as exc:  # noqa: BLE001 - 量化失败回退 fp16，不阻断
        logger.warning("文本编码器 int8 量化失败（回退 fp16）: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════
#  视频模型动态发现（导入 models/ 即可用）
# ═══════════════════════════════════════════════════════════════════
# 《显存阶梯参考》视频档位：Wan2.1 / CogVideoX / LTX-Video / HunyuanVideo 等。
# 识别规则：diffusers 布局目录（model_index.json），_class_name 命中已知
# 视频管线类即视为可加载；显存按权重字节估算，加载时按空闲显存门控，
# 超出时自动启用 CPU offload（诚实降级，不伪造视频帧）。

# 已知视频管线类名（model_index.json _class_name → 能力标记）
_VIDEO_PIPELINE_CLASSES: dict[str, dict] = {
    # LTX-Video（LTX-2 同族）
    "LTXVideoPipeline": {"i2v": False},
    "LTXPipeline": {"i2v": False},
    "LTXImageToVideoPipeline": {"i2v": True},
    # CogVideoX
    "CogVideoXPipeline": {"i2v": False},
    "CogVideoXImageToVideoPipeline": {"i2v": True},
    "CogVideoXVideoToVideoPipeline": {"i2v": False},
    # Wan2.1 / Wan2.2
    "WanPipeline": {"i2v": False},
    "WanImageToVideoPipeline": {"i2v": True},
    "WanVideoToVideoPipeline": {"i2v": False},
    # Wan VACE（统一生成/编辑，T2V+I2V；1.3B 为 16GB 卡官方 I2V 路径）
    "WanVACEPipeline": {"i2v": True},
    # HunyuanVideo
    "HunyuanVideoPipeline": {"i2v": False},
    "HunyuanVideoImageToVideoPipeline": {"i2v": True},
    # 其余常见视频管线
    "MochiPipeline": {"i2v": False},
    "AllegroPipeline": {"i2v": False},
    "StableVideoDiffusionPipeline": {"i2v": True},
    "I2VGenXLPipeline": {"i2v": True},
    "AnimateDiffPipeline": {"i2v": False},
    "AnimateDiffSDXLPipeline": {"i2v": False},
    "TextToVideoSDPipeline": {"i2v": False},
    "EasyAnimatePipeline": {"i2v": False},
}
# I2V 能力判定（generate 时决定是否传入参考图）
_I2V_PIPELINE_NAMES = {n for n, m in _VIDEO_PIPELINE_CLASSES.items() if m["i2v"]}


def _dir_load_bytes_fp16(model_dir: Path) -> int:
    """估算目录权重以 fp16/bf16 加载后的字节数（dtype 感知）。

    safetensors 头部含存储 dtype：F32/F64 存盘转 fp16 加载字节减半，
    F16/BF16 按原字节计；.bin/.ckpt/.pt 等 pickle 格式无法廉价判型，
    按原字节保守估计。同名 .bin 与 .safetensors 并存时跳过 .bin
    （同一权重的复本，避免重复计数）。
    """
    import json as _json
    import struct as _struct

    def _file_bytes(f: Path) -> int:
        try:
            size = f.stat().st_size
        except OSError:
            return 0
        if f.suffix.lower() != ".safetensors":
            return size
        try:
            with open(f, "rb") as fh:
                hlen = _struct.unpack("<Q", fh.read(8))[0]
                header = _json.loads(fh.read(min(hlen, 1 << 20)))
            dtypes = {v["dtype"] for v in header.values()
                      if isinstance(v, dict) and isinstance(v.get("dtype"), str)}
            if dtypes and all(d in ("F32", "F64") for d in dtypes):
                return size // 2
        except Exception:  # noqa: BLE001 - 头部损坏按原字节保守计
            logger.debug("_file_bytes: 降级忽略", exc_info=True)
        return size

    total = 0
    exts = (".safetensors", ".bin", ".ckpt", ".pt", ".pth")
    try:
        for f in model_dir.rglob("*"):
            if not f.is_file() or f.suffix.lower() not in exts:
                continue
            if f.suffix.lower() == ".bin" and f.with_suffix(
                    ".safetensors").is_file():
                continue
            total += _file_bytes(f)
    except OSError:
        logger.debug("_dir_load_bytes_fp16: 降级忽略", exc_info=True)
    return total


def _ltx_align_params(num_frames: int, gen_w: int, gen_h: int) -> tuple[int, int, int]:
    """LTX 系管线约束校正（纯函数，TASK-P1-01 抽取以供直测）。

    - VAE 时序压缩 8:1：帧数需 ≡ 1 (mod 8)，向下限 9 对齐、上限 257
    - VAE 空间压缩 32:1：宽高为 32 的倍数（半向上取整，720→736）
    """
    frames = min(257, ((max(9, num_frames) - 1 + 7) // 8) * 8 + 1)
    w = max(32, (gen_w + 16) // 32 * 32)
    h = max(32, (gen_h + 16) // 32 * 32)
    return frames, w, h


def _wan_align_params(num_frames: int, gen_w: int, gen_h: int) -> tuple[int, int, int]:
    """Wan2.1 管线约束校正（2026-08-22 I2V 换载引入）。

    - VAE 时序压缩 4:1：帧数需 ≡ 1 (mod 4)，向下限 5 对齐、上限 121
      （官方 480P 推荐 81 帧；超限截断防 OOM——14B offload 下 121 帧
      已是 16GB 卡的实际上限）
    - 宽高为 16 的倍数，480P 模型分辨率钳制到 832x480 档（更高分辨率
      属 720P 模型，权重另需 30GB+）
    """
    frames = min(121, ((max(5, num_frames) - 1 + 3) // 4) * 4 + 1)
    w = max(16, gen_w // 16 * 16)
    h = max(16, gen_h // 16 * 16)
    if w * h > 832 * 480:
        # 按宽高比缩到 832x480 档内
        scale = (832 * 480 / (w * h)) ** 0.5
        w = max(16, int(w * scale) // 16 * 16)
        h = max(16, int(h * scale) // 16 * 16)
    return frames, w, h


# Wan 官方推荐负面 prompt（VACE/Wan2.1 系列通用，中文原生编码器）：
# 关键含"静态/静止/静止不动的画面"——I2V 首帧锚定后模型易输出
# 静态展示（2026-08-22 别墅泳池跳舞实测：外观保持但全程静止），
# 官方负面词是抑制静态的直接手段
_WAN_NEGATIVE_PROMPT = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，"
    "画面，静止，整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，"
    "残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，"
    "毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，"
    "三点式，内衣，泳装"
)

# 视频链路专用中译英模板（2026-08-22 模板错配修复）：绘画默认模板
# "美术风格>背景>外观"排序 + "忽略排版指令"会把动作动词（跳舞/奔跑）
# 排到尾部丢弃——视频第一要素是运动。此模板强制保留动作与场景。
_VIDEO_TRANSLATE_SYSTEM = (
    "Translate the Chinese video description into an English video "
    "generation prompt. HARD RULES:\n"
    "1. The ACTION/MOTION must come first and be preserved exactly "
    "(dancing, running, waving, turning...);\n"
    "2. Keep the scene/location and add natural camera movement "
    "(slow zoom in, panning, tracking shot);\n"
    "3. Max 60 words, comma-separated phrases, single line;\n"
    "4. Output ONLY the English prompt, no explanation, no Chinese."
)

# 视频 VL 预处理专用模型（2026-08-22 乒乓装载修复）：显式指定小 VL，
# 防止 ensure_loaded 自动选择在空闲显存充足时挑 qwen3-vl-4b/8b
# （10.5GB+），挤压后续视频管线 offload 推理显存。VL 已就绪时
# （含用户手动加载的任何模型）直接复用，不触发换载。
_VIDEO_VL_MODEL = "qwen2-vl-2b"


def _ensure_video_vl(engine: Any) -> bool:
    """确保视频预处理可用的 VL 引擎（小模型，显存友好）。"""
    if engine.is_ready:
        return True
    try:
        return bool(engine.ensure_loaded(_VIDEO_VL_MODEL))
    except Exception as exc:  # noqa: BLE001 - 加载失败由调用方降级
        logger.warning("视频 VL 模型加载失败: %s", exc)
        return False


# 用户动作词 → 英文锚点（2026-08-22 动作保真校验）：VL 增强输出必须
# 保留用户明确要求的动作，否则按失败处理回退翻译链（2b 小模型易被
# 参考图带偏——"故宫跳舞"实测输出了 sits by window 完全丢动作）
_ACTION_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("跳舞", ("danc",)), ("舞蹈", ("danc",)), ("跳", ("jump", "danc")),
    ("跑", ("run", "sprint", "dash")), ("奔", ("run",)),
    ("走", ("walk", "stroll", "step")), ("转身", ("turn", "spin", "rotat")),
    ("转", ("turn", "spin", "rotat")), ("挥", ("wave",)),
    ("点头", ("nod",)), ("微笑", ("smile",)), ("飞", ("fly",)),
    ("飘", ("drift", "float", "fall")), ("落", ("fall", "drop")),
    ("拉近", ("zoom",)), ("拉远", ("zoom",)), ("摇", ("pan", "sway")),
)


def _action_lost(base_prompt: str, out: str) -> str | None:
    """校验增强输出是否丢失用户动作词；丢失返回命中的中文词。"""
    low = out.lower()
    for zh, ens in _ACTION_HINTS:
        if zh in base_prompt and not any(e in low for e in ens):
            return zh
    return None


def discover_video_models() -> dict[str, dict]:
    """扫描 MODELS_DIR（两层）发现 diffusers 布局的视频生成模型。

    Returns:
        {目录名: {"path": str, "class_name": str, "i2v": bool,
                 "vram_gb": float, "diffusers_available": bool}}
        class_name 为空串表示 model_index.json 未声明已知视频管线（不可加载，
        仍如实列出便于诊断）。
    """
    import json as _json
    found: dict[str, dict] = {}
    base = Path(MODELS_DIR)
    if not base.is_dir():
        return found

    def _probe(d: Path) -> None:
        idx = d / "model_index.json"
        if not idx.is_file():
            return
        try:
            meta = _json.loads(idx.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        cls_name = str(meta.get("_class_name") or "")
        if cls_name not in _VIDEO_PIPELINE_CLASSES:
            return
        # 已折算 fp16 加载字节（dtype 感知），仅加 15% 装载/激活余量
        load_bytes = _dir_load_bytes_fp16(d)
        found.setdefault(d.name, {
            "path": str(d),
            "class_name": cls_name,
            "i2v": _VIDEO_PIPELINE_CLASSES[cls_name]["i2v"],
            "vram_gb": round(load_bytes / (1024 ** 3) * 1.15, 2),
            "diffusers_available": _diffusers is not None
            and getattr(_diffusers, cls_name, None) is not None,
        })

    try:
        for child in sorted(base.iterdir()):
            if child.is_dir() and not child.name.startswith(("_", ".")):
                _probe(child)
                try:
                    for grand in sorted(child.iterdir()):
                        if grand.is_dir():
                            _probe(grand)
                except OSError:
                    logger.debug("discover_video_models: 降级忽略", exc_info=True)
    except OSError:
        logger.debug("discover_video_models: 降级忽略", exc_info=True)
    return found


def _cuda_free_gb() -> float:
    """当前 GPU 空闲显存（GB）；无 CUDA 返回 0。"""
    if _torch is None or not _torch.cuda.is_available():
        return 0.0
    try:
        free, _total = _torch.cuda.mem_get_info()
        return free / (1024 ** 3)
    except Exception:  # noqa: BLE001
        return 0.0


def _release_gpu_cache() -> None:
    """大模型引用释放后的显存回收（幂等）：gc.collect + CUDA 缓存清理。

    供 unload_model / AnimateLCM 加载失败 / 推理失败路径复用；
    torch 不可用或无 CUDA 时静默跳过缓存清理（引用置 None 由调用方完成）。
    """
    gc.collect()
    if _torch is not None:
        try:
            if _torch.cuda.is_available():
                _torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            logger.debug("_release_gpu_cache: 降级忽略", exc_info=True)


def _evict_idle_models_for_video() -> float:
    """视频装载前清场：卸载空闲的非持锁类别模型（容错，返回腾出 GB）。

    2026-08-22 e2e 二测 OOM 根因（团队复核裁定）：VL 提示词增强遗留
    的 qwen3-vl-4b int8（~5.5GB）在 split 布局判定时仍驻留显存，
    "空闲 10.5GB" 口径虚高 5.5GB，装载后真实空闲仅 ~3GB；T5 int8
    量化窗口（int8 累积 + fp16 shard 流水中转 ~4GB 峰值）首发击穿
    物理显存触发 Windows sysmem fallback，去噪激活持续加深溢出，
    叠加 RAM 被 DiT CPU 副本/T5 中转垫高致共享配额耗尽 →
    "28.13GiB allocated > 15.92GiB 物理" OOM 铁证。主动卸载让
    布局判定与装载都基于真实空闲。保留：embedding/voice/auxiliary
    共享小模型 + 功能锁持锁类别（video_gen 持锁时 video 类别保留）。
    """
    freed_gb = 0.0
    try:
        from ...middleware.feature_lock import get_feature_lock
        from ..model_manager import _FEATURE_KEEP_CATEGORIES, get_model_manager
        keep_cats = {"embedding", "voice", "auxiliary"}
        try:
            active = get_feature_lock().active_feature
            if active:
                keep_cats |= _FEATURE_KEEP_CATEGORIES.get(active, set())
        except Exception:  # noqa: BLE001 - 锁探测失败按基础保留集
            logger.debug("_evict_idle_models_for_video: 降级忽略", exc_info=True)
        mgr = get_model_manager()
        for entry in mgr.get_loaded_models():
            if entry.get("category") in keep_cats:
                continue
            try:
                if mgr.unload_model(entry["model_id"]):
                    _freed = float(entry.get("vram_gb", 0.0))
                    freed_gb += _freed
                    logger.info("装载前清场: 卸载空闲模型 %s "
                                "(category=%s, 释放 %.1fGB)",
                                entry["model_id"],
                                entry.get("category"), _freed)
            except Exception as exc:  # noqa: BLE001 - 单个失败不阻断
                logger.debug("清场卸载失败 (%s): %s",
                             entry.get("model_id"), exc)
    except Exception as exc:  # noqa: BLE001 - manager 不可用跳过清场
        logger.debug("装载前清场跳过: %s", exc)
    if freed_gb > 0:
        _release_gpu_cache()
    return freed_gb


class _ProgressRelay:
    """进度回调中继：统一包裹调用方 progress_cb。

    回调内抛出的异常（典型为调用方的取消信号）记录到 self.error 并原样
    穿透；各 except 降级分支用 is_cancel() 识别该信号并改为向上抛出，
    避免「推理中途取消」被当作推理失败而回落 Ken Burns 继续产出。
    """

    def __init__(self, progress_cb: Callable[[float, str], None] | None) -> None:
        self._cb = progress_cb
        self.error: BaseException | None = None

    def __call__(self, fraction: float, stage: str = "") -> None:
        if self._cb is None:
            return
        try:
            self._cb(fraction, stage)
        except BaseException as exc:  # noqa: BLE001 - 取消信号需原样穿透
            self.error = exc
            raise

    def is_cancel(self, exc: BaseException) -> bool:
        """捕获的异常是否源自进度回调（即调用方取消信号）。"""
        return self.error is not None and self.error is exc


def _make_step_callback(pipe_call: Any, relay: _ProgressRelay,
                        num_steps: int, span: float,
                        tail_seconds: float = 20.0) -> dict:
    """构造 diffusers callback_on_step_end 进度参数（管线签名支持时）。

    去噪步进映射到 [0, span] 区间（stage="denoise"）；管线不支持该参数
    （旧版 diffusers / 非标准管线）或内省失败时返回空 dict，进度维持
    调用方分段上报的粗粒度行为。tensor_inputs 置空列表避免不必要的
    latents 张量拷贝开销。

    ETA（2026-08-22）：按已完成步的平均耗时外推剩余去噪时间，叠加
    尾段（VAE 解码 + 编码）常量估计，编码进 stage "denoise;eta=N"
    由调用方解析展示（进度条下预计剩余时间）。
    """
    try:
        import inspect
        params = inspect.signature(pipe_call).parameters
    except Exception:  # noqa: BLE001 - 内省失败不接回调
        return {}
    if "callback_on_step_end" not in params:
        return {}

    t_first: list[float] = []  # 闭包可变容器（首步时间戳）

    def _on_step_end(_pipe: Any, step_index: int, _timestep: Any,
                     callback_kwargs: dict) -> dict:
        import time as _time
        now = _time.monotonic()
        if not t_first:
            t_first.append(now)
        done = step_index + 1
        remain = num_steps - done
        if remain > 0:
            avg = (now - t_first[0]) / done
            eta_s = avg * remain + tail_seconds
        else:
            # denoise 完成：剩 VAE 解码+编码尾段，按常量预算报 ETA
            # （归 0 会让前端尾段显示"预计剩余 0 秒"却迟迟不完成）
            eta_s = tail_seconds
        relay(span * done / num_steps, f"denoise;eta={eta_s:.0f}")
        return callback_kwargs

    kwargs: dict[str, Any] = {"callback_on_step_end": _on_step_end}
    if "callback_on_step_end_tensor_inputs" in params:
        kwargs["callback_on_step_end_tensor_inputs"] = []
    return kwargs


def _install_vae_stage_swap(pipe: Any) -> None:
    """dit-only split 布局的 VAE 阶段交换（实例级 wrap，2026-08-23）。

    背景：TI2V-5B 级大 DiT（fp16 9.3GB）在 16GB 日用卡（GUI 常驻
    ~2GB + bge 1.3GB，清场后空闲 ~12.5GB）上 DiT+VAE 双常驻差 ~1GB
    装不下，但 DiT 单常驻富余。VAE 权重仅 ~1.3GB，encode（I2V 首帧）
    与 decode（成片）都是一次性窗口——在窗口内临时上卡、窗口外
    留 CPU，全程峰值 = max(DiT+去噪激活, VAE+VAE 激活) ≈ 11.1GB。

    编排契约（diffusers 0.39 Wan 家族实测源码）：
      - prepare_latents 把 video_condition 钉 _execution_device（cuda）
        后调 vae.encode → VAE 在 CPU 会设备不匹配，wrap 必须存在；
      - encode 窗口：DiT 留 CPU（T5 窗口 finally 不回卡）→ VAE 上卡
        encode → VAE 下卡 + DiT 上卡（接管去噪前上卡，省一来回搬运）；
      - decode 窗口（去噪完成后）：DiT 下卡 → VAE 上卡 decode →
        VAE 下卡 + DiT 回卡（恢复装载态）。
    wrap 写入 vae 实例 __dict__ 遮蔽类方法（nn.Module.__setattr__ 对
    函数走 object 分支），管线卸载时随实例丢弃，无还原需求。
    """
    vae = pipe.vae
    _orig_encode = vae.encode
    _orig_decode = vae.decode

    def _dit_to(target: str) -> None:
        for _cn in ("transformer", "transformer_2"):
            _comp = getattr(pipe, _cn, None)
            if _comp is not None and _comp.device.type != target:
                _comp.to(target)
        _release_gpu_cache()

    def _encode(x: Any, **kw: Any) -> Any:
        if vae.device.type != "cuda":
            # encode 窗口：DiT 先下卡腾位（VAE encode 与 DiT 去噪互斥，
            # tiling 下激活 ~0.5GB），encode 完 VAE 下卡 + DiT 回卡。
            # 不依赖 x.device 触发（defer 方案废除后 x 恒 cuda，但
            # 显式检查 vae 更稳）
            _dit_to("cpu")
            vae.to("cuda")
            if getattr(x, "device", None) is not None \
                    and x.device.type != "cuda":
                x = x.to("cuda")
            try:
                return _orig_encode(x, **kw)
            finally:
                vae.to("cpu")
                _dit_to("cuda")
        return _orig_encode(x, **kw)

    def _decode(z: Any, **kw: Any) -> Any:
        if vae.device.type != "cuda":
            # decode 窗口（去噪完成后一次性）：DiT 下卡 → VAE 上卡
            # decode → VAE 下卡 + DiT 回卡恢复装载态
            _dit_to("cpu")
            vae.to("cuda")
            if getattr(z, "device", None) is not None \
                    and z.device.type != "cuda":
                z = z.to("cuda")
            try:
                return _orig_decode(z, **kw)
            finally:
                vae.to("cpu")
                _dit_to("cuda")
        return _orig_decode(z, **kw)

    vae.encode = _encode  # type: ignore[method-assign]
    vae.decode = _decode  # type: ignore[method-assign]


def _quantize_transformer_int8(transformer: Any) -> bool:
    """DiT int8 量化替换（bnb Linear8bitLt 惰性量化，2026-08-23）。

    档 2c：坏显存日子（GUI 进程占用高，清场后 free ~9GB）连 DiT
    fp16 9.3GB + 激活都装不下时，nn.Linear → Linear8bitLt 逐层替换
    （持原 fp16 weight 引用，首次 forward 逐层量化 int8+SCB 并释放
    fp16，收敛 ~5.2GB 常驻）。attention 的 q/k/v/out_proj 全为
    nn.Linear 可替换；norm/patch_embed/time_embed 卷积与归一化不动。
    bnb 0.50 在本机 WDDM 已验证可用（T5 int8 同库同路径）。

    不用 transformers.integrations.replace_with_bnb_linear：它在
    init_empty_weights 下创建空权重模块（适配 from_pretrained 加载
    流程），对已加载权重的模型会丢权重（首试 'NoneType' 崩 +
    空模块风险）。此处直接构造并搬运 weight/bias 引用。
    has_fp16_weights=False → weight 上卡后首个 forward 量化。
    """
    try:
        import bitsandbytes as _bnb
        import torch.nn as _nn

        _replaced = 0

        def _walk(mod: Any) -> None:
            nonlocal _replaced
            for _name, _child in list(mod.named_children()):
                if isinstance(_child, _nn.Linear):
                    _new = _bnb.nn.Linear8bitLt(
                        _child.in_features, _child.out_features,
                        _child.bias is not None,
                        has_fp16_weights=False,
                        threshold=6.0,
                    )
                    # weight 必须包 Int8Params 容器（普通 Parameter 缺
                    # CB/SCB 属性，forward 触 'Parameter' object has no
                    # attribute 'CB'）。量化发生在 .to("cuda")（bnb 官方
                    # 契约：has_fp16_weights=False → to(device) 即量化），
                    # 首个 forward 无量化峰值
                    _new.weight = _bnb.nn.Int8Params(
                        _child.weight.data,
                        has_fp16_weights=False,
                        requires_grad=False,
                    )
                    if _child.bias is not None:
                        _new.bias = _child.bias
                    mod._modules[_name] = _new
                    _replaced += 1
                else:
                    _walk(_child)

        _walk(transformer)
        if _replaced == 0:
            logger.warning("DiT int8 量化：未找到可替换的 Linear 层")
            return False
        logger.info("DiT int8 量化替换完成: %d 个 Linear 层", _replaced)
        return True
    except Exception as exc:  # noqa: BLE001 - 替换失败回退 fp16 裸装
        logger.warning("DiT int8 量化替换失败（回退 fp16）: %s", exc)
        return False


def _estimate_animatelcm_vram_gb(sd15_path: str) -> float:
    """估算 AnimateLCM（SD1.5 底座 + 运动模块）fp16 上卡显存（GB）。

    与 discover_video_models 同一折算口径：dtype 感知权重字节 + 15%
    装载/激活余量；探测失败按 4.0GB 保守估计（SD1.5 fp16 经验值）。
    """
    try:
        total = _dir_load_bytes_fp16(Path(sd15_path))
        total += ANIMATELCM_CKPT_PATH.stat().st_size
        if total <= 0:
            raise ValueError("权重字节为 0")
        return total / (1024 ** 3) * 1.15
    except Exception:  # noqa: BLE001 - 探测失败保守估计
        return 4.0


def get_animatelcm_status() -> dict:
    """AnimateLCM 可用性探测（权重存在性），不加载模型，诚实上报原因。"""
    ckpt_ready = ANIMATELCM_CKPT_PATH.is_file()
    sd15_base = _find_sd15_base()
    sd15_ready = sd15_base is not None
    if not ckpt_ready:
        reason = f"AnimateLCM 运动模块权重缺失（{ANIMATELCM_CKPT_PATH}）"
    elif not sd15_ready:
        reason = ("SD1.5 底座权重未随包（需 stable-diffusion-v1-5），"
                  "AnimateLCM 不可用，回落 Ken Burns 降级管线")
    else:
        reason = ""
    return {
        "ckpt_ready": ckpt_ready,
        "ckpt_path": str(ANIMATELCM_CKPT_PATH) if ckpt_ready else "",
        "sd15_ready": sd15_ready,
        "sd15_path": str(sd15_base) if sd15_base else "",
        "ready": ckpt_ready and sd15_ready,
        "reason": reason,
    }

# 字幕字体候选（Windows 中文字体优先；PIL 默认位图字体不支持 CJK）
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
    r"C:\Windows\Fonts\arial.ttf",
)


def _load_font(size: int) -> Any:
    """加载 TrueType 字体；全部缺失时降级 PIL 默认字体（CJK 会显示为方块）。"""
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _decode_screenshot(b64: str) -> Any:
    """解码 4合1 截图（base64 → PIL Image）；失败返回 None。"""
    if not b64:
        return None
    try:
        from PIL import Image
        raw = b64
        if "," in raw and raw.split(",", 1)[0].startswith("data:"):
            raw = raw.split(",", 1)[1]
        img = Image.open(io.BytesIO(base64.b64decode(raw))).convert("RGB")
        if img.width < 8 or img.height < 8:
            return None
        return img
    except Exception:
        return None


def _placeholder_base(description: str, width: int, height: int) -> Any:
    """无截图时按描述文本哈希生成双色竖向渐变底图（确定性配色）。"""
    from PIL import Image
    digest = hashlib.md5((description or "scene").encode("utf-8")).hexdigest()
    r1, g1, b1 = 40 + int(digest[0:2], 16) % 80, 40 + int(digest[2:4], 16) % 80, 60 + int(digest[4:6], 16) % 90
    r2, g2, b2 = 10 + int(digest[6:8], 16) % 40, 10 + int(digest[8:10], 16) % 40, 20 + int(digest[10:12], 16) % 50
    grad = Image.new("RGB", (1, 256))
    for y in range(256):
        t = y / 255.0
        grad.putpixel((0, y), (int(r1 + (r2 - r1) * t),
                               int(g1 + (g2 - g1) * t),
                               int(b1 + (b2 - b1) * t)))
    return grad.resize((width, height))


def _cover_resize(img: Any, width: int, height: int) -> Any:
    """等比缩放至完全覆盖 (width, height)（cover 语义，供 Ken Burns 裁窗）。"""
    from PIL import Image
    scale = max(width / img.width, height / img.height)
    new_size = (max(width, int(img.width * scale + 0.5)),
                max(height, int(img.height * scale + 0.5)))
    return img.resize(new_size, Image.LANCZOS)


def _fit_text(draw: Any, text: str, font: Any, max_width: int) -> str:
    """按像素宽度截断文本（超出追加省略号）。"""
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi) // 2
        if draw.textlength(text[:mid] + ellipsis, font=font) <= max_width:
            lo = mid + 1
        else:
            hi = mid
    return text[:max(lo - 1, 0)] + ellipsis


# ── Ken Burns 运镜语义（2026-08-22 ti2v 贴合修复）──────────────────
# 降级管线曾完全忽略 description（固定 zoom-in + 右下平移，描述仅画字幕），
# 导致「文字+图片生成视频」结果与描述无关。现在解析运镜关键词驱动镜头；
# 无关键词按描述哈希确定性选择（同描述可复现、不同描述不同镜头）。
_KB_MOTION_RULES: list[tuple[tuple[str, ...], str]] = [
    (("拉近", "推近", "特写", "zoom in", "push in"), "zoom_in"),
    (("拉远", "推远", "全景", "zoom out", "pull out"), "zoom_out"),
    (("向左", "左移", "左摇", "pan left"), "pan_left"),
    (("向右", "右移", "右摇", "pan right"), "pan_right"),
    (("向上", "上摇", "仰拍", "tilt up"), "pan_up"),
    (("向下", "下摇", "俯拍", "tilt down"), "pan_down"),
    (("环绕", "旋转", "orbit", "arc"), "orbit"),
]
# 哈希兜底池（zoom+方向组合，与关键词规则同风格）
_KB_FALLBACK_POOL: tuple[tuple[str, ...], ...] = (
    ("zoom_in", "pan_right"),
    ("zoom_out",),
    ("zoom_in", "pan_left"),
    ("pan_up",),
    ("pan_down",),
    ("zoom_out", "pan_right"),
    ("zoom_in", "pan_up"),
    ("orbit",),
)


def detect_kenburns_motion(description: str) -> tuple[str, ...]:
    """从描述解析运镜组合；无命中时按描述哈希选一组确定性组合。"""
    low = (description or "").lower()
    for kws, motion in _KB_MOTION_RULES:
        if any(k in low for k in kws):
            return (motion,)
    digest = hashlib.md5((description or "").encode("utf-8")).digest()
    return _KB_FALLBACK_POOL[digest[0] % len(_KB_FALLBACK_POOL)]


def _kenburns_state(motions: tuple[str, ...], t: float,
                    zoom_max: float) -> tuple[float, float, float]:
    """某时刻的镜头状态：(zoom 倍率, 水平偏移 -1~1, 垂直偏移 -1~1)。"""
    if "orbit" in motions:
        import math
        ang = t * math.tau
        return 1.06, math.sin(ang) * 0.8, math.cos(ang) * 0.8
    if "zoom_out" in motions:
        zoom = zoom_max - (zoom_max - 1.0) * t  # 1.12 → 1.0 画面拉远
    elif "zoom_in" in motions:
        zoom = 1.0 + (zoom_max - 1.0) * t        # 1.0 → 1.12 画面拉近
    else:
        zoom = 1.06                               # 纯平移时固定微缩放
    dx = dy = 0.0
    if "pan_right" in motions:
        dx += t
    if "pan_left" in motions:
        dx -= t
    if "pan_down" in motions:
        dy += t
    if "pan_up" in motions:
        dy -= t
    return zoom, dx, dy


def render_kenburns_frames(
    description: str,
    screenshot_b64: str,
    frame_dir: str | Path,
    width: int,
    height: int,
    fps: int,
    duration_s: float,
    progress_cb: Callable[[float], None] | None = None,
    start_index: int = 0,
    base_image: Any = None,
) -> int:
    """渲染 Ken Burns 推拉帧序列（含字幕条）到 frame_dir，返回帧数。

    - 基图：base_image（AnimateLCM 补足时长时传入 AI 末帧保持画面连续）>
      4合1截图（cover 放大 12% 余量）> 渐变占位底图；
    - 镜头运动（2026-08-22 运镜语义）：解析 description 中的运镜关键词
      （拉近/拉远/左移/右移/上摇/下摇/环绕）驱动 zoom 与平移方向；
      无关键词时按描述哈希确定性选择——不同描述产生不同镜头，
      同一描述可复现；
    - 字幕条：底部半透明黑条 + 场景描述文本（自动截断）；
    - start_index：帧编号起始偏移（接在已有帧序列之后时非 0）。
    """
    from PIL import Image, ImageDraw

    frame_dir = Path(frame_dir)
    frame_dir.mkdir(parents=True, exist_ok=True)

    total = max(1, int(round(duration_s * fps)))
    zoom_max = 1.12
    base = base_image if base_image is not None else _decode_screenshot(screenshot_b64)
    if base is None:
        base = _placeholder_base(description, width, height)
    # 基图放大至 zoom_max 倍目标尺寸，保证最大裁窗仍有图
    base = _cover_resize(base, int(width * zoom_max), int(height * zoom_max))

    bar_h = max(56, height // 9)
    margin = max(24, width // 48)
    font = _load_font(max(20, height // 34))
    pad_y = (bar_h - font.size) // 2 if hasattr(font, "size") else 12

    motions = detect_kenburns_motion(description)

    report_every = max(1, total // 25)
    for i in range(total):
        t = i / max(total - 1, 1)
        zoom, dx, dy = _kenburns_state(motions, t, zoom_max)
        zw, zh = int(width / zoom), int(height / zoom)
        max_x, max_y = base.width - zw, base.height - zh
        # 平移中心基准 + 运镜方向偏移（clamp 防越界裁窗）
        x0 = int(max(0, min(max_x, (max_x / 2) + dx * (max_x / 2))))
        y0 = int(max(0, min(max_y, (max_y / 2) + dy * (max_y / 2))))
        frame = base.crop((x0, y0, x0 + zw, y0 + zh)).resize(
            (width, height), Image.LANCZOS)

        # 字幕条（RGBA 合成）
        canvas = frame.convert("RGBA")
        canvas.alpha_composite(Image.new("RGBA", (width, bar_h), (0, 0, 0, 150)),
                               (0, height - bar_h))
        draw = ImageDraw.Draw(canvas)
        text = _fit_text(draw, description or "未命名镜头", font,
                         width - margin * 2)
        draw.text((margin, height - bar_h + max(pad_y, 4)), text,
                  font=font, fill=(255, 255, 255, 255))

        canvas.convert("RGB").save(frame_dir / f"frame_{start_index + i:05d}.jpg",
                                   quality=90)
        if progress_cb and (i % report_every == 0 or i == total - 1):
            try:
                progress_cb((i + 1) / total)
            except Exception:  # noqa: BLE001
                logger.debug("render_kenburns_frames: 降级忽略", exc_info=True)
    return total


def generate_fallback_video(
    request: VideoGenerateRequest,
    out_path: str | Path,
    progress_cb: Callable[[float, str], None] | None = None,
) -> dict:
    """降级真实管线：渲染帧序列 → FFmpeg 编码为真实可播放视频文件。

    进度映射：帧渲染 0.0~0.6，编码 0.6~1.0。
    产出信息 dict：{"output","encoder","duration_s","size_bytes","frames"}。

    Raises:
        RuntimeError: FFmpeg 不可用或编码失败（调用方标记任务 failed）。
    """
    from ..encoder_service import RESOLUTION_MAP, get_encoder_service

    enc = get_encoder_service()
    if not enc.available:
        raise RuntimeError(
            "FFmpeg 不可用（runtime/ffmpeg、tools/downloads、PATH 均未找到），"
            "无法导出视频文件")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    width, height = RESOLUTION_MAP.get((request.resolution or "").lower(),
                                       (1920, 1080))
    frame_dir = out_path.parent / f"{out_path.stem}_frames"

    def render_progress(f: float) -> None:
        if progress_cb:
            progress_cb(f * 0.6, "render_frames")

    def encode_progress(f: float, msg: str = "") -> None:
        if progress_cb:
            progress_cb(0.6 + f * 0.4, "encode")

    try:
        frames = render_kenburns_frames(
            request.description, request.screenshot_4in1,
            frame_dir, width, height,
            fps=request.fps, duration_s=request.duration_seconds,
            progress_cb=render_progress,
        )
        info = enc.encode_frames_to_video(
            frame_dir, out_path,
            fps=request.fps, resolution=request.resolution,
            codec=request.codec or "h264",
            progress_cb=encode_progress,
            frame_pattern="frame_%05d.jpg",
            audio_path=request.audio_path,
        )
        info["frames"] = frames
        return info
    finally:
        shutil.rmtree(frame_dir, ignore_errors=True)


class VideoEngine(BaseEngine):
    """视频推理引擎——LTX-2 / Wan2.1 / CogVideoX / AnimateLCM(F-07)。"""

    name = "video"
    serves_categories = ("video", "video_gen")

    def __init__(self) -> None:
        super().__init__()
        self._pipeline: Any = None
        self._model: VideoModel | None = VideoModel.COGVIDEOX_2B_CPU
        self._model_name: str = ""
        self._loaded = False
        self._fallback_mode = False
        self._device = "cpu"
        self._router = VideoRouter()
        # AnimateLCM 图生视频分支（F-07）运行时状态
        self._animatelcm_pipe: Any = None
        self._animatelcm_attempted = False
        self._animatelcm_error = ""
        # 视频模型自动装载尝试标记（导入 models/ 即可用，失败只试一轮）
        self._video_autoload_attempted = False
        # 真实管线显存记账/登记标签（_ensure_video_loaded 成功装载后设置；
        # unload_model 对称 track_free + 注销，保证 loaded_models 视图与
        # GPU 实际占用一致）
        self._pipeline_track_label = ""
        self._pipeline_registered_id = ""
        # LTX 同权重双管线切换标记：当前 LTXImageToVideoPipeline 系由
        # LTXPipeline 组件重组而来（纯文本请求时需还原 T2V）
        self._ltx_i2v_swapped = False
        # split 布局标记（2026-08-22 提速，团队审查后重构）：
        # _split_te_needed = 管线仅装载 DiT+VAE 常驻 GPU（T5 不装载，
        # 生成时临时上卡编码一次即释放，编码窗口 DiT 暂下卡腾位）；
        # _external_te_encode = int8 T5 常驻但生成走外部编码
        # （显式 dtype，绕开管线内部 encode_prompt 的 int8 dtype 回退）。
        # 替代全家 CPU offload（每步 RAM↔GPU 搬运 DiT，5s 视频需 16 分钟）
        self._split_te_needed = False
        self._external_te_encode = False
        # dit-only 细分档（2026-08-23 TI2V-5B 接入）：DiT+VAE 常驻
        # 装不下但 DiT 单独常驻装得下时，VAE 留 CPU、经 vae.encode/
        # decode 实例 wrap 阶段交换上卡（encode/decode 窗口 DiT 必然
        # 闲置或可下卡，峰值显存 = max(T5 窗口, DiT+去噪激活)）
        self._split_dit_only = False

        if _diffusers is None or _torch is None:
            logger.warning("视频引擎依赖不可用 (diffusers/torch)，将使用模拟模式")
            self._fallback_mode = True

    def load_model(
        self,
        model_path: str,
        model: VideoModel = VideoModel.COGVIDEOX_2B,
        device: str = "auto",
    ) -> bool:
        """加载视频生成模型管线。

        尝试按以下顺序加载:
          1. LTXVideoPipeline (LTX-2)
          2. CogVideoXPipeline (CogVideoX)
          3. WanPipeline (Wan2.1)
          4. DiffusionPipeline (通用)
          5. AnimateLCM 图生视频分支（F-07，探测链末尾、Ken Burns 之前；
             SD1.5 底座缺失时 gated，不影响本方法返回语义）

        Args:
            model_path: 模型目录路径
            model: 视频模型枚举
            device: 加载设备

        Returns:
            True 如果加载成功
        """
        if self._fallback_mode:
            return False

        if device == "auto":
            self._device = "cuda" if _torch.cuda.is_available() else "cpu"
        else:
            self._device = device

        dtype = _torch.float16 if self._device == "cuda" else _torch.float32

        # 按 model_index.json _class_name 精确命中优先（导入 models/ 任意
        # diffusers 视频模型即可用），未声明/未知时按候选链兜底
        pipeline_classes: list[tuple[str, Any]] = []
        try:
            import json as _json
            idx = Path(model_path) / "model_index.json"
            if idx.is_file():
                hint = str(_json.loads(idx.read_text(encoding="utf-8"))
                           .get("_class_name") or "")
                hint_cls = getattr(_diffusers, hint, None) if hint else None
                if hint_cls is not None:
                    pipeline_classes.append((hint, hint_cls))
        except Exception:  # noqa: BLE001 - 提示读取失败走候选链
            logger.debug("load_model: 降级忽略", exc_info=True)
        for cls_name in (*_VIDEO_PIPELINE_CLASSES.keys(), "DiffusionPipeline"):
            cls = getattr(_diffusers, cls_name, None)
            if cls is not None and all(n != cls_name for n, _ in pipeline_classes):
                pipeline_classes.append((cls_name, cls))

        for cls_name, cls in pipeline_classes:
            try:
                self._pipeline = cls.from_pretrained(model_path, torch_dtype=dtype)
                if self._device == "cuda":
                    self._pipeline = self._pipeline.to(self._device)

                # 启用加速
                try:
                    from .accelerator import get_accelerator
                    self._pipeline = get_accelerator().enable_for_pipeline(self._pipeline)
                except Exception:
                    logger.debug("load_model: 降级忽略", exc_info=True)

                self._model = model
                self._model_name = model_path
                self._loaded = True
                self._fallback_mode = False
                # 上方重绑 _pipeline 已丢弃旧管线引用：对称清理自动装载
                # 链可能遗留的显存记账/台账登记（本路径的新管线由
                # ModelManager.ensure_loaded 以 model_id 另行记账）
                self._release_pipeline_bookkeeping()
                logger.info("视频模型加载成功 (%s): %s", cls_name, model_path)
                return True
            except Exception as e:
                logger.debug("%s 加载失败: %s", cls_name, e)

        # AnimateLCM 图生视频分支（F-07）：真实视频管线全部加载失败后、
        # 宣布降级（Ken Burns）之前探测装载；成功则由 generate() 优先使用。
        # SD1.5 底座缺失时 load_animatelcm 内部 gated 返回 False，不改变
        # 本方法对"所请求真实模型未加载"的语义。
        self.load_animatelcm(device=self._device)

        logger.warning("视频模型加载失败，切换到模拟模式")
        self._fallback_mode = True
        return False

    def load_animatelcm(self, device: str = "auto") -> bool:
        """AnimateLCM 图生视频分支（F-07）加载。

        探测链位置：LTX-2/Wan2.1/CogVideoX 之后、Ken Burns 之前。
        管线组成：MotionAdapter（AnimateLCM_sd15_t2v.ckpt 运动模块）
        + AnimateDiffPipeline.from_pretrained(SD1.5 底座)
        + LCMScheduler（AnimateLCM 少步采样）。

        ckpt 转换优先走 diffusers 自带 from_single_file 转换入口；
        离线拉取参考配置失败时，回退手动调用 diffusers 自带转换函数
        convert_animatediff_checkpoint_to_diffusers + 默认 MotionAdapter 装载
        （本机 ckpt 已验证：unexpected=0，missing 仅 pos_embed 运行时 buffer）。

        SD1.5 底座缺失时 gated（原因见 get_animatelcm_status().reason），
        返回 False，调用方回落 Ken Burns，现状行为不变。
        """
        if self._animatelcm_pipe is not None:
            return True
        if _diffusers is None or _torch is None:
            self._animatelcm_error = "diffusers/torch 依赖不可用"
            return False
        # B3（2026-09-13）路由诚实守卫（与 _ensure_video_loaded 同源）：
        # AnimateLCM 是 F-07 兜底链，权重（ckpt+SD15）已入隔离区、路由
        # 表仅 minimax-h3——产品路径不可达，探测装载本身即浪费。路由
        # 表解锁 diffusers 条目时本闸自动失效。
        from ...data.models import VIDEO_ROUTING_TABLE
        _routed_a = {str(e.get("model") or "") for e in VIDEO_ROUTING_TABLE}
        if not (_routed_a - {VideoModel.MINIMAX_H3.value,
                             VideoModel.MINIMAX_H3.name}):
            self._animatelcm_error = (
                "路由表仅 minimax-h3（B3 守卫）：AnimateLCM 兜底链停用")
            return False
        self._animatelcm_attempted = True

        status = get_animatelcm_status()
        if not status["ready"]:
            self._animatelcm_error = status["reason"]
            logger.info("AnimateLCM 不可用: %s", status["reason"])
            return False

        if device == "auto":
            device = "cuda" if _torch.cuda.is_available() else "cpu"
        dtype = _torch.float16 if device == "cuda" else _torch.float32

        # 显存门控（与 _ensure_video_loaded 同一闸门语义：超出空闲 2%
        # 余量即拦截）：AnimateLCM 无 CPU offload 路径，空闲不足时 gated
        # 返回 False 回落 Ken Burns，避免上卡 OOM 拖垮在载模型。
        required_gb = 0.0
        if device == "cuda":
            required_gb = _estimate_animatelcm_vram_gb(status["sd15_path"])
            free_gb = _cuda_free_gb()
            if free_gb > 0 and required_gb > free_gb * 0.98:
                self._animatelcm_error = (
                    f"显存不足：AnimateLCM 预估 {required_gb:.1f}GB，"
                    f"当前空闲 {free_gb:.1f}GB")
                logger.info("AnimateLCM 显存门控拦截: %s",
                            self._animatelcm_error)
                return False

        try:
            # 1) 运动模块：from_single_file（diffusers 自带转换入口）优先
            adapter = None
            try:
                adapter = _diffusers.MotionAdapter.from_single_file(
                    str(ANIMATELCM_CKPT_PATH), torch_dtype=dtype)
            except Exception as exc:  # noqa: BLE001 - 离线无 HF 配置时走手动转换
                logger.debug("MotionAdapter.from_single_file 不可用（%s），"
                             "回退 diffusers 手动转换", exc)
                from diffusers.loaders.single_file_utils import (
                    convert_animatediff_checkpoint_to_diffusers,
                )
                # B0（2026-09-13）：weights_only=True（恶意权重文件
                # RCE 链收口；AnimateDiff 官方 ckpt 为纯 state_dict）
                raw = _torch.load(str(ANIMATELCM_CKPT_PATH),
                                  map_location="cpu", weights_only=True)
                converted = convert_animatediff_checkpoint_to_diffusers(raw)
                adapter = _diffusers.MotionAdapter()
                missing, unexpected = adapter.load_state_dict(converted,
                                                              strict=False)
                # pos_embed.pe 为 sincos 运行时 buffer，无需权重载入
                real_missing = [k for k in missing
                                if not k.endswith("pos_embed.pe")]
                if real_missing or unexpected:
                    raise RuntimeError(
                        "AnimateLCM ckpt 键不匹配: "
                        f"missing={real_missing[:3]} unexpected={unexpected[:3]}") from exc
                adapter = adapter.to(dtype=dtype)

            # 2) SD1.5 底座 + 运动模块组成 AnimateDiffPipeline
            pipe = _diffusers.AnimateDiffPipeline.from_pretrained(
                status["sd15_path"], motion_adapter=adapter, torch_dtype=dtype)
            # 3) LCM 调度器（AnimateLCM 少步采样）
            pipe.scheduler = _diffusers.LCMScheduler.from_config(
                pipe.scheduler.config)
            if device == "cuda":
                pipe = pipe.to("cuda")

            self._animatelcm_pipe = pipe
            self._animatelcm_error = ""
            # 显存记账（对齐 model_manager 的 vram_manager.track_alloc
            # 机制；CPU 加载不占显存不记账），引擎卸载/推理失败丢弃
            # 管线时对称 track_free
            if device == "cuda" and required_gb > 0:
                try:
                    from ...engines.vram_manager import get_vram_manager
                    get_vram_manager().track_alloc(
                        ANIMATELCM_MODEL_LABEL, required_gb * 1024.0)
                except Exception:  # noqa: BLE001 - 记账失败不阻断加载
                    logger.debug("load_animatelcm: 降级忽略", exc_info=True)
            logger.info("AnimateLCM 管线加载成功（SD1.5 底座: %s）",
                        status["sd15_path"])
            return True
        except Exception as exc:  # noqa: BLE001 - 加载失败回落 Ken Burns
            self._animatelcm_pipe = None
            self._animatelcm_error = f"AnimateLCM 加载失败: {exc}"
            # 部分加载的管线残留显存：引用置 None 后回收（幂等）
            _release_gpu_cache()
            logger.warning("%s", self._animatelcm_error)
            return False

    def load_diffusers_model(
        self,
        model_path: str,
        model: VideoModel = VideoModel.COGVIDEOX_2B,
        device: str = "auto",
    ) -> bool:
        """真实 diffusers 视频模型加载钩子（LTX-2/Wan2.1/CogVideoX 下载后启用）。

        语义与 load_model 一致，单独命名以便 ModelManager.ensure_loaded()
        等外部服务按统一钩子调用；加载成功后 generate() 自动走真实管线。
        """
        return self.load_model(model_path, model=model, device=device)

    def unload_model(self) -> None:
        """卸载引擎持有的全部大模型引用并回收显存（幂等，可重复调用）。

        审计修复：AnimateLCM 分支（_animatelcm_pipe）此前不在
        ModelManager 通用卸载属性清单内，卸载后残留显存。本方法为
        引擎自定义卸载契约（ModelManager.unload_model 优先调用）：
        真实视频管线与 AnimateLCM 管线一并置 None，随后
        gc.collect() + torch.cuda.empty_cache() 回收显存。
        _model 仅为 VideoModel 枚举标签（非显存持有者），同步置 None
        防止误用；读取点已做 None 守卫。

        本方法为显存释放唯一入口：置 None + track_free（AnimateLCM 与
        真实管线记账标签）+ ModelManager 登记注销 + 回收三件套严格对称，
        任何路径不得只清记账不释放引用（或只释放引用不清记账）。
        """
        self._pipeline = None
        self._animatelcm_pipe = None
        self._model = None
        self._loaded = False
        self._ltx_i2v_swapped = False
        self._split_te_needed = False
        self._external_te_encode = False
        self._split_dit_only = False
        # AnimateLCM 显存记账对称释放（未记账时 track_free 返回 0）
        try:
            from ...engines.vram_manager import get_vram_manager
            get_vram_manager().track_free(ANIMATELCM_MODEL_LABEL)
        except Exception:  # noqa: BLE001
            logger.debug("unload_model: 降级忽略", exc_info=True)
        # 真实管线引用已置 None：对称清理其显存记账与 ModelManager 登记
        self._release_pipeline_bookkeeping()
        # 允许卸载后重新探测 AnimateLCM 分支（与 ModelManager 复位
        # _video_autoload_attempted 的重试语义对齐）
        self._animatelcm_attempted = False
        _release_gpu_cache()
        logger.info("视频引擎已卸载（真实管线 + AnimateLCM 分支）")

    def _release_pipeline_bookkeeping(self) -> None:
        """对称清理真实管线的显存记账与 ModelManager 加载台账登记（幂等）。

        仅在管线引用已被置 None/重绑之后调用——记账与引用同生共死；
        经 mgr.unload_model 卸载时台账条目已被弹出，unregister_load
        幂等返回 False。
        """
        if self._pipeline_track_label:
            try:
                from ...engines.vram_manager import get_vram_manager
                get_vram_manager().track_free(self._pipeline_track_label)
            except Exception:  # noqa: BLE001
                logger.debug("_release_pipeline_bookkeeping: 降级忽略", exc_info=True)
            self._pipeline_track_label = ""
        if self._pipeline_registered_id:
            try:
                from ..model_manager import get_model_manager
                get_model_manager().unregister_load(
                    self._pipeline_registered_id)
            except Exception:  # noqa: BLE001
                logger.debug("_release_pipeline_bookkeeping: 降级忽略", exc_info=True)
            self._pipeline_registered_id = ""

    def _ensure_video_loaded(self, prefer_i2v: bool = False) -> None:
        """视频模型自动装载（导入 models/ 即可用）。

        扫描 models/ 下 diffusers 布局的视频模型（Wan/CogVideoX/LTX/
        HunyuanVideo 等），按显存需求升序尝试装载：空闲显存足够时直接
        上卡；超出时启用 model CPU offload（速度换可用性）。一轮尝试后
        无论成败都置标记，避免每次生成重复扫描加载。

        Args:
            prefer_i2v: 请求带参考图时为 True——仅考虑 I2V 能力模型
                （跳过 T2V-only 如 LTX 基础管线），避免"图片被忽略"的
                任务形态错配；纯文本请求保持原全量排序。
        """
        if self._loaded or self._fallback_mode or self._video_autoload_attempted:
            return
        # B3（2026-09-13）路由诚实守卫：VIDEO_ROUTING_TABLE 仅 minimax-h3
        # （漫剧视频主力锁定令，09-06），diffusers 自动装载在任何产品路径
        # 上都到不了——models/ 下遗留的 Wan/CogVideoX/LTX/HunyuanVideo
        # 权重一旦被导入登记，旧逻辑会把它往卡上装（分钟级装载 + 数 GB
        # 常驻 + 与 H3/对话链抢显存），而该结果永远不会被路由选中。守卫
        # 语义：AnalyzeDiffusion 路由被解锁（路由表加回 diffusers 条目）
        # 时本闸自动失效，代码路径整体保留零删除。
        from ...data.models import VIDEO_ROUTING_TABLE
        _routed = {str(e.get("model") or "") for e in VIDEO_ROUTING_TABLE}
        _diffusers_routed = bool(
            _routed - {VideoModel.MINIMAX_H3.value, VideoModel.MINIMAX_H3.name})
        if not _diffusers_routed:
            logger.info(
                "diffusers 视频管线跳过自动装载（路由表仅 minimax-h3，"
                "B3 路由诚实守卫；导入 diffusers 权重不再触装载）")
            return
        self._video_autoload_attempted = True
        if _diffusers is None or _torch is None:
            return
        discovered = {n: i for n, i in discover_video_models().items()
                      if i["diffusers_available"]
                      and (i["i2v"] or not prefer_i2v)}
        if not discovered:
            logger.info("未发现可用 diffusers 视频模型（models/ 导入 Wan/"
                        "CogVideoX/LTX/HunyuanVideo 即可自动启用）")
            return

        free_gb = _cuda_free_gb()
        self._device = "cuda" if _torch.cuda.is_available() else "cpu"
        # 装载前强制清场（2026-08-22 OOM 根因修复）：卸载 VL 增强等
        # 遗留的空闲模型，让 split 布局判定基于真实空闲显存
        # （详见 _evict_idle_models_for_video 注释）
        if self._device == "cuda":
            _freed_gb = _evict_idle_models_for_video()
            if _freed_gb > 0:
                free_gb = _cuda_free_gb() or free_gb
        dtype = _torch.float16 if self._device == "cuda" else _torch.float32
        # 小模型优先（降低 OOM 风险，缩短首载时间）。
        # 混合架构裁定（2026-08-23）：带图请求（I2V/文+图）优先 TI2V
        # 单权重——Wan2.2-TI2V 单 ckpt 原生双条件（文本+首帧），是
        # 视频侧统一底座；装不下（显存门控逐档降级）时其余候选仍按
        # 显存升序保底（vace-1.3b 等），纯文本请求保持全量升序不变。
        last_load_error: Exception | None = None

        def _cand_order(kv: tuple[str, dict]) -> tuple[int, float]:
            ti2v_first = (prefer_i2v and kv[1].get("i2v")
                          and "ti2v" in kv[0].lower())
            return (0 if ti2v_first else 1, kv[1]["vram_gb"])

        for name, info in sorted(discovered.items(), key=_cand_order):
            cls = getattr(_diffusers, info["class_name"], None)
            if cls is None:
                continue
            # 显存门控：超出空闲 2% 余量时改 CPU offload（慢但可用）
            use_offload = (
                self._device == "cuda" and free_gb > 0
                and info["vram_gb"] > free_gb * 0.98)
            # 推理激活显存余量（GB）：整卡运行判定必须为去噪中间量与
            # VAE 解码留出余量。实测教训（2026-08-22）：int8 后权重
            # 12.6GB < 空闲 13.4GB 被判定可整卡，但 80 帧推理激活叠加
            # 后显存贴满 96%，allocator 反复回收导致推理龟速（2 分钟
            # 停留在 3%）。预留不足时宁走 CPU offload（稳定 ~35s 路径）
            _ACTIVATION_RESERVE_GB = 2.5
            extra_kwargs: dict = {}
            split_te = False
            external_encode = False
            if use_offload:
                # 文本编码器 int8 量化：T5-XXL 减半后按三档尝试——
                # 档 1 int8 全家整卡常驻；档 2 split 布局（仅 DiT+VAE
                # 常驻，T5 生成时临时上卡编码一次即释放，编码窗口
                # DiT 暂下卡腾位——16GB 卡对话模型常驻时 T5 9.3GB 与
                # DiT+VAE 无法同时上卡，编排是物理必须而非优化）；
                # 档 3 全家 CPU offload（原路径）。
                # split 白名单：仅 Wan*/CogVideoX*（encode_prompt 2 元组
                # 契约）；LTX 返回 4 元组（含 attention mask），外部编码
                # 解包会错把 mask 当 negative embeds 注入，禁止入档。
                _te_fp16_gb = (_dir_load_bytes_fp16(
                    Path(info["path"]) / "text_encoder") / (1024 ** 3))
                # 非文本编码器常驻量按目录实测（transformer/transformer_2/
                # vae/image_encoder*）。旧公式 vram_gb/1.15 - te 对大 DiT
                # 模型系统性虚高（TI2V-5B：DiT 10.2 + VAE 0.5 ≈ 10.8GB
                # 实测，旧公式算出 ~14GB），1.15 装载系数误差放大后
                # 16GB 卡上 split 恒被误杀（10.8 + 2.5 激活余量 =
                # 13.3GB 本可整卡常驻）。实测字节才是物理真源。
                _non_te_gb = sum(
                    _dir_load_bytes_fp16(Path(info["path"]) / sub)
                    for sub in ("transformer", "transformer_2", "vae",
                                "image_encoder", "image_encoder_2",
                                "image_encoder_3")
                    if (Path(info["path"]) / sub).is_dir()
                ) / (1024 ** 3)
                # DiT 单独常驻量（档 2b）：dit-only 布局的门槛基数。
                # TI2V-5B 实测：DiT+VAE 10.62 + 激活 2.5 = 13.12GB 在
                # 清场后空闲 ~12.5GB（16GB 日用卡：GUI ~2 + bge 1.3）
                # 差 ~1GB 恒被拒；DiT 9.31 + 2.5 = 11.81 可过，VAE
                # 经 _install_vae_stage_swap 阶段交换（encode/decode
                # 窗口临时上卡，见其 docstring）
                _dit_gb = sum(
                    _dir_load_bytes_fp16(Path(info["path"]) / sub)
                    for sub in ("transformer", "transformer_2")
                    if (Path(info["path"]) / sub).is_dir()
                ) / (1024 ** 3)
                _cls_name = info["class_name"]
                _split_ok = (_cls_name.startswith("Wan")
                             or _cls_name.startswith("CogVideoX"))
                dit_only = False
                dit_int8 = False
                if (_split_ok and _non_te_gb > 0
                        and _non_te_gb + _ACTIVATION_RESERVE_GB
                        <= free_gb * 0.98):
                    # 档 2a split：跳过 T5 装载（from_pretrained 传
                    # text_encoder=None），DiT+VAE 上卡。pipe.device
                    # 锚定 vae（cuda）恒定，无 T5 迁移导致的设备漂移
                    extra_kwargs["text_encoder"] = None
                    use_offload = False
                    split_te = True
                    logger.info(
                        "split 布局生效: %s DiT+VAE %.1fGB 常驻 GPU，"
                        "T5 生成时临时上卡编码（免 CPU offload 每步搬运）",
                        name, _non_te_gb)
                elif (_split_ok and _dit_gb > 0
                        and _dit_gb + _ACTIVATION_RESERVE_GB
                        <= free_gb * 0.98):
                    # 档 2b dit-only：仅 DiT 常驻，VAE 留 CPU 阶段交换
                    extra_kwargs["text_encoder"] = None
                    use_offload = False
                    split_te = True
                    dit_only = True
                    logger.info(
                        "split dit-only 布局生效: %s DiT %.1fGB 常驻 GPU"
                        "（+VAE %.1fGB 差 %.1fGB 装不下），VAE encode/"
                        "decode 窗口临时上卡",
                        name, _dit_gb, _non_te_gb - _dit_gb,
                        _non_te_gb + _ACTIVATION_RESERVE_GB
                        - free_gb * 0.98)
                elif (_split_ok and _dit_gb > 0
                        and _dit_gb <= free_gb + 0.8
                        and _dit_gb * 0.58 + _ACTIVATION_RESERVE_GB
                        <= free_gb * 0.98):
                    # 档 2c dit-int8：DiT int8 量化常驻（bnb 惰性量化，
                    # 稳态 ~5.2GB）。坏显存日子（GUI 高占用，free ~9GB）
                    # 的兜底：fp16 裸量上卡峰值 9.31 需 free+0.8 内
                    #（WDDM 共享配额可吸收瞬时超卖），量化逐层释放 fp16
                    # 收敛稳态。0.58 = int8 半字节 + embedding/Head fp16
                    # 残留与 SCB 状态实测系数（对齐 T5 int8 0.82 的口径）
                    extra_kwargs["text_encoder"] = None
                    use_offload = False
                    split_te = True
                    dit_only = True
                    dit_int8 = True
                    logger.info(
                        "split dit-int8 布局生效: %s DiT int8 约 %.1fGB "
                        "常驻 GPU（fp16 %.1fGB 装不下），首步含一次性量化",
                        name, _dit_gb * 0.58, _dit_gb)
                else:
                    te = _load_text_encoder_int8(Path(info["path"]), dtype)
                    if te is not None:
                        # te 加载即上卡（bnb 落 cuda:0）——重测空闲再
                        # 判定档 1（团队审查 P1-1：装载前口径虚高）。
                        # 实测系数 0.82（2026-08-22 实验 v2）：int8 主干
                        # + embedding 256384x4096 fp16 不量化（2.0GB）
                        # + SCB 状态，UMT5 实占 9.32GB（非 fp16 的一半）
                        te_int8_gb = _te_fp16_gb * 0.82
                        free_gb = _cuda_free_gb() or free_gb
                        if (info["vram_gb"] - te_int8_gb
                                + _ACTIVATION_RESERVE_GB <= free_gb * 0.98):
                            extra_kwargs["text_encoder"] = te
                            use_offload = False
                            external_encode = True
                            logger.info(
                                "int8 编码器生效: %s 预估 %.1fGB -> %.1fGB，"
                                "整卡运行（含 %.1fGB 激活余量，外部编码）",
                                name, info["vram_gb"],
                                info["vram_gb"] - te_int8_gb,
                                _ACTIVATION_RESERVE_GB)
                        else:
                            # 档 1 装不下：释放探测加载的 te 回原路径。
                            # 重采样 free（2026-08-23 修复）：te 探测期间
                            # free 读数被自身 8.7GB 占位污染，不重测会让
                            # 后续候选的门槛判定全部误判偏紧
                            del te
                            _release_gpu_cache()
                            free_gb = _cuda_free_gb() or free_gb
            try:
                logger.info("视频模型自动装载: %s (%s, 预估 %.1fGB, 空闲 %.1fGB%s)",
                            name, info["class_name"], info["vram_gb"], free_gb,
                            ", CPU offload" if use_offload else "")
                pipe = cls.from_pretrained(info["path"], torch_dtype=dtype,
                                           **extra_kwargs)
                if self._device == "cuda":
                    if use_offload:
                        try:
                            pipe.enable_model_cpu_offload()
                        except Exception:  # noqa: BLE001 - 不支持 offload 则整载
                            pipe = pipe.to("cuda")
                    elif split_te:
                        # split 布局：DiT 常驻 GPU；text_encoder 已以
                        # None 装载（from_pretrained 跳过），生成时临时
                        # 上卡编码（generate() 编排）。dit-only 档 VAE
                        # 留 CPU，encode/decode 窗口经 wrap 阶段交换；
                        # dit-int8 档 DiT 先 int8 替换再上卡（量化在
                        # 首个 forward 逐层完成并释放 fp16）
                        if dit_int8:
                            _quantize_transformer_int8(pipe.transformer)
                        pipe.transformer.to("cuda")
                        if dit_only:
                            _install_vae_stage_swap(pipe)
                        else:
                            pipe.vae.to("cuda")
                    else:
                        pipe = pipe.to("cuda")
                    # VAE 空间 tiling（2026-08-22 VAE encode 溢出防护）：
                    # 所有 cuda 路径统一开启。Wan VAE encode 时间分块
                    # （4 帧）但无空间分块时激活 ∝ 全幅面积（1024x576
                    # 档 ~8-12GB），叠加常驻权重易触发 sysmem fallback；
                    # tile 256x256 + stride 192 削峰约 9 倍，decode 同
                    # 路径生效。hasattr 守卫防 diffusers 版本差异。
                    try:
                        if hasattr(pipe.vae, "enable_tiling"):
                            pipe.vae.enable_tiling()
                            logger.info("VAE 空间 tiling 已启用 "
                                        "(tile=256, stride=192)")
                    except Exception as exc:  # noqa: BLE001 - tiling 失败不阻断
                        logger.warning("VAE tiling 启用失败（继续非 tiled）: %s",
                                       exc)
                try:
                    from .accelerator import get_accelerator
                    pipe = get_accelerator().enable_for_pipeline(pipe)
                except Exception:  # noqa: BLE001
                    logger.debug("_ensure_video_loaded: 降级忽略", exc_info=True)
                # split 布局自愈（2026-08-22 审查防御）：accelerator 在
                # xformers/SDP 均不可用时会回退 enable_model_cpu_offload，
                # 把已上卡的 DiT+VAE 搬回 CPU 摧毁常驻编排——检测组件
                # 设备偏离并搬回
                if split_te and self._device == "cuda":
                    # dit-only 档 vae 故意留 CPU（wrap 阶段交换），不在
                    # 自愈范围
                    _heal = ("transformer", "transformer_2") if dit_only \
                        else ("transformer", "transformer_2", "vae")
                    for _cn in _heal:
                        _comp = getattr(pipe, _cn, None)
                        try:
                            # device.type 比较：str(device) 是
                            # "cuda:0" 含卡号，直接 != "cuda" 恒真误报
                            if (_comp is not None
                                    and _comp.device.type != "cuda"):
                                _comp.to("cuda")
                                logger.warning(
                                    "split 布局自愈: %s 被移回 CPU，"
                                    "已搬回 cuda", _cn)
                        except Exception:  # noqa: BLE001 - device 探测失败跳过
                            logger.debug("_ensure_video_loaded: 降级忽略", exc_info=True)
                self._pipeline = pipe
                self._model_name = info["path"]
                self._loaded = True
                self._fallback_mode = False
                self._split_te_needed = split_te
                self._external_te_encode = external_encode
                self._split_dit_only = split_te and dit_only
                # 同步模型枚举，使 generate() 取得正确的参数预设
                # （分辨率/帧率/时长上限），未登记目录名保持默认
                try:
                    self._model = VideoModel(name)
                except ValueError:
                    logger.debug("_ensure_video_loaded: 降级忽略", exc_info=True)
                # 显存记账 + ModelManager 加载台账登记（审计修复：自动
                # 装载链此前绕过 ensure_loaded，loaded_models 与 GPU 实际
                # 占用脱节）。上卡才占显存；CPU 装载登记 vram_gb=0。
                # split 布局按实际常驻量（DiT+VAE，同源 vram 口径）
                # 登记，T5 瞬态编码窗口不占常态显存。释放唯一入口为
                # unload_model（置 None + track_free + 注销登记 + 回收
                # 三件套），与装载严格对称。
                if split_te:
                    # split 常驻量按实测非 TE 字节 + 1.15 装载余量
                    # （与门槛判定同源，2026-08-23 大 DiT 修正）；
                    # dit-only 档仅 DiT 常驻（VAE 留 CPU 不占常态），
                    # dit-int8 档按量化稳态（0.58 系数同门槛口径）
                    if dit_only:
                        reg_vram_gb = float(
                            _dit_gb * (0.58 if dit_int8 else 1.0) * 1.15)
                    else:
                        reg_vram_gb = float(_non_te_gb * 1.15)
                else:
                    reg_vram_gb = float(info["vram_gb"])
                if self._device != "cuda":
                    reg_vram_gb = 0.0
                if self._device == "cuda":
                    try:
                        from ...engines.vram_manager import get_vram_manager
                        label = f"video_pipeline:{name}"
                        get_vram_manager().track_alloc(
                            label, reg_vram_gb * 1024.0)
                        self._pipeline_track_label = label
                    except Exception:  # noqa: BLE001 - 记账失败不阻断加载
                        logger.debug("_ensure_video_loaded: 降级忽略", exc_info=True)
                try:
                    from ..model_manager import get_model_manager
                    if get_model_manager().register_external_load(
                            "video", name, info["path"], reg_vram_gb):
                        self._pipeline_registered_id = name
                except Exception:  # noqa: BLE001 - 登记失败不阻断加载
                    logger.debug("_ensure_video_loaded: 降级忽略", exc_info=True)
                logger.info("视频模型自动装载成功: %s (%s)", name,
                            info["class_name"])
                return
            except Exception as exc:  # noqa: BLE001 - 试下一个候选
                logger.warning("视频模型 %s 装载失败（试下一个）: %s", name, exc)
                last_load_error = exc
        logger.info("所有已发现视频模型装载失败，保持 AnimateLCM/Ken Burns 降级链")
        # 并发装载冲突自愈（2026-08-22）：冷启动窗口任务来得太快时，视频
        # 装载会与对话模型加载并发触发 accelerate meta tensor 冲突双败。
        # 此类瞬时失败清除一次性标记，下个任务自动重试——否则一次撞上
        # 冷启动窗口就永久降级 Ken Burns（进程生命周期内不再尝试）
        if last_load_error is not None and "meta tensor" in str(last_load_error).lower():
            self._video_autoload_attempted = False
            logger.warning("装载撞上并发加载冲突（meta tensor），"
                           "已重置标记，下个任务将重试")

    def generate_fallback(
        self,
        request: VideoGenerateRequest,
        out_path: str | Path,
        progress_cb: Callable[[float, str], None] | None = None,
    ) -> dict:
        """降级真实管线（模块函数 generate_fallback_video 的实例封装）。"""
        return generate_fallback_video(request, out_path, progress_cb)

    def prepare_generation(self, light: bool = False) -> str:
        """生成前探测链（导入 models/ 即可用），返回将走的路径。

        Returns:
            "pipeline"   : 真实 diffusers 视频模型已加载/自动装载成功
            "animatelcm" : AnimateLCM 图生视频分支可用（F-07）
            "kenburns"   : 均不可用，回落 Ken Burns 降级真实管线
        """
        if self.is_ready:
            return "pipeline"
        # H3 管线（ComfyUI 子进程，2026-08-25）：权重就绪即报真实
        # 管线（generate() 内分派，不占本进程 diffusers 装载链）
        try:
            from .h3_engine import h3_available
            if h3_available():
                return "pipeline"
        except Exception:  # noqa: BLE001 - 探测失败走 diffusers 判定
            logger.debug("prepare_generation: 降级忽略", exc_info=True)
        if light:
            # 轻探测（2026-08-22 乒乓装载修复）：models/ 存在可装载模型
            # 即报 pipeline，实际装载交由 generate() 内部完成——时序为
            # VL 预处理（对话模型先就位）→ 视频管线装载，避免 worker
            # 探测先装视频管线、随后 VL 加载又将其驱逐的反复换载
            try:
                if any(i.get("diffusers_available")
                       for i in discover_video_models().values()):
                    return "pipeline"
            except Exception:  # noqa: BLE001 - 探测失败走降级判定
                logger.debug("prepare_generation: 降级忽略", exc_info=True)
        else:
            self._ensure_video_loaded()
            if self.is_ready:
                return "pipeline"
        if not self._animatelcm_attempted:
            try:
                self.load_animatelcm()
            except Exception:  # noqa: BLE001
                logger.debug("prepare_generation: 降级忽略", exc_info=True)
        if self._animatelcm_pipe is not None:
            return "animatelcm"
        return "kenburns"

    def select_model(self, available_vram_gb: float) -> VideoModel:
        """根据可用显存选择视频模型（委托给 VideoRouter）。"""
        return self._router.select_model(available_vram_gb)

    def validate_video_duration(self, request: VideoGenerateRequest, model: VideoModel) -> None:
        """校验视频时长（规格 §10.1）。"""
        self._router.validate_video_duration(request, model)

    # ── MiniMax H3（ComfyUI 子进程管线，2026-08-25） ──────────────

    # 分辨率档映射：H3 画幅 768 短边（1344x768 顶格）；720p 请求映射
    # 0.4MP 档（864x480，官方模板 ResolutionSelector 同款）；480p 落
    # 0.2MP 档（608x352）；其余（1080p/2k/4k）→ 顶格档
    _H3_RES_MAP: dict[str, tuple[int, int]] = {
        "720p": (864, 480), "480p": (608, 352),
    }

    def _h3_routed_default(self) -> bool:
        """按当前显存路由，H3 是否为默认选中模型（且管线就绪）。"""
        try:
            from .h3_engine import h3_available
            if not h3_available():
                return False
            return self._router.select_model(_cuda_free_gb()) \
                == VideoModel.MINIMAX_H3
        except Exception:  # noqa: BLE001 - 预判失败按非 H3 处理
            return False

    def _will_use_h3(self, request: VideoGenerateRequest) -> bool:
        """本请求是否将走 H3 管线（override 显式 / 自动路由默认）。

        prompt 预处理前预判：H3 编码器 Qwen3-VL-32B 中文原生，
        VL 增强/翻译全免（也避免后端加载 qwen3-vl-4b 挤兑
        ComfyUI 子进程显存预算）。
        """
        if request.model_override:
            return request.model_override in (
                VideoModel.MINIMAX_H3.value, VideoModel.MINIMAX_H3.name)
        if self._loaded:
            return False  # diffusers 管线已装载，维持现役
        return self._h3_routed_default()

    def _generate_h3(self, request: VideoGenerateRequest,
                     effective_prompt: str,
                     reference_img: Image.Image | None,
                     gen_id: str, start_time: float,
                     relay: _ProgressRelay) -> VideoGenResult:
        """H3 生成：委托 H3Engine（ComfyUI 子进程 + HTTP API）。"""
        from .h3_engine import align_h3_frames, get_h3_engine, h3_available
        if not h3_available():
            raise ApiError(
                code=60003,
                message="MiniMax H3 管线未就绪（ComfyUI 或权重缺失）",
                suggestion="请确认 tools/ComfyUI_windows_portable 与 "
                           "models/video_gen/h3 权重完整",
            )
        width, height = self._H3_RES_MAP.get(request.resolution,
                                             (1344, 768))
        seconds = min(max(request.duration_seconds, 5.0), 15.0)
        out_path = VIDEO_OUT_DIR / f"h3_{gen_id}.mp4"
        get_h3_engine().generate(
            prompt=effective_prompt, width=width, height=height,
            seconds=seconds, out_path=out_path,
            first_frame=reference_img, progress_cb=relay)
        elapsed_ms = int((time.time() - start_time) * 1000)
        return VideoGenResult(
            id=gen_id,
            file_path=str(out_path),
            model_used=VideoModel.MINIMAX_H3.value,
            duration_seconds=round(align_h3_frames(seconds) / 24.0, 2),
            resolution=f"{width}x{height}",
            generation_time_ms=elapsed_ms,
            has_audio_sync=True,  # H3 原生音画联合生成（32kHz 立体声）
        )

    def _ltx_swap_capable(self) -> bool:
        """当前管线是否为 LTX 家族（可同权重组件重组为 I2V，免卸载换载）。"""
        if self._pipeline is None:
            return False
        return type(self._pipeline).__name__ in ("LTXPipeline", "LTXVideoPipeline")

    def _i2v_native_zh(self, request: VideoGenerateRequest) -> bool:
        """本请求是否会走中文原生 I2V 管线（Wan 系 UMT5，免翻译）。

        已装载 Wan 或 models/ 存在 Wan I2V 目录（带图请求将优先装载）
        时为 True。
        """
        if not request.screenshot_4in1:
            return False
        if self._pipeline is not None \
                and type(self._pipeline).__name__.startswith("Wan"):
            return True
        try:
            for info in discover_video_models().values():
                if info.get("i2v") \
                        and str(info.get("class_name", "")).startswith("Wan"):
                    return True
        except Exception:  # noqa: BLE001 - 探测失败按需翻译处理
            logger.debug("_i2v_native_zh: 降级忽略", exc_info=True)
        return False

    # Qwen3-VL 视频提示词增强系统指令（2026-08-22 语义贴合修复）：
    # 视频扩散模型（尤其 LTX 2B）对简短 prompt 的遵循能力弱，经 VL
    # 扩写为主体+动作+运镜+氛围的专业英文 prompt 后显著提升贴合度
    _VIDEO_PROMPT_SYSTEM = (
        "You compose ONE English video prompt (max 60 words, single "
        "line, comma-separated phrases) for image-to-video generation.\n"
        "RULES (strict priority order):\n"
        "1. The FIRST phrase MUST be the user's requested ACTION from "
        "the text request (dance, run, wave, turn, petals falling...).\n"
        "2. MUST include the user's requested SCENE/location exactly "
        "as the text request says (e.g. Temple of Heaven in Beijing).\n"
        "3. The attached image ONLY provides the person's appearance — "
        "copy it in ONE short phrase (clothing/look). The video's first "
        "frame IS this image, so appearance is already guaranteed.\n"
        "4. IGNORE any action/location visible in the image (sitting by "
        "a window etc.); the user's text ALWAYS overrides the image.\n"
        "5. End with a natural camera movement + atmosphere.\n"
        "Output ONLY the prompt itself. No explanation, no Chinese."
    )

    # 四视图参考图判定指令：VL 识别拼图布局并选出正面全身最佳象限。
    # 象限内仍含多个人物时（实测 2026-08-22：象限 A 内含正面+侧面两个
    # 人影）追加 -LEFT/-RIGHT 半幅精裁，保证 I2V 首帧单人物
    _MULTIVIEW_SYSTEM = (
        "You analyze character reference images for image-to-video "
        "generation. Determine if the image is a MULTI-VIEW sheet: "
        "multiple views of the same character arranged in a grid "
        "(2x2 layout: top-left=A, top-right=B, bottom-left=C, "
        "bottom-right=D) or a horizontal strip (left to right: "
        "A, B, C, D).\n"
        "If it is a multi-view sheet, reply with ONLY the single "
        "letter (A/B/C/D) of the quadrant containing the best "
        "FRONT-FACING FULL-BODY view of the character.\n"
        "If that quadrant still contains MULTIPLE figures, append "
        "-LEFT or -RIGHT to pick the half containing the single "
        "best front-facing full-body figure (e.g. A-LEFT, C-RIGHT).\n"
        "If it shows a single view only, reply with ONLY: SINGLE\n"
        "No other words, no explanation."
    )

    def _detect_and_crop_multiview(self, img: Any) -> Any:
        """四视图参考图检测与裁剪（I2V 首帧净化，2026-08-22）。

        角色资产图常为 2x2 四视图拼图——直接做 I2V 首帧会把宫格
        排版变成视频画面（"图片人物参考不符"根因之一）。经 VL 判定
        拼图并选出正面全身象限后裁出单视角，仅该视角进入视频。

        Args:
            img: 解码后的 PIL Image（RGB）

        Returns:
            裁剪后的 PIL Image；非拼图 / VL 不可用 / 判定失败时
            返回原 img（诚实降级，与未接入时行为一致）。
        """
        if img is None:
            return img
        try:
            from .dialog_engine import get_dialog_engine
            engine = get_dialog_engine()
            if not _ensure_video_vl(engine):
                return img
            # 判定用小图（布局识别无需高分辨率，省 prefill）
            judge = img.copy()
            w, h = judge.size
            if max(w, h) > 640:
                scale = 640 / max(w, h)
                judge = judge.resize((int(w * scale), int(h * scale)))
            text = engine.chat(
                [{"role": "system", "content": self._MULTIVIEW_SYSTEM},
                 {"role": "user", "content": [
                     {"type": "image"},
                     {"type": "text", "text": "Analyze this reference image."}]}],
                images=[judge],
                temperature=0.0,
                max_new_tokens=8,
            )
            answer = (text or "").strip().upper()
            logger.info("多视角判定原始回复: %r", answer[:60])
            # 解析优先级：独立词 A-D（可带 -LEFT/-RIGHT 半幅后缀）>
            # 短回复中首个 A-D 字符 > SINGLE > 无法解析（原图直通）。
            # 模型偶发整句回复（如 "BEST VIEW IS A"），此前 len>4 即
            # 判单视角会漏裁
            letter = ""
            half = ""   # "" 整象限 / "LEFT" / "RIGHT" 半幅精裁
            for tok in re.split(r"[\s,.\n:;!]+", answer):
                m = re.fullmatch(r"([ABCD])(?:[-_](LEFT|RIGHT|L|R))?",
                                 tok.strip("-"))
                if m:
                    letter = m.group(1)
                    half = {"L": "LEFT", "R": "RIGHT"}.get(
                        m.group(2) or "", m.group(2) or "")
                    break
            if not letter and "SINGLE" not in answer and len(answer) <= 12:
                letter = next((ch for ch in answer if ch in "ABCD"), "")
            if not letter:
                logger.info("参考图判定为单视角或无法解析，直通 I2V")
                return img
            w, h = img.size
            # 2x2 布局象限裁剪（横条 1x4 布局同样按象限取 1/4，
            # 左右优先取前两格，覆盖常见四视图排版）
            x0 = 0 if letter in "AB" else w // 2
            y0 = 0 if letter in "AC" else h // 2
            # 横条布局（高宽比 > 2）按四列切
            if w > h * 2:
                idx = "ABCD".index(letter)
                cx0, cx1 = w * idx // 4, w * (idx + 1) // 4
                cy0, cy1 = 0, h
            else:
                cx0, cx1, cy0, cy1 = x0, x0 + w // 2, y0, y0 + h // 2
            # 象限内多人物半幅精裁（VACE/LTX 首帧需单人物）
            if half in ("LEFT", "RIGHT"):
                mid = (cx0 + cx1) // 2
                if half == "LEFT":
                    cx1 = mid
                else:
                    cx0 = mid
                logger.info("象限 %s 含多人物，取 %s 半幅", letter, half)
            cropped = img.crop((cx0, cy0, cx1, cy1))
            logger.info("参考图判定为多视角拼图，裁出象限 %s%s 作为 I2V "
                        "首帧 (%dx%d -> %dx%d)", letter,
                        f"-{half}" if half else "", w, h,
                        cropped.width, cropped.height)
            return cropped
        except Exception as exc:  # noqa: BLE001 - 判定失败原图直通
            logger.warning("多视角检测跳过（原图直通）: %s", exc)
            return img

    def _enhance_video_prompt(self, request: VideoGenerateRequest,
                              base_prompt: str,
                              image: Any = None) -> str:
        """Qwen3-VL 视频提示词增强（图片主体 + 文字要求融合扩写）。

        I2V：参考图经 VL 提取主体描述，与用户动作/场景要求融合成
        英文视频 prompt；T2V：简短描述扩写。返回空串表示不可用
        （调用方回退 translate_prompt_zh2en 翻译链）。
        """
        # 已足够详细的 prompt（>30 词）跳过增强，省 VL 延迟
        if len(base_prompt.split()) > 30:
            return ""
        from .dialog_engine import get_dialog_engine
        engine = get_dialog_engine()
        if not _ensure_video_vl(engine):
            return ""
        images: list | None = None
        # 优先用调用方传入的已裁剪图片（多视角净化后），否则解码原图
        img = image
        if img is None and request.screenshot_4in1:
            try:
                from PIL import Image
                raw = base64.b64decode(request.screenshot_4in1)
                img = Image.open(io.BytesIO(raw)).convert("RGB")
            except Exception as exc:  # noqa: BLE001 - 图损坏按 T2V 增强
                logger.warning("参考图解码失败，按纯文本增强: %s", exc)
                img = None
        if img is not None:
            # 压到 560 短边：VL 看清主体即可，省 prefill token
            w, h = img.size
            if min(w, h) > 560:
                scale = 560 / min(w, h)
                img = img.resize((int(w * scale), int(h * scale)))
            images = [img]
        # Qwen-VL 多模态 content 格式（与 build_context 一致：
        # image 占位 + text；后端不支图时自动丢弃 images）
        if images:
            user_content: Any = [{"type": "image"} for _ in images]
            user_content.append({"type": "text",
                                 "text": (f"User's action & scene request "
                                          f"(MUST appear in output): "
                                          f"{base_prompt}\n"
                                          f"Attached image: person "
                                          f"appearance reference ONLY.")})
        else:
            user_content = f"Video request: {base_prompt}"
        messages = [{"role": "system", "content": self._VIDEO_PROMPT_SYSTEM},
                    {"role": "user", "content": user_content}]

        def _clean(raw: str) -> str:
            out = (raw or "").strip()
            out = re.sub(r'^["\'`\s]+|["\'`\s]+$', "", out)
            out = re.sub(r"^(prompt|video prompt)[:：]\s*", "", out,
                         flags=re.IGNORECASE)
            from .prompt_translator import contains_cjk as _has_cjk
            return "" if (not out or _has_cjk(out)) else out

        # 两轮尝试（2026-08-22 稳定性修复）：2b 小模型采样不稳定，
        # 偶发输出中文/空（"故宫跳舞"任务静默失败的根因）。首轮
        # temperature 0.4 保多样性，失败后 0.0 贪心重试。
        # 动作保真校验：输出丢失用户动作词（被参考图带偏）同样作废
        # 本轮，两轮均失败回退翻译链（其模板动作优先，可靠）
        out = ""
        try:
            for temp in (0.4, 0.0):
                text = engine.chat(messages, images=images,
                                   temperature=temp, max_new_tokens=200)
                out = _clean(text)
                if not out:
                    continue
                lost = _action_lost(base_prompt, out)
                if lost:
                    logger.warning(
                        "VL 增强丢失用户动作词 %r，本轮作废（输出: %s）",
                        lost, out[:80])
                    out = ""
                    continue
                break
        except Exception as exc:  # noqa: BLE001 - 增强失败回退翻译链
            logger.warning("VL 提示词增强失败: %s", exc)
            return ""
        if not out:
            logger.warning("VL 增强两轮均未产出合规英文 prompt"
                           "（输出空或含中文），回退视频翻译链")
            return ""
        logger.info("VL 增强视频 prompt: %r -> %r", base_prompt[:60],
                    out[:100])
        return out

    def generate(self, request: VideoGenerateRequest,
                 progress_cb: Callable[[float, str], None] | None = None
                 ) -> VideoGenResult:
        """执行视频生成。

        规格 §5.2 + §10.1:
          - 根据显存自动选择模型
          - 校验时长限制
          - 支持音画同步（LTX-2）
          - 支持 4合1 截图输入

        Args:
            request: 视频生成请求
            progress_cb: 可选进度回调 (fraction 0→1, stage)。
                真实管线接入 diffusers 逐步去噪回调（denoise 段映射 0→0.9，
                encode 0.95，done 1.0）；回调内抛出的异常视为取消信号，
                原样穿透、不回落降级管线。

        Returns:
            VideoGenResult 包含视频文件路径

        Raises:
            ApiError: 生成失败或参数校验失败
        """
        start_time = time.time()
        gen_id = str(uuid.uuid4())
        relay = _ProgressRelay(progress_cb)

        # H3 请求预判（2026-08-25）：H3 编码器为 Qwen3-VL-32B（中文
        # 原生），跳过 VL 增强/翻译链——避免后端加载 qwen3-vl-4b
        # 抢占 ComfyUI 子进程显存预算（16GB 卡 5.5GB 挤兑，
        # DynamicVRAM 换载雪崩）
        will_h3 = self._will_use_h3(request)

        # 视频 prompt 预处理（2026-08-22 语义贴合修复，三级链）：
        # ① Qwen3-VL 增强：I2V 图片主体 + 文字要求融合扩写（最优）
        # ② 翻译链：中文→英文直译（VL 不可用时兜底；英文编码器家族
        #    LTX/CogVideoX 必需——中文对 T5-XXL 是噪声）
        # ③ 原文（诚实降级）
        # 在模型装载前执行——遵循 prompt_translator 显存约定：对话引擎
        # 先就位，视频引擎装载时按剩余显存自动门控 offload，不抢显存。
        effective_prompt = (request.description or "").strip() or "animated scene"
        # 参考图解码一次 + 多视角净化（四视图拼图 → 单视角），裁剪图
        # 贯穿增强与 I2V 首帧（此前宫格整图做首帧是"人物参考不符"
        # 根因之一）
        reference_img = _decode_screenshot(request.screenshot_4in1)
        if reference_img is not None:
            reference_img = self._detect_and_crop_multiview(reference_img)
        if will_h3:
            # H3 中文直入：Qwen3-VL-32B 编码器原生理解中文，
            # 增强翻译全免（reference_img 仍作 I2V 首帧）
            logger.info("H3 请求：prompt 中文直入（跳过 VL 增强/翻译）")
        else:
            try:
                from .prompt_translator import contains_cjk

                enhanced = self._enhance_video_prompt(
                    request, effective_prompt, image=reference_img)
                if enhanced:
                    effective_prompt = enhanced
                elif contains_cjk(effective_prompt) \
                        and not self._i2v_native_zh(request):
                    from .prompt_translator import translate_prompt_zh2en
                    # 视频专用模板（动作优先），不用绘画默认模板——
                    # 否则"跳舞"等动作词被排序规则丢弃
                    translated = translate_prompt_zh2en(
                        effective_prompt, max_tokens=160,
                        system_prompt=_VIDEO_TRANSLATE_SYSTEM)
                    if translated and not contains_cjk(translated):
                        logger.info("视频 prompt 已译英: %r -> %r",
                                    effective_prompt[:60], translated[:80])
                        effective_prompt = translated
                    else:
                        logger.warning("视频 prompt 翻译未产出英文，按原文生成")
            except Exception as exc:  # noqa: BLE001 - 预处理故障不阻断生成
                logger.warning("视频 prompt 预处理跳过: %s", exc)

        # 确定模型
        if request.model_override:
            model = self._router.select_model(0, request.model_override)
        else:
            # 使用当前加载的模型或自动选择
            model = (self._model
                     if self._loaded and self._model is not None
                     else VideoModel.COGVIDEOX_2B_CPU)
            # 自动模式且 H3 为路由默认（16GB 档）时选 H3——
            # 与 _will_use_h3 预判口径一致（已装载 diffusers 管线除外）
            if not self._loaded and model != VideoModel.MINIMAX_H3 \
                    and self._h3_routed_default():
                model = VideoModel.MINIMAX_H3

        # 校验时长
        self.validate_video_duration(request, model)

        # H3 分派（2026-08-25）：ComfyUI 子进程管线，绕过 diffusers
        # 装载/换载链（权重在子进程内由 DynamicVRAM 分时管理）
        if model == VideoModel.MINIMAX_H3:
            return self._generate_h3(request, effective_prompt,
                                     reference_img, gen_id, start_time,
                                     relay)

        # 降级模式（先尝试自动装载导入 models/ 的视频模型）
        # 2026-08-22 换载逻辑：请求形态（带图 I2V / 纯文 T2V）与当前已
        # 装载管线不匹配时卸载换载（如 LTX T2V ⇄ Wan I2V）。LTX 家族
        # 内部由同权重双管线重组处理（见下方 swapped 逻辑），跨家族才
        # 走卸载换载（成本 ~30s，换正确性值得）。
        want_i2v = bool(request.screenshot_4in1)
        if self._loaded:
            cur_cls = type(self._pipeline).__name__
            # 2026-08-22 修复：VACE 为统一管线（T2V+I2V 同形态），恒兼容
            # 带图请求；此前仅认 "ImageToVideo" 子串，VACE 被误判 T2V
            # 触发无谓的卸载换载循环
            cur_i2v = ("ImageToVideo" in cur_cls
                       or cur_cls in ("WanVACEPipeline",))
            if want_i2v and not cur_i2v and not self._ltx_i2v_swapped \
                    and not self._ltx_swap_capable():
                self.unload_model()
                self._video_autoload_attempted = False
                logger.info("管线形态不匹配（T2V 已载但请求带图），卸载换载 I2V 模型")
            elif not want_i2v and "ImageToVideo" in cur_cls \
                    and cur_cls != "WanVACEPipeline":
                # 混合架构（2026-08-23）：TI2V-5B 以 WanImageToVideoPipeline
                # 加载（单 ckpt 双条件），但其 __call__ 的 image 为必填——
                # 纯文本请求不能裸调（TypeError）。I2V-only 管线 + 纯文本
                # 请求 → 卸载换载双模态管线（VACE T2V 兜底，成本 ~30s）。
                # LTX 重组态还原由下方 swapped 逻辑处理，此处跳过。
                if not self._ltx_i2v_swapped:
                    self.unload_model()
                    self._video_autoload_attempted = False
                    logger.info("管线形态不匹配（I2V-only 已载但请求纯文本，"
                                "%s 需必填首帧），卸载换载双模态模型", cur_cls)
            elif not want_i2v and cur_i2v and self._ltx_i2v_swapped:
                # LTX 重组态还原由下方 swapped 逻辑处理，此处不干预
                pass
        if self._fallback_mode or not self._loaded:
            self._ensure_video_loaded(prefer_i2v=want_i2v)
            # 自动装载成功后以实际加载的模型为准，确保参数预设
            # （分辨率/帧率/时长）与真实管线匹配
            if not request.model_override and self._loaded \
                    and self._model is not None:
                model = self._model
        if self._fallback_mode or not self._loaded:
            # I2V 请求但只装到 T2V 模型（I2V 模型全失败）时回落全量探测
            if want_i2v:
                self._video_autoload_attempted = False
                self._ensure_video_loaded()
                if not request.model_override and self._loaded \
                        and self._model is not None:
                    model = self._model
        if self._fallback_mode or not self._loaded:
            # AnimateLCM 图生视频分支（F-07）：Ken Burns 之前最后一次尝试
            # （一次性探测；SD1.5 底座缺失时 gated，直接回落现状行为）
            if not self._animatelcm_attempted:
                self.load_animatelcm()
            if self._animatelcm_pipe is not None:
                return self._generate_animatelcm(request, model, gen_id,
                                                 start_time,
                                                 progress_cb=progress_cb)
            return self._mock_generate(request, model, gen_id, start_time,
                                       progress_cb=progress_cb)

        # 真实生成
        try:
            # 准备生成参数
            params = self._router.get_generation_params(model)
            num_frames = int(request.duration_seconds * request.fps)

            # 复用预处理段已解码并多视角净化的参考图（解码一次贯穿
            # 全链路）；仅 I2V 管线传入，T2V 管线纯文本驱动
            screenshot_img = reference_img
            if request.screenshot_4in1 and screenshot_img is None:
                logger.info("参考图解码失败（按纯文本生成）")

            cls_name = type(self._pipeline).__name__
            i2v_capable = ("ImageToVideo" in cls_name
                           or cls_name in _I2V_PIPELINE_NAMES)

            # LTX 同权重双管线（官方支持）：LTXVideoTransformer 同时兼容
            # T2V/I2V，按请求形态双向切换管线类（组件级重组，零额外读盘；
            # CPU offload hooks 挂在组件 module 上，重组保留）。
            # self._ltx_i2v_swapped 标记当前 I2V 管线系重组而来（区别于
            # 原生导入的 I2V 模型目录，后者无 T2V 形态可还原）。
            if cls_name in ("LTXPipeline", "LTXVideoPipeline") \
                    and request.screenshot_4in1:
                i2v_cls = getattr(_diffusers, "LTXImageToVideoPipeline", None)
                if i2v_cls is not None:
                    try:
                        src = self._pipeline
                        self._pipeline = i2v_cls(
                            scheduler=src.scheduler, vae=src.vae,
                            text_encoder=src.text_encoder,
                            tokenizer=src.tokenizer,
                            transformer=src.transformer)
                        cls_name = type(self._pipeline).__name__
                        i2v_capable = True
                        self._ltx_i2v_swapped = True
                        logger.info("LTX 管线已重组为 I2V（同权重双管线，"
                                    "参考图将驱动首帧）")
                    except Exception as exc:  # noqa: BLE001 - 重组失败保持 T2V
                        logger.warning("LTX I2V 管线重组失败，保持 T2V: %s", exc)
            elif (cls_name == "LTXImageToVideoPipeline"
                  and self._ltx_i2v_swapped
                  and not request.screenshot_4in1):
                # 纯文本请求：还原 T2V 管线（I2V 的 image 为必填参数）
                t2v_cls = getattr(_diffusers, "LTXPipeline", None) \
                    or getattr(_diffusers, "LTXVideoPipeline", None)
                if t2v_cls is not None:
                    try:
                        src = self._pipeline
                        self._pipeline = t2v_cls(
                            scheduler=src.scheduler, vae=src.vae,
                            text_encoder=src.text_encoder,
                            tokenizer=src.tokenizer,
                            transformer=src.transformer)
                        cls_name = type(self._pipeline).__name__
                        i2v_capable = False
                        self._ltx_i2v_swapped = False
                        logger.info("LTX 管线已还原为 T2V（本次纯文本生成）")
                    except Exception as exc:  # noqa: BLE001 - 还原失败保持 I2V
                        logger.warning("LTX T2V 管线还原失败: %s", exc)

            # 分辨率预设解析："768x512" 直取；720p/1080p 按常规宽屏
            res = str(params["max_resolution"])
            if "x" in res:
                rw, rh = res.split("x", 1)
                gen_w, gen_h = int(rw), int(rh)
            elif res == "720p":
                gen_w, gen_h = 1024, 576
            else:
                gen_w, gen_h = 1280, 720

            # 家族约束校正（帧数与分辨率并列）：见各 align 函数注释
            if "LTX" in cls_name:
                num_frames, gen_w, gen_h = _ltx_align_params(
                    num_frames, gen_w, gen_h)
            elif cls_name.startswith("Wan"):
                num_frames, gen_w, gen_h = _wan_align_params(
                    num_frames, gen_w, gen_h)

            # VAE tiling 按分辨率条件开关（2026-08-23 e2e 三测教训）：
            # tiling 把 VAE encode 切成 (W/192)×(H/192) 空间 tile ×
            # 时间块 × VACE 双分支 次小前向（832×464 即 15×21×2=630 次
            # vs 非 tiled 42 次），kernel 启动/feat_cache 管理开销主导，
            # 实测 10 分钟+ 不出 encode——低分辨率是净负优化。清场修复
            # 后空闲 ~6GB，非 tiled encode 峰值（832×464 约 1.5-2GB /
            # 1024×576 约 3-4GB）放得下；仅高分辨率（>1024×576 档，
            # 激活 ~6GB）需要削峰。
            try:
                _vae = getattr(self._pipeline, "vae", None)
                if _vae is not None and hasattr(_vae, "use_tiling"):
                    # dit-only 档强制 tiling：VAE 窗口与 DiT 互斥腾位后
                    # 空间有限（tiling 激活 ~0.5GB vs 非 tiled 3-4GB），
                    # 窗口内装不下非 tiled 峰值（2026-08-23 e2e 崩溃教训）
                    _need_tile = (self._split_dit_only
                                  or gen_w * gen_h > 1024 * 576)
                    if _need_tile and not _vae.use_tiling:
                        _vae.enable_tiling()
                        logger.info("VAE tiling 开启 (%dx%d%s)",
                                    gen_w, gen_h,
                                    "，dit-only 窗口腾位" if self._split_dit_only else "")
                    elif not _need_tile and _vae.use_tiling:
                        # AutoencoderKLWan 无 disable_tiling 方法，
                        # 直接置 flag（enable_tiling 仅设属性）
                        _vae.use_tiling = False
                        logger.info("VAE tiling 按分辨率关闭 (%dx%d，"
                                    "全幅 encode 快 15 倍)", gen_w, gen_h)
            except Exception as exc:  # noqa: BLE001 - 开关失败沿用装载态
                logger.warning("VAE tiling 分辨率开关失败: %s", exc)

            # 家族差异化推理参数：Wan 官方推荐 guidance 5.0 + 负向提示词
            num_steps = 30
            guidance = 7.5
            family_kwargs: dict = {}
            if cls_name.startswith("Wan"):
                # VACE/Wan2.1 官方默认 5.0；6.5 温和上探增强 prompt
                # 语义跟随（2026-08-22 I2V 实测：5.0 下首帧锚定后动作
                # 跟随偏弱，人物外观保真但倾向静态展示）
                guidance = 6.5
                family_kwargs["negative_prompt"] = _WAN_NEGATIVE_PROMPT
            if cls_name == "WanVACEPipeline":
                # VACE 统一管线：T5 长序列官方默认 512
                family_kwargs["max_sequence_length"] = 512
            generation_kwargs = {
                "prompt": effective_prompt,
                "num_frames": num_frames,
                "num_inference_steps": num_steps,
                "guidance_scale": guidance,
                "height": gen_h,
                "width": gen_w,
                **family_kwargs,
            }
            # 逐步去噪进度（管线支持 callback_on_step_end 时接入，
            # denoise 段映射 0→0.9；不支持则维持分段粗粒度）
            # 尾段（VAE 解码+编码）按帧数估 0.25s/帧，下限 10s
            generation_kwargs.update(
                _make_step_callback(self._pipeline.__call__, relay,
                                    num_steps, span=0.9,
                                    tail_seconds=max(10.0, num_frames * 0.25)))
            if i2v_capable and screenshot_img is not None:
                if cls_name == "WanVACEPipeline":
                    # VACE I2V 契约（diffusers WanVACEPipeline）：video =
                    # [首帧] + 空白占位帧，mask = [黑(条件)] + [白(生成)]。
                    # 黑 mask 区域为条件帧——首帧像素级锚定人物与构图，
                    # 其余帧全生成（动作/运镜由 prompt 驱动）。
                    try:
                        from PIL import Image as _PIL
                        first = screenshot_img.convert("RGB").resize(
                            (gen_w, gen_h))
                        blank = _PIL.new("RGB", (gen_w, gen_h), (0, 0, 0))
                        black = _PIL.new("L", (gen_w, gen_h), 0)
                        white = _PIL.new("L", (gen_w, gen_h), 255)
                        generation_kwargs["video"] = (
                            [first] + [blank] * (num_frames - 1))
                        generation_kwargs["mask"] = (
                            [black] + [white] * (num_frames - 1))
                        logger.info("VACE I2V 条件已构造: 首帧锚定 %dx%d，"
                                    "%d 帧", gen_w, gen_h, num_frames)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("VACE 条件构造失败（回退 T2V）: %s",
                                       exc)
                else:
                    generation_kwargs["image"] = screenshot_img
            elif screenshot_img is not None and not i2v_capable:
                logger.info("当前管线 %s 为 T2V，忽略参考图（导入 I2V 变体 "
                            "如 Wan2.1-I2V/CogVideoX-I2V 即可图生视频）", cls_name)

            if request.audio_path and params["supports_audio_sync"]:
                generation_kwargs["audio_path"] = request.audio_path

            # 按管线 __call__ 签名过滤参数（不同管线参数集差异大，诚实适配）
            try:
                import inspect
                sig = inspect.signature(self._pipeline.__call__)
                if not any(p.kind == inspect.Parameter.VAR_KEYWORD
                           for p in sig.parameters.values()):
                    generation_kwargs = {
                        k: v for k, v in generation_kwargs.items()
                        if k in sig.parameters}
            except Exception:  # noqa: BLE001 - 内省失败按原参数尝试
                logger.debug("generate: 降级忽略", exc_info=True)

            # 外部 T5 编码（2026-08-22 团队审查重构）：
            # _split_te_needed：管线无 T5（DiT+VAE 常驻）。编码窗口编排
            #   = DiT 暂下卡（腾 ~7GB）→ int8 T5 上卡编码 → T5 释放
            #     → DiT 回卡 → embeds 直传去噪。物理约束：16GB 卡对话
            #   模型常驻时 T5 5.3GB 与 DiT 7.1GB 无法同时上卡。
            # _external_te_encode：int8 T5 常驻（档 1），直接外部编码
            #   （显式 dtype=transformer.dtype，绕开管线内部 encode_prompt
            #   的 int8 dtype 回退——bnb 参数 dtype 是 int8，不显式传会
            #   把嵌入 cast 成 int8）。
            # encode_prompt 显式传 device/dtype；白名单（Wan*/CogVideoX*）
            #   保证 2 元组契约。max_sequence_length 缺省取管线签名默认。
            if ((self._split_te_needed or self._external_te_encode)
                    and "prompt" in generation_kwargs):
                _prompt_raw = generation_kwargs.pop("prompt")
                _neg_raw = generation_kwargs.pop("negative_prompt", None)
                _msl = generation_kwargs.pop("max_sequence_length", None)
                if _msl is None:
                    try:
                        import inspect as _inspect
                        _p = _inspect.signature(
                            self._pipeline.__call__).parameters.get(
                                "max_sequence_length")
                        _msl = getattr(_p, "default", None) or 226
                    except Exception:  # noqa: BLE001
                        _msl = 226
                _te_tmp = None
                try:
                    if self._split_te_needed:
                        # DiT（含可选 transformer_2 双 DiT 变体）暂下卡
                        for _cn in ("transformer", "transformer_2"):
                            _comp = getattr(self._pipeline, _cn, None)
                            if _comp is not None:
                                _comp.to("cpu")
                        _release_gpu_cache()
                        _te_tmp = _load_text_encoder_int8(
                            Path(self._model_name), _torch.float16)
                        if _te_tmp is None:
                            raise RuntimeError(
                                "split 布局 T5 临时加载失败（bitsandbytes "
                                "不可用或权重损坏）")
                        self._pipeline.text_encoder = _te_tmp
                    _cfg = float(generation_kwargs.get(
                        "guidance_scale", 5.0)) > 1.0
                    # inference_mode 铁证（2026-08-22 实验 v2 + kimi 复核）：
                    # diffusers 0.39 Wan 系 encode_prompt 无 @torch.no_grad()
                    # 装饰器（仅 __call__ 有），裸调用全程建 autograd 图——
                    # 实测 832x464 场景 batch=1/seq=512 CFG 两次 forward
                    # 建图激活 +10.65GB（24 层 × 2 次 × ~200MB/层），
                    # 且 WDDM oversubscription 换页使 encode 耗时 35s
                    # （no_grad 裸 forward <1s）。必须显式包裹。
                    with _torch.inference_mode():
                        _pe = self._pipeline.encode_prompt(
                            prompt=_prompt_raw,
                            negative_prompt=_neg_raw or "",
                            do_classifier_free_guidance=_cfg,
                            max_sequence_length=int(_msl),
                            device=_torch.device("cuda"),
                            dtype=self._pipeline.transformer.dtype,
                        )
                    # embeds 显式钉在 cuda（五测教训 2026-08-22）：VACE
                    # __call__ 对传入 prompt_embeds 只做 dtype 转换
                    # （L900 .to(transformer_dtype)）不搬设备，CPU embeds
                    # 直进 cuda 上的 DiT 触发 addmm 设备不匹配。inference_mode
                    # 下无建图，4MB embeds 常驻 cuda 无害。
                    generation_kwargs["prompt_embeds"] = _pe[0].to("cuda")
                    if _cfg and _pe[1] is not None:
                        generation_kwargs[
                            "negative_prompt_embeds"] = _pe[1].to("cuda")
                    del _pe
                    logger.info("T5 外部编码完成%s",
                                "（split 窗口：DiT 回卡，T5 已释放）"
                                if self._split_te_needed else "")
                finally:
                    if _te_tmp is not None:
                        # del 而非 = None（2026-08-22 实验 v2 铁证）：
                        # pipeline 是 nn.Module，`pipe.text_encoder = None`
                        # 走 __setattr__ 的非 Module 分支只写 __dict__ 遮蔽，
                        # _modules['text_encoder'] 仍持有 te → del _te_tmp
                        # 无效，T5 int8 9.32GB 永不释放（线上 OOM 根因之二）。
                        # nn.Module.__delattr__ 才有删 _modules 条目的分支。
                        try:
                            del self._pipeline.text_encoder
                        except AttributeError:
                            logger.debug("generate: 降级忽略", exc_info=True)
                        del _te_tmp
                        _release_gpu_cache()
                    if self._split_te_needed:
                        # DiT 回卡恢复常驻布局（2026-08-23 v2：dit-only
                        # 亦统一回卡——若 DiT 留 CPU，diffusers
                        # _execution_device 取首个有参模块设备会漂移为
                        # cpu，randn latents/embeds 设备错配 addmm 崩；
                        # VAE encode/decode 窗口的腾位由 wrap 内
                        # _dit_to("cpu") 接管，代价 encode 前后多两次
                        # DiT 搬运 ~8s，换设备编排正确性）
                        for _cn in ("transformer", "transformer_2"):
                            _comp = getattr(self._pipeline, _cn, None)
                            if _comp is not None:
                                _comp.to("cuda")

            result = self._pipeline(**generation_kwargs)

            # 保存视频文件
            relay(0.95, "encode")
            video_dir = DATA_DIR / "videos"
            video_dir.mkdir(parents=True, exist_ok=True)
            video_path = str(video_dir / f"{gen_id}.mp4")

            frames = result.frames if hasattr(result, "frames") else result[0]
            # 2026-08-22 幽灵文件事故修复：VACE 等管线返回嵌套 ndarray 帧
            # （[batch][frames][H][W][C]，uint8/float32 混合），export_to_video
            # 仅吃 PIL 帧列表。递归展平任意嵌套到 3 维 HWC 单帧，统一
            # uint8 PIL（float 裁剪 [0,1] × 255；灰度/单通道扩 RGB）。
            from PIL import Image as _FrameImage
            try:
                import numpy as _np
            except ImportError:  # noqa: BLE001 - numpy 缺失时保底
                _np = None

            def _to_pil_frames(node: Any, out: list) -> None:
                if hasattr(node, "save"):  # PIL 帧
                    out.append(node)
                elif _np is not None and isinstance(node, _np.ndarray):
                    if node.ndim == 3 and node.shape[-1] in (1, 3):
                        if node.dtype != _np.uint8:
                            node = (_np.clip(node, 0.0, 1.0)
                                    * 255).astype(_np.uint8)
                        if node.shape[-1] == 1:
                            node = _np.repeat(node, 3, axis=2)
                        out.append(_FrameImage.fromarray(node))
                    elif node.ndim == 2:  # 灰度单帧
                        if node.dtype != _np.uint8:
                            node = (_np.clip(node, 0.0, 1.0)
                                    * 255).astype(_np.uint8)
                        out.append(
                            _FrameImage.fromarray(node).convert("RGB"))
                    elif node.size:  # ≥4 维嵌套 → 逐层递归降维
                        for sub in node:
                            _to_pil_frames(sub, out)
                elif isinstance(node, (list, tuple)):
                    for sub in node:
                        _to_pil_frames(sub, out)

            frames_list: list = []
            _to_pil_frames(frames, frames_list)
            if not frames_list:
                raise RuntimeError("视频管线未返回可编码帧（帧列表为空）")
            # 导出视频（优先 diffusers export_to_video，回退 imageio）
            try:
                from diffusers.utils import export_to_video
                export_to_video(frames_list, video_path, fps=request.fps)
            except Exception:  # noqa: BLE001
                try:
                    import imageio
                    imageio.mimsave(video_path, frames_list, fps=request.fps)
                except Exception:
                    # 降级：保存第一帧
                    try:
                        frames_list[0].save(video_path.replace(".mp4", ".png"))
                        video_path = video_path.replace(".mp4", ".png")
                    except Exception:
                        logger.debug("generate: 降级忽略", exc_info=True)
            # 落盘校验：编码链全失败时不返回幽灵路径（诚实报错）
            if not Path(video_path).is_file():
                raise RuntimeError(
                    f"视频导出失败，文件未生成: {video_path}")

            elapsed_ms = int((time.time() - start_time) * 1000)
            relay(1.0, "done")

            logger.info(
                "视频生成完成: %s, %s, %.1fs, %dms",
                gen_id,
                model.value,
                request.duration_seconds,
                elapsed_ms,
            )

            return VideoGenResult(
                id=gen_id,
                file_path=video_path,
                model_used=(Path(self._model_name).name
                            if self._model_name else model.value),
                duration_seconds=request.duration_seconds,
                resolution=request.resolution,
                generation_time_ms=elapsed_ms,
                has_audio_sync=bool(request.audio_path and params["supports_audio_sync"]),
            )

        except Exception as e:
            if relay.is_cancel(e):
                raise
            logger.error("视频生成失败: %s", e)
            return self._mock_generate(request, model, gen_id, start_time,
                                       progress_cb=progress_cb)

    def _generate_animatelcm(
        self,
        request: VideoGenerateRequest,
        model: VideoModel,
        gen_id: str,
        start_time: float,
        progress_cb: Callable[[float, str], None] | None = None,
    ) -> VideoGenResult:
        """AnimateLCM 图生视频（F-07）：AI 短 clip + Ken Burns 补足时长 + FFmpeg 编码。

        流程：文本描述（静帧图经由 Ken Burns 补足段体现）驱动 AnimateLCM
        产出 2~4s 短 clip 帧序列（SD1.5 原生分辨率生成，FFmpeg 侧统一缩放）
        → AI 片段不足以覆盖请求时长时，以 AI 末帧为基图 Ken Burns 补足
        → 复用降级管线的 FFmpeg 编码产出真实可播放视频文件。

        进度映射：denoise 0→0.5（逐步去噪回调），extend 0.5→0.7，
        encode 0.7→0.95，done 1.0；回调抛出的取消信号原样穿透。

        model_used 如实标注："animatelcm-sd15-t2v"（AI 覆盖全时长）或
        "animatelcm-sd15-t2v+kenburns-extend"（Ken Burns 补足时长）。

        推理失败（如显存不足）回落 _mock_generate（Ken Burns），
        编码失败与降级路径同语义抛 ApiError 60004。
        """
        from ..encoder_service import RESOLUTION_MAP, get_encoder_service

        enc = get_encoder_service()
        if not enc.available:
            raise ApiError("VIDEO_ENCODE_FAILED",
                "视频导出失败：FFmpeg 不可用（runtime/ffmpeg、tools/downloads、"
                "PATH 均未找到）")

        video_dir = VIDEO_OUT_DIR
        video_dir.mkdir(parents=True, exist_ok=True)
        video_path = video_dir / f"{gen_id}.mp4"
        width, height = RESOLUTION_MAP.get((request.resolution or "").lower(),
                                           (1920, 1080))
        fps = request.fps
        total_frames = max(1, int(round(request.duration_seconds * fps)))
        ai_frames_n = min(total_frames, ANIMATELCM_MAX_FRAMES,
                          max(8, int(round(ANIMATELCM_CLIP_SECONDS * fps))))
        frame_dir = video_dir / f"{gen_id}_frames"
        relay = _ProgressRelay(progress_cb)

        # 1) AnimateLCM 推理（失败回落 Ken Burns，释放管线避免重试风暴）
        try:
            gen_w, gen_h = ((512, 288) if width >= height else (288, 512))
            lcm_kwargs: dict[str, Any] = {
                "prompt": request.description or "animated scene",
                "negative_prompt": _ANIMATELCM_NEG_PROMPT,
                "num_frames": ai_frames_n,
                "num_inference_steps": ANIMATELCM_INFER_STEPS,
                "guidance_scale": ANIMATELCM_GUIDANCE,
                "height": gen_h,
                "width": gen_w,
            }
            # 逐步去噪进度：denoise 段映射 0→0.5（管线支持时接入）
            lcm_kwargs.update(
                _make_step_callback(self._animatelcm_pipe.__call__, relay,
                                    ANIMATELCM_INFER_STEPS, span=0.5))
            result = self._animatelcm_pipe(**lcm_kwargs)
            frames = [img.convert("RGB") for img in result.frames[0]][:ai_frames_n]
            if not frames:
                raise RuntimeError("AnimateLCM 管线返回空帧序列")
        except Exception as exc:  # noqa: BLE001
            if relay.is_cancel(exc):
                raise
            logger.error("AnimateLCM 推理失败，回落 Ken Burns 降级管线: %s", exc)
            self._animatelcm_pipe = None
            self._animatelcm_error = f"AnimateLCM 推理失败: {exc}"
            # 推理期 OOM 等失败：管线已丢弃，对称释放显存记账并回收缓存
            try:
                from ...engines.vram_manager import get_vram_manager
                get_vram_manager().track_free(ANIMATELCM_MODEL_LABEL)
            except Exception:  # noqa: BLE001
                logger.debug("_generate_animatelcm: 降级忽略", exc_info=True)
            _release_gpu_cache()
            return self._mock_generate(request, model, gen_id, start_time,
                                       progress_cb=progress_cb)

        # 2) 帧落盘 + Ken Burns 补足时长 + FFmpeg 编码
        model_used = ANIMATELCM_MODEL_LABEL
        try:
            frame_dir.mkdir(parents=True, exist_ok=True)
            for i, img in enumerate(frames):
                img.save(frame_dir / f"frame_{i:05d}.jpg", quality=92)
            relay(0.55, "save_frames")
            if len(frames) < total_frames:
                remain_s = (total_frames - len(frames)) / fps
                render_kenburns_frames(
                    request.description, request.screenshot_4in1,
                    frame_dir, width, height, fps, remain_s,
                    progress_cb=(lambda f: relay(0.55 + f * 0.15, "extend")),
                    start_index=len(frames), base_image=frames[-1])
                model_used = f"{ANIMATELCM_MODEL_LABEL}+kenburns-extend"
            relay(0.7, "encode")
            info = enc.encode_frames_to_video(
                frame_dir, video_path, fps=fps,
                resolution=request.resolution,
                codec=request.codec or "h264",
                frame_pattern="frame_%05d.jpg",
                audio_path=request.audio_path,
            )
            relay(0.95, "encode")
        except RuntimeError as exc:  # 编码失败与降级路径同语义
            raise ApiError("VIDEO_ENCODE_FAILED", f"视频导出失败：{exc}") from exc
        except Exception as exc:  # noqa: BLE001 - 后处理失败回落 Ken Burns
            if relay.is_cancel(exc):
                raise
            logger.error("AnimateLCM 后处理失败，回落 Ken Burns 降级管线: %s", exc)
            return self._mock_generate(request, model, gen_id, start_time,
                                       progress_cb=progress_cb)
        finally:
            shutil.rmtree(frame_dir, ignore_errors=True)

        elapsed_ms = int((time.time() - start_time) * 1000)
        relay(1.0, "done")
        logger.info(
            "[AnimateLCM] 视频生成完成: %s, ai_frames=%d/%d, model_used=%s, %dms",
            gen_id, len(frames), total_frames, model_used, elapsed_ms,
        )

        return VideoGenResult(
            id=gen_id,
            file_path=str(info.get("output", video_path)),
            model_used=model_used,
            duration_seconds=request.duration_seconds,
            resolution=request.resolution,
            generation_time_ms=elapsed_ms,
            has_audio_sync=False,
        )

    def _mock_generate(
        self,
        request: VideoGenerateRequest,
        model: VideoModel,
        gen_id: str,
        start_time: float,
        progress_cb: Callable[[float, str], None] | None = None,
    ) -> VideoGenResult:
        """降级真实管线：真实模型未加载时仍产出真实可播放视频文件。

        （旧实现写入 "MOCK_VIDEO_FILE" 虚构文件，已替换为 PIL 帧渲染 +
        FFmpeg 编码的真实产物；FFmpeg 不可用时抛 ApiError 60004。）
        """
        video_dir = VIDEO_OUT_DIR
        video_dir.mkdir(parents=True, exist_ok=True)
        video_path = video_dir / f"{gen_id}.mp4"

        try:
            info = generate_fallback_video(request, video_path, progress_cb)
        except RuntimeError as exc:
            logger.error("降级管线视频导出失败: %s", exc)
            raise ApiError("VIDEO_ENCODE_FAILED", f"视频导出失败：{exc}") from exc

        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.info(
            "[降级管线] 视频生成完成: %s, encoder=%s, %dms",
            gen_id, info.get("encoder"), elapsed_ms,
        )

        return VideoGenResult(
            id=gen_id,
            file_path=str(info.get("output", video_path)),
            model_used=f"{model.value}+fallback-kenburns",
            duration_seconds=request.duration_seconds,
            resolution=request.resolution,
            generation_time_ms=elapsed_ms,
            has_audio_sync=False,
        )

    @property
    def is_ready(self) -> bool:
        """引擎是否就绪。"""
        return self._loaded and not self._fallback_mode

    @property
    def model_name(self) -> str:
        """当前加载的模型名。"""
        return self._model_name or "mock-video"

    def animatelcm_status(self) -> dict:
        """AnimateLCM（F-07）分支状态：权重探测 + 运行时加载状态，如实上报。

        字段：ckpt_ready（运动模块 ckpt 存在）、sd15_ready（SD1.5 底座存在）、
        ready（可用于生成）、reason（不可用中文原因，可用时为空串）。
        """
        st = get_animatelcm_status()
        st["pipe_loaded"] = self._animatelcm_pipe is not None
        if self._animatelcm_pipe is not None:
            st["ready"] = True
            st["reason"] = ""
        elif st["ready"] and self._animatelcm_error:
            # 权重齐备但加载/推理曾失败：如实标注 gated 原因
            st["ready"] = False
            st["reason"] = self._animatelcm_error
        return st

    def get_status(self) -> dict:
        """返回引擎状态。"""
        from .base_engine import derive_state
        return {
            "engine": "video",
            # ADR-003 P3：统一状态（fallback=诚实降级 mock 模式，不算 ready）
            "state": derive_state(
                loaded=self._loaded and not self._fallback_mode,
                unavailable=self._fallback_mode),
            "model": self._model.value if self._model is not None else "",
            "model_path": self._model_name,
            "loaded": self._loaded,
            "fallback": self._fallback_mode,
            "device": self._device,
            "has_diffusers": _diffusers is not None,
            "has_torch": _torch is not None,
            "animatelcm": self.animatelcm_status(),
            "discovered_models": discover_video_models(),
        }


# ── 模块级单例 ──────────────────────────────────────────────────
# manga 视频工作线程与 ModelManager 必须共享同一引擎实例：否则
# ModelManager.unload_model/force_unload 作用于另一空实例，真实管线
# 引用（及其显存）永远无法经管理器释放，loaded_models 视图与 GPU
# 实际占用脱节（审计修复，对齐 dialog/paint 引擎的 getter 契约）。
_video_engine_instance: VideoEngine | None = None
_video_engine_instance_lock = threading.Lock()


def get_video_engine() -> VideoEngine:
    """获取 VideoEngine 进程级单例（双检锁懒创建）。"""
    global _video_engine_instance
    if _video_engine_instance is None:
        with _video_engine_instance_lock:
            if _video_engine_instance is None:
                _video_engine_instance = VideoEngine()
    return _video_engine_instance
# 本项目仅供学习使用，商业授权请+Q 3559331368
