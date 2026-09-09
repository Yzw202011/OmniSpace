"""OmniSpace AI v2.3 模型管理包（TASK-011 / 规格 §2.1~2.4 + §5.3 + §5.4）。

提供 ModelManager 单例，整合模型导入、校验、分类、选择、缓存，并对外提供
稳定的运行时契约（其他服务按此调用）：

  - ensure_loaded(category, model_id)  显存检查 → 驱逐 → 引擎加载
  - get_gpu_status()                   pynvml 显存/利用率（无 GPU 降级）
  - get_loaded_models()                当前已加载模型列表
  - check_mutual_exclusion(target)     互斥矩阵（规格 §2.1）
  - allocate_memory(required_gb)       显存分配（含驱逐重试）
  - evict_lowest_priority()            驱逐最低优先级模型
  - record_feature_switch()/predict_next_feature()  ML 预测预加载

引擎单例（dialog_engine/paint_engine/video_engine）一律 try import 容错：
引擎不可用/模型未下载/显存不足时 ensure_loaded 返回 False 并写 last_error，
绝不抛出未捕获异常导致调用方崩溃。
"""
from __future__ import annotations

import gc
import importlib
import logging
import re as _re
import threading
import time
from pathlib import Path
from typing import Any

from ...config import MODELS_DIR
from ...data.models import (
    DIALOG_ROUTING_TABLE,
    PAINT_ROUTING_TABLE,
    VIDEO_ROUTING_TABLE,
    ModelCategory,
    ModelInfo,
)

log = logging.getLogger("omnispace.model_manager")


def _try_import(name: str) -> Any:
    """容错导入（SyntaxError/ImportError 等均返回 None）。"""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


_pynvml = _try_import("pynvml")
_torch = _try_import("torch")


# ═══════════════════════════════════════════════════════════════════
#  互斥矩阵（规格 §2.1 大模型功能并发管理）
# ═══════════════════════════════════════════════════════════════════
# key 活跃时，value 中的功能需要置灰。browser/behavior 学习为后台低优先级，
# 不影响任何功能。manga 与 video_gen 同义（漫剧视频生成），双向归一。
MUTUAL_EXCLUSION_MATRIX: dict[str, list[str]] = {
    "dialog":           ["paint", "video_gen", "training"],
    "paint":            ["dialog", "video_gen", "training"],
    "video_gen":        ["dialog", "paint", "training"],
    "training":         ["dialog", "paint", "video_gen"],
    "ltx2_training":    ["dialog", "paint", "video_gen"],
    "browser_learning": [],
    "behavior_learning": [],
}

_FEATURE_ALIASES = {"manga": "video_gen", "video": "video_gen", "draw": "paint"}

# 功能 -> 驱逐优先级（数值越小越先被驱逐；用户当前操作最高不列于此）
_EVICTION_PRIORITY = {
    "auxiliary": 0, "embedding": 0, "voice": 1,
    "training": 2, "video": 3, "vision": 4, "dialog": 5,
    # omni（视觉语音全模态）与 dialog 同服务对话功能，优先级对齐
    "omni": 5,
}

# 功能 -> 模块切换资源释放时保留的模型类别（用户裁定 2026-08-21：
# 切入某模块时其他模块 3 秒内释放显存/内存，优先供应目标模块）
_FEATURE_KEEP_CATEGORIES: dict[str, set[str]] = {
    "dialog":    {"dialog", "language", "omni"},
    "paint":     {"paint", "vision", "image"},
    "video_gen": {"video", "video_gen"},
    "training":  {"training", "ltx2_training"},
}

# 跨模块共享的小体量类别（embedding 检索 / 语音 / 辅助）始终保留，
# 不参与模块切换释放（显存占用小且各模块常驻复用）
_SHARED_KEEP_CATEGORIES: set[str] = {"embedding", "voice", "auxiliary"}

# P3 §3.2 常驻热备：重显存目标模块（需要抢占 vLLM 的 ~12GB 显存）集合。
# 切到这些模块时 vLLM 按需终止腾显存（过度占用必然 OOM）；其余轻量
# 切换（设置/日志/学习视图等不加载重模型）保留 vLLM 热备、复用已加载
# worker，切回对话免二次 ~157s 冷启动。
_VRAM_HEAVY_FEATURES: frozenset[str] = frozenset(
    {"paint", "video_gen", "training", "ltx2_training"})

# 已知模型的磁盘相对路径提示（MODELS_DIR 下），磁盘扫描的权威补充
_MODEL_PATH_HINTS: dict[str, str] = {
    "qwen3-vl-4b":   "qwen3-vl-4b",
    "qwen2-vl-2b":   "qwen2-vl-2b",
    # modelscope 嵌套快照布局（两层通用扫描无法命中）
    "qwen3-vl-8b":   "qwen3-vl-8b/models/Qwen--Qwen3-VL-8B-Instruct/snapshots/master",
    # AWQ int4 量化版（vLLM 子进程后端，2026-08-21 集成）
    "qwen3-vl-8b-awq": "qwen3-vl-8b-awq",
    # 视觉语音全模态（omni，2026-08-21 新增）
    "qwen2.5-omni-7b": "qwen2.5-omni-7b/models/Qwen--Qwen2.5-Omni-7B/snapshots/master",
    "sdxl-base-1.0": "paint/sdxl-base-1.0",
    # 四视图 one-pass 中文直入底座（paint/ 下 diffusers 布局）
    "flux2-klein-4b": "paint/flux2-klein-4b",
    # FLUX.2 Klein 9B（quanto float8 量化布局，2026-08-25 接入）
    "flux2-klein-9b": "paint/flux2-klein-9b",
    # DeepSeek-R1-Distill-14B W4A16（vLLM 子进程，2026-08-25 接入）
    "deepseek-r1-14b-w4a16": "deepseek-r1-14b-w4a16",
    # MiniMax H3 33B（ComfyUI 子进程管线，2026-08-25）：
    # video_gen/h3 为 ComfyUI 单文件权重布局（非 diffusers），
    # 扫描无法命中，显式登记供模型管理页发现
    "minimax-h3": "video_gen/h3",
    "bge-large-zh":  "embed/bge-large-zh",
}

# 孙层组件目录黑名单：父模型仓库的组成部分，不是独立模型
# （diffusers 布局组件 / GPT-SoVITS 说话人验证 / 通用资产目录名）
_COMPONENT_DIR_NAMES: set[str] = {
    "unet", "vae", "text_encoder", "text_encoder_2", "text_encoder_3",
    "safety_checker", "feature_extractor", "scheduler", "tokenizer",
    "tokenizer_2", "tokenizer_3", "speech_tokenizer", "sv",
    "checkpoints", "snapshots", "blobs", "hub", "models", "torch_hub_cache",
}

# LoRA/说话人版本目录（lora/v1、lora/v2、gpt-sovits/sv 之外的 vN 命名）
_LORA_VERSION_RE = _re.compile(r"^v\d{1,2}$")

# 显存估算人工覆盖（引擎候选表/路由表之后、磁盘大小回退之前）：
# 磁盘含 fp32 全量权重导致 size×1.2 严重高估的模型按真实加载档位修正
_VRAM_OVERRIDES: dict[str, float] = {
    "sd15": 4.0,             # SD1.5 fp16 实加载约 3.5~4GB（AnimateLCM 基座）
    "ltx-video-0.9.5": 12.0, # fp16 权重约 12GB（磁盘 23.6GB 为 fp32 全量）
    # H3 NVFP4：DynamicVRAM 分时换载采样峰值 ~12GB（磁盘 42.7GB 为
    # NVFP4+int4 全家桶——DiT 11.7 + 编码器 13.9 + VAE 5.4 分时驻留）
    "minimax-h3": 13.0,
    "qwen3-vl-8b": 16.3,     # bf16 真实权重（2026-08-20 崩溃修复：候选表 12GB
                             # 严重低估，16GB 卡加载必然 OOM 杀进程）
    # 视觉语音全模态（omni）：Thinker3B + Talker0.5B bf16 约 15GB；
    # AWQ int4 约 6GB（磁盘约 6.5GB，×1.2 高估幅度小，不覆盖）
    "qwen2.5-omni-7b": 15.0,
    # FLUX.2 Klein 9B（2026-08-25 接入）：磁盘 32.3GB 为 bf16 全量
    # （transformer 16.9 + 编码器 15.3），实际 quanto float8 量化后
    # transformer 9.7GB 常驻 GPU，磁盘扫描 ×1.2≈38.8GB 严重高估
    "flux2-klein-9b": 10.5,
    # DeepSeek-R1-Distill-14B W4A16（2026-08-25 接入）：vLLM 子进程
    # 加载权重 ~9.3GB + KV cache + 激活，GPU 需求约 11.5GB
    "deepseek-r1-14b-w4a16": 11.5,
}

