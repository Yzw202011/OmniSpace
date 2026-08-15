"""OmniSpace AI v2.1 视频推理引擎（规格 §5.2 + §5.3 视频模型路由）。

使用 LTX-2 / Wan2.1 / CogVideoX 进行视频生成（真实模型经 load_diffusers_model()
钩子加载）。真实视频模型未下载时按探测链尝试 AnimateLCM 图生视频分支（F-07：
AnimateLCM_sd15_t2v 运动模块 + SD1.5 底座组成 AnimateDiffPipeline，LCM 少步采样
产出 2~4s 短 clip，不足时长由 Ken Burns 补足）；AnimateLCM 不可用（SD1.5 底座
未随包）时走"降级真实管线"（TASK-010）：PIL 渲染 Ken Burns 推拉帧序列 + 字幕条
→ FFmpeg 编码产出**真实可播放**的 MP4/AV1 文件到 data/generated/videos/。
"""

from __future__ import annotations

import base64
import gc
import hashlib
import importlib
import io
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from ...config import VIDEO_MAX_DURATION, LTX2_MAX_AUDIO_SYNC, DATA_DIR, MODELS_DIR
from ...data.models import (
    VideoGenerateRequest,
    VideoGenResult,
    VideoModel,
    VIDEO_ROUTING_TABLE,
)
from ...middleware.error_handler import ApiError
from ..scheduler.video_router import VideoRouter

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


def _find_sd15_base() -> Optional[Path]:
    """在 SD15_BASE_CANDIDATES 中探测有效 SD1.5 diffusers 目录。"""
    for path in SD15_BASE_CANDIDATES:
        if (path / "model_index.json").is_file():
            return path
    return None


def _load_text_encoder_int8(model_dir: Path, dtype: Any) -> Any:
    """文本编码器 int8 量化加载（bitsandbytes），显存不足时调用。

    LTX/Wan 系管线的 T5-XXL 编码器约占整管线 2/3 显存（fp16 ~9.4GB），
    int8 后约减半，常可让 16GB 卡免 CPU offload 整卡运行。
    目录缺失 / bitsandbytes 未装 / 加载失败均返回 None（回退原路径）。
    """
    if _try_import("bitsandbytes") is None:
        return None
    te_dir = Path(model_dir) / "text_encoder"
    if not (te_dir / "config.json").is_file():
        return None
    try:
        from transformers import BitsAndBytesConfig, T5EncoderModel
        qcfg = BitsAndBytesConfig(load_in_8bit=True)
        te = T5EncoderModel.from_pretrained(
            str(te_dir), quantization_config=qcfg, torch_dtype=dtype)
        logger.info("文本编码器 int8 量化加载成功: %s", te_dir)
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
            pass
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
        pass
    return total


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
                    pass
    except OSError:
        pass
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
            pass


class _ProgressRelay:
    """进度回调中继：统一包裹调用方 progress_cb。

    回调内抛出的异常（典型为调用方的取消信号）记录到 self.error 并原样
    穿透；各 except 降级分支用 is_cancel() 识别该信号并改为向上抛出，
    避免「推理中途取消」被当作推理失败而回落 Ken Burns 继续产出。
    """

    def __init__(self, progress_cb: Optional[Callable[[float, str], None]]) -> None:
        self._cb = progress_cb
        self.error: Optional[BaseException] = None

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
                        num_steps: int, span: float) -> dict:
    """构造 diffusers callback_on_step_end 进度参数（管线签名支持时）。

    去噪步进映射到 [0, span] 区间（stage="denoise"）；管线不支持该参数
    （旧版 diffusers / 非标准管线）或内省失败时返回空 dict，进度维持
    调用方分段上报的粗粒度行为。tensor_inputs 置空列表避免不必要的
    latents 张量拷贝开销。
    """
    try:
        import inspect
        params = inspect.signature(pipe_call).parameters
    except Exception:  # noqa: BLE001 - 内省失败不接回调
        return {}
    if "callback_on_step_end" not in params:
        return {}

    def _on_step_end(_pipe: Any, step_index: int, _timestep: Any,
                     callback_kwargs: dict) -> dict:
        relay(span * (step_index + 1) / num_steps, "denoise")
        return callback_kwargs

    kwargs: dict[str, Any] = {"callback_on_step_end": _on_step_end}
    if "callback_on_step_end_tensor_inputs" in params:
        kwargs["callback_on_step_end_tensor_inputs"] = []
    return kwargs


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
        raw = base64.b64decode(b64, validate=False)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
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


