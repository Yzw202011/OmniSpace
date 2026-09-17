"""OmniSpace AI v2.3 对话推理引擎（TASK-004 真实推理实现）。

专属推理框架的**编排层**（2026-08-21 统一后端协议裁定）：本模块只负责
选型、物理显存闸门、显存腾挪协调、上下文组装与统一指标计时；推理介质
细节收敛在可插拔后端（backends/ 包，DialogBackend 协议）：

- vl/text → TransformersBackend（本进程直载，含知识 LoRA 挂载 R2-B04）
- gguf    → GGUFBackend（llama.cpp，未安装时诚实门控，绝不伪造推理）
- vllm    → VLLMBackend（独立子进程 py313，PagedAttention + AWQ int4）

首选硬编码候选 Qwen3-VL（qwen3-vl-4b/8b，回退 qwen2-vl-2b）；同时支持
**models/ 目录动态发现**（导入即用）。接入新推理引擎只须实现
backends.base.DialogBackend 并在 backends.create_backend 注册。

单例用法::

    from src.services.inference.dialog_engine import get_dialog_engine
    engine = get_dialog_engine()
"""

from __future__ import annotations

import gc
import importlib
import logging
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar

from ...config import DIALOG_IDLE_UNLOAD_SECONDS, DIALOG_MAX_PREFILL_TOKENS, MODELS_DIR
from .backends import DialogBackend, TransformersBackend, create_backend
from .base_engine import BaseEngine

logger = logging.getLogger("omnispace.inference.dialog")


def _remote_dialog_enabled() -> bool:
    """远程对话模式是否启用（懒探测；配置读取失败按未启用走本地）。"""
    try:
        from .backends.remote_backend import is_remote_dialog_enabled
        return bool(is_remote_dialog_enabled())
    except Exception:  # noqa: BLE001 - 探测失败按未启用
        return False


def _try_import(name: str) -> Any:
    """容错导入可选依赖。"""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


# ── 候选对话模型（按优先级排序）─────────────────────────────────────
# model_id -> (相对 models/ 的目录, 需求显存 GB)
# 2026-08-21 多模型热切换：qwen3-vl-8b-awq（AWQ int4 ~6GB 权重，vLLM
# 子进程后端）纳入候选。自动选择默认仍 4b（保留知识 LoRA 能力），
# 显式请求 8b-awq 时经 load_model 热切换（杀 vLLM 进程 → 换目录重启）。
# 口径标注（B6）：本表第三元=weights 权重体积口径（调度预算另见
# vram_policy.DIALOG_TIERS 的 dispatch 口径——qwen35-9b 11.0 vs 14.9
# 非矛盾，是两种坐标）。
DIALOG_MODEL_CANDIDATES: list[tuple[str, str, float]] = [
    # Qwen3.5-9B W4A16（2026-09-06 接入，架构升级计划 B-阶段一「换脑子」）：
    # RedHatAI compressed-tensors int4（10.95GB 权重 + 0.49GB MTP 投机
    # 解码模块）；config 声明 quant_method=compressed-tensors → 经
    # _detect_src/_is_awq_model 自动路由 vLLM 子进程（py310 inproc
    # transformers 4.57.6 不认 qwen3_5 架构，与 deepseek 同一条子进程
    # 路）。原生多模态（vision_config）+ 原生 <think> 思考模式（现有
    # _ThinkingStreamParser 标签链直接消费）；候选首位=自动选择默认，
    # 显存不足时 _pick_model 自动跳过降级到后位旧模型（旧项全保留=
    # 回退开关）。
    ("qwen35-9b-w4a16", "qwen35-9b-w4a16", 11.0),
    # Qwen3.5-9B GGUF Q4_K_M 共存档（显存调度机制批4 2026-09-10，
    # D2=A）：实测 5.7GB / 114 tok/s——**2026-09-10 用户令删除**：
    # Q4 量化思考退化（「你好」也英文自纠结 12 秒+，泄漏事故两轮），
    # 权重已清（5.5G）。如需恢复：ModelScope 重下 Q4_K_M gguf 至
    # models/qwen35-9b-gguf-q4km/ + 恢复本行 + vram_policy DIALOG_TIERS
    # 对应档。llama_server 后端（kind=llama）代码保留未删。
    ("qwen3-vl-4b", "qwen3-vl-4b", 9.0),
    ("qwen3-vl-8b-awq", "qwen3-vl-8b-awq", 7.5),
    # DeepSeek-R1-Distill-Qwen-14B W4A16（2026-08-25 接入）：
    # neuralmagic GPTQ int4 compressed-tensors（9.25GB 权重，
    # transformers 无 compressed_tensors 包加载不了 → 自动路由
    # vLLM 子进程推理）；R1 推理模型，<think> 段由 vllm_service
    # 的 --reasoning-parser deepseek_r1 剥离。纯文本（无视觉），
    # 仅显式点名加载，不作自动缺省。2026-08-29 模型裁剪后为
    # 漫剧·文字槽（manga-dialog）专用底座。
    ("deepseek-r1-14b-w4a16", "deepseek-r1-14b-w4a16", 11.5),
]

# R2-B10：高档位硬件（min_vram≥12GB 档，RTX 4090/5090）追加 8B 首选候选
_HIGH_TIER_DIALOG_CANDIDATE = ("qwen3-vl-8b", "qwen3-vl-8b", 12.0)

# ── 动态模型发现（导入 models/ 即可用）──────────────────────────────
# 后端类型: vl=多模态(AutoModelForImageTextToText) /
#           text=纯文本(AutoModelForCausalLM) / gguf=llama.cpp
# VL 架构 model_type 集合（走多模态加载路径）
_VL_MODEL_TYPES = {
    "qwen2_vl", "qwen3_vl", "qwen2_5_vl", "llava", "llava_next",
    "internvl_chat", "minicpmv", "cogvlm", "phi4_multimodal",
    "qwen2_audio", "mllama", "paligemma", "idefics2", "idefics3",
}
# 明确非对话架构（语音/嵌入/视觉塔等），发现时排除
_NON_DIALOG_MODEL_TYPES = {
    "whisper", "bark", "seamless_m4t", "seamless_m4t_v2", "speech_to_text",
    "sense_voice", "hubert", "wav2vec2", "clap", "encodec", "bert",
    "roberta", "xlm-roberta", "sentence-transformers", "clip", "siglip",
    "vit", "deit", "detr", "sam", "dpt", "depth_anything",
    "qwen3_tts", "qwen2_tts", "cosyvoice",
}
# 目录/文件最小权重体积（<100MB 视为非完整模型，如 adapter/配置残留）
_MIN_WEIGHT_BYTES = 100 * 1024 * 1024


def _dir_weight_bytes(model_dir: Path) -> int:
    """模型目录内权重文件总字节数（safetensors/bin/gguf/ckpt）。"""
    total = 0
    try:
        for f in model_dir.iterdir():
            if f.is_file() and f.suffix.lower() in (
                    ".safetensors", ".bin", ".gguf", ".ckpt", ".pt", ".pth"):
                total += f.stat().st_size
    except OSError:
        logger.debug("_dir_weight_bytes: 降级忽略", exc_info=True)
    return total


def _read_config_model_type(model_dir: Path) -> tuple[str, list[str]]:
    """读取 config.json 的 (model_type, architectures)；失败返回 ("", [])。"""
    cfg = model_dir / "config.json"
    if not cfg.is_file():
        return "", []
    try:
        import json as _json
        with open(cfg, encoding="utf-8") as f:
            data = _json.load(f)
        archs = data.get("architectures", [])
        if not isinstance(archs, list):
            archs = []
        return str(data.get("model_type") or "").lower(), [
            str(a).lower() for a in archs]
    except Exception:  # noqa: BLE001
        return "", []


def _detect_backend(model_dir: Path) -> str:
    """判定 transformers 目录的对话后端：vl / text / vllm；不适合对话返回 ""。

    vllm：仅 vLLM 可推理的量化目录（quant_method ∈ awq /
    compressed-tensors）——py310 主进程无 autoawq / compressed_tensors
    包，transformers 加载不了，必须走 vllm_service 独立子进程
    （runtime/py313）推理。
    llama（批4 2026-09-10）：GGUF 文件布局（无 config.json）——
    llama-server 官方子进程（llama-cpp-python 不认 qwen3.5）；候选
    链 GGUF 档统一走路，动态发现路径的 backend 判定不受影响。
    """
    # GGUF 布局优先判定（目录下有 .gguf 即子进程路，无需 config）
    if any(model_dir.glob("*.gguf")):
        return "llama"
    model_type, archs = _read_config_model_type(model_dir)
    if model_type in _NON_DIALOG_MODEL_TYPES:
        return ""
    # TTS 架构（Qwen3TTSForConditionalGeneration 等）虽以
    # ForConditionalGeneration 注册，但 transformers 对话链路不支持，
    # 必须在架构层一并排除（2026-08-25 实测：自动选择回退误载
    # qwen3-tts → KeyError: 'qwen3_tts'）
    if any("whisper" in a or "bark" in a or "tts" in a for a in archs):
        return ""
    if _is_awq_model(model_dir):
        return "vllm"
    if model_type in _VL_MODEL_TYPES:
        return "vl"
    if any("forcausallm" in a for a in archs):
        # Phi-4-multimodal 等以 CausalLM 注册但具备视觉能力的架构：
        # config 含 vision_config/vision_tower 时按 VL 路径加载
        try:
            import json as _json
            with open(model_dir / "config.json", encoding="utf-8") as f:
                raw = _json.load(f)
            if "vision_config" in raw or "vision_tower" in raw:
                return "vl"
        except Exception:  # noqa: BLE001
            logger.debug("_detect_backend: 降级忽略", exc_info=True)
        return "text"
    if any("forconditionalgeneration" in a or "imagetexttotext" in a
           for a in archs):
        return "vl"
    # model_type 未知但存在 tokenizer：按纯文本尝试（LLaMA 衍生架构）
    if model_type and (model_dir / "tokenizer_config.json").is_file():
        return "text"
    return ""


def _is_awq_model(model_dir: Path) -> bool:
    """config.json 是否声明仅 vLLM 可推理的量化格式。

    覆盖 quant_method ∈ {awq, compressed-tensors}：
    - awq: py310 无 autoawq，transformers 加载不了
    - compressed-tensors: int4 pack-quantized（2026-08-21 实测
      Qwen3-VL-8B AWQ 官方包实为该格式），py310 无 compressed_tensors
      包同样加载不了；两种格式 vLLM（py313）均原生支持
    """
    try:
        import json as _json
        with open(model_dir / "config.json", encoding="utf-8") as f:
            raw = _json.load(f)
        qcfg = raw.get("quantization_config")
        if isinstance(qcfg, dict):
            method = str(qcfg.get("quant_method") or "").lower()
        else:
            # 顶层 fmt（部分打包工具布局）
            method = str(raw.get("quant_method") or "").lower()
        return method in ("awq", "compressed-tensors")
    except Exception:  # noqa: BLE001
        return False


def _estimate_vram_gb(path: Path, backend: str) -> float:
    """估计加载所需显存（GB）。

    - GGUF：文件体积 × 1.10（llama.cpp 权重常驻 + KV/上下文余量）
    - transformers：权重字节 × dtype 系数（fp32 存盘加载 bf16 约 0.55；
      fp16/bf16/量化存盘约 1.15）× 1.10 KV 余量
    """
    if backend in ("gguf", "llama"):
        size = path.stat().st_size if path.is_file() else _dir_weight_bytes(path)
        return round(size / (1024 ** 3) * 1.10, 2)
    weight_bytes = _dir_weight_bytes(path)
    dtype_factor = 1.15
    try:
        import json as _json
        with open(path / "config.json", encoding="utf-8") as f:
            raw = _json.load(f)
        if str(raw.get("torch_dtype") or raw.get("dtype") or "").lower() \
                in ("float32", "fp32"):
            dtype_factor = 0.55
    except Exception:  # noqa: BLE001
        logger.debug("_estimate_vram_gb: 降级忽略", exc_info=True)
    return round(weight_bytes / (1024 ** 3) * dtype_factor * 1.10, 2)


