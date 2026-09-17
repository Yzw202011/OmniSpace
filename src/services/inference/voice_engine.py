"""OmniSpace AI v2.1 语音推理引擎（规格 §5.3 语音模型路由）。

使用 CosyVoice3 / ChatTTS 进行语音合成。
GPT-SoVITS（F-08）：权重已随包 models/gpt-sovits/，但官方推理代码包与
中文 G2P（pypinyin）未随包，且权重目录内无音素符号表（s1 config 声明
phoneme_vocab_size=732，其 ID 映射定义在官方代码中）——文本前端无法产出
正确音素序列，合成质量不可接受。按诚实降级原则做探测与门控：引擎链中
跳过 sovits 并如实上报 get_status().sovits，绝不伪造 AI 合成。
模型未随包时分级回退（诚实标注，绝不伪造 AI 音色）：
  1. Windows SAPI5 系统语音（comtypes COM interop，真实发声，非 AI 音色）
  2. 静音占位 WAV（SAPI5 不可用时的最后兜底）
回退路径通过 fallback_backend / get_status().tts_backend 如实上报。
"""

from __future__ import annotations

import importlib
import logging
import os
import struct
import threading
import uuid
import wave
from pathlib import Path
from typing import Any

from ...config import DATA_DIR, MODELS_DIR, VOICE_PRESET_EMOTIONS
from ...middleware.error_handler import ApiError
from .base_engine import BaseEngine

logger = logging.getLogger("omnispace.inference.voice")