def render_kenburns_frames(
    description: str,
    screenshot_b64: str,
    frame_dir: str | Path,
    width: int,
    height: int,
    fps: int,
    duration_s: float,
    progress_cb: Optional[Callable[[float], None]] = None,
    start_index: int = 0,
    base_image: Any = None,
) -> int:
    """渲染 Ken Burns 推拉帧序列（含字幕条）到 frame_dir，返回帧数。

    - 基图：base_image（AnimateLCM 补足时长时传入 AI 末帧保持画面连续）>
      4合1截图（cover 放大 12% 余量）> 渐变占位底图；
    - 镜头运动：匀速 zoom-in（1.0→1.12）+ 缓慢右下平移；
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

    report_every = max(1, total // 25)
    for i in range(total):
        t = i / max(total - 1, 1)
        zoom = 1.0 + (zoom_max - 1.0) * t
        zw, zh = int(width / zoom), int(height / zoom)
        max_x, max_y = base.width - zw, base.height - zh
        # 缓慢平移（右下方向），模拟镜头运动
        x0 = int(max_x * t * 0.55)
        y0 = int(max_y * t * 0.35)
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
                pass
    return total


def generate_fallback_video(
    request: VideoGenerateRequest,
    out_path: str | Path,
    progress_cb: Optional[Callable[[float, str], None]] = None,
) -> dict:
    """降级真实管线：渲染帧序列 → FFmpeg 编码为真实可播放视频文件。

    进度映射：帧渲染 0.0~0.6，编码 0.6~1.0。
    产出信息 dict：{"output","encoder","duration_s","size_bytes","frames"}。

    Raises:
        RuntimeError: FFmpeg 不可用或编码失败（调用方标记任务 failed）。
    """
    from ..encoder_service import (RESOLUTION_MAP, get_encoder_service)

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


class VideoEngine:
    """视频推理引擎——LTX-2 / Wan2.1 / CogVideoX / AnimateLCM(F-07)。"""

    def __init__(self) -> None:
        self._pipeline: Any = None
        self._model: Optional[VideoModel] = VideoModel.COGVIDEOX_2B_CPU
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
            pass
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
                    pass

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
                    convert_animatediff_checkpoint_to_diffusers)
                raw = _torch.load(str(ANIMATELCM_CKPT_PATH), map_location="cpu")
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
                        f"missing={real_missing[:3]} unexpected={unexpected[:3]}")
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
                    pass
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
        # AnimateLCM 显存记账对称释放（未记账时 track_free 返回 0）
        try:
            from ...engines.vram_manager import get_vram_manager
            get_vram_manager().track_free(ANIMATELCM_MODEL_LABEL)
        except Exception:  # noqa: BLE001
            pass
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
                pass
            self._pipeline_track_label = ""
        if self._pipeline_registered_id:
            try:
                from ..model_manager import get_model_manager
                get_model_manager().unregister_load(
                    self._pipeline_registered_id)
            except Exception:  # noqa: BLE001
                pass
            self._pipeline_registered_id = ""

    def _ensure_video_loaded(self) -> None:
        """视频模型自动装载（导入 models/ 即可用）。

        扫描 models/ 下 diffusers 布局的视频模型（Wan/CogVideoX/LTX/
        HunyuanVideo 等），按显存需求升序尝试装载：空闲显存足够时直接
        上卡；超出时启用 model CPU offload（速度换可用性）。一轮尝试后
        无论成败都置标记，避免每次生成重复扫描加载。
        """
        if self._loaded or self._fallback_mode or self._video_autoload_attempted:
            return
        self._video_autoload_attempted = True
        if _diffusers is None or _torch is None:
            return
        discovered = {n: i for n, i in discover_video_models().items()
                      if i["diffusers_available"]}
        if not discovered:
            logger.info("未发现可用 diffusers 视频模型（models/ 导入 Wan/"
                        "CogVideoX/LTX/HunyuanVideo 即可自动启用）")
            return

        free_gb = _cuda_free_gb()
        self._device = "cuda" if _torch.cuda.is_available() else "cpu"
        dtype = _torch.float16 if self._device == "cuda" else _torch.float32
        # 小模型优先（降低 OOM 风险，缩短首载时间）
        for name, info in sorted(discovered.items(),
                                 key=lambda kv: kv[1]["vram_gb"]):
            cls = getattr(_diffusers, info["class_name"], None)
            if cls is None:
                continue
            # 显存门控：超出空闲 2% 余量时改 CPU offload（慢但可用）
            use_offload = (
                self._device == "cuda" and free_gb > 0
                and info["vram_gb"] > free_gb * 0.98)
            extra_kwargs: dict = {}
            if use_offload:
                # 文本编码器 int8 量化：T5-XXL 减半后若整管线和可装下，
                # 免 offload 整卡运行（offload 与 bnb 量化权重不兼容，
                # 仅在免 offload 成立时注入量化编码器）
                te = _load_text_encoder_int8(Path(info["path"]), dtype)
                if te is not None:
                    # int8 较 fp16 约减半（te 字节同样按 fp16 基准折算）
                    te_fp16_gb = _dir_load_bytes_fp16(
                        Path(info["path"]) / "text_encoder") / (1024 ** 3)
                    vram_after = info["vram_gb"] - te_fp16_gb * 0.5
                    if vram_after <= free_gb * 0.98:
                        extra_kwargs["text_encoder"] = te
                        use_offload = False
                        logger.info(
                            "int8 编码器生效: %s 预估 %.1fGB -> %.1fGB，整卡运行",
                            name, info["vram_gb"], vram_after)
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
                    else:
                        pipe = pipe.to("cuda")
                try:
                    from .accelerator import get_accelerator
                    pipe = get_accelerator().enable_for_pipeline(pipe)
                except Exception:  # noqa: BLE001
                    pass
                self._pipeline = pipe
                self._model_name = info["path"]
                self._loaded = True
                self._fallback_mode = False
                # 同步模型枚举，使 generate() 取得正确的参数预设
                # （分辨率/帧率/时长上限），未登记目录名保持默认
                try:
                    self._model = VideoModel(name)
                except ValueError:
                    pass
                # 显存记账 + ModelManager 加载台账登记（审计修复：自动
                # 装载链此前绕过 ensure_loaded，loaded_models 与 GPU 实际
                # 占用脱节）。上卡才占显存；CPU 装载登记 vram_gb=0。
                # 释放唯一入口为 unload_model（置 None + track_free +
                # 注销登记 + 回收三件套），与装载严格对称。
                reg_vram_gb = (float(info["vram_gb"])
                               if self._device == "cuda" else 0.0)
                if self._device == "cuda":
                    try:
                        from ...engines.vram_manager import get_vram_manager
                        label = f"video_pipeline:{name}"
                        get_vram_manager().track_alloc(
                            label, reg_vram_gb * 1024.0)
                        self._pipeline_track_label = label
                    except Exception:  # noqa: BLE001 - 记账失败不阻断加载
                        pass
                try:
                    from ..model_manager import get_model_manager
                    if get_model_manager().register_external_load(
                            "video", name, info["path"], reg_vram_gb):
                        self._pipeline_registered_id = name
                except Exception:  # noqa: BLE001 - 登记失败不阻断加载
                    pass
                logger.info("视频模型自动装载成功: %s (%s)", name,
                            info["class_name"])
                return
            except Exception as exc:  # noqa: BLE001 - 试下一个候选
                logger.warning("视频模型 %s 装载失败（试下一个）: %s", name, exc)
        logger.info("所有已发现视频模型装载失败，保持 AnimateLCM/Ken Burns 降级链")

    def generate_fallback(
        self,
        request: VideoGenerateRequest,
        out_path: str | Path,
        progress_cb: Optional[Callable[[float, str], None]] = None,
    ) -> dict:
        """降级真实管线（模块函数 generate_fallback_video 的实例封装）。"""
        return generate_fallback_video(request, out_path, progress_cb)

    def prepare_generation(self) -> str:
        """生成前探测链（导入 models/ 即可用），返回将走的路径。

        Returns:
            "pipeline"   : 真实 diffusers 视频模型已加载/自动装载成功
            "animatelcm" : AnimateLCM 图生视频分支可用（F-07）
            "kenburns"   : 均不可用，回落 Ken Burns 降级真实管线
        """
        if self.is_ready:
            return "pipeline"
        self._ensure_video_loaded()
        if self.is_ready:
            return "pipeline"
        if not self._animatelcm_attempted:
            try:
                self.load_animatelcm()
            except Exception:  # noqa: BLE001
                pass
        if self._animatelcm_pipe is not None:
            return "animatelcm"
        return "kenburns"

    def select_model(self, available_vram_gb: float) -> VideoModel:
        """根据可用显存选择视频模型（委托给 VideoRouter）。"""
        return self._router.select_model(available_vram_gb)

    def validate_video_duration(self, request: VideoGenerateRequest, model: VideoModel) -> None:
        """校验视频时长（规格 §10.1）。"""
        self._router.validate_video_duration(request, model)

    def generate(self, request: VideoGenerateRequest,
                 progress_cb: Optional[Callable[[float, str], None]] = None
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

        # 确定模型
        if request.model_override:
            model = self._router.select_model(0, request.model_override)
        else:
            # 使用当前加载的模型或自动选择
            model = (self._model
                     if self._loaded and self._model is not None
                     else VideoModel.COGVIDEOX_2B_CPU)

        # 校验时长
        self.validate_video_duration(request, model)

        # 降级模式（先尝试自动装载导入 models/ 的视频模型）
        if self._fallback_mode or not self._loaded:
            self._ensure_video_loaded()
            # 自动装载成功后以实际加载的模型为准，确保参数预设
            # （分辨率/帧率/时长）与真实管线匹配
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

            # 解码 4合1 截图（可选；仅 I2V 管线传入，T2V 管线纯文本驱动）
            screenshot_img = None
            if request.screenshot_4in1:
                try:
                    import base64 as _b64
                    from PIL import Image
                    import io as _io
                    raw = request.screenshot_4in1
                    if "," in raw and raw.split(",", 1)[0].startswith("data:"):
                        raw = raw.split(",", 1)[1]
                    screenshot_img = Image.open(
                        _io.BytesIO(_b64.b64decode(raw))).convert("RGB")
                except Exception as exc:  # noqa: BLE001 - 截图损坏不阻断 T2V
                    logger.info("4合1 截图解码失败（按纯文本生成）: %s", exc)
                    screenshot_img = None

            cls_name = type(self._pipeline).__name__
            i2v_capable = ("ImageToVideo" in cls_name
                           or cls_name in _I2V_PIPELINE_NAMES)

            # 分辨率预设解析："768x512" 直取；720p/1080p 按常规宽屏
            res = str(params["max_resolution"])
            if "x" in res:
                rw, rh = res.split("x", 1)
                gen_w, gen_h = int(rw), int(rh)
            elif res == "720p":
                gen_w, gen_h = 1024, 576
            else:
                gen_w, gen_h = 1280, 720

            # LTX 系约束校正（帧数与分辨率并列）：
            # - VAE 时序压缩 8:1，帧数需 ≡ 1 (mod 8)，上限 257 帧
            # - VAE 空间压缩 32:1，宽高需为 32 的倍数（1080p 预设
            #   1280x720 中 720%32=16，按半向上取整对齐为 736）
            if "LTX" in cls_name:
                num_frames = min(257, ((max(9, num_frames) - 1 + 7) // 8) * 8 + 1)
                gen_w = max(32, (gen_w + 16) // 32 * 32)
                gen_h = max(32, (gen_h + 16) // 32 * 32)

            num_steps = 30
            generation_kwargs = {
                "prompt": request.description or "animated scene",
                "num_frames": num_frames,
                "num_inference_steps": num_steps,
                "guidance_scale": 7.5,
                "height": gen_h,
                "width": gen_w,
            }
            # 逐步去噪进度（管线支持 callback_on_step_end 时接入，
            # denoise 段映射 0→0.9；不支持则维持分段粗粒度）
            generation_kwargs.update(
                _make_step_callback(self._pipeline.__call__, relay,
                                    num_steps, span=0.9))
            if i2v_capable and screenshot_img is not None:
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
                pass

            result = self._pipeline(**generation_kwargs)

            # 保存视频文件
            relay(0.95, "encode")
            video_dir = DATA_DIR / "videos"
            video_dir.mkdir(parents=True, exist_ok=True)
            video_path = str(video_dir / f"{gen_id}.mp4")

            frames = result.frames if hasattr(result, "frames") else result[0]
            # 规整为 PIL 帧列表（多数视频管线返回 [batch][frames]）
            if frames is not None and len(frames) \
                    and isinstance(frames[0], (list, tuple)):
                frames = frames[0]
            frames_list = [f for f in frames]
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
                        pass

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
        progress_cb: Optional[Callable[[float, str], None]] = None,
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
            raise ApiError(
                60004,
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
                pass
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
            raise ApiError(60004, f"视频导出失败：{exc}") from exc
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
        progress_cb: Optional[Callable[[float, str], None]] = None,
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
            raise ApiError(60004, f"视频导出失败：{exc}") from exc

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
        return {
            "engine": "video",
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
_video_engine_instance: Optional[VideoEngine] = None
_video_engine_instance_lock = threading.Lock()


def get_video_engine() -> VideoEngine:
    """获取 VideoEngine 进程级单例（双检锁懒创建）。"""
    global _video_engine_instance
    if _video_engine_instance is None:
        with _video_engine_instance_lock:
            if _video_engine_instance is None:
                _video_engine_instance = VideoEngine()
    return _video_engine_instance
