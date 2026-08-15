"""OmniSpace AI v2.3 对话推理引擎（TASK-004 真实推理实现）。

首选硬编码候选 Qwen3-VL（qwen3-vl-4b/8b，回退 qwen2-vl-2b）；同时支持
**models/ 目录动态发现**——导入即用的三种后端：

- vl   : 多模态模型（Qwen2/3-VL、LLaVA、InternVL2、MiniCPM-V、Phi-4-mm…）
         AutoModelForImageTextToText + AutoProcessor
- text : 纯文本/代码 LLM（Qwen3、Llama 3.x/4、GLM-4、DeepSeek 全系、Mistral、
         Yi-1.5、Phi-3、StarCoder2、Codestral、Qwen-Coder…）
         AutoModelForCausalLM + AutoTokenizer（trust_remote_code 覆盖自定义架构）
- gguf : llama.cpp 后端（《显存阶梯参考》Q4_K_M 等量化格式单文件；
         llama-cpp-python 未安装时诚实门控并给出安装指引，绝不伪造推理）

加载安全：bfloat16 + device_map=cuda + 显存预检（不足先腾挪绘画引擎）。
TextIteratorStreamer / llama.cpp stream 逐 token 产出，记录首 token 延迟。

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
from pathlib import Path
from typing import Any, Callable, Generator, Iterator, List, Optional

from ...config import DIALOG_MAX_PREFILL_TOKENS, MODELS_DIR

logger = logging.getLogger("omnispace.inference.dialog")


def _try_import(name: str) -> Any:
    """容错导入可选依赖。"""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


# ── 候选对话模型（按优先级排序）─────────────────────────────────────
# model_id -> (相对 models/ 的目录, 需求显存 GB)
DIALOG_MODEL_CANDIDATES: list[tuple[str, str, float]] = [
    ("qwen3-vl-4b", "qwen3-vl-4b", 9.0),
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
        with open(cfg, "r", encoding="utf-8") as f:
            data = _json.load(f)
        archs = data.get("architectures", [])
        if not isinstance(archs, list):
            archs = []
        return str(data.get("model_type") or "").lower(), [
            str(a).lower() for a in archs]
    except Exception:  # noqa: BLE001
        return "", []


def _detect_backend(model_dir: Path) -> str:
    """判定 transformers 目录的对话后端：vl / text；不适合对话返回 ""。"""
    model_type, archs = _read_config_model_type(model_dir)
    if model_type in _NON_DIALOG_MODEL_TYPES:
        return ""
    if any("whisper" in a or "bark" in a for a in archs):
        return ""
    if model_type in _VL_MODEL_TYPES:
        return "vl"
    if any("forcausallm" in a for a in archs):
        # Phi-4-multimodal 等以 CausalLM 注册但具备视觉能力的架构：
        # config 含 vision_config/vision_tower 时按 VL 路径加载
        try:
            import json as _json
            with open(model_dir / "config.json", "r", encoding="utf-8") as f:
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
        with open(path / "config.json", "r", encoding="utf-8") as f:
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
    """
    if _gpu_tier_min_vram_gb() >= _HIGH_TIER_DIALOG_CANDIDATE[2]:
        return [_HIGH_TIER_DIALOG_CANDIDATE, *DIALOG_MODEL_CANDIDATES]
    return list(DIALOG_MODEL_CANDIDATES)

DEFAULT_SYSTEM_PROMPT = (
    "你是 OmniSpace AI 的内置创作助手，精通中文，擅长绘画提示词、剧本、"
    "分镜与创作相关问答。回答简洁准确，必要时使用 Markdown 格式。"
)


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


def _resolve_candidate_dir(rel: str) -> Optional[Path]:
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
    """读取 models.config.precision 加载偏好（bf16/fp16/fp32/int8/int4）。

    读取失败一律回退 bf16（历史默认行为）。
    """
    try:
        import json as _json
        from ...data.database import get_db_safe
        db = get_db_safe()
        if db is not None:
            row = db.query_one(
                "SELECT value FROM system_settings WHERE key='models.config'")
            if row:
                return str((_json.loads(row["value"]) or {}).get(
                    "precision", "bf16")).lower()
    except Exception:  # noqa: BLE001
        pass
    return "bf16"