def _try_import(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except Exception:
        return None


_torch = _try_import("torch")

# ── SAPI5 系统语音回退（Windows COM interop）────────────────────────────
# SAPI 音频格式常量：SAFT24kHz16BitMono（与 AI 引擎输出采样率对齐）
_SAPI_FMT_24K_MONO = 26
# SpFileStream 打开模式：SSFMCreateForWrite
_SAPI_SSFM_CREATE_FOR_WRITE = 3
# 情感 → SAPI 语速映射（Rate 取值 -10..+10，尽力而为的情感表达）
_SAPI_RATE_MAP = {"愤怒": 2, "悲伤": -2}
# SAPI5 可用性惰性检测结果缓存（None=未探测）
_sapi5_ok: bool | None = None


def _probe_sapi5() -> bool:
    """探测 Windows SAPI5 系统语音是否可用（结果缓存，进程内探测一次）。"""
    global _sapi5_ok
    if _sapi5_ok is not None:
        return _sapi5_ok
    if os.name != "nt":
        _sapi5_ok = False
        return _sapi5_ok
    try:
        import comtypes.client  # noqa: PLC0415 - 惰性导入可选依赖
        comtypes.client.CreateObject("SAPI.SpVoice")
        _sapi5_ok = True
    except Exception:  # noqa: BLE001 - 任何 COM/依赖异常均视为不可用
        _sapi5_ok = False
    return _sapi5_ok


# ── GPT-SoVITS 探测与门控（F-08）──────────────────────────────────────
# 权重已随包（torch.load 实测可读：s1 含 weight/config/info，s2G 含 776 键
# state_dict），但完整 TTS 链路缺关键环节，合成质量不可接受，故只做探测
# 与如实上报，不接入合成链：
#   1. 官方推理代码包（gpt_sovits / GPT_SoVITS）未随包——AR Text2Semantic
#      decoder、VITS SynthesizerTrn、cnhubert 语义提取等模型类定义缺失；
#   2. 中文 G2P（pypinyin）未安装——汉字→拼音→音素转换无多音字消歧；
#   3. 权重目录内无音素符号表——s1 config 声明 phoneme_vocab_size=732，
#      其音素↔ID 映射定义在官方代码 text/symbols 中，未随包；
#   4. 未随包参考音频（克隆合成的必要输入）。
_SOVITS_MODEL_DIR = MODELS_DIR / "gpt-sovits"

# ── 语音模型动态发现（导入 models/ 即可用）──────────────────────────
# 《显存阶梯参考》语音档位：ASR=Whisper large-v3 等，TTS=Bark/CosyVoice2 等。
# transformers 原生支持 Whisper(ASR) 与 Bark(TTS)，导入即可真实推理；
# CosyVoice2/Fish Speech/Voxtral/Qwen3-TTS/FunASR/Canary 需各自官方代码包，
# 发现后如实上报依赖状态（不伪造合成/转写）。
_VOICE_NAME_HINTS = {
    "whisper": "asr_whisper",
    "canary": "asr_canary",
    "funasr": "asr_funasr",
    "paraformer": "asr_funasr",
    "sensevoice": "asr_funasr",
    "-asr": "asr_qwen",
    "bark": "tts_bark",
    "cosyvoice": "tts_cosyvoice",
    "chattts": "tts_chattts",
    "chat-tts": "tts_chattts",
    "fish-speech": "tts_fish",
    "fish_speech": "tts_fish",
    "voxtral": "tts_voxtral",
    "-tts": "tts_qwen",
}


def discover_voice_models() -> dict[str, dict]:
    """扫描 MODELS_DIR（两层）发现语音模型。

    Returns:
        {模型目录名: {"path": str, "kind": "asr_whisper"|"tts_bark"|...,
                    "ready": bool, "reason": str}}
        ready=True 表示当前环境可直接真实推理（whisper/bark 经 transformers）。
    """
    found: dict[str, dict] = {}
    base = Path(MODELS_DIR)
    if not base.is_dir():
        return found
    transformers = _try_import("transformers")

    def _probe(d: Path) -> None:
        name = d.name.lower()
        kind = ""
        for kw, k in _VOICE_NAME_HINTS.items():
            if kw in name:
                kind = k
                break
        if not kind:
            # config.json model_type 兜底识别
            cfg = d / "config.json"
            if cfg.is_file():
                try:
                    import json as _json
                    mt = str(_json.loads(cfg.read_text(encoding="utf-8"))
                             .get("model_type") or "").lower()
                    if mt == "whisper":
                        kind = "asr_whisper"
                    elif mt == "bark":
                        kind = "tts_bark"
                except Exception:  # noqa: BLE001
                    logger.debug("_probe: 降级忽略", exc_info=True)
        if not kind:
            return
        ready, reason = True, ""
        if kind in ("asr_whisper", "tts_bark"):
            if transformers is None:
                ready, reason = False, "transformers 依赖不可用"
            elif not (d / "config.json").is_file():
                ready, reason = False, "缺 config.json（非完整 transformers 模型目录）"
        elif kind == "asr_canary":
            ready, reason = False, "Canary 需 NVIDIA NeMo 代码包（未随包），已门控"
        elif kind == "asr_funasr":
            ready, reason = _try_import("funasr") is not None, \
                "" if _try_import("funasr") else "funasr 代码包未安装，已门控"
        elif kind == "asr_qwen":
            ready, reason = False, "Qwen3-ASR 官方推理代码包未随包，已门控"
        elif kind == "tts_cosyvoice":
            ready, reason = _try_import("cosyvoice") is not None, \
                "" if _try_import("cosyvoice") else "cosyvoice 代码包未安装，已门控"
        elif kind == "tts_chattts":
            ready, reason = _try_import("ChatTTS") is not None, \
                "" if _try_import("ChatTTS") else "ChatTTS 代码包未安装，已门控"
        elif kind == "tts_fish":
            ready, reason = False, "Fish Speech 官方代码包未随包，已门控"
        elif kind == "tts_voxtral":
            ready, reason = False, "Voxtral 需 mistral_common/vllm 代码包（未随包），已门控"
        elif kind == "tts_qwen":
            ready, reason = False, "Qwen3-TTS 官方推理代码包未随包，已门控"
        found[d.name] = {"path": str(d), "kind": kind,
                         "ready": ready, "reason": reason}

    try:
        for child in sorted(base.iterdir()):
            if child.is_dir() and not child.name.startswith(("_", ".")):
                _probe(child)
                try:
                    for grand in sorted(child.iterdir()):
                        if grand.is_dir():
                            _probe(grand)
                except OSError:
                    logger.debug("discover_voice_models: 降级忽略", exc_info=True)
    except OSError:
        logger.debug("discover_voice_models: 降级忽略", exc_info=True)
    return found


def _read_wav_float32(wav_path: str) -> tuple[Any, int]:
    """读取 WAV 为 float32 单声道数组（Whisper 输入要求 16kHz）。

    非 16kHz 采样率经 numpy 线性插值重采样；多声道取均值混单声道。
    支持 8/16/32bit PCM。

    Returns:
        (float32 numpy 数组, 采样率 16000)
    """
    import numpy as np
    with wave.open(wav_path, "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        width = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    if width == 2:
        data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        data = (np.frombuffer(frames, dtype=np.int32).astype(np.float32)
                / 2147483648.0)
    elif width == 1:
        data = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32)
                - 128.0) / 128.0
    else:
        raise ApiError(code=71004,
                       message=f"不支持的 WAV 位深: {width * 8}bit",
                       suggestion="请使用 16bit PCM WAV 或其他常见音频格式")
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    if sr != 16000 and len(data) > 0:
        n = max(1, int(round(len(data) * 16000.0 / sr)))
        data = np.interp(np.linspace(0.0, len(data) - 1, n),
                         np.arange(len(data)), data).astype(np.float32)
    return data, 16000


