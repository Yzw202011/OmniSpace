"""OmniSpace AI v2.1 模型类型识别模块（规格 §5.4 模型导入）。

根据文件特征和元数据自动识别模型类型（ModelCategory）。
支持 GGUF、Safetensors、ONNX、Diffusers 目录等格式。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368

from __future__ import annotations

import json
import logging
from pathlib import Path

from ...data.models import ModelCategory

log = logging.getLogger("omnispace.model_manager.classifier")


# ── 文件特征映射 ────────────────────────────────────────────────
# 文件扩展名 -> 模型类型
_EXTENSION_MAP = {
    ".gguf": ModelCategory.DIALOG,       # GGUF 通常是对话模型
    ".safetensors": None,                 # 需进一步判断（通用格式）
    ".onnx": None,                        # 需进一步判断
    ".pt": None,
    ".bin": None,
    ".ckpt": ModelCategory.VISION,               # checkpoint 通常为绘画模型
    ".pth": ModelCategory.AUXILIARY,
}

# 目录特征文件 -> 模型类型
_DIR_SIGNATURES = {
    "model_index.json": ModelCategory.VISION,    # diffusers 绘画模型
    "config.json": ModelCategory.DIALOG,          # transformers 对话模型
    "unet_config.json": ModelCategory.VISION,     # UNet 绘画模型
    "vae_config.json": ModelCategory.VISION,
    "tokenizer_config.json": ModelCategory.DIALOG,
    "model.safetensors.index.json": ModelCategory.DIALOG,
}

# 关键词 -> 模型类型（用于文件名/目录名匹配）
# 覆盖《显卡显存阶梯参考》全部入库模型家族：文本/代码 LLM、多模态 VLM、
# ASR/TTS 语音、视频生成。dict 保序，靠前的关键词优先命中。
_NAME_KEYWORDS = {
    # ── 语音（ASR/TTS）优先：whisper/bark 等名字可能被通用词遮蔽 ──
    "whisper": ModelCategory.VOICE,
    "canary": ModelCategory.VOICE,
    "funasr": ModelCategory.VOICE,
    "paraformer": ModelCategory.VOICE,
    "sensevoice": ModelCategory.VOICE,
    "-asr": ModelCategory.VOICE,          # Qwen3-ASR-1.7B / 0.6B
    "_asr": ModelCategory.VOICE,
    "-tts": ModelCategory.VOICE,          # Qwen3-TTS-1.7B
    "_tts": ModelCategory.VOICE,
    "fish-speech": ModelCategory.VOICE,
    "fish_speech": ModelCategory.VOICE,
    "voxtral": ModelCategory.VOICE,
    "cosyvoice": ModelCategory.VOICE,
    "chat-tts": ModelCategory.VOICE,
    "chattts": ModelCategory.VOICE,
    "sovits": ModelCategory.VOICE,
    "bark": ModelCategory.VOICE,
    # ── 语音子组件（声码器/音频编码器，SoVITS/TTS 依赖）─────────
    "hubert": ModelCategory.VOICE,        # chinese-hubert-base（SoVITS 语义提取）
    "bigvgan": ModelCategory.VOICE,       # BigVGAN 声码器
    "wav2vec": ModelCategory.VOICE,
    "encodec": ModelCategory.VOICE,
    # ── 视觉语音全模态（omni）：文/图/视/音输入，文+语音输出 ────
    # 必须排在 "qwen" 之前（qwen2.5-omni 含 qwen 前缀会被遮蔽）
    "-omni": ModelCategory.OMNI,          # Qwen2.5-Omni-7B
    "_omni": ModelCategory.OMNI,
    # ── 文本编码器/嵌入（非对话生成模型）─────────────────────────
    "roberta": ModelCategory.LANGUAGE,    # chinese-roberta-wwm（SoVITS 文本编码）
    "-bert": ModelCategory.LANGUAGE,
    "_bert": ModelCategory.LANGUAGE,
    "bge-": ModelCategory.LANGUAGE,       # BGE 嵌入系列
    "text2vec": ModelCategory.LANGUAGE,
    # ── 视频生成 ────────────────────────────────────────────────
    "ltx": ModelCategory.VIDEO,           # LTX-Video / LTX-2.x
    "wan2": ModelCategory.VIDEO,          # Wan 2.1 / 2.2
    "cogvideo": ModelCategory.VIDEO,
    "hunyuan": ModelCategory.VIDEO,       # HunyuanVideo
    "animatediff": ModelCategory.VIDEO,
    "animatelcm": ModelCategory.VIDEO,
    "modelscope-t2v": ModelCategory.VIDEO,
    "t2v": ModelCategory.VIDEO,           # ModelScope-T2V 等文本生视频
    "i2v": ModelCategory.VIDEO,
    "minimax": ModelCategory.VIDEO,       # MiniMax H3
    # ── 多模态 VLM（导入对话模块使用，归为 DIALOG）───────────────
    "-vl-": ModelCategory.DIALOG,         # Qwen2-VL-7B / Qwen3-VL-8B
    "_vl_": ModelCategory.DIALOG,
    "internvl": ModelCategory.DIALOG,
    "llava": ModelCategory.DIALOG,
    "minicpm-v": ModelCategory.DIALOG,
    "minicpm_v": ModelCategory.DIALOG,
    "cogvlm": ModelCategory.DIALOG,
    "phi-4-multimodal": ModelCategory.DIALOG,
    # ── 文本 / 代码 LLM ─────────────────────────────────────────
    "qwen": ModelCategory.DIALOG,         # Qwen3 全系 / Qwen2.5-Coder 等
    "llama": ModelCategory.DIALOG,        # Llama 3.1/3.3/4、CodeLlama
    "mistral": ModelCategory.DIALOG,
    "codestral": ModelCategory.DIALOG,
    "chatglm": ModelCategory.DIALOG,
    "glm-4": ModelCategory.DIALOG,
    "glm4": ModelCategory.DIALOG,
    "deepseek": ModelCategory.DIALOG,     # V2/V3/R1/Coder 全系
    "yi-1.5": ModelCategory.DIALOG,
    "yi_1.5": ModelCategory.DIALOG,
    "phi-3": ModelCategory.DIALOG,
    "phi_3": ModelCategory.DIALOG,
    "starcoder": ModelCategory.DIALOG,
    "coder": ModelCategory.DIALOG,        # *-Coder 系列兜底
    "gemma": ModelCategory.DIALOG,
    "baichuan": ModelCategory.DIALOG,
    # ── 绘画 / 视觉 ─────────────────────────────────────────────
    "flux": ModelCategory.VISION,
    "sdxl": ModelCategory.VISION,
    "stable-diffusion": ModelCategory.VISION,
    "kolors": ModelCategory.VISION,
    "z-image": ModelCategory.VISION,       # Z-Image-Turbo（Z1 2026-09-15）
    "z_image": ModelCategory.VISION,
    "clip": ModelCategory.VISION,
    "vit": ModelCategory.VISION,
    "depth": ModelCategory.VISION,
    # ── 3D ──────────────────────────────────────────────────────
    "triposr": ModelCategory.THREE_D,
    "instantmesh": ModelCategory.THREE_D,
}


class ModelClassifier:
    """模型分类器——根据文件特征/元数据自动归类。"""

    def classify(self, path: str) -> ModelCategory:
        """识别模型文件的类型。

        识别顺序:
          1. 如果是目录且含 config.json，先读 model_type/architectures 元数据
             （whisper/bark 等含 config.json 但非对话模型，元数据优先）
          2. 检查文件名/目录名中的关键词
          3. 目录特征文件（model_index.json 等）
          4. 检查文件扩展名
          5. 尝试读取元数据（safetensors header）
          6. 兜底返回 AUXILIARY

        Args:
            path: 模型文件或目录路径

        Returns:
            ModelCategory 枚举值
        """
        p = Path(path)

        # 1. 目录元数据检测（config.json model_type 优先于泛化签名）
        if p.is_dir() and (p / "config.json").is_file():
            category = self._classify_by_metadata(p)
            if category is not None:
                return category

        # 2. 文件名/目录名关键词匹配
        name_lower = p.name.lower()
        for keyword, category in _NAME_KEYWORDS.items():
            if keyword in name_lower:
                log.debug("关键词匹配: '%s' -> %s", keyword, category.value)
                return category

        # 3. 目录特征文件检测
        if p.is_dir():
            category = self._classify_by_dir(p)
            if category is not None:
                return category

        # 4. 文件扩展名
        if p.is_file():
            ext = p.suffix.lower()
            if ext in _EXTENSION_MAP and _EXTENSION_MAP[ext] is not None:
                return _EXTENSION_MAP[ext]

        # 5. 元数据检测（safetensors header 等）
        category = self._classify_by_metadata(p)
        if category is not None:
            return category

        # 6. 兜底
        log.warning("无法识别模型类型，默认归类为 AUXILIARY: %s", path)
        return ModelCategory.AUXILIARY

    def _classify_by_dir(self, dir_path: Path) -> ModelCategory | None:
        """通过目录中的特征文件识别类型。"""
        for filename, category in _DIR_SIGNATURES.items():
            if (dir_path / filename).exists():
                log.debug("目录特征文件 '%s' -> %s", filename, category.value)
                return category
        return None

    def _classify_by_metadata(self, path: Path) -> ModelCategory | None:
        """尝试读取元数据文件识别类型。"""
        # 读取 config.json
        config_path = path / "config.json" if path.is_dir() else None
        if config_path and config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    config = json.load(f)
                model_type = config.get("model_type", "").lower()

                type_map = {
                    # ── 文本 / 代码 LLM → DIALOG ──
                    "qwen2_vl": ModelCategory.DIALOG,
                    "qwen2": ModelCategory.DIALOG,
                    "qwen3": ModelCategory.DIALOG,
                    "qwen3_vl": ModelCategory.DIALOG,
                    "qwen3_moe": ModelCategory.DIALOG,
                    # ── 视觉语音全模态 → OMNI（Thinker/Talker 端到端）──
                    # Qwen2.5-Omni Thinker 的 model_type；Talker/Tokenize
                    # 组件目录含 speaker 配置时同样归 OMNI
                    "qwen2_5_omni": ModelCategory.OMNI,
                    "qwen2_5_omni_thinker": ModelCategory.OMNI,
                    "qwen2_5_omni_talker": ModelCategory.OMNI,
                    "llama": ModelCategory.DIALOG,
                    "mistral": ModelCategory.DIALOG,
                    "mixtral": ModelCategory.DIALOG,
                    "chatglm": ModelCategory.DIALOG,
                    "glm": ModelCategory.DIALOG,
                    "glm4": ModelCategory.DIALOG,
                    "deepseek_v2": ModelCategory.DIALOG,
                    "deepseek_v3": ModelCategory.DIALOG,
                    "yi": ModelCategory.DIALOG,
                    "phi3": ModelCategory.DIALOG,
                    "phi": ModelCategory.DIALOG,
                    "phimoe": ModelCategory.DIALOG,
                    "gemma": ModelCategory.DIALOG,
                    "gemma2": ModelCategory.DIALOG,
                    "gemma3": ModelCategory.DIALOG,
                    "baichuan": ModelCategory.DIALOG,
                    "mpt": ModelCategory.DIALOG,
                    "falcon": ModelCategory.DIALOG,
                    "gpt_bigcode": ModelCategory.DIALOG,   # StarCoder2
                    "stablelm": ModelCategory.DIALOG,
                    "internlm2": ModelCategory.DIALOG,
                    # ── 多模态 VLM → DIALOG（对话模块可用）──
                    "llava": ModelCategory.DIALOG,
                    "llava_next": ModelCategory.DIALOG,
                    "internvl_chat": ModelCategory.DIALOG,
                    "minicpmv": ModelCategory.DIALOG,
                    "cogvlm": ModelCategory.DIALOG,
                    "phi4_multimodal": ModelCategory.DIALOG,
                    "qwen2_audio": ModelCategory.DIALOG,
                    # ── 语音 → VOICE ──
                    "whisper": ModelCategory.VOICE,
                    "bark": ModelCategory.VOICE,
                    "seamless_m4t": ModelCategory.VOICE,
                    "seamless_m4t_v2": ModelCategory.VOICE,
                    "speech_to_text": ModelCategory.VOICE,
                    "sense_voice": ModelCategory.VOICE,
                    # ── 语音子组件（声码器/音频编码器）──
                    "hubert": ModelCategory.VOICE,
                    "wav2vec2": ModelCategory.VOICE,
                    "bigvgan": ModelCategory.VOICE,
                    "encodec": ModelCategory.VOICE,
                    "hifigan": ModelCategory.VOICE,
                    # ── 文本编码器/嵌入 → LANGUAGE（非对话生成）──
                    "bert": ModelCategory.LANGUAGE,
                    "roberta": ModelCategory.LANGUAGE,
                    "xlm-roberta": ModelCategory.LANGUAGE,
                    "sentence-transformers": ModelCategory.LANGUAGE,
                    # ── 绘画 ──
                    "stable_diffusion": ModelCategory.VISION,
                    "flux": ModelCategory.VISION,
                    "kolors": ModelCategory.VISION,
                    "sd3": ModelCategory.VISION,
                    # ── 视频 ──
                    "wan": ModelCategory.VIDEO,
                    "cogvideox": ModelCategory.VIDEO,
                    "hunyuan_video": ModelCategory.VIDEO,
                    "ltx_video": ModelCategory.VIDEO,
                }
                if model_type in type_map:
                    return type_map[model_type]

                # 检查 architectures 字段
                architectures = config.get("architectures", [])
                if isinstance(architectures, list):
                    for arch in architectures:
                        arch_lower = arch.lower()
                        if "whisper" in arch_lower or "bark" in arch_lower:
                            return ModelCategory.VOICE
                        if "forcausallm" in arch_lower:
                            return ModelCategory.DIALOG
                        if "clip" in arch_lower:
                            return ModelCategory.VISION
                        if "t5" in arch_lower or "llama" in arch_lower:
                            return ModelCategory.DIALOG
                        if "flux" in arch_lower or "unet" in arch_lower:
                            return ModelCategory.VISION
                        if "forconditionalgeneration" in arch_lower:
                            # seq2seq 架构（Whisper 除外，上面已拦截）按对话可用
                            return ModelCategory.DIALOG
            except Exception:
                log.debug("_classify_by_metadata: 降级忽略", exc_info=True)

        # 读取 safetensors header（单个文件）
        if path.is_file() and path.suffix == ".safetensors":
            try:
                import struct
                with open(path, "rb") as f:
                    header_size = struct.unpack("<Q", f.read(8))[0]
                    header = json.loads(f.read(header_size))
                # 通过键名推断类型
                keys = list(header.keys()) if isinstance(header, dict) else []
                for key in keys[:20]:
                    key_lower = key.lower()
                    if "transformer" in key_lower or "language" in key_lower:
                        return ModelCategory.DIALOG
                    if "unet" in key_lower or "vae" in key_lower:
                        return ModelCategory.VISION
            except Exception:
                log.debug("_classify_by_metadata: 降级忽略", exc_info=True)

        return None