# 模型目录的“已下载”判定特征文件
_DIR_SIGNATURES = (
    "config.json", "model_index.json", "model.safetensors.index.json",
    "model.safetensors", "pytorch_model.bin", "modules.json",
)


def deflate_cuda_pool(min_reserved_gb: float = 4.0) -> float:
    """压缩 CUDA 缓存池，归还大模型卸载后的显存锚定（2026-08-23 修复）。

    根因：PyTorch caching allocator 的 empty_cache() 只释放无活跃块的
    segment。大模型（视频 DiT/T5、对话 4b/8b）卸载后，长驻小模型
    （bge ~1.3GB）的活跃块仍散布在大 segment 中，把整个缓存池钉死
    （实测：loaded=0 但 reserved=18.2GB，物理显存 15.3GB 锚定，
    vLLM 0.85 预算三连启动失败；进程退出才归还）。

    压缩编排：bge 临时停靠 CPU → empty_cache()（此时无活跃块，
    全 segment 释放，物理显存归还驱动）→ bge 回卡。

    Args:
        min_reserved_gb: reserved 低于该值视为池已干净，跳过（省
            2-4s 停靠开销）。

    Returns:
        实际释放的 reserved GB（失败返回 0）。
    """
    try:
        import torch
        if not torch.cuda.is_available():
            return 0.0
        before = float(torch.cuda.memory_reserved(0)) / (1024 ** 3)
        if before < min_reserved_gb:
            return 0.0
        # bge 停靠（未加载/哈希回退态返回 True，同样执行清池）
        from ...data.vector_db import get_vector_db as _get_vdb
        vdb = _get_vdb()
        parked = vdb.park_embed_model()
        try:
            torch.cuda.empty_cache()
        finally:
            if parked:
                vdb.restore_embed_model()
        after = float(torch.cuda.memory_reserved(0)) / (1024 ** 3)
        released = max(0.0, before - after)
        if released >= 0.1:
            log.info("显存缓存池压缩完成: reserved %.1fGB → %.1fGB"
                     "（归还 %.1fGB）", before, after, released)
        return released
    except Exception as exc:  # noqa: BLE001 - 压缩失败不影响主流程
        log.debug("显存池压缩跳过: %s", exc)
        return 0.0