# TTS 推理必需权重清单（s2D 判别器与 sv 说话人验证模型非推理必需，不计入）
_SOVITS_REQUIRED_WEIGHTS = (
    "chinese-hubert-base/config.json",
    "chinese-hubert-base/pytorch_model.bin",
    "chinese-roberta-wwm-ext-large/config.json",
    "chinese-roberta-wwm-ext-large/tokenizer.json",
    "chinese-roberta-wwm-ext-large/pytorch_model.bin",
    "gsv-v2final-pretrained/s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt",
    "gsv-v2final-pretrained/s2G2333k.pth",
)
# GPT-SoVITS 探测结果缓存（None=未探测）
_sovits_probe_cache: dict | None = None


def _probe_sovits() -> dict:
    """探测 GPT-SoVITS 权重完整性与推理代码可用性（结果缓存，进程内一次）。

    Returns:
        {
          "weights_ready": bool,    # 必需权重文件齐备且非空
          "code_missing": bool,     # 官方推理代码或 pypinyin 缺失
          "missing_weights": list,  # 缺失的权重相对路径
          "reason": str,            # 门控/缺失原因（可接入时为空串）
        }
    """
    global _sovits_probe_cache
    if _sovits_probe_cache is not None:
        return _sovits_probe_cache
    missing = []
    for rel in _SOVITS_REQUIRED_WEIGHTS:
        path = _SOVITS_MODEL_DIR / rel
        if not path.is_file() or path.stat().st_size == 0:
            missing.append(rel)
    weights_ready = not missing
    code_missing = (
        (_try_import("gpt_sovits") is None and _try_import("GPT_SoVITS") is None)
        or _try_import("pypinyin") is None
    )
    if not weights_ready:
        reason = "GPT-SoVITS 权重未随包或不完整（缺: {}）".format("、".join(missing))
    elif code_missing:
        reason = (
            "GPT-SoVITS 权重已随包，但官方推理代码包与 pypinyin（中文 G2P）未安装，"
            "且权重目录无音素符号表（s1 config: phoneme_vocab_size=732 的映射未随包），"
            "合成质量不可接受，按诚实降级原则门控跳过"
        )
    else:
        reason = ""
    _sovits_probe_cache = {
        "weights_ready": weights_ready,
        "code_missing": code_missing,
        "missing_weights": missing,
        "reason": reason,
    }
    return _sovits_probe_cache