def discover_dialog_models() -> dict[str, dict]:
    """扫描 MODELS_DIR（两层），发现可用于对话的模型。

    识别规则:
      - 目录含 config.json + ≥100MB 权重 → transformers 后端（vl/text 按架构）
      - 目录内含单个 ≥100MB .gguf，或 models/ 顶层 .gguf 文件 → gguf 后端

    Returns:
        {model_id: {"path": str, "backend": "vl"|"text"|"gguf",
                    "vram_gb": float}}
        model_id 与 model_manager 磁盘扫描口径一致（目录名 / gguf 文件 stem）。
    """
    found: dict[str, dict] = {}
    base = Path(MODELS_DIR)
    if not base.is_dir():
        return found

    def _probe_dir(d: Path) -> None:
        if not (d / "config.json").is_file():
            # GGUF 目录（如 models/Qwen3-7B-GGUF/model.gguf）
            try:
                ggufs = [f for f in d.iterdir()
                         if f.is_file() and f.suffix.lower() == ".gguf"
                         and f.stat().st_size >= _MIN_WEIGHT_BYTES]
            except OSError:
                ggufs = []
            if ggufs:
                g = max(ggufs, key=lambda f: f.stat().st_size)
                found.setdefault(d.name, {
                    "path": str(g), "backend": "gguf",
                    "vram_gb": _estimate_vram_gb(g, "gguf"),
                })
            return
        if _dir_weight_bytes(d) < _MIN_WEIGHT_BYTES:
            return
        backend = _detect_backend(d)
        if not backend:
            return
        found.setdefault(d.name, {
            "path": str(d), "backend": backend,
            "vram_gb": _estimate_vram_gb(d, backend),
        })

    try:
        for child in sorted(base.iterdir()):
            if child.is_dir() and not child.name.startswith(("_", ".")):
                _probe_dir(child)
                try:
                    for grand in sorted(child.iterdir()):
                        if grand.is_dir():
                            _probe_dir(grand)
                except OSError:
                    logger.debug("discover_dialog_models: 降级忽略", exc_info=True)
            elif child.is_file() and child.suffix.lower() == ".gguf" \
                    and child.stat().st_size >= _MIN_WEIGHT_BYTES:
                found.setdefault(child.stem, {
                    "path": str(child), "backend": "gguf",
                    "vram_gb": _estimate_vram_gb(child, "gguf"),
                })
    except OSError:
        logger.debug("discover_dialog_models: 降级忽略", exc_info=True)
    return found


def imported_dialog_models() -> dict[str, dict]:
    """登记表（用户导入的外部路径）中可作对话模型使用的条目。

    与 discover_dialog_models 同构（{id: {path, backend, vram_gb}}），
    供 _pick_model 与 /dialog/models 清单兜底：导入的 GGUF 单文件直走
    gguf 后端，目录经 _detect_backend 探测（awq→vllm、VL→vl、其余→text）；
    非对话类别（vision/video/voice/…）不进对话链。models/ 内的登记条目
    已由 discover 覆盖，此处跳过避免重复。
    """
    found: dict[str, dict] = {}
    try:
        from ...data.database import get_db_safe
        db = get_db_safe()
        if db is None:
            return found
        rows = db.query(
            "SELECT id, category, min_vram_gb, file_path FROM models"
            " WHERE category IN ('dialog', 'language', 'omni')")
    except Exception:  # noqa: BLE001 - 登记表不可用时静默降级
        return found
    base = Path(MODELS_DIR)
    for row in rows:
        path = Path(row.get("file_path") or "")
        if not path.exists():
            continue
        try:
            if path.is_relative_to(base):
                continue
        except (OSError, ValueError):
            logger.debug("imported_dialog_models: 降级忽略", exc_info=True)
        if path.is_file():
            if path.suffix.lower() != ".gguf":
                continue
            if path.stat().st_size < _MIN_WEIGHT_BYTES:
                continue
            backend = "gguf"
        else:
            if _dir_weight_bytes(path) < _MIN_WEIGHT_BYTES:
                continue
            backend = _detect_backend(path) or ""
            if not backend:
                continue
        vram = float(row.get("min_vram_gb") or 0)
        if vram <= 0:
            vram = _estimate_vram_gb(path, backend)
        found.setdefault(row["id"], {
            "path": str(path), "backend": backend, "vram_gb": vram,
        })
    return found


