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

    from backend.services.inference.dialog_engine import get_dialog_engine
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

from ...config import DIALOG_MAX_PREFILL_TOKENS, MODELS_DIR
from .backends import DialogBackend, TransformersBackend, create_backend

logger = logging.getLogger("omnispace.inference.dialog")


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
DIALOG_MODEL_CANDIDATES: list[tuple[str, str, float]] = [
    ("qwen3-vl-4b", "qwen3-vl-4b", 9.0),
    ("qwen3-vl-8b-awq", "qwen3-vl-8b-awq", 7.5),
    ("qwen2-vl-2b", "qwen2-vl-2b", 5.0),
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
        pass
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
    """
    model_type, archs = _read_config_model_type(model_dir)
    if model_type in _NON_DIALOG_MODEL_TYPES:
        return ""
    if any("whisper" in a or "bark" in a for a in archs):
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
            pass
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
    if backend == "gguf":
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
        pass
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
                    pass
            elif child.is_file() and child.suffix.lower() == ".gguf" \
                    and child.stat().st_size >= _MIN_WEIGHT_BYTES:
                found.setdefault(child.stem, {
                    "path": str(child), "backend": "gguf",
                    "vram_gb": _estimate_vram_gb(child, "gguf"),
                })
    except OSError:
        pass
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

    def feed(self, text: str):
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

    def flush(self):
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
    """模型目录是否可用于加载（config.json + 权重文件齐全）。"""
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
            pass
    return None


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
                pass
    except Exception:
        pass


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
    if backend != "gguf" and _try_import("bitsandbytes") is not None:
        prec = _precision_pref()
        if prec == "int8":
            est *= 0.5
        elif prec == "int4":
            est *= 0.28
    return est


class DialogEngine:
    """对话推理引擎（编排层）——后端可插拔（transformers/gguf/vllm）。

    状态机: unavailable -> unloaded -> ready / error
      - unavailable: 依赖缺失或所有候选模型目录不完整
      - unloaded:    至少一个候选模型就绪但尚未加载
      - ready:       模型已加载可推理
      - error:       上次加载失败（可重试 load_model）

    推理串行化由各后端自治（transformers/gguf 持锁，vLLM 子进程天然
    并发）；引擎层只做生命周期串行（_lock）与统一指标计时。
    """

    def __init__(self) -> None:
        # 当前后端实例（None 表示未加载）
        self._backend: DialogBackend | None = None
        # 当前后端类型字符串（vl/text/gguf/vllm，get_status 兼容口径）
        self._backend_name: str = ""
        self._model_id: str = ""
        self._model_dir: Path | None = None
        self._state: str = "unavailable"
        self._last_error: str = ""
        self._lock = threading.Lock()

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

    def _pick_model(self, model_id: str | None) -> tuple[str, Path, float, str] | None:
        """选择要加载的模型：指定优先，否则按候选顺序取第一个就绪的。

        物理显存硬闸门（2026-08-20 修复）：无论显式指定还是自动选择，
        估算加载量超过物理总量 98% 一律拒绝——16.3GB 权重塞 16GB 卡
        会在推理时触发 CUDA 驱动级崩溃直接杀死后端进程。

        Returns:
            (model_id, 路径, 预估显存GB, 后端类型 vl|text|gguf)；无可用返回 None
        """
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
            pass
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
            _t0 = time.perf_counter()
            _prev_model = self._model_id
            if self._state == "ready":
                if not model_id or model_id in (self._model_id,
                                                Path(self._model_id).stem):
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
                    pass
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
                    pass

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
                        pass
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
                        pass
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
                    pass
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
                    pass
                return False

    def _switch_reset(self) -> None:
        """热切换前的就地重置（调用方须持 _lock）。"""
        backend, self._backend = self._backend, None
        self._backend_name = ""
        self._model_id = ""
        self._model_dir = None
        self._state = "unloaded"
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
            backend, self._backend = self._backend, None
            had = backend is not None
            _unloaded_name = self._model_id
            _unloaded_backend = self._backend_name
            self._backend_name = ""
            self._model_id = ""
            self._model_dir = None
            if had:
                self._state = "unloaded"
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
                    pass
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
        if self._state == "ready":
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
                pass

        # model_manager 协调契约（容错 import）
        try:
            from ..model_manager import get_model_manager  # type: ignore
            mgr = get_model_manager()
            ensure = getattr(mgr, "ensure_loaded", None)
            if callable(ensure):
                try:
                    ensure("dialog", model_id or self._default_model_id())
                except Exception as exc:
                    logger.debug("model_manager.ensure_loaded 调用失败: %s", exc)
        except Exception:
            pass

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
        backend = self._backend
        if self._state != "ready" or backend is None:
            raise RuntimeError(self._last_error or "对话模型未就绪")

        start = time.perf_counter()
        self.last_first_token_ms = 0.0
        self.last_output_tokens = 0
        produced = 0
        try:
            for text in backend.chat_stream(
                    messages, images=images,
                    temperature=temperature,
                    max_new_tokens=max_new_tokens):
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
        """
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
    ) -> str:
        """非流式推理：返回完整回复文本。"""
        return "".join(self.chat_stream(
            messages, images=images,
            temperature=temperature, max_new_tokens=max_new_tokens,
        ))

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
        """引擎状态快照。"""
        backend = self._backend
        return {
            "engine": "dialog",
            "state": self._state,               # unavailable/unloaded/ready/error
            "loaded": self._state == "ready",
            "model": self._model_id,
            "model_dir": str(self._model_dir) if self._model_dir else "",
            "backend": self._backend_name,      # vl / text / gguf / vllm
            "available_models": self.available_models(),
            "discovered_models": sorted(discover_dialog_models().keys()),
            "knowledge_lora": backend.lora_version if backend else "",
            "last_error": self._last_error,
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