class VoiceEngine(BaseEngine):
    """语音推理引擎——CosyVoice3 / ChatTTS / Bark（GPT-SoVITS 探测门控）。

    自动装载链（_ensure_tts_loaded）：cosyvoice → chattts → bark（transformers
    原生，导入 models/ 即可用）→ SAPI5/静音回退。
    ASR：Whisper（transformers 原生，导入 models/whisper* 即可真实转写）。

    协议注记（ADR-003 P2）：本引擎 load_model 契约为路径/后端驱动
    （model_path + engine_type），与 BaseEngine 的 model_id 语义不同——
    管理器侧只消费 is_ready/get_status/unload_model 面，load_model 由
    voice 服务自行编排（当前无 ensure_loaded("voice",...) 调用方）。
    """

    name = "voice"
    serves_categories = ("voice",)

    def __init__(self) -> None:
        super().__init__()
        self._model: Any = None
        self._model_name: str = ""
        self._loaded = False
        self._fallback_mode = False
        self._device = "cpu"
        self._engine_type: str = ""  # cosyvoice | chattts | bark | sovits | mock
        # 最近一次回退合成实际使用的后端：sapi5 | silent（非回退时为空串）
        self._last_fallback_backend: str = ""
        # TTS 自动装载尝试标记（失败只试一次，避免每次合成重复装载）
        self._tts_autoload_attempted = False
        # ── ASR（Whisper）运行时状态 ──
        self._asr_pipe: Any = None
        self._asr_model_id: str = ""
        self._asr_error: str = ""
        self._asr_lock = threading.Lock()

        if _torch is None:
            logger.warning("语音引擎依赖不可用 (torch)，将使用模拟模式")
            self._fallback_mode = True

    def load_model(self, model_path: str, engine_type: str = "cosyvoice", device: str = "auto") -> bool:
        """加载语音合成模型。

        支持三种后端:
          - cosyvoice: CosyVoice3 模型
          - chattts: ChatTTS 模型
          - sovits: GPT-SoVITS（F-08）——仅探测与门控，代码包缺失时
            如实跳过并回退（见 _load_sovits）

        Args:
            model_path: 模型路径
            engine_type: 引擎类型 ('cosyvoice' | 'chattts' | 'sovits')
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

        if engine_type == "cosyvoice":
            return self._load_cosyvoice(model_path)
        elif engine_type == "chattts":
            return self._load_chattts(model_path)
        elif engine_type == "bark":
            return self._load_bark(model_path)
        elif engine_type == "sovits":
            return self._load_sovits(model_path)
        else:
            logger.warning("未知语音引擎类型: %s", engine_type)
            self._fallback_mode = True
            return False

    def _load_cosyvoice(self, model_path: str) -> bool:
        """加载 CosyVoice3 模型。"""
        try:
            # 尝试导入 CosyVoice
            cosyvoice_cls = _try_import("cosyvoice.cli.cosyvoice")
            if cosyvoice_cls is None:
                # 尝试其他导入路径
                cosyvoice_cls = _try_import("CosyVoice")
            if cosyvoice_cls is None:
                logger.warning("CosyVoice 库不可用")
                self._fallback_mode = True
                return False

            CosyVoice = getattr(cosyvoice_cls, "CosyVoice", None) or cosyvoice_cls
            self._model = CosyVoice(model_path)
            self._model_name = model_path
            self._engine_type = "cosyvoice"
            self._loaded = True
            self._fallback_mode = False
            logger.info("CosyVoice3 加载成功: %s", model_path)
            return True
        except Exception as e:
            logger.warning("CosyVoice3 加载失败: %s", e)
            self._fallback_mode = True
            return False

    def _load_chattts(self, model_path: str) -> bool:
        """加载 ChatTTS 模型。"""
        try:
            chattts_module = _try_import("ChatTTS")
            if chattts_module is None:
                logger.warning("ChatTTS 库不可用")
                self._fallback_mode = True
                return False

            self._model = chattts_module.Chat()
            self._model.load(compile=False)  # 初次加载不编译以加速
            self._model_name = model_path
            self._engine_type = "chattts"
            self._loaded = True
            self._fallback_mode = False
            logger.info("ChatTTS 加载成功: %s", model_path)
            return True
        except Exception as e:
            logger.warning("ChatTTS 加载失败: %s", e)
            self._fallback_mode = True
            return False

    def _load_bark(self, model_path: str) -> bool:
        """加载 Bark TTS 模型（transformers 原生支持，导入 models/ 即可用）。

        《显存阶梯参考》TTS Top5 第 5 名：多语言 + 音效生成，~2GB 显存。
        BarkModel + AutoProcessor 本地加载，无需官方代码包。
        """
        transformers = _try_import("transformers")
        if transformers is None or _torch is None:
            logger.warning("Bark 加载失败: transformers/torch 不可用")
            self._fallback_mode = True
            return False
        try:
            processor = transformers.AutoProcessor.from_pretrained(model_path)
            bark_cls = getattr(transformers, "BarkModel", None)
            if bark_cls is None:
                raise RuntimeError("当前 transformers 版本不含 BarkModel")
            dtype = _torch.float16 if self._device == "cuda" else _torch.float32
            model = bark_cls.from_pretrained(model_path, torch_dtype=dtype)
            model = model.to(self._device)
            self._model = (model, processor)
            self._model_name = model_path
            self._engine_type = "bark"
            self._loaded = True
            self._fallback_mode = False
            logger.info("Bark 加载成功: %s", model_path)
            return True
        except Exception as e:
            logger.warning("Bark 加载失败: %s", e)
            self._fallback_mode = True
            return False

    def _ensure_tts_loaded(self) -> None:
        """TTS 自动装载链（导入 models/ 即可用）：cosyvoice → chattts → bark。

        仅在未加载且未尝试过时执行一次；全部不可用保持回退模式
        （SAPI5/静音，诚实上报），不伪造 AI 合成。
        """
        if self._loaded or self._tts_autoload_attempted or _torch is None:
            return
        self._tts_autoload_attempted = True
        if _torch.cuda.is_available():
            self._device = "cuda"
        order = (("tts_cosyvoice", "cosyvoice"),
                 ("tts_chattts", "chattts"),
                 ("tts_bark", "bark"))
        discovered = discover_voice_models()
        for kind, engine_type in order:
            for name, info in discovered.items():
                if info["kind"] != kind or not info["ready"]:
                    continue
                logger.info("TTS 自动装载尝试: %s (%s)", name, engine_type)
                if self.load_model(info["path"], engine_type=engine_type,
                                   device=self._device):
                    return
        logger.info("未发现可用 AI TTS 模型，保持 SAPI5/静音回退链")

    def _synthesize_bark(
        self, voice_id: str, text: str, emotion: str, output_path: str
    ) -> str:
        """使用 Bark 合成语音（transformers BarkModel，24kHz 输出）。"""
        import numpy as np
        model, processor = self._model
        # voice_id 映射 Bark voice_preset（v2 内置音色）；非预设名则用默认
        preset = voice_id if voice_id.startswith("v2/") else "v2/zh_speaker_0"
        try:
            inputs = processor(text, voice_preset=preset)
        except Exception:  # noqa: BLE001 - 音色名无效时退回默认音色
            logger.info("Bark 音色 %s 不可用，退回默认中文音色", preset)
            inputs = processor(text, voice_preset="v2/zh_speaker_0")
        inputs = {k: (v.to(self._device) if hasattr(v, "to") else v)
                  for k, v in inputs.items()}
        with _torch.no_grad():
            audio = model.generate(**inputs, do_sample=True)
        audio = audio.detach().cpu().float().numpy().squeeze()
        sr = int(getattr(model.config, "sampling_rate", 24000) or 24000)
        pcm = np.clip(audio, -1.0, 1.0)
        pcm16 = (pcm * 32767.0).astype(np.int16)
        with wave.open(output_path, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sr)
            wav_file.writeframes(pcm16.tobytes())
        logger.info("Bark 合成完成: %s (emotion=%s, sr=%d)",
                    output_path, emotion, sr)
        return output_path

    # ── ASR：Whisper 真实转写（导入 models/whisper* 即可用）────────────

    def load_asr(self, model_id: str | None = None) -> bool:
        """加载 ASR 模型（Whisper，transformers pipeline，30s 分块长音频）。

        Args:
            model_id: 指定发现的 whisper 目录名；None 自动选第一个 ready 的

        Returns:
            True 加载成功
        """
        with self._asr_lock:
            if self._asr_pipe is not None:
                if not model_id or model_id == self._asr_model_id:
                    return True
            transformers = _try_import("transformers")
            if transformers is None or _torch is None:
                self._asr_error = "transformers/torch 依赖不可用"
                return False
            candidates = {n: i for n, i in discover_voice_models().items()
                          if i["kind"] == "asr_whisper" and i["ready"]}
            if not candidates:
                self._asr_error = ("未发现 Whisper 模型目录（models/whisper*），"
                                   "请先导入模型（如 openai/whisper-large-v3）")
                logger.info("ASR 门控: %s", self._asr_error)
                return False
            if model_id:
                if model_id not in candidates:
                    self._asr_error = f"指定 ASR 模型未就绪: {model_id}"
                    return False
                name, info = model_id, candidates[model_id]
            else:
                # large 优先（质量最高），其余按名称序
                names = sorted(candidates, key=lambda n: ("large" not in n, n))
                name, info = names[0], candidates[names[0]]
            try:
                device = "cuda" if _torch.cuda.is_available() else "cpu"
                dtype = _torch.float16 if device == "cuda" else _torch.float32
                self._asr_pipe = transformers.pipeline(
                    "automatic-speech-recognition",
                    model=info["path"],
                    torch_dtype=dtype,
                    device=device,
                )
                self._asr_model_id = name
                self._asr_error = ""
                logger.info("ASR 模型加载成功: %s (%s)", name, info["path"])
                return True
            except Exception as exc:  # noqa: BLE001
                self._asr_pipe = None
                self._asr_error = f"ASR 模型加载失败: {exc}"
                logger.warning("%s", self._asr_error)
                return False

    def transcribe(self, audio_path: str,
                   language: str | None = None,
                   model_id: str | None = None) -> dict:
        """语音转写（Whisper 真实推理）。

        音频解码：WAV 走 stdlib wave（16k 重采样经 numpy 线性插值）；
        其他格式经 runtime/ffmpeg 转码为 16k 单声道 WAV。ffmpeg 也不可用
        时抛 ApiError 诚实报错。

        Returns:
            {"text": str, "language": str, "duration_s": float,
             "model": str, "backend": "whisper-transformers"}
        """
        if not os.path.isfile(audio_path):
            raise ApiError(code=71002, message=f"音频文件不存在: {audio_path}",
                           suggestion="请上传有效的音频文件")
        if self._asr_pipe is None and not self.load_asr(model_id):
            raise ApiError(code=71003,
                           message=self._asr_error or "ASR 模型未就绪",
                           suggestion="把 whisper 模型目录放入 models/ 后重试")
        wav_path, tmp = self._ensure_wav16k(audio_path)
        try:
            audio, sr = _read_wav_float32(wav_path)
            duration_s = round(len(audio) / float(sr), 2)
            gen_kwargs: dict[str, Any] = {}
            if language:
                gen_kwargs["language"] = language
            result = self._asr_pipe(
                audio, chunk_length_s=30, stride_length_s=5,
                # 空字典也要传 dict：None 会炸 transformers 的
                # generate_kwargs.pop（auto 语言模式，2026-09-11 实弹揪出）
                **({"generate_kwargs": gen_kwargs} if gen_kwargs else {}),
                return_timestamps=False,
            )
            text = str(result.get("text") or "").strip()
            logger.info("ASR 转写完成: %s (%.1fs -> %d 字)",
                        audio_path, duration_s, len(text))
            return {"text": text, "language": language or "auto",
                    "duration_s": duration_s, "model": self._asr_model_id,
                    "backend": "whisper-transformers"}
        finally:
            if tmp:
                try:
                    os.unlink(wav_path)
                except OSError:
                    logger.debug("transcribe: 降级忽略", exc_info=True)

    def _ensure_wav16k(self, audio_path: str) -> tuple[str, bool]:
        """确保得到 16kHz 单声道 WAV；非 WAV 经 ffmpeg 转码。

        Returns:
            (wav 路径, 是否临时文件)
        """
        if audio_path.lower().endswith(".wav"):
            return audio_path, False
        ffmpeg = ""
        try:
            from ..encoder_service import get_encoder_service
            ffmpeg = get_encoder_service()._ffmpeg or ""
        except Exception:  # noqa: BLE001
            logger.debug("_ensure_wav16k: 降级忽略", exc_info=True)
        if not ffmpeg:
            raise ApiError(code=71004,
                           message="非 WAV 音频需要 FFmpeg 转码，当前环境 FFmpeg 不可用",
                           suggestion="请上传 WAV 格式音频")
        tmp_path = str(DATA_DIR / "audio" / f"asr_{uuid.uuid4().hex}.wav")
        Path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        import subprocess
        proc = subprocess.run(
            [ffmpeg, "-y", "-i", audio_path, "-ar", "16000", "-ac", "1",
             "-f", "wav", tmp_path],
            capture_output=True, timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if proc.returncode != 0 or not os.path.isfile(tmp_path):
            raise ApiError(code=71004, message="音频转码失败（FFmpeg）",
                           detail={"stderr": proc.stderr.decode("utf-8", "ignore")[-300:]})
        return tmp_path, True

    @property
    def asr_ready(self) -> bool:
        """ASR 是否可用（whisper 已加载或存在可加载的 whisper 目录）。"""
        if self._asr_pipe is not None:
            return True
        return any(i["kind"] == "asr_whisper" and i["ready"]
                   for i in discover_voice_models().values())

    def _load_sovits(self, model_path: str) -> bool:
        """GPT-SoVITS 引擎（F-08）——探测与门控，不执行合成。

        权重已随包 models/gpt-sovits/（torch.load 实测可读），但完整 TTS
        链路缺关键环节（官方推理代码包、pypinyin 中文 G2P、732 音素符号表、
        参考音频均缺失，详见模块顶部注释），合成质量不可接受。按诚实降级
        原则：此处仅探测并记录跳过原因，引擎链下沉到 SAPI5/静音回退，
        探测结果经 get_status().sovits 如实上报。
        """
        probe = _probe_sovits()
        if probe["weights_ready"] and not probe["code_missing"]:
            # 权重与代码齐备的未来扩展点：当前版本尚未实现 sovits 合成路径，
            # 仍诚实回退，不伪造 AI 音色。
            logger.warning("GPT-SoVITS 依赖齐备，但合成路径尚未实现，回退降级")
        else:
            logger.warning("GPT-SoVITS 门控跳过: %s", probe["reason"])
        self._fallback_mode = True
        return False

    def select_voice(self, available_vram_gb: float) -> str:
        """根据可用显存选择语音模型。

        路由:
          - 8GB+ -> CosyVoice3
          - 4GB+ -> ChatTTS
          - 0GB+ -> CPU 降级

        Args:
            available_vram_gb: 可用显存（GB）

        Returns:
            语音模型标识
        """
        if available_vram_gb >= 8:
            return "cosyvoice3"
        elif available_vram_gb >= 4:
            return "chattts"
        else:
            return "cpu-fallback"

    def synthesize(
        self,
        voice_id: str,
        text: str,
        emotion: str = "默认",
    ) -> str:
        """合成语音。

        规格 §5.3: 支持多音色、情感标注。
        情感标签: 默认 / 愤怒 / 悲伤（规格 §7 manga.voice_preset_emotions）

        Args:
            voice_id: 音色 ID
            text: 待合成文本
            emotion: 情感标签

        Returns:
            生成的音频文件路径

        Raises:
            ApiError: 合成失败
        """
        if not text.strip():
            raise ApiError(
                code=71001,
                message="待合成文本为空",
                suggestion="请输入有效的文本内容",
            )

        # 未加载时先尝试自动装载链（cosyvoice → chattts → bark，导入即可用）
        if not self._loaded and not self._fallback_mode:
            self._ensure_tts_loaded()

        # 降级模式
        if self._fallback_mode or not self._loaded:
            return self._mock_synthesize(voice_id, text, emotion)

        # 真实合成
        try:
            audio_dir = DATA_DIR / "audio"
            audio_dir.mkdir(parents=True, exist_ok=True)
            audio_path = str(audio_dir / f"{uuid.uuid4()}.wav")

            if self._engine_type == "cosyvoice":
                return self._synthesize_cosyvoice(voice_id, text, emotion, audio_path)
            elif self._engine_type == "chattts":
                return self._synthesize_chattts(voice_id, text, audio_path)
            elif self._engine_type == "bark":
                return self._synthesize_bark(voice_id, text, emotion, audio_path)
            else:
                return self._mock_synthesize(voice_id, text, emotion)

        except Exception as e:
            logger.error("语音合成失败: %s", e)
            return self._mock_synthesize(voice_id, text, emotion)

    def _synthesize_cosyvoice(
        self, voice_id: str, text: str, emotion: str, output_path: str
    ) -> str:
        """使用 CosyVoice3 合成语音。"""
        try:
            # CosyVoice 接口
            chunks = self._model.inference_sft(
                text,
                voice_id,
                stream=False,
            )
            # 保存音频
            import torchaudio
            for i, chunk in enumerate(chunks):
                wav = chunk.get("tts_speech") if isinstance(chunk, dict) else chunk
                if i == 0:
                    torchaudio.save(output_path, wav, 24000)
                else:
                    # 追加
                    prev_wav, sr = torchaudio.load(output_path)
                    combined = _torch.cat([prev_wav, wav], dim=1)
                    torchaudio.save(output_path, combined, sr)

            logger.info("CosyVoice 合成完成: %s (emotion=%s)", output_path, emotion)
            return output_path
        except Exception as e:
            logger.warning("CosyVoice 合成异常: %s", e)
            raise

    def _synthesize_chattts(self, voice_id: str, text: str, output_path: str) -> str:
        """使用 ChatTTS 合成语音。"""
        try:
            import torchaudio
            wavs = self._model.infer(text, use_decoder=True)
            for wav in wavs:
                torchaudio.save(
                    output_path,
                    _torch.from_numpy(wav).unsqueeze(0),
                    24000,
                )
                break  # 只取第一个

            logger.info("ChatTTS 合成完成: %s", output_path)
            return output_path
        except Exception as e:
            logger.warning("ChatTTS 合成异常: %s", e)
            raise

    def _mock_synthesize(self, voice_id: str, text: str, emotion: str) -> str:
        """回退语音合成（AI 模型未随包时的分级降级）。

        优先级：SAPI5 系统语音（真实发声）→ 静音占位 WAV（最后兜底）。
        实际后端记入 _last_fallback_backend，供 API 层生成诚实降级文案。
        """
        audio_dir = DATA_DIR / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)

        # 一级回退：Windows SAPI5 系统语音（真实可听，非 AI 音色）
        if _probe_sapi5():
            audio_path = str(audio_dir / f"{uuid.uuid4()}_sapi5.wav")
            try:
                result = self._synthesize_sapi5(text, emotion, audio_path)
                self._last_fallback_backend = "sapi5"
                logger.info("[回退-SAPI5] 系统语音合成: %s (voice=%s, emotion=%s)",
                            result, voice_id, emotion)
                return result
            except Exception as exc:  # noqa: BLE001 - 失败继续下沉到静音兜底
                logger.warning("SAPI5 合成失败，下沉到静音占位: %s", exc)
                try:
                    os.unlink(audio_path)
                except OSError:
                    logger.debug("_mock_synthesize: 降级忽略", exc_info=True)

        # 二级回退：静音占位 WAV（基于文本长度估算时长）
        audio_path = str(audio_dir / f"{uuid.uuid4()}_mock.wav")
        duration_seconds = max(1.0, len(text) * 0.1)  # 每字约 0.1 秒
        sample_rate = 24000
        num_samples = int(duration_seconds * sample_rate)

        with wave.open(audio_path, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(sample_rate)
            # 写入静音数据（全零）
            silence = struct.pack("<" + "h" * num_samples, *([0] * num_samples))
            wav_file.writeframes(silence)

        self._last_fallback_backend = "silent"
        logger.info("[回退-静音] 占位合成: %s (voice=%s, emotion=%s, %.1fs)",
                    audio_path, voice_id, emotion, duration_seconds)
        return audio_path

    @staticmethod
    def _synthesize_sapi5(text: str, emotion: str, output_path: str) -> str:
        """用 Windows SAPI5 将文本合成为 24kHz 16bit 单声道 WAV（真实发声）。

        情感以语速映射尽力表达（愤怒+2 / 悲伤-2 / 默认0）。
        引擎方法为同步核心；COM 调用阻塞由 API 层经 run_blocking
        （services/offload.py 唯一同步推理入口）统一卸载，结构上无
        直调误用面（P1-06）。
        """
        import comtypes.client  # noqa: PLC0415 - 惰性导入可选依赖

        speaker = comtypes.client.CreateObject("SAPI.SpVoice")
        stream = comtypes.client.CreateObject("SAPI.SpFileStream")
        fmt = comtypes.client.CreateObject("SAPI.SpAudioFormat")
        fmt.Type = _SAPI_FMT_24K_MONO
        stream.Format = fmt
        stream.Open(output_path, _SAPI_SSFM_CREATE_FOR_WRITE, False)
        try:
            speaker.AudioOutputStream = stream
            speaker.Rate = _SAPI_RATE_MAP.get(emotion, 0)
            speaker.Volume = 100
            speaker.Speak(text, 0)  # SVSFDefault：同步读完
        finally:
            try:
                stream.Close()
            except Exception:  # noqa: BLE001
                logger.debug("_synthesize_sapi5: 降级忽略", exc_info=True)
            try:
                speaker.AudioOutputStream = None
            except Exception:  # noqa: BLE001
                logger.debug("_synthesize_sapi5: 降级忽略", exc_info=True)
        if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 44:
            raise RuntimeError("SAPI5 输出文件无效")
        return output_path

    @property
    def is_ready(self) -> bool:
        """引擎是否就绪。"""
        return self._loaded and not self._fallback_mode

    @property
    def fallback_backend(self) -> str:
        """回退模式实际使用的后端：sapi5 | silent（非回退/未合成过为 ""）。"""
        return self._last_fallback_backend

    @property
    def model_name(self) -> str:
        """当前加载的模型名。"""
        return self._model_name or "mock-voice"

    def get_status(self) -> dict:
        """返回引擎状态。

        sovits 字段为 GPT-SoVITS（F-08）探测门控结果：权重齐但代码包缺失时
        code_missing=true 且 reason 说明门控原因；sovits 永不会成为
        tts_backend（门控路径不合成），实际回退后端见 tts_backend。
        """
        from .base_engine import derive_state
        if self._loaded and not self._fallback_mode:
            tts_backend = self._engine_type
        elif _probe_sapi5():
            tts_backend = "sapi5"
        else:
            tts_backend = "silent"
        return {
            "engine": "voice",
            # ADR-003 P3：统一状态（SAPI5/静音回退不算 AI ready）
            "state": derive_state(
                loaded=self._loaded and not self._fallback_mode),
            "engine_type": self._engine_type,
            "model": self._model_name,
            "loaded": self._loaded,
            "fallback": self._fallback_mode,
            "device": self._device,
            "has_torch": _torch is not None,
            "tts_backend": tts_backend,
            "sapi5_available": _probe_sapi5(),
            "sovits": _probe_sovits(),
            "asr_ready": self.asr_ready,
            "asr_model": self._asr_model_id,
            "asr_error": self._asr_error,
            "discovered_models": discover_voice_models(),
            "supported_emotions": VOICE_PRESET_EMOTIONS,
        }

    def unload_model(self) -> bool:
        """卸载 TTS/ASR 模型引用（BaseEngine 协议薄适配）。

        只释放 Python 引用与复位标记，CUDA 缓存回收由调用方
        （ModelManager.unload_model 通用尾段）统一执行。

        Returns:
            是否确有已加载内容被释放（空载返回 False，非错误）。
        """
        had_any = self._model is not None or self._asr_pipe is not None
        self._model = None
        self._model_name = ""
        self._loaded = False
        self._engine_type = ""
        # 复位自动装载标记：卸载后允许下次合成重试真实管线
        self._tts_autoload_attempted = False
        with self._asr_lock:
            self._asr_pipe = None
            self._asr_model_id = ""
        if had_any:
            logger.info("语音引擎模型引用已释放")
        return had_any



# =============================================================
#  单例（ADR-003 P2：voice 品类注册表解析入口，与管理器共享实例）
# =============================================================

_engine_instance: VoiceEngine | None = None
_engine_lock = threading.Lock()


def get_voice_engine() -> VoiceEngine:
    """获取语音引擎全局单例（线程安全双重检查）。"""
    global _engine_instance
    if _engine_instance is None:
        with _engine_lock:
            if _engine_instance is None:
                _engine_instance = VoiceEngine()
    return _engine_instance
# 本项目仅供学习使用，商业授权请+Q 3559331368