def _gpu_tier_min_vram_gb() -> float:
    """当前 GPU 命中的硬件档位 min_vram_gb（文档B §4.2 六档）；探测失败返回 0。"""
    try:
        torch = _try_import("torch")
        if torch is None or not torch.cuda.is_available():
            return 0.0
        props = torch.cuda.get_device_properties(0)
        from ...data.models import detect_hardware_tier
        tier = detect_hardware_tier(
            str(props.name), int(props.total_memory // (1024 ** 2)))
        return float(tier.get("min_vram_gb", 0) or 0)
    except Exception:  # noqa: BLE001 - 探测失败按低档路由（保守）
        return 0.0


def _effective_candidates() -> list[tuple[str, str, float]]:
    """按硬件 tier 生成对话候选（R2-B10 对话模型 tier 路由）。

    高档位（HARDWARE_TIER_TABLE min_vram_gb ≥ 12，即 RTX 4090/5090 档）
    将 qwen3-vl-8b 纳入首选候选；其余档位保持 4b/2b 保守路由不变。

    2026-08-20 修复：tier 表 min_vram_gb=12 严重低估 8b bf16 真实体积
    （磁盘实测 16.3GB）——16GB 卡（5070 Ti 等）纳入 8b 首选后必然
    OOM/驱动级崩溃。纳入前必须通过物理显存闸门校验。
    """
    if _gpu_tier_min_vram_gb() >= _HIGH_TIER_DIALOG_CANDIDATE[2]:
        path = _resolve_candidate_dir(_HIGH_TIER_DIALOG_CANDIDATE[1])
        over = _exceeds_physical_vram(path, "vl") if path else 0.0
        if over <= 0:
            return [_HIGH_TIER_DIALOG_CANDIDATE, *DIALOG_MODEL_CANDIDATES]
        logger.info(
            "高档位候选 %s 剔除：估算加载 %.1fGB 超过物理显存 %.1fGB（回退 4b/2b 路由）",
            _HIGH_TIER_DIALOG_CANDIDATE[0], over, _cuda_total_gb())
    return list(DIALOG_MODEL_CANDIDATES)

DEFAULT_SYSTEM_PROMPT = (
    "你是 OmniSpace AI 的内置创作助手，精通中文，擅长绘画提示词、剧本、"
    "分镜与创作相关问答。回答简洁准确，必要时使用 Markdown 格式。"
)

# 深度思考模式追加段（2026-08-22 思考过程展示）：Qwen3-VL Instruct
# 模板无 enable_thinking 变量（实测渲染 diff 为空）；实测模型对
# <think> 标签指令遵守度不足（0/1 输出标签），但对【步骤名】标记
# 协议 100% 遵守 → 思考/正文分界改用【最终回答】标记协议。
THINKING_SYSTEM_SUFFIX = (
    "\n\n【深度思考模式】收到问题后，请先展示你的思考过程，"
    "严格按以下四步框架组织，每步以【步骤名】开头：\n"
    "【问题分析】拆解问题的核心诉求与关键约束；\n"
    "【信息检索】列出回答所需的已知信息与缺失信息；\n"
    "【方案评估】对比候选方案的优劣；\n"
    "【决策依据】说明最终选择的理由。\n"
    "四步思考完成后，另起一行输出标记【最终回答】，然后在标记之后"
    "输出最终回答（简洁准确，不要重复思考内容）。"
)

# 思考/正文分界标记（与 THINKING_SYSTEM_SUFFIX 协议配对）
_ANSWER_MARKER = "【最终回答】"


class _ThinkingStreamParser:
    """流式思考分隔解析器（思考过程展示，2026-08-22）。

    协议（实测裁定）：把后端原始文本流切分为 reasoning / content 双通道——
      - thinking 模式下初始即 reasoning 态（思考先行）；
      - reasoning 态遇 ``【最终回答】`` 切换 content 态（标记本身吞掉
        不输出，正文气泡只见答案）；
      - holdback：产出尾部若是标记真前缀（``【`` ``【最`` ``【最终`` 等）
        扣留至可判定（标记跨 chunk 分割安全），其余直通零缓冲；
      - 退化：EOF 仍未见标记（模型未遵守协议）→ reasoning 全文复制为
        content 一条事件（保底气泡可见正文，宁重复不丢答案）。
    """

    _SEP = _ANSWER_MARKER
    # 标记的全部真前缀，用于尾部扣留判定
    _PREFIXES = tuple(sorted(
        {_ANSWER_MARKER[:i] for i in range(1, len(_ANSWER_MARKER))},
        key=len, reverse=True))

    def __init__(self) -> None:
        self._in_reasoning = True    # thinking 模式思考先行
        self._seen_sep = False
        self._reasoning_all = ""     # 退化复制用（全文累积）
        self._buf = ""

    def _tail_prefix_len(self, s: str) -> int:
        """s 尾部匹配标记前缀的最长长度（0 = 无扣留）。"""
        for p in self._PREFIXES:
            if s.endswith(p):
                return len(p)
        return 0

    def feed(self, text: str) -> list[tuple[str, str]]:
        """喂入一段增量，产出 [(kind, chunk), ...]（kind: reasoning/content）。"""
        self._buf += text
        out: list[tuple[str, str]] = []
        while True:
            if self._in_reasoning:
                idx = self._buf.find(self._SEP)
                if idx >= 0:
                    if idx:
                        chunk = self._buf[:idx]
                        out.append(("reasoning", chunk))
                        self._reasoning_all += chunk
                    self._buf = self._buf[idx + len(self._SEP):]
                    self._in_reasoning = False
                    self._seen_sep = True
                    continue
                keep = self._tail_prefix_len(self._buf)
                emit, self._buf = self._buf[:len(self._buf) - keep], \
                    self._buf[len(self._buf) - keep:]
                if emit:
                    out.append(("reasoning", emit))
                    self._reasoning_all += emit
            else:
                # 正文态：分隔只切一次，其后标记按字面量直通
                if self._buf:
                    out.append(("content", self._buf))
                    self._buf = ""
            return out

    def flush(self) -> list[tuple[str, str]]:
        """EOF 冲刷：残余按当前状态归属；未见标记时思考全文复制为正文。"""
        out: list[tuple[str, str]] = []
        if self._buf:
            if self._in_reasoning:
                out.append(("reasoning", self._buf))
                self._reasoning_all += self._buf
            else:
                out.append(("content", self._buf))
            self._buf = ""
        if not self._seen_sep and self._reasoning_all:
            out.append(("content", self._reasoning_all))
        return out


def strip_think_tags(text: str) -> str:
    """非流式兜底剥离（M-4）：解析器异常路径残留标签不再污染上下文。"""
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    if "<think>" in text:
        text = text.replace("<think>", "", 1)
    return text


def _find_weight_file(model_dir: Path) -> bool:
    """检查目录下是否存在完整的权重文件（忽略 .incomplete 下载残留）。"""
    if not model_dir.is_dir():
        return False
    for pattern in ("*.safetensors", "*.bin", "*.gguf"):
        for f in model_dir.glob(pattern):
            if f.is_file() and f.stat().st_size > 1024 * 1024:
                return True
    return False


def model_dir_ready(model_dir: Path) -> bool:
    """模型目录是否可用于加载。

    两种布局（批4 2026-09-10）：GGUF 文件布局（目录含 .gguf 即就绪，
    llama-server 子进程路）；transformers 布局（config.json + 权重齐全）。
    """
    if any(model_dir.glob("*.gguf")):
        return True
    return (model_dir / "config.json").is_file() and _find_weight_file(model_dir)


def _resolve_candidate_dir(rel: str) -> Path | None:
    """解析候选模型目录：支持扁平布局与 modelscope/HF 嵌套快照布局。

    嵌套布局: models/<id>/models/<Org--Name>/snapshots/<rev>/
    （modelscope 下载默认结构，如 qwen3-vl-8b，扁平探测无法命中）。
    找到第一个 model_dir_ready 的目录即返回；均未就绪返回 None。
    """
    direct = MODELS_DIR / rel
    if model_dir_ready(direct):
        return direct
    nested_root = direct / "models"
    if nested_root.is_dir():
        try:
            for org in sorted(nested_root.iterdir()):
                snap = org / "snapshots"
                if not snap.is_dir():
                    continue
                for rev in sorted(snap.iterdir()):
                    if model_dir_ready(rev):
                        return rev
        except OSError:
            logger.debug("_resolve_candidate_dir: 降级忽略", exc_info=True)
    return None


def _cuda_free_gb() -> float:
    """当前 GPU 空闲显存（GB）；无 CUDA 时返回 0。

    数据源优先 pynvml（驱动级计数器，与硬件遥测/nvidia-smi 同源）——
    torch.cuda.mem_get_info 在 Windows WDDM 下不含其他进程占用，
    2026-09-05 实测 vLLM 子进程吃满 15.6GB 时该口径仍报空闲 11.69GB，
    导致 /dialog/status 的 vram_free_gb 严重失真，故统一走 NVML。
    """
    try:
        import pynvml

        pynvml.nvmlInit()
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            return mem.free / (1024 ** 3)
        finally:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                logger.debug("_cuda_free_gb: 降级忽略", exc_info=True)
    except Exception:
        logger.debug("_cuda_free_gb: 降级忽略", exc_info=True)
    torch = _try_import("torch")
    if torch is None or not torch.cuda.is_available():
        return 0.0
    try:
        free, _total = torch.cuda.mem_get_info()
        return free / (1024 ** 3)
    except Exception:
        return 0.0


def _idle_unload_due(*, state: str, idle_seconds: float,
                     threshold_seconds: float,
                     active_feature: str | None) -> bool:
    """空闲卸载判定（纯函数，2026-09-05 方案A）。

    仅当：阈值>0 且引擎就绪 且 系统完全空闲（无任何重量级功能持锁——
    含进行中的对话请求）且 空闲时长达标。持锁期间不回收：别的任务在跑
    时不添乱，显存协调交给 release_for_module / offload 机制。
    """
    if threshold_seconds <= 0:
        return False
    if state != "ready":
        return False
    if active_feature is not None:
        return False
    return idle_seconds >= threshold_seconds


def _cuda_total_gb() -> float:
    """GPU 物理显存总量（GB）；无 CUDA 时返回 0。"""
    torch = _try_import("torch")
    if torch is None or not torch.cuda.is_available():
        return 0.0
    try:
        return torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
    except Exception:
        return 0.0


def _exceeds_physical_vram(path: Path, backend: str) -> float:
    """硬闸门：估算加载量超过物理显存总量 98% 时返回估算值（>0 = 拒绝）。

    2026-08-20 后端崩溃修复：qwen3-vl-8b bf16 权重 16.3GB 被显式请求加载进
    15.92GB 的 5070 Ti，device_map 塞下后推理时 CUDA 驱动级崩溃直接杀死
    进程（后端失联 → 前端代理 500）。任何模型（显式指定也不例外）超过
    物理总量都必须在选型阶段拒绝——这不是"空闲不足可腾挪"的问题，是
    物理装不下。返回 0 表示通过。
    """
    total = _cuda_total_gb()
    if total <= 0:
        return 0.0
    est = _estimated_load_gb(path, backend)
    if est > total * 0.98:
        return est
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
                logger.debug("_release_cuda_memory: 降级忽略", exc_info=True)
    except Exception:
        logger.debug("_release_cuda_memory: 降级忽略", exc_info=True)


def _precision_pref() -> str:
    """读取 models.config.precision 加载偏好（转发 backends.base，兼容旧引用）。"""
    from .backends.base import _precision_pref as _pref
    return _pref()


def _estimated_load_gb(path: Path, backend: str) -> float:
    """按当前精度偏好估算实际加载显存（GB）。

    与 preferred_load_dtype 口径一致：int8/int4 量化按权重量化比例折减；
    bitsandbytes 缺失时量化不可用，保持 bf16 估算（诚实不低估）。
    """
    est = _estimate_vram_gb(path, backend)
    if backend not in ("gguf", "llama") \
            and _try_import("bitsandbytes") is not None:
        prec = _precision_pref()
        if prec == "int8":
            est *= 0.5
        elif prec == "int4":
            est *= 0.28
    return est


class DialogEngine(BaseEngine):
    """对话推理引擎（编排层）——后端可插拔（transformers/gguf/vllm）。

    状态机: unavailable -> unloaded -> ready / error
      - unavailable: 依赖缺失或所有候选模型目录不完整
      - unloaded:    至少一个候选模型就绪但尚未加载
      - ready:       模型已加载可推理
      - error:       上次加载失败（可重试 load_model）

    推理串行化由各后端自治（transformers/gguf 持锁，vLLM 子进程天然
    并发）；引擎层只做生命周期串行（_lock）与统一指标计时。
    """

    name = "dialog"
    serves_categories = ("dialog", "language", "omni")

    def __init__(self) -> None:
        super().__init__()
        # 当前后端实例（None 表示未加载）
        self._backend: DialogBackend | None = None
        # 当前后端类型字符串（vl/text/gguf/vllm，get_status 兼容口径）
        self._backend_name: str = ""
        self._model_id: str = ""
        self._model_dir: Path | None = None
        self._state: str = "unavailable"
        self._last_error: str = ""
        # 降级来源（2026-09-02 用户拍板）：ensure_loaded 显存装不下自动
        # 降级小模型时记录原目标 id；用户重新加载原模型/卸载即清空
        self._degraded_from: str = ""
        self._lock = threading.Lock()

        # 空闲看门狗（2026-09-05 方案A）：最后活动时间戳（monotonic）+
        # 常驻巡检线程——就绪态引擎在阈值内无人使用且系统完全空闲时，
        # 走正规卸载链还显存（vLLM KV 预分配池不卸会一直占着）
        self._last_activity_ts: float = time.monotonic()
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = threading.Thread(
            target=self._idle_watchdog_loop, name="dialog-idle-watchdog",
            daemon=True)
        self._watchdog_thread.start()

        # 推理统计（编排层统一计时，后端无感知）
        self.last_first_token_ms: float = 0.0
        self.last_total_ms: float = 0.0
        self.last_output_tokens: int = 0
        # 深度思考模式：正文首字延迟（首个 content 片段时刻）
        self.last_first_content_ms: float = 0.0

        self._refresh_availability()

    # ── 可用性探测 ────────────────────────────────────────────────

    def _refresh_availability(self) -> None:
        """扫描候选模型目录，刷新 unloaded/unavailable 状态。"""
        if self._state == "ready":
            return
        for _mid, rel, _vram in _effective_candidates():
            if _resolve_candidate_dir(rel) is not None:
                self._state = "unloaded"
                return
        if discover_dialog_models():
            self._state = "unloaded"
            return
        self._state = "unavailable"

    def available_models(self) -> list[str]:
        """返回本地已就绪（可加载）的对话模型 id 列表（硬编码候选 ∪ 动态发现）。"""
        ids = [mid for mid, rel, _v in _effective_candidates()
               if _resolve_candidate_dir(rel) is not None]
        for mid in discover_dialog_models():
            if mid not in ids:
                ids.append(mid)
        return ids

    def _pick_model(self, model_id: str | None) -> tuple[str, Path | None, float, str] | None:
        """选择要加载的模型：指定优先，否则按候选顺序取第一个就绪的。

        物理显存硬闸门（2026-08-20 修复）：无论显式指定还是自动选择，
        估算加载量超过物理总量 98% 一律拒绝——16.3GB 权重塞 16GB 卡
        会在推理时触发 CUDA 驱动级崩溃直接杀死后端进程。

        Returns:
            (model_id, 路径, 预估显存GB, 后端类型 vl|text|gguf|remote)；无可用返回 None
        """
        # 云端API接入（批1 2026-09-06）：请求级云端虚拟模型
        # cloud::prov_xxx::model-name 优先于槽位默认绑定——对话页下拉
        # 直选某个云服务商的模型即走该连接；连接无效报带出路的错。
        try:
            from ..cloud_provider_service import parse_cloud_model_id, resolve_provider_endpoint
            parsed = parse_cloud_model_id(model_id or "")
        except Exception:  # noqa: BLE001 - 接线缺失按非云端请求
            parsed = None
        if parsed is not None:
            ep = resolve_provider_endpoint(parsed[0])
            if ep is None:
                self._last_error = (
                    f"云端服务商连接无效或已停用（{parsed[0]}）："
                    "请到「设置 → 云端 API 服务」检查连接状态")
                logger.warning("云端虚拟模型解析失败: %s", model_id)
                return None
            # 2026-09-06 实弹双连接错位修复：必须把完整 cloud:: 虚拟 id
            # 传给 backend.load——返回裸模型名会让 load() 回落 dialog.text
            # 槽位端点（对话=A 漫剧=B 时，切 B 实际连 A 的错位）
            return (str(model_id), None, 0.0, "remote")
        # 云端/远程模式（批3 D3 → 批1 泛化）：本地选型/显存闸门全部绕行，
        # 模型名取 dialog.text 槽位绑定（未绑模型时透传请求的 model_id）
        if _remote_dialog_enabled():
            try:
                from ..cloud_provider_service import get_dialog_text_endpoint
                ep = get_dialog_text_endpoint()
            except Exception:  # noqa: BLE001 - 配置读取失败按透传
                ep = None
            name = (ep.model if ep and ep.model else "") or model_id \
                or "remote-model"
            return (str(name), None, 0.0, "remote")
        total = _cuda_total_gb()
        for mid, rel, vram in _effective_candidates():
            if model_id and mid != model_id:
                continue
            path = _resolve_candidate_dir(rel)
            if path is None:
                continue
            # 后端按目录真实探测（2026-08-21 热切换修复：候选表纳入
            # qwen3-vl-8b-awq 后曾硬编码 "vl" 导致 AWQ 误走 transformers
            # 加载失败 compressed_tensors ImportError——必须经
            # _detect_backend 路由到 vllm 子进程后端）
            kind = _detect_backend(path) or "vl"
            # 物理闸门：超物理总量的模型显式指定也拒绝（防止进程级崩溃）
            over = _exceeds_physical_vram(path, kind)
            if over > 0:
                if model_id is None:
                    logger.info(
                        "自动选择跳过 %s：预估加载 %.1fGB 超过物理显存 %.1fGB",
                        mid, over, total)
                    continue
                self._last_error = (
                    f"模型 {mid} 预估加载 {over:.1f}GB 超过本机物理显存 "
                    f"{total:.1f}GB，无法加载（请选择更小的模型或量化版本）")
                logger.warning("物理闸门拒绝加载 %s: %s", mid, self._last_error)
                return None
            if model_id is None:
                # 自动选择：预估显存装不下当前空闲时跳过（候选常量仅为
                # 档位下限，bf16 实载可能远超；空闲不足可腾挪故仅自动跳过）
                est = _estimated_load_gb(path, kind)
                free = _cuda_free_gb()
                if est > free:
                    logger.info(
                        "自动选择跳过 %s：预估加载 %.1fGB > 空闲 %.1fGB",
                        mid, est, free)
                    continue
                # 自动选档可见性（批4 2026-09-10）：未点名请求而默认档
                # 装不下自动落后续档时记大白话事件（与 load_model 的
                # model_fallback 装载失败回退广播互补——这是选档阶段
                # 前移判定，气泡模型名可见之外的第二通道）
                try:
                    _first_id = _effective_candidates()[0][0]
                    if mid != _first_id:
                        from ..event_log import log_event
                        log_event(
                            "dialog", "model_tier_autoselect",
                            f"默认对话模型「{_first_id}」当前显存装不下，"
                            f"已自动选用「{mid}」档继续（关闭占显存的"
                            "应用后重新加载默认档即可换回）",
                            level="info",
                            detail=f"from={_first_id} to={mid} "
                                   f"free={free:.1f}GB est={est:.1f}GB")
                except Exception:  # noqa: BLE001 - 事件失败不影响选档
                    logger.debug("_pick_model: 降级忽略", exc_info=True)
            return mid, path, vram, kind
        # 动态发现（导入 models/ 即可用）
        discovered = discover_dialog_models()
        if model_id:
            hit = discovered.get(model_id)
            if hit is None:
                # 兜底：model_id 可能带 .gguf 后缀或是子目录别名
                for mid, info in discovered.items():
                    if mid == Path(model_id).stem:
                        hit = info
                        model_id = mid
                        break
            if hit is None:
                # 登记表兜底：用户导入的外部路径模型（2026-09-01 完整接入）
                hit = imported_dialog_models().get(model_id)
                if hit is None:
                    return None
            # 动态发现命中同样过物理闸门
            over = _exceeds_physical_vram(Path(hit["path"]), hit["backend"])
            if over > 0:
                self._last_error = (
                    f"模型 {model_id} 预估加载 {over:.1f}GB 超过本机物理显存 "
                    f"{total:.1f}GB，无法加载（请选择更小的模型或量化版本）")
                logger.warning("物理闸门拒绝加载 %s: %s", model_id, self._last_error)
                return None
            return model_id, Path(hit["path"]), float(hit["vram_gb"]), hit["backend"]
        # 自动选择：按显存需求升序取第一个能放下的（小模型优先，加载更快更稳）
        free = _cuda_free_gb()
        for mid, info in sorted(discovered.items(),
                                key=lambda kv: kv[1]["vram_gb"]):
            if info["vram_gb"] <= max(free, 0.1):
                # 自动选档可见性（测试发现 F-1 修复，2026-09-10）：动态
                # 发现兜底分支同样发大白话事件——候选链分支 03:39 预热
                # 实测走此路径却无通知（与候选链分支的事件对齐）
                try:
                    _first_id = _effective_candidates()[0][0]
                    if mid != _first_id:
                        from ..event_log import log_event
                        log_event(
                            "dialog", "model_tier_autoselect",
                            f"默认对话模型「{_first_id}」当前显存装不下，"
                            f"已自动选用「{mid}」档继续（关闭占显存的"
                            "应用后重新加载默认档即可换回）",
                            level="info",
                            detail=f"from={_first_id} to={mid} "
                                   f"free={free:.1f}GB "
                                   f"est={info['vram_gb']:.1f}GB（发现兜底）")
                except Exception:  # noqa: BLE001 - 事件失败不影响选档
                    logger.debug("_pick_model: 降级忽略", exc_info=True)
                return mid, Path(info["path"]), float(info["vram_gb"]), info["backend"]
        # 全部超过空闲显存时仍返回最小者（由 check_vram 腾挪/报错）；
        # 但最小者也超物理总量时拒绝（物理装不下腾挪无意义）
        if discovered:
            mid, info = min(discovered.items(), key=lambda kv: kv[1]["vram_gb"])
            over = _exceeds_physical_vram(Path(info["path"]), info["backend"])
            if over > 0:
                self._last_error = (
                    f"模型 {mid} 预估加载 {over:.1f}GB 超过本机物理显存 "
                    f"{total:.1f}GB，无法加载（请选择更小的模型或量化版本）")
                return None
            return mid, Path(info["path"]), float(info["vram_gb"]), info["backend"]
        return None

    # ── 显存协调 ──────────────────────────────────────────────────

    # 2026-08-22 VACE 误卸事故：video_gen 任务持锁生成期间，VL 腾挪把
    # 刚装载的 VACE 视为"冲突模型"卸载，任务回退到弱能力 LTX 重组管线
    # （画面质量投诉根因）。功能锁活动期间，其依赖类别的模型受保护。
    _LOCK_CATEGORY_SHIELD: ClassVar[dict[str, frozenset[str]]] = {
        "video_gen": frozenset({"video", "video_gen"}),
        "paint": frozenset({"paint", "image"}),
    }

    @classmethod
    def _locked_shielded_categories(cls) -> frozenset[str]:
        """当前功能锁持有者所保护、不可腾挪卸载的模型类别。"""
        try:
            from ...middleware.feature_lock import get_feature_lock
            holder = get_feature_lock().active_feature
            if holder:
                return cls._LOCK_CATEGORY_SHIELD.get(holder, frozenset())
        except Exception:  # noqa: BLE001 - 锁查询失败不阻断腾挪
            logger.debug("_locked_shielded_categories: 降级忽略", exc_info=True)
        return frozenset()

    def _try_free_vram(self, required_gb: float) -> float:
        """显存不足时尝试腾挪：先走 model_manager 契约，再直接卸载绘画引擎。

        Returns:
            腾挪后的空闲显存（GB）
        """
        free = _cuda_free_gb()
        if free >= required_gb:
            return free

        shielded = self._locked_shielded_categories()
        if shielded:
            logger.info("显存腾挪: 功能锁保护类别 %s（持锁任务模型不卸载）",
                        sorted(shielded))
        # 契约 1: model_manager（容错 import）——卸载记账中的冲突类别模型
        try:
            from ..model_manager import get_model_manager  # type: ignore
            mgr = get_model_manager()
            for entry in mgr.get_loaded_models():
                if entry.get("category") in shielded:
                    continue
                if entry.get("category") in ("vision", "paint", "image",
                                             "video", "video_gen"):
                    logger.info("经 model_manager 卸载冲突模型: %s",
                                entry.get("model_id"))
                    mgr.unload_model(entry["model_id"])
            # 审计 R2-C01：单模型驻留多类别（paint+embedding+3D 等）时
            # 驱逐一个可能仍不足，循环驱逐最低优先级直到满足或无可驱逐。
            # 驱逐了盾类别模型时立即停止（锁保护止损）。
            free = _cuda_free_gb()
            while free < required_gb and mgr.get_loaded_models():
                before = {e["model_id"]: e.get("category")
                          for e in mgr.get_loaded_models()}
                if not mgr.evict_lowest_priority():
                    break
                after_ids = {e["model_id"] for e in mgr.get_loaded_models()}
                evicted = [(mid, cat) for mid, cat in before.items()
                           if mid not in after_ids]
                if any(cat in shielded for _, cat in evicted):
                    logger.warning("腾挪驱逐了功能锁保护模型，停止后续腾挪")
                    break
                free = _cuda_free_gb()
        except Exception as exc:
            logger.debug("model_manager 腾显存不可用: %s", exc)

        # 契约 2: 直接调用绘画引擎单例卸载
        try:
            from .paint_engine import get_paint_engine
            paint = get_paint_engine()
            if getattr(paint, "is_loaded", False):
                logger.info("显存不足（%.1fGB < %.1fGB），卸载绘画引擎腾挪", free, required_gb)
                paint.unload_model()
        except Exception as exc:
            logger.debug("绘画引擎卸载不可用: %s", exc)

        _release_cuda_memory()
        return _cuda_free_gb()

    def check_vram(self, required_gb: float) -> tuple[bool, float]:
        """检查并（必要时）腾挪显存。

        Returns:
            (是否满足, 当前空闲 GB)
        """
        free = _cuda_free_gb()
        if free >= required_gb:
            return True, free
        free = self._try_free_vram(required_gb)
        return free >= required_gb, free

    # ── 加载 / 卸载 ───────────────────────────────────────────────

    def load_model(self, model_id: str | None = None) -> bool:
        """加载对话模型到 GPU（统一后端协议路由）。

        流程: 选模型（硬编码候选 ∪ models/ 动态发现，物理显存闸门）
        → transformers 系显存预检（不足尝试腾挪；gguf 可部分 offload、
        vllm 子进程预算制，二者无须硬闸）→ create_backend(kind).load()。
        任何失败都收敛为状态 error/unavailable，不抛异常。

        Args:
            model_id: 指定模型 id；None 时按优先级自动选择

        Returns:
            True 加载成功
        """
        with self._lock:
            self._last_error = ""
            self._degraded_from = ""  # 新加载意图=降级状态结束
            _t0 = time.perf_counter()
            _prev_model = self._model_id
            if self._state == "ready":
                # 远程模式：后端已是 remote → 就绪即够（远端模型名由
                # 设置页配置接管，本地下拉传来的 model_id 不触发热切换）。
                # 批1 多连接：matches_target 校验目标端点身份，绑定切换/
                # cloud:: 指定换目标时不短路，落到下方重载（仅健康探测）。
                if self._backend is not None \
                        and getattr(self._backend, "name", "") == "remote":
                    matches = getattr(self._backend, "matches_target", None)
                    if callable(matches) and matches(model_id):
                        return True
                    # 实弹 10:46 解绑事故修复：云端已解绑/换绑后旧 remote
                    # 后端目标不再匹配——必须卸载切换，绝不落回下方
                    # 「model_id 匹配即返回」（那会让已解绑的云端继续
                    # 服役，解绑形同虚设）
                    logger.info(
                        "云端目标不再匹配（解绑/换绑），卸载 remote 后端"
                        "回本地: 当前=%s", self._model_id)
                    _old_cloud = self._model_id
                    # 2026-09-06 实弹死锁修复：此处已持 self._lock，
                    # 必须用锁内版 _unload_locked——unload_model 会重抢
                    # 同一把非重入锁，云端 A→B 切换请求永挂
                    self._unload_locked()
                    try:
                        from ..model_manager import get_model_manager
                        get_model_manager().release_stale(_old_cloud)
                    except Exception:  # noqa: BLE001 - 台账同步失败不阻断
                        logger.debug("load_model: 降级忽略", exc_info=True)
                # 实弹 21:16 事故修复：远程刚启用而引擎还挂着本地模型时，
                # 旧的「model_id 匹配即返回」会让本地模型继续服役、远程
                # 配置形同虚设——远程启用期间必须强制走 _pick_model 的
                # remote 分支完成切换
                if not _remote_dialog_enabled() and (
                        not model_id or model_id in (self._model_id,
                                                     Path(self._model_id).stem)):
                    return True
                # 请求了不同模型：先卸载再切换（经 model_manager 显存记账
                # 的场景由调用方保证先 unload_model 解除预留，这里处理
                # 引擎直连路径）
                logger.info("load_model 请求模型 %s 与当前 %s 不同，先卸载切换",
                            model_id, self._model_id)
                try:  # 大白话事件：模型热切换开始
                    from ..event_log import log_event
                    log_event(
                        "dialog", "model_switching",
                        f"正在切换对话模型：从「{_prev_model or '未加载'}」"
                        f"换成「{model_id}」（需要先释放旧模型的显存，"
                        "大约需要 1 分钟）",
                        level="info",
                        detail=f"from={_prev_model}, to={model_id}")
                except Exception:  # noqa: BLE001
                    logger.debug("load_model: 降级忽略", exc_info=True)
                self._switch_reset()
                # 同步 model_manager 旧条目（2026-08-22 事故根修）：
                # 不清理会残留 stale loaded 条目，resource_guard 驱逐
                # stale 时经 dialog 关联回调 unload_model() 误杀引擎
                # 新持有模型（台账与实际错位）；release_stale 只清台账
                # 不回调引擎（引擎侧 _switch_reset 已释放完毕）
                try:
                    from ..model_manager import get_model_manager
                    get_model_manager().release_stale(_prev_model)
                except Exception:  # noqa: BLE001 - 台账同步失败不阻断加载
                    logger.debug("load_model: 降级忽略", exc_info=True)

            # 2026-09-08：装载全程如实报 loading。vl/text/gguf 后端此前
            # 装载期停在 unavailable/unloaded——右栏冷启动进度条
            # （DialogWarmupBar 认 loading/booting）在模型切换装载时
            # 从不出现，用户只能干等；先短路的 ready 路径不受影响
            self._state = "loading"
            pick = self._pick_model(model_id)
            if pick is None:
                # 物理闸门拒绝时 _last_error 已有详细原因，优先保留
                if not self._last_error:
                    self._last_error = (
                        f"对话模型未找到（尝试过: "
                        f"{[c[0] for c in _effective_candidates()]} + models/ 动态发现），"
                        "请先把模型目录或 GGUF 文件放入 models/"
                    )
                self._state = "unavailable"
                logger.warning(self._last_error)
                return False

            mid, path, required_gb, kind = pick

            # transformers 系（vl/text）：本进程直载，须显存预检
            if kind in ("vl", "text"):
                torch = _try_import("torch")
                transformers = _try_import("transformers")
                if torch is None or transformers is None:
                    self._last_error = "torch/transformers 依赖不可用"
                    self._state = "unavailable"
                    logger.warning("对话引擎不可用: %s", self._last_error)
                    return False

                if not torch.cuda.is_available():
                    self._last_error = "未检测到 CUDA GPU，无法加载对话模型"
                    self._state = "error"
                    logger.warning(self._last_error)
                    return False

                ok_vram, free_gb = self.check_vram(required_gb)
                if not ok_vram:
                    self._last_error = (
                        f"显存不足：空闲 {free_gb:.1f}GB，需求约 {required_gb:.0f}GB"
                    )
                    self._state = "error"
                    logger.warning(self._last_error)
                    try:  # 大白话事件：显存不足
                        from ..event_log import log_event
                        log_event(
                            "dialog", "model_load_failed",
                            f"对话模型「{mid}」没加载成功：显存不够了"
                            f"（空闲 {free_gb:.1f}GB，需要约 {required_gb:.0f}GB）。"
                            "建议先关掉其他占显存的功能，或换个小一点的模型",
                            level="error",
                            detail=f"free={free_gb:.1f}GB, "
                                   f"required={required_gb:.0f}GB")
                    except Exception:  # noqa: BLE001
                        logger.debug("load_model: 降级忽略", exc_info=True)
                    return False

            backend = create_backend(kind)
            try:
                if not backend.load(mid, path, required_gb):
                    self._last_error = backend.last_error()
                    self._state = "error"
                    self._backend = None
                    self._backend_name = ""
                    logger.warning("后端加载失败(%s): %s", kind,
                                   self._last_error)
                    try:  # 大白话事件：后端加载失败
                        from ..event_log import log_event
                        log_event(
                            "dialog", "model_load_failed",
                            f"对话模型「{mid}」加载失败："
                            f"{backend.last_error()[:120]}",
                            level="error",
                            detail=f"backend={kind}")
                    except Exception:  # noqa: BLE001
                        logger.debug("load_model: 降级忽略", exc_info=True)
                    gc.collect()
                    return False

                self._backend = backend
                self._backend_name = kind
                self._model_id = mid
                self._model_dir = path
                self._state = "ready"
                self._last_error = ""
                _load_ms = (time.perf_counter() - _t0) * 1000
                logger.info("对话模型加载成功: %s（后端: %s，知识 LoRA: %s）",
                            mid, kind, backend.lora_version or "无")
                try:  # 大白话事件：模型加载成功
                    from ..event_log import log_event
                    _friendly = (
                        f"对话模型「{mid}」切换完成，用了 {_load_ms/1000:.0f} 秒"
                        if _prev_model else
                        f"对话模型「{mid}」加载完成，用了 {_load_ms/1000:.0f} 秒")
                    log_event(
                        "dialog", "model_loaded", _friendly,
                        level="success", duration_ms=_load_ms,
                        detail=(f"backend={kind}, vram_free="
                                f"{_cuda_free_gb():.1f}GB, "
                                f"lora={backend.lora_version or 'none'}"))
                except Exception:  # noqa: BLE001
                    logger.debug("load_model: 降级忽略", exc_info=True)
                return True
            except Exception as exc:  # noqa: BLE001 - 加载失败收敛为状态
                self._last_error = f"对话模型加载失败: {exc}"
                self._state = "error"
                self._backend = None
                self._backend_name = ""
                logger.exception("对话模型加载失败")
                gc.collect()
                _release_cuda_memory()
                try:  # 大白话事件：加载异常
                    from ..event_log import log_event
                    log_event(
                        "dialog", "model_load_failed",
                        f"对话模型「{model_id or '自动选择'}」加载出错了："
                        f"{exc}。可以重试一次，或换个小一点的模型",
                        level="error", detail=f"exc={exc}")
                except Exception:  # noqa: BLE001
                    logger.debug("load_model: 降级忽略", exc_info=True)
                return False

    def _switch_reset(self) -> None:
        """热切换前的就地重置（调用方须持 _lock）。"""
        backend, self._backend = self._backend, None
        self._backend_name = ""
        self._model_id = ""
        self._model_dir = None
        self._state = "unloaded"
        self._degraded_from = ""  # 旧模型已走,降级状态结束
        if backend is not None:
            try:
                backend.unload()
            except Exception as exc:  # noqa: BLE001
                logger.warning("切换卸载旧后端异常: %s", exc)
        _release_cuda_memory()

    def unload_model(self) -> bool:
        """卸载模型并释放显存（统一走 backend.unload）。

        vLLM 后端卸载 = 杀整棵子进程树，显存随进程销毁瞬时回收
        （独立进程架构核心收益）；transformers/gguf 由后端置空引用 +
        引擎层 _release_cuda_memory 兜底。
        """
        with self._lock:
            return self._unload_locked()

    def _unload_locked(self) -> bool:
        """卸载实现（调用方必须已持有 self._lock；看门狗持锁二次确认用）。"""
        backend, self._backend = self._backend, None
        had = backend is not None
        _unloaded_name = self._model_id
        _unloaded_backend = self._backend_name
        self._backend_name = ""
        self._model_id = ""
        self._model_dir = None
        if had:
            self._state = "unloaded"
            self._degraded_from = ""  # 卸载即不再处于降级态
            try:
                backend.unload()
            except Exception as exc:  # noqa: BLE001
                logger.warning("后端卸载异常: %s", exc)
        _release_cuda_memory()
        if had:
            logger.info("对话模型已卸载，显存已释放（空闲 %.1fGB）",
                        _cuda_free_gb())
            try:  # 大白话事件：模型卸载
                from ..event_log import log_event
                log_event(
                    "dialog", "model_unloaded",
                    f"对话模型「{_unloaded_name}」已卸载，"
                    f"显存已释放（当前空闲 {_cuda_free_gb():.1f}GB）",
                    level="info",
                    detail=f"backend={_unloaded_backend or 'unknown'}")
            except Exception:  # noqa: BLE001
                logger.debug("_unload_locked: 降级忽略", exc_info=True)
        return had

    # ── 知识 LoRA 挂载（R2-B04 自主进化闭环）────────────────────
    # 实现收敛在 TransformersBackend（仅 transformers 路径支持 peft）

    def refresh_knowledge_lora(self) -> dict:
        """热更新知识 LoRA（R2-B04）：current 版本变化时卸载旧挂新版。

        低成本切换路径：引擎未 ready 时无需动作（下次 load_model 自动
        挂载最新版）；已 ready 时经 PeftModel.unload() 退回基座后重新挂载。
        训练完成（lora_training_service._run_task）会调用本方法。
        """
        with self._lock:
            backend = self._backend
            if self._state != "ready" or backend is None:
                return {"changed": False, "version": "",
                        "reason": "engine_not_ready"}
            if not isinstance(backend, TransformersBackend):
                return {"changed": False, "version": "",
                        "reason": "backend_no_lora"}
            return backend.refresh_lora()

    def ensure_loaded(self, model_id: str | None = None) -> bool:
        """确保模型已加载（供 API 调用前使用）。

        优先尝试 model_manager.ensure_loaded 契约协调（另一 agent 实现），
        不可用或返回 False 时走自身加载流程。
        """
        self._last_activity_ts = time.monotonic()  # 空闲看门狗活动戳
        # 云端/远程模式（批3 D3 2026-09-05）：对话由远端/云端端点承载，
        # 本地零显存——短路走 load_model 的 remote 分支，绝不进
        # model_manager 本地装载/显存闸门；就绪即直接返回（远端模型名
        # 与本地下拉选择无关，无需热切换）。
        # 批1 多连接扩展（2026-09-06）：就绪不等于目标没变——绑定切换
        # 或请求级 cloud:: 模型与当前端点身份不一致时轻量重载（remote
        # 重载仅健康探测，零显存成本）。
        if self._state == "ready" and self._backend is not None \
                and getattr(self._backend, "name", "") == "remote":
            matches = getattr(self._backend, "matches_target", None)
            _m = matches(model_id) if callable(matches) else False
            logger.info(
                "ensure_loaded[remote分支]: 请求=%s matches=%s", model_id, _m)
            if _m:
                return True
            return self.load_model(model_id)
        # 请求级云端虚拟模型直通（2026-09-06 写作台实弹修复）：对话本地
        # +漫剧文字/写作台云端组合下 _remote_dialog_enabled() 为 False，
        # cloud:: 请求若继续走下方 model_manager 协调会被「模型未下载」
        # 拒绝（云端模型无本地目录）——直通 load_model（其 _pick_model
        # 的 cloud 分支正确解析 provider）。
        try:
            from ..cloud_provider_service import parse_cloud_model_id
            _cloud_req = parse_cloud_model_id(model_id or "")
        except Exception:  # noqa: BLE001 - 接线缺失按非云端
            _cloud_req = None
        if _cloud_req is not None:
            return self.load_model(model_id)
        if _remote_dialog_enabled():
            return self.load_model(model_id)
        # 假 ready 防线（2026-09-05 实测）：引擎标志 ready 但 vLLM 子进程
        # 事实已消失（孤儿清扫/模块切换终止/异常退出）时，若直接信任标志
        # 会放行生成、撞上已死服务报「vLLM 服务未就绪」——先做子进程健康
        # 体检，失联即如实降级为 unloaded 走下面的重载流程。
        if self._state == "ready" and self._backend is not None \
                and getattr(self._backend, "name", "") == "vllm":
            try:
                from ...engines.vllm_service import get_vllm_service
                if not get_vllm_service().is_healthy():
                    logger.warning(
                        "ensure_loaded: 引擎态 ready 但 vLLM 子进程失联，"
                        "降级 unloaded 走重载")
                    self._state = "unloaded"
            except Exception:  # noqa: BLE001 - 服务不可用时按原状态走
                logger.debug("ensure_loaded: 降级忽略", exc_info=True)
        if self._state == "ready":
            # 云端解绑/换绑兜底（实弹 11:14 定位）：引擎挂着 remote 后端
            # 一直 ready，dialog.py 的发送路径见 ready 即跳过 ensure_loaded
            # ——解绑后旧云端继续服役（卸载守卫放 load_model 永远走不到）。
            # 此处对 remote 后端每次都复核目标身份，不匹配即卸载降级，
            # 落到下方本地装载。
            if self._backend is not None \
                    and getattr(self._backend, "name", "") == "remote":
                matches = getattr(self._backend, "matches_target", None)
                if not (callable(matches) and matches(model_id)):
                    logger.info(
                        "ensure_loaded: 云端已解绑/换绑（目标不匹配），"
                        "卸载 remote 后端回本地: 当前=%s", self._model_id)
                    _old_cloud = self._model_id
                    self.unload_model()
                    try:
                        from ..model_manager import get_model_manager
                        get_model_manager().release_stale(_old_cloud)
                    except Exception:  # noqa: BLE001
                        logger.debug("ensure_loaded: 降级忽略", exc_info=True)
                    return self.load_model(model_id)
            if not model_id or model_id in (self._model_id,
                                            Path(self._model_id).stem):
                return True
            # 请求了不同的已发现模型：先卸载当前模型再切换
            logger.info("请求模型 %s 与当前 %s 不同，执行热切换",
                        model_id, self._model_id)
            _old_model = self._model_id
            self.unload_model()
            # 同步 mgr 旧条目（stale 防误杀，同 load_model 热切换路径）
            try:
                from ..model_manager import get_model_manager
                get_model_manager().release_stale(_old_model)
            except Exception:  # noqa: BLE001
                logger.debug("ensure_loaded: 降级忽略", exc_info=True)

        # model_manager 协调契约（容错 import）
        # mgr_result 三态（2026-08-28 V77 事故根修）：True=mgr 已加载
        # 成功；False=mgr 明确拒绝/被取消（显存分配拒绝、模块切换释放
        # 等）——此前返回值被丢弃，fallback 直载把「取消」当「失败」
        # 立刻重试，vLLM 与关键帧生成并发 240s 抢卡触发深度降步；
        # None=mgr 不可用（旧契约兜底）才走自身加载流程
        mgr_result: bool | None = None
        try:
            from ..model_manager import get_model_manager  # type: ignore
            mgr = get_model_manager()
            ensure = getattr(mgr, "ensure_loaded", None)
            if callable(ensure):
                try:
                    mgr_result = bool(
                        ensure("dialog", model_id or self._default_model_id()))
                except Exception as exc:
                    logger.debug("model_manager.ensure_loaded 调用失败: %s", exc)
        except Exception:
            logger.debug("ensure_loaded: 降级忽略", exc_info=True)

        if mgr_result is True:
            return True  # mgr 协调加载成功（台账已记账，无需补登记）
        if mgr_result is False:
            # 孤儿收养兜底（2026-09-02 P1 修复）：mgr 因「显存不足」拒绝
            # 常见于后端在 vLLM 启动中途重启——上个进程树的 vLLM 孤儿
            # 健康服务在 8101 且占着显存（记账恒不足）。先探测收养：
            # 端口健康且模型匹配 → 直接引擎加载（秒级、零显存新增），
            # 不再尊重 mgr 的拒绝。
            try:
                from ...config import MODELS_DIR
                _want = model_id or self._default_model_id()
                _dir = next(
                    (str(MODELS_DIR / rel) for mid, rel, _v
                     in DIALOG_MODEL_CANDIDATES if mid == _want), None)
                if _dir is not None:
                    from ...engines.vllm_service import get_vllm_service
                    _svc = get_vllm_service()
                    if _svc._adopt_if_healthy(_dir):
                        if self.load_model(_want):
                            logger.info(
                                "mgr 拒绝后经 vLLM 孤儿收养恢复加载: %s",
                                _want)
                            return True
                    # 收养不匹配（端口服务着别的模型）：孤儿仍占显存，
                    # mgr 恒拒绝——清理 py313 vLLM 孤儿后重试 mgr 一次
                    _svc._reap_orphans()
                    import time as _time
                    _time.sleep(3.0)  # 显存释放窗口
                    try:
                        if bool(ensure("dialog", _want)):
                            logger.info(
                                "清理不匹配 vLLM 孤儿后 mgr 加载成功: %s",
                                _want)
                            return True
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("孤儿清理后重试 mgr 失败: %s", exc)
            except Exception as exc:  # noqa: BLE001 - 收养失败回落原拒绝
                logger.debug("孤儿收养兜底失败: %s", exc)
            # 显存装不下 → 自动降级（2026-09-02 用户拍板）：冷引擎且
            # 大模型装不下时不再只报错——改用引擎默认小模型继续生成，
            # 并广播大白话事件告知（不偷偷降质）。仅当失败原因属显存
            # 不足类、且目标 ≠ 默认小模型时触发；降级模型也装不下则
            # 走原诚实报错路径。换回：显存恢复后重新加载原模型即可
            # （load_model 起手自动清降级旗标）。
            _reason = " ".join(filter(None, (
                str(getattr(mgr, "last_error", "") or ""),
                self._last_error or "")))
            _want_id = (model_id or self._default_model_id()).strip()
            # 2026-09-08：回退目标=装得下的候选（默认==请求时旧逻辑
            # 无回退目标直接拒绝——9B 默认机常态碰壁），见方法注释
            _fallback_id = self._vram_fallback_model(_want_id)
            # 云端复核（2026-09-10 竞态根修）：本地装载线程可能跨越大显存
            # 等待窗口——期间用户绑定 dialog.text 切云端，此后的降级广播
            # 与本地 vLLM 启动均属误动作（误导横幅挂上云端答复气泡 +
            # 空拉本地模型）。降级决策前按当前状态复核，云端已承载即
            # 放弃本地降级（返回 False=未就绪；发送路径按云端路由自愈）。
            if _fallback_id and _remote_dialog_enabled():
                logger.info(
                    "对话已切云端承载，放弃本地降级（原目标 %s，"
                    "本地等待期间绑定发生变化）", _want_id)
                return False
            # 竞态守卫（UAT 2026-09-10 F3）：决策/等待期间，vLLM 自动
            # 重试或 V9-γ 收养可能已把引擎弄 ready——ready 即可用，
            # 严禁为降级把刚就绪的引擎再卸载（实测自伤：8B ready 1 秒
            # 后被降级链杀掉，用户收到张冠李戴的 9B 错误）。
            if self._state == "ready" and self._backend is not None:
                logger.info(
                    "引擎已被自动重试/收养弄就绪（%s），跳过降级卸载",
                    self._model_id)
                return True
            if "显存" in _reason and _fallback_id:
                logger.warning(
                    "对话模型 %s 显存装不下，自动降级 %s 继续并通知用户"
                    "（原因: %s）", _want_id, _fallback_id,
                    _reason.strip()[:80])
                try:  # 大白话事件：降级通知（事件日志；用户当场可见
                      # 的告知走 dialog.py 流末 meta 帧的 degraded_from）
                    from ..event_log import log_event
                    log_event(
                        "dialog", "model_fallback",
                        f"「{_want_id}」在当前显存余量下装不下，已自动改"
                        f"用「{_fallback_id}」继续生成（效果可能略降）。"
                        f"关闭占用显存的应用后，重新加载「{_want_id}」"
                        "即可换回。",
                        level="warning",
                        detail={"from": _want_id, "to": _fallback_id,
                                "reason": _reason.strip()[:200]})
                except Exception:  # noqa: BLE001
                    logger.debug("ensure_loaded: 降级忽略", exc_info=True)
                if self.load_model(_fallback_id):
                    self._degraded_from = _want_id
                    # 台账补登记（同下方 ok 路径，防 08-23 显存锚定）
                    try:
                        _mid = self._model_id
                        if _mid:
                            _mgr2 = get_model_manager()
                            if not any(e.get("model_id") == _mid
                                       for e in _mgr2.get_loaded_models()):
                                _req = _mgr2.estimate_vram_gb(
                                    _mid, "dialog")
                                _pth = _mgr2.resolve_model_path(_mid) or _mid
                                _mgr2.register_external_load(
                                    "dialog", _mid, str(_pth),
                                    max(0.5, _req))
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("降级加载台账补登记跳过: %s", exc)
                    return True
            logger.info(
                "model_manager 未加载 dialog 模型（%s），尊重裁决不直载",
                getattr(mgr, "last_error", "") or "已取消/拒绝")
            return False

        ok = self.load_model(model_id)
        if ok:
            # 台账补登记（2026-08-23 显存锚定事故根修）：显存紧张时
            # mgr.allocate_memory 拒绝（mgr.ensure_loaded 失败未入账），
            # 但引擎直连 load_model 经自身腾挪仍可加载成功——此后
            # /models/unload 报 MODEL_NOT_LOADED、resource_guard 看不到
            # 可卸模型（"越线但无空闲模型可卸"）、深回收也不触达，
            # 显存被不可治地锚定。成功后核查台账缺失即补登记
            # （register_external_load 与正常 ensure_loaded 记账同构）。
            mid = self._model_id
            if mid:
                try:
                    from ..model_manager import get_model_manager
                    mgr = get_model_manager()
                    if not any(e.get("model_id") == mid
                               for e in mgr.get_loaded_models()):
                        required = mgr.estimate_vram_gb(mid, "dialog")
                        path = mgr.resolve_model_path(mid) or mid
                        mgr.register_external_load(
                            "dialog", mid, str(path), max(0.5, required))
                        logger.info("补登记引擎直连加载模型: %s "
                                    "(%.1fGB)", mid, max(0.5, required))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("台账补登记跳过: %s", exc)
        return ok

    def _default_model_id(self) -> str:
        pick = self._pick_model(None)
        return pick[0] if pick else _effective_candidates()[0][0]

    def _vram_fallback_model(self, want_id: str) -> str:
        """显存不足时的回退模型（2026-09-08 排队超时事故收口）。

        旧逻辑回退目标恒=默认候选——9B 为默认首位的机器上「默认=
        请求」时无回退目标，直接诚实拒绝（本机 13.3GB 空闲装不下
        14.9GB 的 9B 是常态，页面记住 9B 的用户每条消息都碰壁、
        排队 300s 后仍超时）。改为：默认装得下且≠请求 → 回默认；
        否则按候选优先级回第一个显存装得下的其他候选（质量优先，
        9B → 8B-awq → 4B 逐级让档）。找不到装得下的返回空（走原
        诚实报错）。

        UAT 2026-09-10 F2 根修：拟合判据从候选表静态 vram 改为
        mgr.estimate_vram_gb 权威口径（ADR-003 注记的运行时显存权威），
        并做下限复核——此前 9B 候选表值偏低导致 8B 失败后反向回落
        更大的 9B（又立即被 14.9GB 下限拒绝，白跑一趟）。
        """
        try:
            free = _cuda_free_gb()
        except Exception:  # noqa: BLE001 - 探测失败按无回退
            return ""
        candidates = _effective_candidates()
        default = self._default_model_id()
        mgr = None
        try:
            from ..model_manager import get_model_manager as _gmm
            mgr = _gmm()
        except Exception:  # noqa: BLE001 - mgr 不可用退回候选表口径
            mgr = None

        def _fits(mid: str, vram: float) -> bool:
            """拟合判据：优先 mgr 权威估值（含可行下限），失败退表值。"""
            if mgr is not None:
                try:
                    return float(mgr.estimate_vram_gb(mid, "dialog")) \
                        <= free + 0.5
                except Exception:  # noqa: BLE001 - 估值失败退表值
                    logger.debug("_fits: 降级忽略", exc_info=True)
            return vram <= free + 0.5

        if default and default != want_id:
            for mid, rel, vram in candidates:
                if mid == default and _fits(mid, vram) \
                        and _resolve_candidate_dir(rel) is not None:
                    return default
                    # 默认装不下/不在盘：落下去找其他装得下的
        for mid, rel, vram in candidates:
            # 磁盘就绪校验（2026-09-08 实测教训：高档位候选
            # qwen3-vl-8b 物理闸门可过但本机磁盘没有该目录——
            # 选它做回退只会再失败一次）
            if mid != want_id and _fits(mid, vram) \
                    and _resolve_candidate_dir(rel) is not None:
                return mid
        return ""

    # ── 上下文组装 ────────────────────────────────────────────────

    def _count_tokens(self, text: str) -> int:
        """token 计数：转发当前后端（真实 tokenizer 或粗估）；
        未加载时按字符粗估（build_context 可在加载前调用）。"""
        if self._backend is not None:
            return self._backend.count_tokens(text)
        return max(1, int(len(text) / 1.5))

    # 输入截断标记（头 1/4 + 尾 3/4 之间插入）
    _TRUNC_MARK = "\n...[中间内容过长已截断]...\n"

    def _truncate_to_budget(self, text: str, budget: int) -> str:
        """把超长文本截断到 token 预算内（规格 L14：上下文截断策略）。

        头 1/4 + 尾 3/4 保留（指令/问题通常在尾部），中间插入截断标记。
        二分字符长度，用真实 tokenizer 校准，保证结果 ≤ budget。
        """
        if budget < 64:
            budget = 64
        if self._count_tokens(text) <= budget:
            return text
        lo, hi = 0, len(text)
        best = ""
        while lo <= hi:
            mid = (lo + hi) // 2
            head = max(1, mid // 4)
            cand = text[:head] + self._TRUNC_MARK + text[len(text) - (mid - head):]
            if self._count_tokens(cand) <= budget:
                best = cand
                lo = mid + 1
            else:
                hi = mid - 1
        if not best:  # 极小预算兜底：纯尾部截断
            best = text[-max(16, budget * 2):]
        logger.info("用户输入超长截断: %d 字 -> %d 字（预算 %d token）",
                    len(text), len(best), budget)
        return best

    def build_context(
        self,
        user_input: str,
        history: list[dict] | None = None,
        knowledge_text: str = "",
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        images: list | None = None,
        max_tokens: int = 8192,
    ) -> list[dict]:
        """组装对话上下文（系统 Prompt + RAG 注入 + 最近 N 轮历史 + 当前输入）。

        在 max_tokens 预算内从最早的历史开始截断，保证系统 Prompt 与当前
        输入始终保留。

        Args:
            user_input: 当前用户输入文本
            history: 历史消息 [{"role": "user"|"assistant", "content": str}]
            knowledge_text: RAG 注入文本（拼到系统 Prompt 之后）
            system_prompt: 系统提示词
            images: 当前输入附带的 PIL 图片列表（多模态）
            max_tokens: token 预算（默认 8192）

        Returns:
            chat template 格式的 messages 列表
        """
        history = list(history or [])

        # 历史净化（2026-09-10 用户二次报「你好→大段思考」）：会话历史里的
        # 旧答案若带 <think> 块/四步思考框架/【最终回答】标记，会成少样本
        # 样板——模型照历史模仿，新提示词也挡不住（in-context 模仿优先）。
        # 规则：assistant 历史剥 <think> 块与裸标签；含【最终回答】的旧协
        # 议答案只保留标记之后正文（真答案）。
        import re as _re
        sanitized: list[dict] = []
        for m in history:
            if (m.get("role") == "assistant"
                    and isinstance(m.get("content"), str)
                    and ("<think>" in m["content"] or "</think>" in m["content"]
                         or "【最终回答】" in m["content"]
                         or "【问题分析】" in m["content"])):
                c = m["content"]
                c = _re.sub(r"<think>.*?</think>", "", c, flags=_re.DOTALL)
                c = c.replace("<think>", "").replace("</think>", "")
                if "【最终回答】" in c:
                    c = c.rsplit("【最终回答】", 1)[1]
                c = c.strip()
                if not c:
                    continue  # 净化后为空（纯思考旧答案）→ 整条剔除
                m = {**m, "content": c}
            sanitized.append(m)
        history = sanitized

        sys_content = system_prompt.strip()
        if knowledge_text:
            sys_content = f"{sys_content}\n\n【参考资料】\n{knowledge_text.strip()}"

        system_msg = {"role": "system", "content": sys_content}

        # 用户输入本身超窗口时先截断（规格 L14：长文本上下文截断），
        # 否则 32K 字符（约 30K+ token）直接进入 prefill 会超时/OOM。
        # 预算再叠加硬件安全线 DIALOG_MAX_PREFILL_TOKENS：generate() 首步
        # 全位置 logits（151K 词表）在 16GB 显存下，>4K token 即触发颠簸。
        sys_tokens = self._count_tokens(sys_content)
        input_budget = max(256, max_tokens - sys_tokens - 32 - 1024)
        input_budget = min(input_budget, DIALOG_MAX_PREFILL_TOKENS)
        user_input = self._truncate_to_budget(user_input, input_budget)

        # 固定开销：系统消息 + 当前输入 + 生成预留
        reserved = sys_tokens + self._count_tokens(user_input) + 32 + 1024
        budget = max(256, max_tokens - reserved)

        # 从最新往回累加历史，超预算即截断最早的轮次
        kept: list[dict] = []
        used = 0
        for msg in reversed(history):
            content = msg.get("content", "") or ""
            cost = self._count_tokens(content) + 8
            if used + cost > budget:
                break
            kept.insert(0, {"role": msg.get("role", "user"), "content": content})
            used += cost

        if len(kept) < len(history):
            logger.info("上下文截断: 历史 %d 轮 -> %d 轮（预算 %d token）",
                        len(history) // 2, len(kept) // 2, max_tokens)

        user_content: Any
        backend = self._backend
        if images and (backend is None or not backend.supports_images):
            # 纯文本 / GGUF 后端不支持图片输入：诚实丢弃并记日志，
            # 不伪造多模态理解（vl/vllm 后端支持图片透传）
            logger.info("当前后端(%s)不支持图片输入，已忽略 %d 张图片",
                        self._backend_name or "未加载", len(images))
            images = None
        if images:
            # Qwen-VL 多模态 content 格式
            user_content = [{"type": "image"} for _ in images]
            user_content.append({"type": "text", "text": user_input})
        else:
            user_content = user_input

        return [system_msg, *kept, {"role": "user", "content": user_content}]

    # ── 推理 ──────────────────────────────────────────────────────
    # 介质细节（streamer / llama.cpp chunk / vLLM SSE）在各后端，
    # 引擎层统一做：就绪校验、中断检查、首 token / 总耗时指标。

    def chat_stream(
        self,
        messages: list[dict],
        images: list | None = None,
        temperature: float = 0.7,
        max_new_tokens: int = 1024,
        stop_check: Callable[[], bool] | None = None,
        extra_params: dict | None = None,
    ) -> Iterator[str]:
        """流式推理：路由到当前后端，统一计时与中断检查。

        Args:
            messages: build_context 组装的消息列表
            images: PIL 图片列表（多模态）
            temperature: 采样温度
            max_new_tokens: 最大生成 token 数
            stop_check: 可选的中断检查回调（返回 True 时提前结束产出）

        Yields:
            文本 token 片段

        Raises:
            RuntimeError: 引擎未就绪
        """
        self._last_activity_ts = time.monotonic()  # 空闲看门狗活动戳
        backend = self._backend
        if self._state != "ready" or backend is None:
            raise RuntimeError(self._last_error or "对话模型未就绪")

        # llama 后端失联自愈（测试发现 F-3 修复，2026-09-10）：子进程
        # 崩溃/被杀后引擎态仍 ready 直接报错——UX 铁律要求自动重载续跑
        # （vLLM 侧有 V9-γ 收养旁路，此处为 llama 对等能力）
        if getattr(backend, "name", "") == "llama":
            try:
                from ...engines.llama_service import get_llama_service

                if not get_llama_service().is_healthy():
                    logger.warning("llama-server 失联，按 UX 铁律自动重载"
                                   "（自愈，模型=%s）", self._model_id)
                    _mid = self._model_id
                    with self._lock:
                        self._unload_locked()
                    if _mid and self.load_model(_mid):
                        backend = self._backend
                    if backend is None:
                        raise RuntimeError(
                            "llama-server 自动重载失败: "
                            + (self._last_error or "未知原因"))
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001 - 自愈异常收敛为引擎语义
                raise RuntimeError(f"llama-server 自愈失败: {exc}") from exc

        start = time.perf_counter()
        self.last_first_token_ms = 0.0
        self.last_output_tokens = 0
        produced = 0
        try:
            for text in backend.chat_stream(
                    messages, images=images,
                    temperature=temperature,
                    max_new_tokens=max_new_tokens,
                    extra_params=extra_params):
                if stop_check is not None and stop_check():
                    break
                if text:
                    if produced == 0:
                        self.last_first_token_ms = \
                            (time.perf_counter() - start) * 1000
                    produced += 1
                    yield text
        finally:
            self.last_total_ms = (time.perf_counter() - start) * 1000
            self.last_output_tokens = produced
            logger.info("对话流式完成[%s]: %d 片段, 首token %.0fms, 总 %.0fms",
                        self._backend_name, produced,
                        self.last_first_token_ms, self.last_total_ms)

    def chat_stream_ex(
        self,
        messages: list[dict],
        images: list | None = None,
        temperature: float = 0.7,
        max_new_tokens: int = 1024,
        enable_thinking: bool = False,
        stop_check: Callable[[], bool] | None = None,
    ) -> Iterator[dict]:
        """结构化流式推理（思考过程展示，2026-08-22）。

        在 chat_stream 原始文本流之上叠加 _ThinkingStreamParser 切分
        reasoning / content 双通道——解析收敛在编排层一处实现，
        vllm / transformers / gguf 三后端统一免费支持。

        Args:
            enable_thinking: 思考模式（messages 的 system prompt 须已
                拼接 THINKING_SYSTEM_SUFFIX，本方法不重复注入）

        Yields:
            {"type": "reasoning" | "content", "text": str}；
            enable_thinking=False 时全部为 content（调用方无需分支）。

        2026-09-10 根修（用户报「你好→一大段无关」）：vLLM / GGUF 后端
        均为原生思考模型——<think> 块由引擎层负责剥离（vLLM 用
        reasoning parser；GGUF 流里带原生标签按下文切分），若再走
        【最终回答】标记解析器会双思考打架：模型会在思考里「引用标记
        本身」，解析器在首次出现处（思考中段）就切换 → 思考大段泄漏
        进正文 + 裸 </think> 标签。故原生思考后端旁路标记解析器。
        GGUF：按原生 </think> 切分 reasoning / content（<think> 开头
        剥除）；vLLM：引擎已剥离，全部按 content 直通。
        """
        _bn = self._backend_name or ""
        if enable_thinking and _bn == "vllm":
            enable_thinking = False
        if _bn in ("gguf", "llama"):
            # GGUF（qwen35-9b-gguf-q4km 实测）：模型流内反复开关
            # <think>…</think> 块（Q4 量化思考退化冗长）——in_think
            # 翻转态全程处理：块内=reasoning（思考开才展示，关=吞掉），
            # 块外=content。标签可能跨 chunk 分割 → 保留 7 字尾部缓冲
            # （最长标签 "</think>" 前缀）至下一 chunk 再判定。
            _OPEN, _CLOSE = "<think>", "</think>"
            _HOLDBACK = len(_CLOSE) - 1
            in_think = False
            buffer = ""
            for text in self.chat_stream(
                    messages, images=images, temperature=temperature,
                    max_new_tokens=max_new_tokens, stop_check=stop_check):
                buffer += text
                while True:
                    idx_open = buffer.find(_OPEN)
                    idx_close = buffer.find(_CLOSE)
                    # 取最早出现的标签
                    if idx_open == -1 and idx_close == -1:
                        break
                    if idx_close == -1 or (idx_open != -1
                                           and idx_open < idx_close):
                        idx, tag = idx_open, _OPEN
                    else:
                        idx, tag = idx_close, _CLOSE
                    before = buffer[:idx]
                    if before:
                        yield {"type": "reasoning" if in_think else "content",
                               "text": before}
                    buffer = buffer[idx + len(tag):]
                    in_think = tag == _OPEN
                # 尾部缓冲：防标签跨 chunk 被当正文漏出
                if len(buffer) > _HOLDBACK:
                    emit = buffer[:-_HOLDBACK]
                    buffer = buffer[-_HOLDBACK:]
                    if emit:
                        yield {"type": "reasoning" if in_think else "content",
                               "text": emit}
            if buffer:
                yield {"type": "reasoning" if in_think else "content",
                       "text": buffer}
            return
        if not enable_thinking:
            for text in self.chat_stream(
                    messages, images=images, temperature=temperature,
                    max_new_tokens=max_new_tokens, stop_check=stop_check):
                yield {"type": "content", "text": text}
            return

        parser = _ThinkingStreamParser()
        self.last_first_content_ms = 0.0
        start = time.perf_counter()
        got_content = False

        def _emit(kind: str, chunk: str) -> dict:
            nonlocal got_content
            if kind == "content" and not got_content:
                got_content = True
                # 首个 content 片段时刻（M-1 双口径：first_token_ms=
                # 首个任意产出=感知延迟；first_content_ms=正文首字）
                self.last_first_content_ms = \
                    (time.perf_counter() - start) * 1000
            return {"type": kind, "text": chunk}

        try:
            for text in self.chat_stream(
                    messages, images=images, temperature=temperature,
                    max_new_tokens=max_new_tokens, stop_check=stop_check):
                for kind, chunk in parser.feed(text):
                    yield _emit(kind, chunk)
            for kind, chunk in parser.flush():
                yield _emit(kind, chunk)
        finally:
            if got_content:
                logger.info(
                    "深度思考流式完成[%s]: 正文首字 %.0fms（思考通道已分离）",
                    self._backend_name, self.last_first_content_ms)

    def chat(
        self,
        messages: list[dict],
        images: list | None = None,
        temperature: float = 0.7,
        max_new_tokens: int = 1024,
        extra_params: dict | None = None,
    ) -> str:
        """非流式推理：返回完整回复文本。

        extra_params: 采样扩展参数透传（如 repetition_penalty，
        2026-09-05 小说长文防复读接入；对话页等既有调用方不传=行为不变）。
        """
        return "".join(self.chat_stream(
            messages, images=images,
            temperature=temperature, max_new_tokens=max_new_tokens,
            extra_params=extra_params,
        ))

    # ── 状态 ──────────────────────────────────────────────────────

    @property
    def is_loaded(self) -> bool:
        return self._state == "ready"

    @property
    def is_ready(self) -> bool:
        return self._state == "ready"

    # ── V9-γ（2026-09-09 卡 90% 事故根治）：唤醒产物收养重挂 ──────

    _last_reattach_ts: float = 0.0   # 限频（避免状态查询高频触发）

    def _maybe_reattach(self, min_interval_s: float = 5.0) -> bool:
        """限频包装的状态查询侧自愈入口（_unified_state 调用）。"""
        now = time.time()
        if now - self._last_reattach_ts < min_interval_s:
            return self._state == "ready" and self._backend is not None
        self._last_reattach_ts = now
        return self._reattach_vllm_backend()

    def _reattach_vllm_backend(self, target: str | None = None) -> bool:
        """服务健康在跑但引擎未持有时，重挂 vllm 客户端壳。

        场景：绘画让渡后 wake 后台线程重启了服务（vllm_service 内部），
        引擎的 backend 在驱逐时已清空——_unified_state 永远报 booting、
        ensure_loaded 又因台账盲区拒绝「装载已装好的模型」。重挂零成本
        客户端（不发装载请求）+ 台账补记，一条龙自愈。
        target 给定时要求服务所载模型与之相符（try_adopt 用）。
        """
        try:
            from ...engines.vllm_service import get_vllm_service
            svc = get_vllm_service()
        except Exception:  # noqa: BLE001 - 服务不可达即无从重挂
            return False
        if not (svc.is_running() and svc.is_healthy()):
            return False
        served = svc.served_name or ""
        if not served:
            return False
        if target and served != target:
            return False
        model_dir: Path | None = None
        try:
            from ..model_manager import get_model_manager
            _resolved = get_model_manager().resolve_model_path(served)
            model_dir = Path(_resolved) if _resolved else None
        except Exception:  # noqa: BLE001 - 路径解析失败不阻断重挂
            model_dir = None
        from .backends.vllm_backend import VLLMBackend
        self._backend = VLLMBackend.reattach(served, model_dir)
        self._backend_name = "vllm"
        self._model_id = served
        self._model_dir = model_dir
        self._state = "ready"
        self._last_error = ""
        logger.info("vLLM 唤醒产物收养重挂（V9-γ）: %s（服务已在跑，"
                    "零装载成本）", served)
        try:  # 台账补记（防分配器再以盲区拒绝）
            from ..model_manager import get_model_manager
            get_model_manager().note_external_load(served, "dialog")
        except Exception as exc:  # noqa: BLE001 - 补记失败只留痕
            logger.debug("收养台账补记失败（下次 ensure 会再补）: %s", exc)
        return True

    def try_adopt(self, model_id: str, wait_s: float = 150.0) -> bool:
        """mgr.ensure_loaded 收养旁路（V9-γ）+ 启动中等待（互踩收编）。

        ①目标已健康服务 → 重挂即真（原 V9-γ 语义）；
        ②外部通道（P3 常驻热备后台启动 / 唤醒线程）正启动**同目标** →
        等它就绪再收养——防「热备正在装 X、手动装 X 反被台账盲区以
        显存不足拒绝」的实测互踩（2026-09-09 07:46：热备装 8B 占显存
        → 手动 8B 任务 duration=0s 冤败）。等待期间健康→True；启动
        结束仍不健康 / 在装别的模型 / 超时 → False 走原装载路径。
        """
        target = (model_id or "").strip() or None
        if self._reattach_vllm_backend(target=target):
            return True
        deadline = time.time() + max(0.0, wait_s)
        while time.time() < deadline:
            try:
                from ...engines.vllm_service import get_vllm_service
                svc = get_vllm_service()
                if not svc.is_booting():
                    return False  # 无在飞启动且未健康 → 原路径
                served = svc.served_name or ""
                if target and served and served != target:
                    return False  # 外部通道在装别的模型 → 不等
            except Exception:  # noqa: BLE001 - 服务不可达走原路径
                return False
            time.sleep(2.0)
            if self._reattach_vllm_backend(target=target):
                return True
        return False

    @property
    def model_name(self) -> str:
        return self._model_id or "none"

    def _unified_state(self) -> str:
        """统一状态（ADR-003 P3）：vLLM 子进程态并入引擎状态机。

        进程内后端直接映射既有 _state 字符串（值域与 EngineState 对齐）；
        vLLM 后端以子进程事实为准——**无论引擎是否已持有 backend**都查
        询服务（启动预热先拉子进程、后建 backend，engine 态缺失不应
        遮蔽 booting 窗口）：booting（含健康未通窗口与预热期）/
        sleeping（Windows fallback 让渡）/ready（健康就绪且引擎已持有
        backend），并检测「引擎态 ready 但子进程已消失」的失联（如实
        报 unloaded）。
        """
        backend = self._backend
        if backend is not None and getattr(backend, "name", "") != "vllm":
            return self._state
        try:
            from ...engines.vllm_service import get_vllm_service
            svc = get_vllm_service()
        except Exception:  # noqa: BLE001 - 服务解析失败按引擎自身状态
            return self._state
        if svc.is_booting():
            return "booting"
        if svc.is_running():
            # 健康未通 = 权重装载窗口；健康已通但引擎未持有 backend =
            # load_model 收尾窗口——两者都如实报 booting
            if svc.is_healthy() and backend is not None:
                return "ready"
            if svc.is_healthy():
                # V9-γ（2026-09-09）：健康但未持有 backend = 唤醒后台
                # 重启产物——重挂自愈（限频）；挂上即 ready，不再永久
                # 卡 booting（04:00 卡 90% 事故根因③）
                if self._maybe_reattach():
                    return "ready"
            return "booting"
        if getattr(svc, "stopped_for_paint", False):
            return "sleeping"
        if backend is not None and self._state == "ready":
            # 引擎态 ready 但子进程已消失（被模块切换终止等）——如实降级
            logger.info("对话引擎态 ready 但 vLLM 子进程已消失，状态降级 unloaded")
            return "unloaded"
        return self._state

    # ── 空闲看门狗（方案A，阈值=config.scheduler.dialog_idle_unload_seconds）──

    def _idle_watchdog_loop(self) -> None:
        """常驻巡检线程（daemon）：每 60s 判定一次，异常不致死。"""
        while not self._watchdog_stop.wait(60.0):
            try:
                self._maybe_idle_unload()
            except Exception as exc:  # noqa: BLE001 - 看门狗绝不能带崩引擎
                logger.debug("空闲看门狗巡检异常: %s", exc)

    def _active_feature_snapshot(self) -> str | None:
        """读功能锁当前持有人（引擎层不长期依赖中间件，惰性导入）。"""
        try:
            from ...middleware.feature_lock import FeatureLockManager
            return FeatureLockManager.instance().active_feature
        except Exception:  # noqa: BLE001 - 锁不可用时按系统空闲处理
            return None

    def _maybe_idle_unload(self) -> None:
        """空闲达标且全系统空闲 → 持锁二次确认后走正规卸载链还显存。

        持引擎锁期间重读活动戳再判定：与 ensure_loaded/生成入口串行化，
        消灭「判定时空闲、卸载瞬间用户刚好发消息」的竞态——若用户已
        回来（活动戳刷新），本次放弃，下个巡检周期再看。
        """
        # 批5 登记簿联动（D4=A，方案 §3.6）：云任务在跑=系统非空闲——
        # 用户云绘画/云视频中可能马上需要对话（总结/解说），不卸对话
        # 模型；本地任务跑时功能锁先行拦截（下方 holder 判定）
        try:
            from ..inference.gpu_budget import get_busy_registry
            if get_busy_registry().is_busy(kind="cloud"):
                return
        except Exception:  # noqa: BLE001 - 登记簿不可用按原判定
            logger.debug("_maybe_idle_unload: 降级忽略", exc_info=True)
        holder = self._active_feature_snapshot()
        with self._lock:
            idle_seconds = time.monotonic() - self._last_activity_ts
            if not _idle_unload_due(
                    state=self._state, idle_seconds=idle_seconds,
                    threshold_seconds=DIALOG_IDLE_UNLOAD_SECONDS,
                    active_feature=holder):
                return
            name = self._model_id
            logger.info("对话引擎空闲看门狗: 空闲 %.0f 分钟达阈值，走正规链卸载「%s」",
                        idle_seconds / 60.0, name)
            had = self._unload_locked()
        if had:
            # 审计 P1-23 残留收口：台账同步与热切换路径同规——卸载后
            # release_stale 清 mgr 台账，防残留导致后续显存/就绪误判
            try:
                from ..model_manager import get_model_manager
                get_model_manager().release_stale(name)
            except Exception:  # noqa: BLE001 - 台账同步失败不影响卸载
                logger.debug("_maybe_idle_unload: 降级忽略", exc_info=True)
            try:  # 大白话事件：解释「模型怎么没了」，下次对话自动加载
                from ..event_log import log_event
                log_event(
                    "dialog", "idle_unload",
                    f"对话模型「{name}」空闲 "
                    f"{DIALOG_IDLE_UNLOAD_SECONDS / 60:.0f} 分钟无人使用，"
                    f"已自动卸载释放显存（当前空闲 {_cuda_free_gb():.1f}GB）；"
                    "下次对话将自动重新加载（约 1-2 分钟）")
            except Exception:  # noqa: BLE001
                logger.debug("_maybe_idle_unload: 降级忽略", exc_info=True)

    def get_status(self) -> dict:
        """引擎状态快照。"""
        backend = self._backend
        return {
            "engine": "dialog",
            "state": self._unified_state(),     # EngineState 值域（P3 含 booting/sleeping）
            "loaded": self._state == "ready",
            "model": self._model_id,
            "model_dir": str(self._model_dir) if self._model_dir else "",
            "backend": self._backend_name,      # vl / text / gguf / vllm
            "available_models": self.available_models(),
            "discovered_models": sorted(discover_dialog_models().keys()),
            "knowledge_lora": backend.lora_version if backend else "",
            "last_error": self._last_error,
            "degraded_from": self._degraded_from,
            "vram_free_gb": round(_cuda_free_gb(), 2),
            "last_first_token_ms": round(self.last_first_token_ms, 1),
        }


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

_engine_instance: DialogEngine | None = None
_engine_lock = threading.Lock()


def get_dialog_engine() -> DialogEngine:
    """获取对话引擎全局单例（线程安全双重检查）。"""
    global _engine_instance
    if _engine_instance is None:
        with _engine_lock:
            if _engine_instance is None:
                _engine_instance = DialogEngine()
    return _engine_instance
# 本项目仅供学习使用，商业授权请+Q 3559331368