class ModelManager:
    """模型管理器单例——管理模型的全生命周期与 GPU 协同调度。

    规格 §5.4: 模型导入时自动分类、SHA256 校验、注册到数据库。
    规格 §5.3: 根据硬件状态自动选择最合适的模型。
    规格 §2.3: 模型加载互斥规则（显存不足 → 驱逐最低优先级 → 加载）。
    """

    _instance: ModelManager | None = None
    _lock = threading.Lock()

    def __new__(cls) -> ModelManager:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True

        # 延迟导入避免循环依赖
        from .cache import ModelCache
        from .classifier import ModelClassifier
        from .importer import ModelImporter
        from .predictor import FeaturePredictor
        from .selector import ModelSelector
        from .validator import ModelValidator

        self.importer = ModelImporter()
        self.validator = ModelValidator()
        self.classifier = ModelClassifier()
        self.selector = ModelSelector()
        self.cache = ModelCache()
        self.predictor = FeaturePredictor()

        # 已注册模型表（运行时内存索引）
        self._models: dict[str, ModelInfo] = {}

        # 运行时加载状态: model_id -> {category, path, vram_gb, priority,
        #                                loaded_at, engine}
        self._loaded: dict[str, dict] = {}
        self._loaded_lock = threading.Lock()
        # 用户装载钉（#8 2026-09-02 修复）：model_id -> 打钉时间戳。
        # 用户显式装载（/models/load）后 30 分钟内，调度器空闲深层
        # 回收跳过该模型——用户刚装的模型不该 5 分钟空闲就被预防性
        # 卸载（09-02 上午实测事故：对话模型装好去干了别的，回来被
        # 卸了）。硬件压力卸载（resource_guard）不豁免——救场优先。
        self._user_load_pins: dict[str, float] = {}
        self._reserved_vram_gb = 0.0        # allocate_memory 逻辑预留量
        # 审计 P0-5：_reserved_vram_gb 读改写必须原子，专用锁全程保护
        # （allocate_memory / unload_model / ensure_loaded 回滚三处共用）。
        # 必须用 RLock：allocate_memory 持锁期间会经 evict_lowest_priority
        # 嵌套调用 unload_model（其内部再次获取本锁扣减预留量），
        # 普通 Lock 不可重入会导致同线程自死锁（绘画首载卡死根因）。
        self._vram_lock = threading.RLock()
        self._engines: dict[str, Any] = {}  # category -> 引擎实例（懒加载）

        # 审计 P0-1：调度策略真实落点——CPU offload 标记与精度策略，
        # 由 dispatcher.migrate_to_cpu/degrade 写入，引擎加载时读取参考。
        self._cpu_offload_enabled = False
        self._cpu_offload_layers: list[str] = []
        self._precision_policy = "fp16"
        self._policy_lock = threading.Lock()

        # 最近一次 ensure_loaded 失败原因（契约返回 bool，细节经此暴露）
        self.last_error: str = ""

        # NVML 惰性初始化状态
        self._nvml_ready: bool | None = None

        # 磁盘扫描缓存（mtime 粗粒度失效）
        self._disk_scan_ts = 0.0
        self._disk_scan_cache: dict[str, dict] = {}

    # ═══════════════════════════════════════════════════════════════
    #  既有注册表接口（保持原契约）
    # ═══════════════════════════════════════════════════════════════

    def register(self, model_info: ModelInfo) -> None:
        """注册模型到管理器。"""
        self._models[model_info.id] = model_info

    def get_model(self, model_id: str) -> ModelInfo | None:
        """按 ID 获取模型信息。"""
        return self._models.get(model_id)

    def list_models(self, category: ModelCategory | None = None) -> list[ModelInfo]:
        """列出所有模型，可按分类过滤。"""
        if category is None:
            return list(self._models.values())
        return [m for m in self._models.values() if m.category == category]

    def remove_model(self, model_id: str) -> bool:
        """移除模型注册。"""
        if model_id in self._models:
            del self._models[model_id]
            return True
        return False

    # ═══════════════════════════════════════════════════════════════
    #  GPU 状态（pynvml，无 GPU 降级）
    # ═══════════════════════════════════════════════════════════════

    def _ensure_nvml(self) -> bool:
        if self._nvml_ready is not None:
            return self._nvml_ready
        if _pynvml is None:
            self._nvml_ready = False
            return False
        try:
            _pynvml.nvmlInit()
            self._nvml_ready = True
        except Exception:
            self._nvml_ready = False
        return self._nvml_ready

    def get_gpu_status(self, device: int | None = None) -> dict:
        """返回 GPU 状态：已用/总量/利用率（无 GPU 时降级为 zeros + available=False）。

        device=None 取主卡（0 号，历史口径）；多卡机器按 gpu_domains
        分域时传目标卡号——此前硬编码 0 号卡，1 号卡判定失明（显存
        调度机制批1，2026-09-10）。返回带 device 字段标明读数来源卡。
        """
        _dev = 0 if device is None else int(device)
        status = {
            "available": False, "gpu_name": "", "vendor": "none",
            "device": _dev,
            "vram_total_mb": 0, "vram_used_mb": 0, "vram_free_mb": 0,
            "vram_total_gb": 0.0, "vram_used_gb": 0.0, "vram_free_gb": 0.0,
            "util_percent": 0.0, "temp_celsius": 0.0,
            "reserved_vram_gb": round(self._reserved_vram_gb, 2),
        }
        if self._ensure_nvml():
            try:
                handle = _pynvml.nvmlDeviceGetHandleByIndex(_dev)
                mem = _pynvml.nvmlDeviceGetMemoryInfo(handle)
                util = _pynvml.nvmlDeviceGetUtilizationRates(handle)
                name = _pynvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode("utf-8", "ignore")
                status.update({
                    "available": True, "gpu_name": name, "vendor": "nvidia",
                    "vram_total_mb": int(mem.total // (1024 * 1024)),
                    "vram_used_mb": int(mem.used // (1024 * 1024)),
                    "vram_free_mb": int(mem.free // (1024 * 1024)),
                    "util_percent": float(util.gpu),
                })
                try:
                    status["temp_celsius"] = float(_pynvml.nvmlDeviceGetTemperature(
                        handle, _pynvml.NVML_TEMPERATURE_GPU))
                except Exception:
                    pass
            except Exception as exc:  # noqa: BLE001
                log.warning("NVML 采集失败: %s", exc)
        elif _torch is not None and getattr(_torch, "cuda", None) is not None:
            # 降级：torch 仅能取显存，无法取利用率
            try:
                if _torch.cuda.is_available():
                    total = _torch.cuda.get_device_properties(_dev).total_memory
                    used = _torch.cuda.memory_allocated(_dev)
                    status.update({
                        "available": True,
                        "gpu_name": _torch.cuda.get_device_name(_dev),
                        "vendor": "nvidia",
                        "vram_total_mb": int(total // (1024 * 1024)),
                        "vram_used_mb": int(used // (1024 * 1024)),
                        "vram_free_mb": int((total - used) // (1024 * 1024)),
                    })
            except Exception:
                pass
        status["vram_total_gb"] = round(status["vram_total_mb"] / 1024.0, 2)
        status["vram_used_gb"] = round(status["vram_used_mb"] / 1024.0, 2)
        status["vram_free_gb"] = round(status["vram_free_mb"] / 1024.0, 2)
        return status

    # ═══════════════════════════════════════════════════════════════
    #  磁盘扫描（models/ 目录存在性 → downloaded 标记）
    # ═══════════════════════════════════════════════════════════════

    def scan_downloaded_models(self, force: bool = False) -> dict[str, dict]:
        """扫描 MODELS_DIR，返回 {model_id: {path, size_gb, downloaded}}。

        合并策略：显式路径提示（_MODEL_PATH_HINTS）+ 两层目录特征扫描。
        结果缓存 30 秒避免高频 IO。

        剪枝（模型管理页分组视图的准确性前提）：
        - 孙层组件目录黑名单：diffusers 组件（unet/vae/text_encoder/...）、
          说话人/适配器版本（sv、v1~v9）不是独立模型，是父仓库的组成部分；
        - 祖先包含剪枝：路径位于另一已登记模型目录内的条目视为组件剔除
          （如 sd15/unet、qwen3-tts/speech_tokenizer）。
        """
        if not force and (time.time() - self._disk_scan_ts) < 30.0 \
                and self._disk_scan_cache:
            return dict(self._disk_scan_cache)

        found: dict[str, dict] = {}
        base = Path(MODELS_DIR)

        def _probe(model_id: str, rel: str) -> None:
            p = base / rel
            if p.is_dir() and self._looks_like_model_dir(p):
                found[model_id] = {
                    "path": str(p),
                    "size_gb": self._dir_size_gb(p),
                    "downloaded": True,
                }

        for mid, rel in _MODEL_PATH_HINTS.items():
            _probe(mid, rel)

        # 两层通用扫描（models/* 与 models/<group>/*）
        try:
            for child in sorted(base.iterdir()):
                if child.is_dir() and not child.name.startswith(("_", ".")):
                    if self._looks_like_model_dir(child):
                        found.setdefault(child.name, {
                            "path": str(child),
                            "size_gb": self._dir_size_gb(child),
                            "downloaded": True,
                        })
                    for grand in sorted(child.iterdir()):
                        if not grand.is_dir():
                            continue
                        # 组件目录黑名单：父仓库组成部分，不是独立模型
                        if (grand.name in _COMPONENT_DIR_NAMES
                                or _LORA_VERSION_RE.match(grand.name)):
                            continue
                        if self._looks_like_model_dir(grand):
                            found.setdefault(grand.name, {
                                "path": str(grand),
                                "size_gb": self._dir_size_gb(grand),
                                "downloaded": True,
                            })
                elif child.is_file() and child.suffix.lower() in (
                        ".gguf", ".safetensors", ".ckpt", ".onnx") \
                        and child.stat().st_size > 100 * 1024 * 1024:
                    # 顶层散置单文件模型（如 models/Qwen3-7B-Q4_K_M.gguf）
                    found.setdefault(child.stem, {
                        "path": str(child),
                        "size_gb": round(child.stat().st_size / (1024 ** 3), 3),
                        "downloaded": True,
                    })
        except Exception as exc:  # noqa: BLE001
            log.warning("模型目录扫描异常: %s", exc)

        # 祖先包含剪枝：路径位于另一模型目录内 → 是组件而非独立模型。
        # 相同路径不视为嵌套——hints 显式 id（minimax-h3）与通用扫描的
        # 目录名 id（h3）同路径互剪会让两者双双消失（2026-08-25 实测），
        # 且同路径去重时 hints 权威 id 优先于目录名 id
        pruned: dict[str, dict] = {}
        paths = sorted(found.items(), key=lambda kv: len(kv[1]["path"]))
        for mid, hit in paths:
            nested = any(
                other is not hit
                and other["path"] != hit["path"]
                and Path(hit["path"]).is_relative_to(Path(other["path"]))
                for other in found.values()
            )
            if nested:
                continue
            dup = next((k for k, v in pruned.items()
                        if v["path"] == hit["path"]), None)
            if dup is None:
                pruned[mid] = hit
            elif mid in _MODEL_PATH_HINTS and dup not in _MODEL_PATH_HINTS:
                del pruned[dup]
                pruned[mid] = hit

        self._disk_scan_cache = pruned
        self._disk_scan_ts = time.time()
        return dict(pruned)

    def shed_memory(self) -> dict:
        """RAM 危急收缩（resource_guard ≥90% 危急线调用）。

        只吐「可再生」内存：权重缓存全量卸载（miss 后下次装载重新
        读盘）+ 磁盘扫描缓存丢弃（下次 list 重扫磁盘自动重建）。不碰
        已加载模型、功能锁、会话状态——生成链路零影响。

        背景（2026-09-02 18:49 静默死亡事故）：WER RADAR_PRE_LEAK_64
        实证 RAM 提交耗尽原生硬死；85% 动作线卸完空闲模型后仍可能
        持续高位，危急线把可再生数据全让出来防提交打顶。
        """
        try:
            cache_unloaded = self.cache.full_unload()
        except Exception as exc:  # noqa: BLE001 - 收缩失败不阻断守卫
            log.warning("权重缓存卸载失败: %s", exc)
            cache_unloaded = -1
        scan_dropped = len(self._disk_scan_cache)
        self._disk_scan_cache = {}
        self._disk_scan_ts = 0.0
        log.info("RAM 危急收缩: 权重缓存卸载 %s 条, 磁盘扫描缓存丢弃 %d 条",
                 cache_unloaded, scan_dropped)
        return {"cache_unloaded": cache_unloaded,
                "scan_cache_dropped": scan_dropped}

    @staticmethod
    def _looks_like_model_dir(p: Path) -> bool:
        for sig in _DIR_SIGNATURES:
            if (p / sig).exists():
                return True
        # 常见权重扩展名
        try:
            for f in p.iterdir():
                if f.suffix in (".safetensors", ".gguf", ".ckpt", ".bin", ".onnx"):
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _dir_size_gb(p: Path) -> float:
        total = 0
        try:
            for f in p.rglob("*"):
                if f.is_file() and ".cache" not in f.parts:
                    total += f.stat().st_size
        except Exception:
            pass
        return round(total / (1024 ** 3), 3)

    def registry_entry(self, model_id: str) -> dict | None:
        """登记表（models 表）条目查询——用户导入模型的路径真源兜底。

        导入的外部路径（models/ 之外）不在磁盘扫描命名空间内，
        resolve_model_path / estimate_vram_gb 扫描未命中时经此解析。
        条目失效（文件被删）返回 None，与扫描层「未下载」语义一致。
        """
        try:
            from ...data.database import get_db_safe
            db = get_db_safe()
            if db is None:
                return None
            row = db.query_one(
                "SELECT id, category, min_vram_gb, size_gb, file_path"
                " FROM models WHERE id = ?", (model_id,))
        except Exception:  # noqa: BLE001 - 登记表不可用时静默降级
            return None
        if not row:
            return None
        path = row.get("file_path") or ""
        if not path or not Path(path).exists():
            return None
        return row

    def resolve_model_path(self, model_id: str) -> str | None:
        """解析模型的本地路径（注册表 file_path → 磁盘扫描 → 登记表兜底），未下载返回 None。"""
        info = self._models.get(model_id)
        if info is not None and info.file_path and Path(info.file_path).exists():
            return info.file_path
        scanned = self.scan_downloaded_models()
        hit = scanned.get(model_id)
        if hit:
            return hit["path"]
        row = self.registry_entry(model_id)
        return row["file_path"] if row else None

    def estimate_vram_gb(self, model_id: str, category: str = "") -> float:
        """估计加载所需显存：人工覆盖 → 引擎候选表 → 路由表 min_vram_gb → 磁盘大小 × 1.2 → 默认 4GB。

        人工覆盖最高优先（2026-08-20 崩溃修复）：_VRAM_OVERRIDES 是实测
        裁定值，路由表/候选表的静态 min_vram_gb 可能严重低估（如
        qwen3-vl-8b 标 12GB 实测 bf16 权重 16.3GB），低估值放行会导致
        allocate_memory 误判通过 → 引擎加载时 OOM 杀进程。

        引擎候选表其次：模型目录常含 fp32/fp16 双份权重与单文件兜底
        （如 sdxl-base-1.0 磁盘 25.9GB 实需约 7GB），磁盘扫描会严重高估，
        导致 allocate_memory 永远失败、驱逐记账失真。
        """
        if model_id in _VRAM_OVERRIDES:
            return _VRAM_OVERRIDES[model_id]
        try:
            from ..inference.dialog_engine import DIALOG_MODEL_CANDIDATES
            from ..inference.paint_engine import PAINT_MODEL_CANDIDATES
            for mid, _rel, vram in (*PAINT_MODEL_CANDIDATES,
                                    *DIALOG_MODEL_CANDIDATES):
                if mid == model_id:
                    return float(vram)
        except Exception:  # noqa: BLE001 - 引擎表不可用时回退既有估计
            pass
        for table in (DIALOG_ROUTING_TABLE, PAINT_ROUTING_TABLE, VIDEO_ROUTING_TABLE):
            for entry in table:
                mid = entry["model"]
                mid = mid.value if hasattr(mid, "value") else str(mid)
                if mid == model_id:
                    return float(entry["min_vram_gb"]) or 1.0
        # 登记表兜底（用户导入的外部路径模型：导入时按体积 ×1.2 落库）
        row = self.registry_entry(model_id)
        if row is not None:
            if float(row.get("min_vram_gb") or 0) > 0:
                return float(row["min_vram_gb"])
            if float(row.get("size_gb") or 0) > 0:
                return round(float(row["size_gb"]) * 1.2, 2)
        scanned = self.scan_downloaded_models()
        if model_id in scanned and scanned[model_id]["size_gb"] > 0:
            return round(scanned[model_id]["size_gb"] * 1.2, 2)
        return 4.0

    # ═══════════════════════════════════════════════════════════════
    #  互斥矩阵（规格 §2.1）
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _normalize_feature(feature: str) -> str:
        f = (feature or "").strip().lower()
        return _FEATURE_ALIASES.get(f, f)

    def check_mutual_exclusion(self, target: str) -> list[str]:
        """按规格 §2.1 互斥矩阵返回 target 活跃时需要置灰的功能列表。

        manga/video/draw 等别名归一化到 feature_lock 语义
        （dialog/paint/video_gen/training）。
        """
        return list(MUTUAL_EXCLUSION_MATRIX.get(self._normalize_feature(target), []))

    def get_blocked_features(self) -> list[str]:
        """基于当前功能锁状态，返回此刻应置灰的功能列表。"""
        try:
            from ...middleware.feature_lock import get_feature_lock
            active = get_feature_lock().active_feature
        except Exception:  # noqa: BLE001
            active = None
        if not active:
            return []
        return self.check_mutual_exclusion(active)

    def is_feature_blocked(self, target: str) -> tuple[bool, str]:
        """检查 target 当前是否被互斥阻断；返回 (blocked, reason)。"""
        try:
            from ...middleware.feature_lock import get_feature_lock
            mgr = get_feature_lock()
            reason = mgr.get_block_reason(self._normalize_feature(target))
            return (reason is not None, reason or "")
        except Exception:  # noqa: BLE001
            return (False, "")

    # ═══════════════════════════════════════════════════════════════
    #  显存分配与驱逐（规格 §2.3 模型加载互斥规则）
    # ═══════════════════════════════════════════════════════════════

    def allocate_memory(self, required_gb: float) -> bool:
        """为即将加载的模型分配显存；不足时按优先级驱逐。

        审计 P0-5 并发安全：_reserved_vram_gb 的「检查→驱逐→累加」全程
        持有 _vram_lock，防止并发加载（如对话与绘画同时 ensure_loaded）
        各自看到同一空闲值而双双超发显存（真实 OOM 风险）。
        锁内只做记账与驱逐决策，引擎真实加载在锁外执行。

        Returns:
            True 表示可满足（逻辑预留已记账）
        """
        required_gb = float(required_gb)
        if required_gb <= 0:
            return True

        with self._vram_lock:
            gpu = self.get_gpu_status()
            if not gpu["available"]:
                # 无 GPU：仅允许 0 显存需求（CPU 模型由调用方自行降级）
                self.last_error = "无可用 GPU"
                return False

            def _free_logical() -> float:
                return gpu["vram_free_gb"] - self._reserved_vram_gb

            if _free_logical() >= required_gb:
                self._reserved_vram_gb += required_gb
                return True

            # 驱逐重试（规格 §2.3：有空闲可卸载模型 → 驱逐最低优先级）
            while self._loaded and _free_logical() < required_gb:
                if not self.evict_lowest_priority():
                    break
                gpu = self.get_gpu_status()

            # V9-α（2026-09-09）：账本外 vLLM 孤儿对账——live e2e 测试
            # 退出等遗留的孤儿进程占着显存但不在 _loaded（无可驱逐），
            # 预检闸会拒绝、而清孤儿的逻辑原本在 start() 里（闸后）永远
            # 跑不到（2026-09-08 15:26 实测死锁：孤儿占 15GB →
            # MODEL_NO_EVICTABLE）。清完复测空闲再判；清扫尽力而为，
            # 失败/无孤儿均不影响原判定路径。
            if _free_logical() < required_gb:
                try:
                    from ...engines.vllm_service import get_vllm_service
                    reaped = get_vllm_service().reap_orphans()
                except Exception as exc:  # noqa: BLE001 - 对账失败按原判定
                    log.debug("vLLM 孤儿对账通道异常（按原判定）: %s", exc)
                    reaped = 0
                if reaped:
                    gpu = self.get_gpu_status()

            if _free_logical() >= required_gb:
                self._reserved_vram_gb += required_gb
                return True
            self.last_error = (
                f"显存不足：需要 {required_gb:.1f}GB，"
                f"可用 {max(_free_logical(), 0.0):.1f}GB"
            )
            return False

    # ═══════════════════════════════════════════════════════════════
    #  调度策略落点（审计 P0-1：dispatcher 真实接线目标）
    # ═══════════════════════════════════════════════════════════════

    def set_cpu_offload(self, enabled: bool,
                        layers: list[str] | None = None) -> None:
        """写入 CPU offload 标记（由 dispatcher.migrate_to_cpu 调用）。

        引擎下次加载模型时读取本标记，决定是否启用 CPU offload
        （如 diffusers enable_model_cpu_offload / transformers device_map）。
        """
        with self._policy_lock:
            self._cpu_offload_enabled = bool(enabled)
            if layers is not None:
                self._cpu_offload_layers = list(layers)
        log.info("CPU offload 策略: enabled=%s, layers=%s",
                 enabled, self._cpu_offload_layers)

    def get_cpu_offload(self) -> dict:
        """读取 CPU offload 策略（引擎加载时参考）。"""
        with self._policy_lock:
            return {
                "enabled": self._cpu_offload_enabled,
                "layers": list(self._cpu_offload_layers),
            }

    def set_precision_policy(self, precision: str) -> None:
        """写入全局精度策略（由 dispatcher.degrade 调用）。

        引擎加载时读取本策略选择 torch_dtype（fp16/bf16/int8/int4）。
        """
        with self._policy_lock:
            self._precision_policy = str(precision)
        log.info("精度策略: %s", precision)

    def get_precision_policy(self) -> str:
        """读取全局精度策略（引擎加载时参考）。"""
        with self._policy_lock:
            return self._precision_policy

    def _active_feature_keep_categories(self) -> set[str]:
        """持锁活跃功能所需类别（驱逐统一语言，批3 2026-09-10）。

        与 resource_guard/_release_for_module 用同一张
        _FEATURE_KEEP_CATEGORIES 表——驱逐不再只按类别优先级盲选
        （此前两套语言并存：evict 不看锁、守卫看锁，同一模型在不同
        路径「可不可卸」判定不一致；VACE 误卸事故即两套语言打架）。
        共享小模型（embedding/voice/auxiliary）语义不变仍可被驱逐
        （60s 表层回收本就是它们的归宿）。
        """
        try:
            from ...middleware.feature_lock import get_feature_lock

            holder = get_feature_lock().active_feature
        except Exception:  # noqa: BLE001 - 锁查询失败不拦截驱逐
            return set()
        if not holder:
            return set()
        return set(_FEATURE_KEEP_CATEGORIES.get(holder, set()))

    def evict_lowest_priority(self) -> bool:
        """驱逐已加载模型中优先级最低者（平级取最久未用）。

        批3（2026-09-10）：候选先剔除持锁活跃功能所需类别（驱逐
        统一语言）；无候选（全被活跃功能保护）返回 False，由调用方
        的引擎降级链兜底。

        Returns:
            True 表示成功驱逐一个模型
        """
        keep_cats = self._active_feature_keep_categories()
        with self._loaded_lock:
            candidates = {
                mid: e for mid, e in self._loaded.items()
                if str(e.get("category", "")) not in keep_cats}
            if not candidates:
                return False
            victim_id, victim = min(
                candidates.items(),
                key=lambda kv: (kv[1].get("priority", 0), kv[1].get("loaded_at", 0.0)),
            )
        log.info("驱逐最低优先级模型: %s (priority=%s, keep=%s)",
                 victim_id, victim.get("priority"), sorted(keep_cats) or "无锁保护")
        return self.unload_model(victim_id)

    # ═══════════════════════════════════════════════════════════════
    #  引擎接线（try import 容错）
    # ═══════════════════════════════════════════════════════════════

    def _get_engine(self, category: str) -> Any:
        """按类别获取推理引擎单例（ADR-003 P2：注册表驱动，懒加载 + 容错）。

        品类 → 引擎映射收敛至 inference.base_engine.ENGINE_MODULES：
        新增品类调用 register_engine_module() 即接入本管理器，无需改动
        此处。未注册品类（auxiliary/embedding/3d 等）→ None（调用方按
        CPU 侧自管语义记账）。缓存与容错语义与迁移前逐条对齐。
        """
        cat = (category or "").strip().lower()
        if cat in self._engines:
            return self._engines[cat]

        from ..inference.base_engine import resolve_engine
        engine = resolve_engine(cat)

        self._engines[cat] = engine
        return engine

    # ═══════════════════════════════════════════════════════════════
    #  加载 / 卸载主流程
    # ═══════════════════════════════════════════════════════════════

    def note_external_load(self, model_id: str, category: str) -> bool:
        """引擎经外部通道（V9-γ 唤醒收养等）完成装载后的台账补记。

        幂等：已登记直接 True。补记后分配器不再因台账盲区拒绝
        「装载一个已装好的模型」（2026-09-09 04:00 卡 90% 事故
        根因②）；显存为服务已消耗的事实占用，不进 _reserved_vram_gb
        （该值只跟踪 ensure 流程的在途预留）。
        """
        cat = (category or "").strip().lower()
        with self._loaded_lock:
            if model_id in self._loaded:
                return True
            path = self.resolve_model_path(model_id)
            if path is None:
                return False
            required = self.estimate_vram_gb(model_id, cat)
            try:
                from ...engines.gpu_domains import resolve_feature_device
                _dev = resolve_feature_device(self._normalize_feature(cat))
            except Exception:  # noqa: BLE001 - 分配失败按主卡
                _dev = 0
            self._loaded[model_id] = {
                "category": cat, "path": path,
                "vram_gb": required,
                "priority": _EVICTION_PRIORITY.get(cat, 0),
                "loaded_at": time.time(),
                "engine": "external_adopt",
                "device": _dev,
            }
        log.info("台账补记（外部通道装载收养，V9-γ）: %s", model_id)
        return True

    def ensure_loaded(self, category: str, model_id: str) -> bool:
        """确保模型加载到 GPU（其他服务依赖的稳定契约）。

        流程：互斥检查 → 已加载短路 → 路径解析 → 显存分配（必要时驱逐）
              → 引擎加载 → 状态记账。

        Returns:
            True 加载成功；False 失败（原因见 self.last_error）
        """
        self.last_error = ""
        cat = (category or "").strip().lower()
        model_id = (model_id or "").strip()
        if not model_id:
            self.last_error = "model_id 为空"
            return False

        # 已加载短路
        with self._loaded_lock:
            if model_id in self._loaded:
                return True

        # 路径解析（未下载直接失败，不崩溃）
        path = self.resolve_model_path(model_id)
        if path is None:
            self.last_error = f"模型未下载: {model_id}"
            log.warning("ensure_loaded 失败: %s", self.last_error)
            return False

        # V9-γ 收养旁路（2026-09-09）：目标模型已由外部通道健康服务中
        #（绘画让渡后 wake 后台重启的产物：后端亲儿子、服务健康，但
        # 唤醒线程不经过本函数，台账无记录）——先问引擎能否零成本收养，
        # 能则跳过显存分配直接入账。防「装载已装好的模型被盲区拒绝」
        #（04:00 卡 90% 事故根因②）。
        try:
            engine = self._get_engine(cat)
        except Exception:  # noqa: BLE001
            engine = None
        if engine is not None:
            try:
                adopt_fn = getattr(engine, "try_adopt", None)
                if callable(adopt_fn) and bool(adopt_fn(model_id)):
                    self.note_external_load(model_id, cat)
                    self.last_error = ""
                    log.info("收养旁路命中（已健康服务，零显存装载）: %s",
                             model_id)
                    return True
            except Exception as exc:  # noqa: BLE001 - 收养失败走原路径
                log.debug("收养旁路探测失败: %s", exc)

        # 显存检查与分配
        required = self.estimate_vram_gb(model_id, cat)
        engine = self._get_engine(cat)
        if engine is None:
            # 无 GPU 引擎的类别（embedding/auxiliary/voice）：登记为已加载，
            # 显存需求视为 0（由所属功能在 CPU 侧惰性加载）。
            with self._loaded_lock:
                self._loaded[model_id] = {
                    "category": cat, "path": path, "vram_gb": 0.0,
                    "priority": _EVICTION_PRIORITY.get(cat, 0),
                    "loaded_at": time.time(), "engine": "none",
                }
            log.info("模型登记（无引擎类别，CPU 侧管理）: %s", model_id)
            return True

        if not self.allocate_memory(required):
            log.warning("ensure_loaded 显存分配失败: %s", self.last_error)
            return False

        # 审计 P0-2 接线 gpu_backend.select_backend：引擎加载前按 GPU 信息
        # 选择计算后端（cuda/directml/cpu），结果记入加载台账供诊断。
        backend_tag = "cuda"
        try:
            from ...engines.gpu_backend import select_backend
            gpu_info = self.get_gpu_status()
            backend_tag = select_backend({
                "vendor": gpu_info.get("vendor", "none"),
                "vram_total_mb": gpu_info.get("vram_total_mb", 0),
            })
            if backend_tag != "cuda":
                log.info("模型 %s 后端选择: %s（非 CUDA，引擎内部自行适配）",
                         model_id, backend_tag)
        except Exception as exc:  # noqa: BLE001 - 后端探测失败不阻断加载
            log.debug("后端选择探测跳过: %s", exc)

        # 调用引擎加载（引擎内部已做 fallback 容错）。
        # 注意：引擎 load_model 契约接收 model_id（候选表匹配），而非文件路径。
        t0 = time.time()
        ok = False
        try:
            load_fn = getattr(engine, "load_model", None)
            if callable(load_fn):
                ok = bool(load_fn(model_id))
        except Exception as exc:  # noqa: BLE001
            log.error("引擎加载异常 (%s): %s", model_id, exc)
            ok = False

        if not ok:
            # 审计 P0-5：预留量回滚同样持锁，与 allocate_memory 原子对应
            with self._vram_lock:
                self._reserved_vram_gb = max(
                    0.0, self._reserved_vram_gb - required)
            # 2026-09-07 降级链回归修复：只写「引擎加载失败: id」会吞掉
            # 引擎详细原因（显存不足等），下游 dialog_engine 的降级判定
            # （按「显存」关键字识别可降级失败）随之失明——9B 装不下时
            # 不再自动降级 4b、消息直接报错。此处透传引擎 last_error。
            _detail = ""
            try:
                _detail = str(engine.last_error() or "").strip()
            except Exception:  # noqa: BLE001 - 访问器失败不影响主流程
                pass
            self.last_error = (
                f"引擎加载失败: {model_id}" + (f" {_detail}" if _detail else ""))
            log.warning("ensure_loaded 引擎加载失败: %s（%s）",
                        model_id, _detail[:120] or "无详细原因")
            return False

        with self._loaded_lock:
            # 归属卡（批1 多卡地基 2026-09-05）：按类别资源域记录；
            # 未知类别（embedding/voice 等小模型）跟随主卡。单卡机器
            # 恒 0，台账语义不变。
            try:
                from ...engines.gpu_domains import resolve_feature_device
                _dev = resolve_feature_device(self._normalize_feature(cat))
            except Exception:  # noqa: BLE001 - 分配失败按主卡
                _dev = 0
            self._loaded[model_id] = {
                "category": cat, "path": path, "vram_gb": required,
                "priority": _EVICTION_PRIORITY.get(cat, 0),
                "loaded_at": time.time(),
                "engine": type(engine).__name__,
                "backend": backend_tag,
                "load_seconds": round(time.time() - t0, 2),
                "device": _dev,
            }

        # 审计 P0-2 接线 engines.vram_manager / memory_manager：
        # 真实跟踪显存分配 + 注册内存块（供 MEMORY_PRESSURE 策略压缩）。
        try:
            from ...engines.vram_manager import get_vram_manager
            get_vram_manager().track_alloc(model_id, required * 1024.0,
                                           device=_dev)
        except Exception as exc:  # noqa: BLE001
            log.debug("vram_manager 跟踪跳过: %s", exc)
        try:
            from ...engines.memory_manager import get_memory_manager
            get_memory_manager().register(
                f"model:{model_id}", data={"path": path, "category": cat},
                size_mb=required * 1024.0)
        except Exception as exc:  # noqa: BLE001
            log.debug("memory_manager 注册跳过: %s", exc)

        log.info("模型加载完成: %s (%.1fs, 预估显存 %.1fGB)",
                 model_id, time.time() - t0, required)
        return True

    def release_stale(self, model_id: str) -> bool:
        """仅清台账与显存记账，不回调引擎（2026-08-22 事故根修）。

        适用场景：引擎侧已自行释放旧模型（如 dialog_engine 热切换
        _switch_reset），但台账条目残留 loaded —— resource_guard 驱逐
        stale 条目时会经 dialog 关联回调 unload_model() 误杀引擎当前
        持有模型。热切换完成时调用本方法同步台账，消除错位。
        """
        with self._loaded_lock:
            entry = self._loaded.pop(model_id, None)
        if entry is None:
            return False
        with self._vram_lock:
            self._reserved_vram_gb = max(
                0.0, self._reserved_vram_gb - float(entry.get("vram_gb", 0.0)))
        try:
            from ...engines.vram_manager import get_vram_manager
            get_vram_manager().track_free(model_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("vram_manager 释放跟踪跳过: %s", exc)
        try:
            from ...engines.memory_manager import get_memory_manager
            get_memory_manager().unregister(f"model:{model_id}")
        except Exception as exc:  # noqa: BLE001
            log.debug("memory_manager 注销跳过: %s", exc)
        log.info("模型台账清理（引擎已自行释放）: %s", model_id)
        return True

    def rollback_allocation(self, required_gb: float) -> None:
        """回滚 allocate_memory 的预留量（对称扣减，clamp 0）。

        供引擎直载路径使用（2026-08-25 ModelSwitchEngine P2 video
        接入）：allocate 成功但 video_engine.load_model 失败时，
        预留量已加而台账未登记——不经此回滚会永久泄漏记账显存。
        """
        with self._vram_lock:
            self._reserved_vram_gb = max(
                0.0, self._reserved_vram_gb - float(required_gb))

    def unload_model(self, model_id: str) -> bool:
        """从 GPU 卸载模型并释放记账显存。"""
        with self._loaded_lock:
            entry = self._loaded.pop(model_id, None)
            # 钉随卸载失效（getattr 兜底：测试裸实例可能未建该属性）
            getattr(self, "_user_load_pins", {}).pop(model_id, None)
        if entry is None:
            return False

        # 经 _get_engine 解析（懒加载接线）：引擎类别尚未实例化时现场
        # 获取共享单例，否则自动装载登记的模型会出现「台账已清、真实
        # 管线引用未释放」的脱节
        engine = self._get_engine(entry.get("category", ""))
        # 防御 stale 台账（2026-08-22 事故）：dialog 引擎当前持有模型与
        # 台账条目不一致（热切换竞态窗口残留）时，回调 unload_model()
        # 会误杀引擎新持有模型——只清台账不动引擎
        if engine is not None and entry.get("category", "") == "dialog":
            try:
                cur = engine.get_status().get("model") or ""
            except Exception:  # noqa: BLE001 - 状态查询失败按正常路径走
                cur = model_id
            if cur and cur != model_id:
                log.warning(
                    "卸载 %s 跳过引擎回调（引擎当前持有 %s，台账 stale）",
                    model_id, cur)
                engine = None
        if engine is not None:
            try:
                unload_fn = getattr(engine, "unload_model", None)
                if callable(unload_fn):
                    # 引擎自定义卸载契约优先（如 VideoEngine.unload_model
                    # 负责 AnimateLCM 分支等引擎私有引用的释放）
                    unload_fn()
                else:
                    # 通用卸载：释放管线引用 + 清空 CUDA 缓存
                    for attr in ("_model", "_pipeline", "_tokenizer",
                                 "_animatelcm_pipe"):
                        if hasattr(engine, attr):
                            setattr(engine, attr, None)
                    if hasattr(engine, "_loaded"):
                        engine._loaded = False
            except Exception as exc:  # noqa: BLE001
                log.warning("引擎卸载异常 (%s): %s", model_id, exc)
            # 审计修复：视频引擎一次性自动装载标记复位——卸载（含
            # dispatcher.force_unload 链路）后允许下次
            # _ensure_video_loaded 重试真实管线。通用层防御式处理，
            # 不硬编码 video_engine 类型。
            try:
                if hasattr(engine, "_video_autoload_attempted"):
                    engine._video_autoload_attempted = False
            except Exception:  # noqa: BLE001
                pass

        # 审计 P0-5：预留量扣减持锁（与 allocate_memory 原子对应）
        with self._vram_lock:
            self._reserved_vram_gb = max(
                0.0, self._reserved_vram_gb - float(entry.get("vram_gb", 0.0)))

        # 审计 P0-2：同步释放 vram_manager / memory_manager 跟踪记录
        try:
            from ...engines.vram_manager import get_vram_manager
            get_vram_manager().track_free(model_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("vram_manager 释放跟踪跳过: %s", exc)
        try:
            from ...engines.memory_manager import get_memory_manager
            get_memory_manager().unregister(f"model:{model_id}")
        except Exception as exc:  # noqa: BLE001
            log.debug("memory_manager 注销跳过: %s", exc)

        gc.collect()
        if _torch is not None:
            try:
                if _torch.cuda.is_available():
                    _torch.cuda.empty_cache()
            except Exception:
                pass
        log.info("模型已卸载: %s", model_id)
        return True

    def register_external_load(self, category: str, model_id: str,
                               path: str, vram_gb: float) -> bool:
        """登记引擎侧自动装载的模型（绕过 ensure_loaded 的加载路径）。

        视频引擎 _ensure_video_loaded 自动装载链不经 ensure_loaded，
        装载成功后经本方法登记加载台账，使 get_loaded_models() /
        evict_lowest_priority / dispatcher.force_unload 与 GPU 实际占用
        一致。幂等：model_id 已登记时返回 False，不产生重复条目。
        预留量在此累加，与 unload_model/unregister_load 的扣减对称。

        Returns:
            True 本次新登记；False 已登记或参数无效
        """
        cat = (category or "").strip().lower()
        model_id = (model_id or "").strip()
        if not model_id:
            return False
        with self._loaded_lock:
            if model_id in self._loaded:
                return False
            self._loaded[model_id] = {
                "category": cat, "path": path, "vram_gb": float(vram_gb),
                "priority": _EVICTION_PRIORITY.get(cat, 0),
                "loaded_at": time.time(),
                "engine": "autoload",
                "backend": "cuda" if vram_gb > 0 else "cpu",
            }
        # 与 unload_model/unregister_load 的预留量扣减对称（审计 P0-5
        # 同一把锁保护读改写）
        with self._vram_lock:
            self._reserved_vram_gb += float(vram_gb)
        log.info("登记引擎自动装载模型: %s (category=%s, %.1fGB)",
                 model_id, cat, vram_gb)
        return True

    def unregister_load(self, model_id: str) -> bool:
        """注销 register_external_load 的台账登记（引擎直接卸载时调用）。

        仅移除加载台账条目并对称扣减预留量：不触碰引擎引用与
        vram_manager 记账（由引擎 unload_model 自身对称释放），
        因此经 unload_model 正常卸载链路调用时条目已被弹出，幂等
        返回 False，不会重复扣减。
        """
        with self._loaded_lock:
            entry = self._loaded.pop(model_id, None)
        if entry is None:
            return False
        with self._vram_lock:
            self._reserved_vram_gb = max(
                0.0, self._reserved_vram_gb - float(entry.get("vram_gb", 0.0)))
        log.info("注销引擎自动装载模型登记: %s", model_id)
        return True

    def get_loaded_models(self) -> list[dict]:
        """返回当前已加载模型列表（含类别/路径/显存/优先级/加载时间）。"""
        with self._loaded_lock:
            return [dict(model_id=mid, **info) for mid, info in self._loaded.items()]

    # ── 用户装载钉（#8 2026-09-02 修复）──────────────────────────
    USER_PIN_WINDOW_S = 30 * 60.0

    def note_user_load(self, model_id: str) -> None:
        """用户显式装载（/models/load）→ 打 30 分钟免空闲回收钉。"""
        with self._loaded_lock:
            self._user_load_pins[model_id] = time.time()
        log.info("用户装载钉: %s（%d 分钟内空闲深层回收豁免）",
                 model_id, int(self.USER_PIN_WINDOW_S // 60))

    def is_user_pinned(self, model_id: str) -> bool:
        """钉窗口内（且模型确在加载台账）返回 True。"""
        with self._loaded_lock:
            ts = self._user_load_pins.get(model_id)
            return bool(ts and model_id in self._loaded
                        and time.time() - ts < self.USER_PIN_WINDOW_S)

    # ═══════════════════════════════════════════════════════════════
    #  模块切换资源调度（用户裁定 2026-08-21）
    # ═══════════════════════════════════════════════════════════════

    def release_for_module(self, target_feature: str,
                           timeout_seconds: float = 3.0) -> dict:
        """切入 target_feature 模块时释放其他模块已加载模型（显存/内存优先供应）。

        整个释放过程带时间预算（默认 3s）：每次卸载前检查剩余预算，
        超出预算停止后续卸载并标记 completed=False（部分释放）。

        安全约束：
        - 功能锁持有中的模型（该功能有生成任务正在运行）跳过不卸，
          避免拆掉运行中任务的推理管线；
        - 目标模块自身类别与共享小模型类别（embedding/voice/auxiliary）保留；
        - 卸载链复用 unload_model（释放引擎引用 + vram/memory 记账 +
          gc + cuda empty_cache）。

        Returns:
            {module, completed, freed_models, freed_count,
             freed_vram_gb, skipped, duration_ms}
        """
        t0 = time.monotonic()
        target = self._normalize_feature(target_feature)
        keep_cats = set(_FEATURE_KEEP_CATEGORIES.get(target, set()))
        keep_cats |= _SHARED_KEEP_CATEGORIES

        # 目标卡（批1 多卡地基 2026-09-05）：只腾目标卡的地方。
        # 单卡机器全在 0 号卡，过滤与守卫全部退化为旧行为。
        try:
            from ...engines.gpu_domains import resolve_feature_device
            target_device = resolve_feature_device(target)
        except Exception:  # noqa: BLE001
            target_device = 0

        # 功能锁持有中的类别不可卸（运行中任务的管线引用）
        locked_cats: set[str] = set()
        try:
            from ...middleware.feature_lock import get_feature_lock
            active = get_feature_lock().active_feature
            if active and self._normalize_feature(active) != target:
                locked_cats = (_FEATURE_KEEP_CATEGORIES.get(
                    self._normalize_feature(active), set()) - keep_cats)
        except Exception:  # noqa: BLE001
            pass

        freed_models: list[str] = []
        freed_vram = 0.0
        skipped: list[str] = []
        completed = True

        for entry in self.get_loaded_models():
            cat = (entry.get("category") or "").strip().lower()
            model_id = entry.get("model_id", "")
            if cat in keep_cats:
                continue
            # 异卡模型不占目标卡的地方（批1）：单卡下恒 0==0 不过此门，
            # 行为与历史一致
            entry_dev = entry.get("device")
            if entry_dev is not None and int(entry_dev) != target_device:
                continue
            if cat in locked_cats:
                skipped.append(model_id)
                continue
            if time.monotonic() - t0 > timeout_seconds:
                completed = False
                skipped.append(model_id)
                continue
            vram = float(entry.get("vram_gb", 0.0) or 0.0)
            if self.unload_model(model_id):
                freed_models.append(model_id)
                freed_vram += vram

        # 内存侧补充：释放闲置内存块（unload_model 已做进程级 gc）
        try:
            from ...engines.memory_manager import get_memory_manager
            get_memory_manager().release()
        except Exception:  # noqa: BLE001
            pass

        # vLLM 独立子进程（AWQ 对话模型）：不在 _loaded 台账，显存由
        # 子进程整卡持有。P3 §3.2 常驻热备 + 按需冷启双策略：
        #  - 目标为重显存模块（绘画/视频/训练）→ 按需终止 vLLM 腾
        #    ~12GB 显存（否则重模型加载必然 OOM）；
        #  - 目标非对话且非重显存（设置/日志等轻量切换）→ 保留 vLLM
        #    热备、复用已加载 worker，切回对话免二次 ~157s 冷启动。
        # 功能锁保护（2026-08-22 竞态事故）：dialog 锁持有中（对话请求
        # 进行中，含 vLLM 冷启动窗口）不可杀——实测用户发消息后
        # 切走再切回，release_for_module(paint) 把刚就绪的 vLLM 杀掉，
        # 对话流式直接失败"服务未就绪"。锁保护与台账模型同权。
        vllm_stopped = False
        vllm_kept_hot = False
        if ("dialog" not in keep_cats
                and "dialog" not in locked_cats):
            # 异卡不杀（批1）：vLLM 与目标不在同一张卡时互不抢地方
            # （单卡下 dialog/target 恒同卡，与历史一致）
            _vllm_same_card = True
            try:
                from ...engines.gpu_domains import resolve_feature_device
                _vllm_same_card = (
                    resolve_feature_device("dialog") == target_device)
            except Exception:  # noqa: BLE001
                pass
            if _VRAM_HEAVY_FEATURES.intersection({target}) and _vllm_same_card:
                try:
                    from ...engines.vllm_service import get_vllm_service
                    svc = get_vllm_service()
                    if svc.is_running():
                        t_vllm = time.monotonic()
                        # 先捕获服务名：终止后 _served_name 即被清空
                        _served = svc.served_name
                        vllm_stopped = svc.stop(timeout_s=2.0)
                        if vllm_stopped:
                            # 台账记真实服务名（批1 修复 2026-09-10）：
                            # 原硬编码旧默认模型名 qwen3-vl-8b-awq(vllm)，
                            # 默认模型已换代 qwen35-9b-w4a16，事件日志/
                            # 统计口径失真
                            freed_models.append(
                                f"{_served}(vllm)" if _served
                                else "vllm-subprocess")
                            log.info("vLLM 子进程按需终止(→%s)供显存: %dms",
                                     target,
                                     round((time.monotonic() - t_vllm) * 1000))
                            # ADR-003 P3 验收①（面板一致性）：子进程已死
                            # 而台账/引擎态还挂着 → 模型面板 stale。经
                            # 正常 unload_model 链路同步（幂等：条目不
                            # 存在或进程已死时均为安全 no-op）
                            if _served:
                                try:
                                    self.unload_model(_served)
                                except Exception as exc:  # noqa: BLE001
                                    log.debug("vLLM 台账同步跳过: %s", exc)
                except Exception as exc:  # noqa: BLE001 - vLLM 释放失败不阻断
                    log.warning("vLLM 子进程释放异常: %s", exc)
            else:
                try:
                    from ...engines.vllm_service import get_vllm_service
                    svc = get_vllm_service()
                    if svc.is_running() or svc.is_booting():
                        vllm_kept_hot = True
                        log.info("vLLM 子进程热备保留(→%s): 复用已加载 worker"
                                 "免二次冷启动", target)
                except Exception:  # noqa: BLE001
                    pass
        elif "dialog" in locked_cats:
            # 同上：跳过释放记账也用真实服务名（批1 修复 2026-09-10）
            _skip_name = "vllm-subprocess"
            try:
                from ...engines.vllm_service import (
                    get_vllm_service as _get_vllm_svc,
                )
                _svc = _get_vllm_svc()
                if _svc.served_name:
                    _skip_name = f"{_svc.served_name}(vllm)"
            except Exception:  # noqa: BLE001 - 查询失败用中性名
                pass
            skipped.append(_skip_name)
            log.info("vLLM 子进程跳过释放（dialog 功能锁持有中）")

        # ComfyUI 子进程（H3 视频 + ComfyUI 绘画共用，2026-08-31 治理）：
        # 仅卸载权重（/free）释放不了进程的 CUDA context（数百 MB 显存）
        # 与 torch 常驻（GB 级 RAM）——16GB 卡上对目标模块就是抢占。
        # 目标为对话（vLLM 需 ~12GB）或训练（GPU 全占）→ 直接杀进程；
        # 目标为绘画/视频（本模块在用）→ 保留；轻量切换（设置/日志等）
        # → 保留热备，交给空闲自动关闭兜底（comfy_proc 默认 300s）。
        if target in ("dialog", "training"):
            # 异卡不杀（批1）：ComfyUI 不在目标卡时无需终止
            # （单卡下 paint/target 恒同卡，与历史一致）
            _comfy_same_card = True
            try:
                from ...engines.gpu_domains import resolve_feature_device
                _comfy_same_card = (
                    resolve_feature_device("paint") == target_device)
            except Exception:  # noqa: BLE001
                pass
            if _comfy_same_card:
                # 视频任务进行中绝不杀 ComfyUI（2026-09-06 颗粒级实测
                # 事故：training 切换在 H3 任务运行中杀掉其执行引擎，
                # 任务悬挂 5 分钟靠手动取消才解锁；对话/小说路径已有
                # 「视频生成中」拒绝，此处补齐同款守卫——跳过并在
                # skipped 里如实记账，训练侧由显存准入闸诚实拒绝）
                _video_running = False
                try:
                    from ...middleware.feature_lock import get_feature_lock
                    _video_running = (
                        get_feature_lock().active_feature == "video_gen")
                except Exception:  # noqa: BLE001 - 锁查询失败不阻断释放
                    pass
                if _video_running:
                    skipped.append("comfyui-subprocess(video_gen 运行中)")
                    log.info("ComfyUI 子进程跳过释放（video_gen 功能锁持有"
                             "中，运行中视频任务的执行引擎）")
                else:
                    try:
                        from ..inference.comfy_proc import get_comfy_proc
                        proc_mgr = get_comfy_proc()
                        if proc_mgr.poll() is None:
                            t_comfy = time.monotonic()
                            proc_mgr.shutdown()
                            freed_models.append("comfyui-subprocess")
                            log.info("ComfyUI 子进程按需终止(→%s)供显存: %dms",
                                     target, round((time.monotonic() - t_comfy) * 1000))
                    except Exception as exc:  # noqa: BLE001 - 释放失败不阻断
                        log.warning("ComfyUI 子进程释放异常: %s", exc)

        duration_ms = round((time.monotonic() - t0) * 1000)
        if freed_models or not vllm_kept_hot:
            log.info(
                "模块资源释放(→%s): 卸载 %d 个模型释放 %.1fGB 显存, "
                "耗时 %dms%s",
                target, len(freed_models), freed_vram, duration_ms,
                "" if completed else "（超预算部分跳过）")
        try:  # 大白话事件：模块切换资源释放（2026-08-21 日志可视化）
            from ..event_log import log_event
            _names = "、".join(freed_models[:4])
            if _names or vllm_kept_hot:
                if freed_models:
                    _msg = (f"你切到了新功能，已自动腾出显存：卸载了 "
                            f"{len(freed_models)} 个其他模块的模型（{_names}"
                            f"{'等' if len(freed_models) > 4 else ''}），"
                            f"释放约 {freed_vram:.1f}GB 显存，"
                            f"用了 {duration_ms / 1000:.1f} 秒"
                            + ("。有个别模型因为正在干活先保留了"
                               if skipped else ""))
                else:
                    _msg = (f"切换到了 {target}（不占显存），AI 对话引擎保持"
                            "后台热备：切回对话时不用重新加载，秒回。")
                log_event(
                    "models", "module_released", _msg,
                    level="success" if (completed or vllm_kept_hot)
                    else "warning",
                    detail=(f"target={target}, models={freed_models}, "
                            f"skipped={skipped}, vllm_stopped={vllm_stopped}, "
                            f"vllm_kept_hot={vllm_kept_hot}"),
                    duration_ms=duration_ms)
            elif skipped:
                log_event(
                    "models", "module_release_skipped",
                    f"切换到新功能时没有可释放的模型，但 "
                    f"{len(skipped)} 个模型因为正在运行任务被保留了",
                    level="warning",
                    detail=f"target={target}, skipped={skipped}")
        except Exception:  # noqa: BLE001 - 事件日志失败不影响释放
            pass
        return {
            "module": target,
            "completed": completed,
            "freed_models": freed_models,
            "freed_count": len(freed_models),
            "freed_vram_gb": round(freed_vram, 2),
            "skipped": skipped,
            "vllm_stopped": vllm_stopped,
            "vllm_kept_hot": vllm_kept_hot,
            "duration_ms": duration_ms,
        }

    # ═══════════════════════════════════════════════════════════════
    #  ML 预测预加载（委托 FeaturePredictor）
    # ═══════════════════════════════════════════════════════════════

    def record_feature_switch(self, from_feature: str, to_feature: str) -> None:
        """记录功能切换事件（供 ML 预测学习）。"""
        self.predictor.record_event(from_feature, to_feature)

    def predict_next_feature(self, current_feature: str | None = None) -> dict:
        """预测下一功能；概率 > 0.7 时返回预加载建议（推理 < 50ms）。"""
        result = self.predictor.predict_next(current_feature)
        # 预加载建议附上具体模型（按路由表 + 当前可用显存选择）
        if result.get("preload") and result.get("preload_model_category"):
            gpu = self.get_gpu_status()
            free = gpu["vram_free_gb"] if gpu["available"] else 0.0
            try:
                result["preload_model_id"] = self.selector.select_for_feature(
                    result["next_feature"], free)
            except Exception:  # noqa: BLE001
                result["preload_model_id"] = ""
        else:
            result["preload_model_id"] = ""
        return result

    # ═══════════════════════════════════════════════════════════════
    #  状态快照（供 /models/status）
    # ═══════════════════════════════════════════════════════════════

    def get_status(self) -> dict:
        """模型管理全景状态：GPU + 已加载 + 互斥 + 预测器。"""
        try:
            from ...middleware.feature_lock import get_feature_lock
            lock_status = get_feature_lock().status()
        except Exception:  # noqa: BLE001
            lock_status = {"active_feature": None, "held_seconds": 0, "task_id": None}
        return {
            "gpu": self.get_gpu_status(),
            "loaded_models": self.get_loaded_models(),
            "feature_lock": lock_status,
            "blocked_features": self.get_blocked_features(),
            "predictor": self.predictor.get_stats(),
            "downloaded_models": sorted(self.scan_downloaded_models().keys()),
        }


# ── 模块级单例 ──────────────────────────────────────────────────
_manager_instance: ModelManager | None = None
_singleton_lock = threading.Lock()


def _wire_gpu_budget_ledger(mgr: ModelManager) -> None:
    """台账源注入显存统一账本（显存调度机制批2，2026-09-10）。

    gpu_budget 不反向依赖本模块（避免循环导入与冷启动开销），
    由本侧注入回调：device → 该卡在载模型显存合计（GB）——
    账本的 ledger_gb/external_gb 对账口径由此而来。
    """

    def _ledger(device: int) -> float:
        with mgr._loaded_lock:
            return sum(
                float(e.get("vram_gb", 0.0))
                for e in mgr._loaded.values()
                if int(e.get("device", 0)) == int(device))

    try:
        from ..inference.gpu_budget import get_gpu_budget
        get_gpu_budget().set_ledger_source(_ledger)
    except Exception as exc:  # noqa: BLE001 - 注入失败账本按无台账降级
        log.debug("gpu_budget 台账源注入跳过: %s", exc)


def get_model_manager() -> ModelManager:
    """获取 ModelManager 全局单例。"""
    global _manager_instance
    if _manager_instance is None:
        with _singleton_lock:
            if _manager_instance is None:
                _manager_instance = ModelManager()
                _wire_gpu_budget_ledger(_manager_instance)
    return _manager_instance