def _preferred_load_dtype(torch) -> tuple:
    """读取 models.config.precision 加载偏好（MODEL-034，PUT /models/config）。

    Returns:
        (dtype, extra_kwargs)：bf16/fp16/fp32 直接映射；int8/int4 需
        bitsandbytes（未安装回退 bf16 并记日志，绝不伪造量化）。
    """
    prec = _precision_pref()
    if prec == "fp16":
        return torch.float16, {}
    if prec == "fp32":
        return torch.float32, {}
    if prec in ("int8", "int4"):
        if _try_import("bitsandbytes") is not None:
            logger.info("按 models.config 偏好启用 %s 量化加载", prec)
            if prec == "int8":
                return torch.bfloat16, {"load_in_8bit": True}
            return torch.bfloat16, {"load_in_4bit": True}
        logger.warning("bitsandbytes 未安装，%s 量化不可用，回退 bf16", prec)
    return torch.bfloat16, {}


def _estimated_load_gb(path: Path, backend: str) -> float:
    """按当前精度偏好估算实际加载显存（GB）。

    与 _preferred_load_dtype 口径一致：int8/int4 量化按权重量化比例折减；
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
    """对话推理引擎——Qwen3-VL / Qwen2-VL（transformers 后端）。

    状态机: unavailable -> unloaded -> ready / error
      - unavailable: 依赖缺失或所有候选模型目录不完整
      - unloaded:    至少一个候选模型就绪但尚未加载
      - ready:       模型已加载可推理
      - error:       上次加载失败（可重试 load_model）
    """

    def __init__(self) -> None:
        self._model: Any = None
        self._processor: Any = None
        self._model_id: str = ""
        self._model_dir: Optional[Path] = None
        self._state: str = "unavailable"
        self._last_error: str = ""
        # 当前后端类型：vl / text / gguf（"" 表示未加载）
        self._backend: str = ""
        # 当前挂载的知识 LoRA 版本（R2-B04，"" 表示纯基座推理）
        self._lora_version: str = ""
        self._lock = threading.Lock()
        # 推理串行锁：单 GPU 单模型实例，并发 generate 会叠加 KV 缓存与
        # logits 显存导致分配器颠簸（实测 16GB 显存两路并发 prefill 卡死）。
        # 功能锁同功能可重入（规格 §6.1），故在引擎层串行化推理。
        self._infer_lock = threading.Lock()

        # 推理统计
        self.last_first_token_ms: float = 0.0
        self.last_total_ms: float = 0.0
        self.last_output_tokens: int = 0

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

    def _pick_model(self, model_id: Optional[str]) -> Optional[tuple[str, Path, float, str]]:
        """选择要加载的模型：指定优先，否则按候选顺序取第一个就绪的。

        Returns:
            (model_id, 路径, 预估显存GB, 后端类型 vl|text|gguf)；无可用返回 None
        """
        for mid, rel, vram in _effective_candidates():
            if model_id and mid != model_id:
                continue
            path = _resolve_candidate_dir(rel)
            if path is None:
                continue
            if model_id is None:
                # 自动选择：预估显存装不下时跳过（候选常量仅为档位下限，
                # bf16 实载可能远超，如 8B/16.3GB 权重在 16GB 卡上 OOM）
                est = _estimated_load_gb(path, "vl")
                free = _cuda_free_gb()
                if est > free:
                    logger.info(
                        "自动选择跳过 %s：预估加载 %.1fGB > 空闲 %.1fGB",
                        mid, est, free)
                    continue
            return mid, path, vram, "vl"
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
            return model_id, Path(hit["path"]), float(hit["vram_gb"]), hit["backend"]
        # 自动选择：按显存需求升序取第一个能放下的（小模型优先，加载更快更稳）
        free = _cuda_free_gb()
        for mid, info in sorted(discovered.items(),
                                key=lambda kv: kv[1]["vram_gb"]):
            if info["vram_gb"] <= max(free, 0.1):
                return mid, Path(info["path"]), float(info["vram_gb"]), info["backend"]
        # 全部超过空闲显存时仍返回最小者（由 check_vram 腾挪/报错）
        if discovered:
            mid, info = min(discovered.items(), key=lambda kv: kv[1]["vram_gb"])
            return mid, Path(info["path"]), float(info["vram_gb"]), info["backend"]
        return None

    # ── 显存协调 ──────────────────────────────────────────────────

    def _try_free_vram(self, required_gb: float) -> float:
        """显存不足时尝试腾挪：先走 model_manager 契约，再直接卸载绘画引擎。

        Returns:
            腾挪后的空闲显存（GB）
        """
        free = _cuda_free_gb()
        if free >= required_gb:
            return free

        # 契约 1: model_manager（容错 import）——卸载记账中的冲突类别模型
        try:
            from ..model_manager import get_model_manager  # type: ignore
            mgr = get_model_manager()
            for entry in mgr.get_loaded_models():
                if entry.get("category") in ("vision", "paint", "image",
                                             "video", "video_gen"):
                    logger.info("经 model_manager 卸载冲突模型: %s",
                                entry.get("model_id"))
                    mgr.unload_model(entry["model_id"])
            # 审计 R2-C01：单模型驻留多类别（paint+embedding+3D 等）时
            # 驱逐一个可能仍不足，循环驱逐最低优先级直到满足或无可驱逐。
            free = _cuda_free_gb()
            while free < required_gb and mgr.get_loaded_models():
                if not mgr.evict_lowest_priority():
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

    def load_model(self, model_id: Optional[str] = None) -> bool:
        """加载对话模型到 GPU。

        流程: 选模型（硬编码候选 ∪ models/ 动态发现）→ 显存预检（不足尝试
        腾挪）→ 按后端加载（vl/text 走 transformers，gguf 走 llama.cpp）。
        任何失败都收敛为状态 error/unavailable，不抛异常。

        Args:
            model_id: 指定模型 id；None 时按优先级自动选择

        Returns:
            True 加载成功
        """
        with self._lock:
            if self._state == "ready":
                if not model_id or model_id in (self._model_id,
                                                Path(self._model_id).stem):
                    return True
                # 请求了不同模型：先卸载再切换（经 model_manager 显存记账
                # 的场景由调用方保证先 unload_model 解除预留，这里处理
                # 引擎直连路径）
                logger.info("load_model 请求模型 %s 与当前 %s 不同，先卸载切换",
                            model_id, self._model_id)
                self._model = None
                self._processor = None
                self._model_id = ""
                self._model_dir = None
                self._backend = ""
                self._lora_version = ""
                self._state = "unloaded"
                _release_cuda_memory()

            pick = self._pick_model(model_id)
            if pick is None:
                self._last_error = (
                    f"对话模型未找到（尝试过: "
                    f"{[c[0] for c in _effective_candidates()]} + models/ 动态发现），"
                    "请先把模型目录或 GGUF 文件放入 models/"
                )
                self._state = "unavailable"
                logger.warning(self._last_error)
                return False

            mid, path, required_gb, backend = pick

            if backend == "gguf":
                return self._load_gguf_model(mid, path, required_gb)

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
                return False

            try:
                logger.info("开始加载对话模型 %s <- %s（后端: %s）", mid, path, backend)
                if backend == "text":
                    model, processor = self._load_text_transformers(
                        torch, transformers, path)
                else:
                    model, processor = self._load_vl_transformers(
                        torch, transformers, path)

                self._processor = processor
                # R2-B04 自主进化闭环：基座加载成功后挂载当前生效的知识
                # LoRA adapter（训练成果影响推理）；失败回退基座，不崩溃。
                model, lora_version = self._attach_knowledge_lora(model, path)
                self._model = model
                self._lora_version = lora_version
                self._model_id = mid
                self._model_dir = path
                self._backend = backend
                self._state = "ready"
                self._last_error = ""
                logger.info("对话模型加载成功: %s（后端: %s，知识 LoRA: %s）",
                            mid, backend, lora_version or "无")
                return True
            except Exception as exc:  # noqa: BLE001 - 加载失败收敛为状态
                self._last_error = f"对话模型加载失败: {exc}"
                self._state = "error"
                self._model = None
                self._processor = None
                self._backend = ""
                logger.exception("对话模型加载失败")
                gc.collect()
                try:
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass
                return False

    @staticmethod
    def _load_vl_transformers(torch, transformers, path: Path):
        """VL 多模态加载路径（Qwen-VL / LLaVA / InternVL 等）。

        Returns:
            (model, processor)
        """
        try:
            processor = transformers.AutoProcessor.from_pretrained(
                str(path), trust_remote_code=True)
        except TypeError:  # 旧版 transformers 无 trust_remote_code 参数
            processor = transformers.AutoProcessor.from_pretrained(str(path))

        model_cls = getattr(transformers, "AutoModelForImageTextToText", None)
        if model_cls is None:
            model_cls = getattr(transformers, "AutoModelForVision2Seq", None)
        if model_cls is None:
            raise RuntimeError("当前 transformers 版本不支持视觉对话模型")

        dtype, extra = _preferred_load_dtype(torch)
        try:
            model = model_cls.from_pretrained(
                str(path),
                torch_dtype=dtype,
                device_map="cuda",
                trust_remote_code=True,
                **extra,
            )
        except Exception as exc:
            # Qwen3-VL 类不可用时回退 Vision2Seq
            logger.warning("主加载路径失败(%s)，尝试回退加载", exc)
            fallback_cls = getattr(transformers, "AutoModelForVision2Seq")
            model = fallback_cls.from_pretrained(
                str(path),
                torch_dtype=dtype,
                device_map="cuda",
                trust_remote_code=True,
                **extra,
            )
        return model, processor

    @staticmethod
    def _load_text_transformers(torch, transformers, path: Path):
        """纯文本 LLM 加载路径（Qwen3 / Llama / GLM-4 / DeepSeek / Mistral /
        Yi / Phi / 代码模型等，trust_remote_code 覆盖 GLM/MiniCPM 自定义架构）。

        Returns:
            (model, tokenizer)
        """
        tokenizer = transformers.AutoTokenizer.from_pretrained(
            str(path), trust_remote_code=True)
        dtype, extra = _preferred_load_dtype(torch)
        model = transformers.AutoModelForCausalLM.from_pretrained(
            str(path),
            torch_dtype=dtype,
            device_map="cuda",
            trust_remote_code=True,
            **extra,
        )
        return model, tokenizer

    def _load_gguf_model(self, mid: str, path: Path, required_gb: float) -> bool:
        """GGUF 后端加载（llama.cpp，文档《显存阶梯参考》主力量化格式）。

        llama-cpp-python 未安装时诚实门控（状态 error + 明确指引），
        绝不伪造推理结果。
        """
        llama_cpp = _try_import("llama_cpp")
        if llama_cpp is None:
            self._last_error = (
                f"模型 {mid} 为 GGUF 格式，需要 llama.cpp 推理后端；"
                "请执行: pip install llama-cpp-python 后重启服务"
            )
            self._state = "error"
            logger.warning("GGUF 加载门控: llama-cpp-python 不可用")
            return False

        torch = _try_import("torch")
        cuda_ok = bool(torch is not None and torch.cuda.is_available())
        if cuda_ok:
            ok_vram, free_gb = self.check_vram(required_gb)
            if not ok_vram:
                # llama.cpp 支持部分 offload：显存不足时不直接失败，
                # 由 n_gpu_layers 自动截断到可放入显存的层数
                logger.info("显存 %.1fGB < 预估 %.1fGB，GGUF 将部分层 offload 到 CPU",
                            free_gb, required_gb)

        try:
            logger.info("开始加载 GGUF 对话模型 %s <- %s", mid, path)
            llm = llama_cpp.Llama(
                model_path=str(path),
                n_ctx=8192,
                n_gpu_layers=-1 if cuda_ok else 0,  # -1=尽可能全量 offload
                verbose=False,
            )
            self._model = llm
            self._processor = None
            self._model_id = mid
            self._model_dir = path
            self._backend = "gguf"
            self._lora_version = ""   # llama.cpp 路径不挂载 peft LoRA
            self._state = "ready"
            self._last_error = ""
            logger.info("GGUF 对话模型加载成功: %s（GPU offload: %s）",
                        mid, "全量尝试" if cuda_ok else "纯 CPU")
            return True
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"GGUF 模型加载失败: {exc}"
            self._state = "error"
            self._model = None
            self._backend = ""
            logger.exception("GGUF 模型加载失败")
            return False

    def unload_model(self) -> bool:
        """卸载模型并释放显存。返回是否有模型被卸载。"""
        with self._lock:
            had = self._model is not None
            self._model = None
            self._processor = None
            self._model_id = ""
            self._model_dir = None
            self._backend = ""
            self._lora_version = ""
            if had:
                self._state = "unloaded"
            _release_cuda_memory()
            if had:
                logger.info("对话模型已卸载，显存已释放（空闲 %.1fGB）",
                            _cuda_free_gb())
            return had

    # ── 知识 LoRA 挂载（R2-B04 自主进化闭环）────────────────────

    def _attach_knowledge_lora(self, model: Any,
                               model_dir: Optional[Path]) -> tuple[Any, str]:
        """基座加载后挂载当前生效的知识 LoRA adapter。

        查询 lora_training_service 的 current 版本（models/lora/vN，
        adapter_model.bin/safetensors + adapter_config.json），基座匹配时
        用 peft.PeftModel 包装（QLoRA adapter 仅数十 MB，不破坏现有
        generate 调用）。无版本/基座不匹配/peft 缺失/挂载异常 →
        log warning 并回退基座推理，绝不崩溃。

        Returns:
            (模型（可能为 PeftModel 包装）, 已挂载版本号（"" 表示基座）)
        """
        try:
            from ..lora_training_service import (
                LORA_DIR, get_lora_training_service)
            svc = get_lora_training_service()
            version = svc.get_current()
            if not version:
                return model, ""
            adapter_dir = LORA_DIR / version
            if not (adapter_dir / "adapter_config.json").is_file():
                logger.warning("知识 LoRA %s 缺 adapter_config.json，"
                               "回退基座推理", version)
                return model, ""
            # 基座匹配校验：adapter 训练基座须与当前加载模型目录一致
            meta = svc._read_meta(adapter_dir) or {}
            base_name = Path(str(meta.get("base_model") or "")).name
            if (base_name and model_dir is not None
                    and base_name != model_dir.name):
                logger.warning("知识 LoRA %s 训练基座(%s)与当前模型(%s)不匹配，"
                               "跳过挂载（回退基座推理）",
                               version, base_name, model_dir.name)
                return model, ""
            peft = _try_import("peft")
            if peft is None:
                logger.warning("peft 不可用，知识 LoRA %s 跳过挂载"
                               "（回退基座推理）", version)
                return model, ""
            wrapped = peft.PeftModel.from_pretrained(model, str(adapter_dir))
            wrapped.eval()
            logger.info("知识 LoRA 已挂载: %s → %s", version,
                        self._model_id or model_dir)
            return wrapped, version
        except Exception as exc:  # noqa: BLE001 - 挂载失败回退基座
            logger.warning("知识 LoRA 挂载失败，回退基座推理: %s", exc)
            return model, ""

    def refresh_knowledge_lora(self) -> dict:
        """热更新知识 LoRA（R2-B04）：current 版本变化时卸载旧 adapter 挂新版。

        低成本切换路径：引擎未 ready 时无需动作（下次 load_model 自动
        挂载最新版）；已 ready 时经 PeftModel.unload() 退回基座后重新挂载。
        训练完成（lora_training_service._run_task）会调用本方法。
        """
        with self._lock:
            if self._state != "ready" or self._model is None:
                return {"changed": False, "version": self._lora_version,
                        "reason": "engine_not_ready"}
            try:
                from ..lora_training_service import get_lora_training_service
                target = get_lora_training_service().get_current()
            except Exception:  # noqa: BLE001
                target = ""
            if target == self._lora_version:
                return {"changed": False, "version": self._lora_version}
            base = self._model
            if self._lora_version and hasattr(base, "unload"):
                try:
                    base = base.unload()      # 退回基座（卸载旧 adapter）
                except Exception as exc:  # noqa: BLE001
                    logger.warning("知识 LoRA 旧版本卸载失败，保持现状: %s", exc)
                    return {"changed": False, "version": self._lora_version}
            self._model, self._lora_version = self._attach_knowledge_lora(
                base, self._model_dir)
            logger.info("知识 LoRA 热更新完成: → %s",
                        self._lora_version or "基座")
            return {"changed": True, "version": self._lora_version}

    def ensure_loaded(self, model_id: Optional[str] = None) -> bool:
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
            self.unload_model()

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

        return self.load_model(model_id)

    def _default_model_id(self) -> str:
        pick = self._pick_model(None)
        return pick[0] if pick else _effective_candidates()[0][0]

    # ── 上下文组装 ────────────────────────────────────────────────

    def _count_tokens(self, text: str) -> int:
        """token 计数：有 processor/tokenizer 用真实计数，否则按字符估算。"""
        tokenizer = self._processor
        if tokenizer is not None:
            try:
                tok = getattr(tokenizer, "tokenizer", tokenizer)
                return len(tok(text, add_special_tokens=False).input_ids)
            except Exception:
                pass
        # 粗估：中英文混合约 1 token / 1.5 字符
        return max(1, int(len(text) / 1.5))

    def _history_token_count(self, messages: list[dict]) -> int:
        return sum(self._count_tokens(m.get("content", "")) + 8
                   for m in messages)

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
        history: Optional[List[dict]] = None,
        knowledge_text: str = "",
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        images: Optional[list] = None,
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
        if images and self._backend not in ("vl", ""):
            # 纯文本 / GGUF 后端不支持图片输入：诚实丢弃并记日志，
            # 不伪造多模态理解
            logger.info("当前后端(%s)不支持图片输入，已忽略 %d 张图片",
                        self._backend, len(images))
            images = None
        if images:
            # Qwen-VL 多模态 content 格式
            user_content = [{"type": "image"} for _ in images]
            user_content.append({"type": "text", "text": user_input})
        else:
            user_content = user_input

        return [system_msg, *kept, {"role": "user", "content": user_content}]

    # ── 推理 ──────────────────────────────────────────────────────

    def _prepare_inputs(self, messages: list[dict], images: Optional[list]):
        """应用 chat template 并编码输入（transformers 后端：vl / text）。"""
        processor = self._processor
        text = processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        if self._backend == "text":
            # 纯文本后端：processor 即 tokenizer
            inputs = processor(text, return_tensors="pt")
        else:
            kwargs: dict[str, Any] = {"text": [text], "return_tensors": "pt"}
            if images:
                kwargs["images"] = images
            inputs = processor(**kwargs)
        try:
            inputs = inputs.to("cuda")
        except AttributeError:
            inputs = {k: (v.cuda() if hasattr(v, "cuda") else v)
                      for k, v in inputs.items()}
        return inputs

    @staticmethod
    def _flatten_messages_for_gguf(messages: list[dict]) -> list[dict]:
        """把 chat messages 压平为 llama.cpp 兼容格式（content 必须为 str）。"""
        flat: list[dict] = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                content = "".join(
                    str(p.get("text", "")) for p in content
                    if isinstance(p, dict) and p.get("type") == "text")
            flat.append({"role": m.get("role", "user"),
                         "content": content or ""})
        return flat

    def _chat_stream_gguf(
        self,
        messages: list[dict],
        temperature: float,
        max_new_tokens: int,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> Iterator[str]:
        """GGUF（llama.cpp）流式推理：create_chat_completion 逐段产出。"""
        start = time.perf_counter()
        self.last_first_token_ms = 0.0
        self.last_output_tokens = 0
        produced = 0
        kwargs: dict[str, Any] = dict(
            messages=self._flatten_messages_for_gguf(messages),
            stream=True,
            max_tokens=max_new_tokens,
        )
        if temperature and temperature > 0:
            kwargs.update(temperature=temperature, top_p=0.9)
        else:
            kwargs.update(temperature=0.0)
        try:
            for chunk in self._model.create_chat_completion(**kwargs):
                if stop_check is not None and stop_check():
                    break
                text = ""
                try:
                    text = (chunk["choices"][0].get("delta") or {}).get("content") or ""
                except Exception:  # noqa: BLE001
                    continue
                if not text:
                    continue
                if produced == 0:
                    self.last_first_token_ms = (time.perf_counter() - start) * 1000
                produced += 1
                yield text
        finally:
            self.last_total_ms = (time.perf_counter() - start) * 1000
            self.last_output_tokens = produced
            logger.info("GGUF 流式完成: %d 片段, 首token %.0fms, 总 %.0fms",
                        produced, self.last_first_token_ms, self.last_total_ms)

    def chat_stream(
        self,
        messages: list[dict],
        images: Optional[list] = None,
        temperature: float = 0.7,
        max_new_tokens: int = 1024,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> Iterator[str]:
        """流式推理：后台线程 generate + TextIteratorStreamer 逐 token 产出。

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
        if self._state != "ready" or self._model is None:
            raise RuntimeError(self._last_error or "对话模型未就绪")

        # GGUF 后端走 llama.cpp 独立流式路径（不依赖 transformers streamer）
        if self._backend == "gguf":
            with self._infer_lock:
                yield from self._chat_stream_gguf(
                    messages, temperature, max_new_tokens, stop_check)
            return

        torch = _try_import("torch")
        transformers = _try_import("transformers")

        # 串行化推理：并发 generate 在同一模型实例上叠加显存并互相拖慢，
        # 这里排队执行（lock 在生成器生命周期内持有，close 时自动释放）。
        with self._infer_lock:
            inputs = self._prepare_inputs(messages, images)
            streamer = transformers.TextIteratorStreamer(
                getattr(self._processor, "tokenizer", self._processor),
                skip_prompt=True,
                skip_special_tokens=True,
            )

            gen_kwargs: dict[str, Any] = dict(
                **inputs,
                max_new_tokens=max_new_tokens,
                streamer=streamer,
            )
            if temperature and temperature > 0:
                gen_kwargs.update(do_sample=True, temperature=temperature, top_p=0.9)
            else:
                gen_kwargs.update(do_sample=False)

            start = time.perf_counter()
            self.last_first_token_ms = 0.0
            self.last_output_tokens = 0

            def _generate_worker() -> None:
                """后台生成线程：异常须显式记录并收尾 streamer，
                否则 daemon 线程静默死亡导致主流 0 产出且无法定位。"""
                try:
                    self._model.generate(**gen_kwargs)
                except Exception:  # noqa: BLE001
                    logger.exception("对话生成线程异常")
                    try:
                        streamer.end()
                    except Exception:  # noqa: BLE001
                        pass

            thread = threading.Thread(target=_generate_worker, daemon=True)
            thread.start()

            produced = 0
            try:
                for text in streamer:
                    if stop_check is not None and stop_check():
                        break
                    if produced == 0 and text:
                        self.last_first_token_ms = (time.perf_counter() - start) * 1000
                    if text:
                        produced += 1
                        yield text
            finally:
                # 等待生成线程结束，避免后台残留写 streamer
                thread.join(timeout=5.0)
                self.last_total_ms = (time.perf_counter() - start) * 1000
                self.last_output_tokens = produced
                logger.info("对话流式完成: %d 片段, 首token %.0fms, 总 %.0fms",
                            produced, self.last_first_token_ms, self.last_total_ms)

    def chat(
        self,
        messages: list[dict],
        images: Optional[list] = None,
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
        return {
            "engine": "dialog",
            "state": self._state,               # unavailable/unloaded/ready/error
            "loaded": self._state == "ready",
            "model": self._model_id,
            "model_dir": str(self._model_dir) if self._model_dir else "",
            "backend": self._backend,           # vl / text / gguf
            "available_models": self.available_models(),
            "discovered_models": sorted(discover_dialog_models().keys()),
            "knowledge_lora": self._lora_version,   # R2-B04 挂载的知识 LoRA 版本
            "last_error": self._last_error,
            "vram_free_gb": round(_cuda_free_gb(), 2),
            "last_first_token_ms": round(self.last_first_token_ms, 1),
        }


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

_engine_instance: Optional[DialogEngine] = None
_engine_lock = threading.Lock()


def get_dialog_engine() -> DialogEngine:
    """获取对话引擎全局单例（线程安全双重检查）。"""
    global _engine_instance
    if _engine_instance is None:
        with _engine_lock:
            if _engine_instance is None:
                _engine_instance = DialogEngine()
    return _engine_instance
