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
import threading
import time
from pathlib import Path
from typing import Any, Optional, List, Dict

from ...config import MODELS_DIR
from ...data.models import (DIALOG_ROUTING_TABLE, PAINT_ROUTING_TABLE,
                            VIDEO_ROUTING_TABLE, ModelCategory, ModelInfo)

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
}

# 已知模型的磁盘相对路径提示（MODELS_DIR 下），磁盘扫描的权威补充
_MODEL_PATH_HINTS: dict[str, str] = {
    "qwen3-vl-4b":   "qwen3-vl-4b",
    "qwen2-vl-2b":   "qwen2-vl-2b",
    # modelscope 嵌套快照布局（两层通用扫描无法命中）
    "qwen3-vl-8b":   "qwen3-vl-8b/models/Qwen--Qwen3-VL-8B-Instruct/snapshots/master",
    "sdxl-base-1.0": "paint/sdxl-base-1.0",
    "bge-large-zh":  "embed/bge-large-zh",
}

# 模型目录的“已下载”判定特征文件
_DIR_SIGNATURES = (
    "config.json", "model_index.json", "model.safetensors.index.json",
    "model.safetensors", "pytorch_model.bin", "modules.json",
)


class ModelManager:
    """模型管理器单例——管理模型的全生命周期与 GPU 协同调度。

    规格 §5.4: 模型导入时自动分类、SHA256 校验、注册到数据库。
    规格 §5.3: 根据硬件状态自动选择最合适的模型。
    规格 §2.3: 模型加载互斥规则（显存不足 → 驱逐最低优先级 → 加载）。
    """

    _instance: Optional["ModelManager"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "ModelManager":
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
        from .importer import ModelImporter
        from .validator import ModelValidator
        from .classifier import ModelClassifier
        from .selector import ModelSelector
        from .cache import ModelCache
        from .predictor import FeaturePredictor

        self.importer = ModelImporter()
        self.validator = ModelValidator()
        self.classifier = ModelClassifier()
        self.selector = ModelSelector()
        self.cache = ModelCache()
        self.predictor = FeaturePredictor()

        # 已注册模型表（运行时内存索引）
        self._models: Dict[str, ModelInfo] = {}

        # 运行时加载状态: model_id -> {category, path, vram_gb, priority,
        #                                loaded_at, engine}
        self._loaded: Dict[str, dict] = {}
        self._loaded_lock = threading.Lock()
        self._reserved_vram_gb = 0.0        # allocate_memory 逻辑预留量
        # 审计 P0-5：_reserved_vram_gb 读改写必须原子，专用锁全程保护
        # （allocate_memory / unload_model / ensure_loaded 回滚三处共用）。
        # 必须用 RLock：allocate_memory 持锁期间会经 evict_lowest_priority
        # 嵌套调用 unload_model（其内部再次获取本锁扣减预留量），
        # 普通 Lock 不可重入会导致同线程自死锁（绘画首载卡死根因）。
        self._vram_lock = threading.RLock()
        self._engines: Dict[str, Any] = {}  # category -> 引擎实例（懒加载）

        # 审计 P0-1：调度策略真实落点——CPU offload 标记与精度策略，
        # 由 dispatcher.migrate_to_cpu/degrade 写入，引擎加载时读取参考。
        self._cpu_offload_enabled = False
        self._cpu_offload_layers: List[str] = []
        self._precision_policy = "fp16"
        self._policy_lock = threading.Lock()

        # 最近一次 ensure_loaded 失败原因（契约返回 bool，细节经此暴露）
        self.last_error: str = ""

        # NVML 惰性初始化状态
        self._nvml_ready: Optional[bool] = None

        # 磁盘扫描缓存（mtime 粗粒度失效）
        self._disk_scan_ts = 0.0
        self._disk_scan_cache: Dict[str, dict] = {}

    # ═══════════════════════════════════════════════════════════════
    #  既有注册表接口（保持原契约）
    # ═══════════════════════════════════════════════════════════════

    def register(self, model_info: ModelInfo) -> None:
        """注册模型到管理器。"""
        self._models[model_info.id] = model_info

    def get_model(self, model_id: str) -> Optional[ModelInfo]:
        """按 ID 获取模型信息。"""
        return self._models.get(model_id)

    def list_models(self, category: Optional[ModelCategory] = None) -> List[ModelInfo]:
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

    def get_gpu_status(self) -> dict:
        """返回 GPU 状态：已用/总量/利用率（无 GPU 时降级为 zeros + available=False）。"""
        status = {
            "available": False, "gpu_name": "", "vendor": "none",
            "vram_total_mb": 0, "vram_used_mb": 0, "vram_free_mb": 0,
            "vram_total_gb": 0.0, "vram_used_gb": 0.0, "vram_free_gb": 0.0,
            "util_percent": 0.0, "temp_celsius": 0.0,
            "reserved_vram_gb": round(self._reserved_vram_gb, 2),
        }
        if self._ensure_nvml():
            try:
                handle = _pynvml.nvmlDeviceGetHandleByIndex(0)
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
                    total = _torch.cuda.get_device_properties(0).total_memory
                    used = _torch.cuda.memory_allocated(0)
                    status.update({
                        "available": True,
                        "gpu_name": _torch.cuda.get_device_name(0),
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

    def scan_downloaded_models(self, force: bool = False) -> Dict[str, dict]:
        """扫描 MODELS_DIR，返回 {model_id: {path, size_gb, downloaded}}。

        合并策略：显式路径提示（_MODEL_PATH_HINTS）+ 两层目录特征扫描。
        结果缓存 30 秒避免高频 IO。
        """
        if not force and (time.time() - self._disk_scan_ts) < 30.0 \
                and self._disk_scan_cache:
            return dict(self._disk_scan_cache)

        found: Dict[str, dict] = {}
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
                        if grand.is_dir() and self._looks_like_model_dir(grand):
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

        self._disk_scan_cache = found
        self._disk_scan_ts = time.time()
        return dict(found)

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

    def resolve_model_path(self, model_id: str) -> Optional[str]:
        """解析模型的本地路径（注册表 file_path → 磁盘扫描），未下载返回 None。"""
        info = self._models.get(model_id)
        if info is not None and info.file_path and Path(info.file_path).exists():
            return info.file_path
        scanned = self.scan_downloaded_models()
        hit = scanned.get(model_id)
        return hit["path"] if hit else None

    def estimate_vram_gb(self, model_id: str, category: str = "") -> float:
        """估计加载所需显存：引擎候选表 → 路由表 min_vram_gb → 磁盘大小 × 1.2 → 默认 4GB。

        引擎候选表优先：模型目录常含 fp32/fp16 双份权重与单文件兜底
        （如 sdxl-base-1.0 磁盘 25.9GB 实需约 7GB），磁盘扫描会严重高估，
        导致 allocate_memory 永远失败、驱逐记账失真。
        """
        try:
            from ..inference.paint_engine import PAINT_MODEL_CANDIDATES
            from ..inference.dialog_engine import DIALOG_MODEL_CANDIDATES
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

    def check_mutual_exclusion(self, target: str) -> List[str]:
        """按规格 §2.1 互斥矩阵返回 target 活跃时需要置灰的功能列表。

        manga/video/draw 等别名归一化到 feature_lock 语义
        （dialog/paint/video_gen/training）。
        """
        return list(MUTUAL_EXCLUSION_MATRIX.get(self._normalize_feature(target), []))

    def get_blocked_features(self) -> List[str]:
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
                        layers: Optional[List[str]] = None) -> None:
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

    def evict_lowest_priority(self) -> bool:
        """驱逐已加载模型中优先级最低者（平级取最久未用）。

        Returns:
            True 表示成功驱逐一个模型
        """
        with self._loaded_lock:
            if not self._loaded:
                return False
            victim_id, victim = min(
                self._loaded.items(),
                key=lambda kv: (kv[1].get("priority", 0), kv[1].get("loaded_at", 0.0)),
            )
        log.info("驱逐最低优先级模型: %s (priority=%s)",
                 victim_id, victim.get("priority"))
        return self.unload_model(victim_id)

    # ═══════════════════════════════════════════════════════════════
    #  引擎接线（try import 容错）
    # ═══════════════════════════════════════════════════════════════

    def _get_engine(self, category: str) -> Any:
        """按类别获取推理引擎单例（懒加载 + 容错）。"""
        cat = (category or "").strip().lower()
        if cat in self._engines:
            return self._engines[cat]

        engine = None
        if cat in ("dialog", "language"):
            mod = _try_import("backend.services.inference.dialog_engine")
            if mod is not None:
                try:
                    getter = getattr(mod, "get_dialog_engine", None)
                    engine = getter() if callable(getter) else mod.DialogEngine()
                except Exception as exc:  # noqa: BLE001
                    log.warning("对话引擎实例化失败: %s", exc)
        elif cat in ("vision", "paint", "image"):
            # paint_engine 可能损坏/缺失 —— 容错导入
            mod = _try_import("backend.services.inference.paint_engine")
            if mod is not None:
                try:
                    getter = getattr(mod, "get_paint_engine", None)
                    engine = getter() if callable(getter) else mod.PaintEngine()
                except Exception as exc:  # noqa: BLE001
                    log.warning("绘画引擎实例化失败: %s", exc)
        elif cat in ("video", "video_gen"):
            mod = _try_import("backend.services.inference.video_engine")
            if mod is not None:
                try:
                    # 优先模块级单例 getter（与 manga 视频工作线程同一
                    # 实例），否则卸载/记账会作用于另一空实例而真实管线
                    # 引用残留（对齐 dialog/paint 引擎接线方式）
                    getter = getattr(mod, "get_video_engine", None)
                    engine = getter() if callable(getter) else mod.VideoEngine()
                except Exception as exc:  # noqa: BLE001
                    log.warning("视频引擎实例化失败: %s", exc)
        # auxiliary/voice/embedding 等无 GPU 引擎类别 → None（由调用方本地加载）

        self._engines[cat] = engine
        return engine

    # ═══════════════════════════════════════════════════════════════
    #  加载 / 卸载主流程
    # ═══════════════════════════════════════════════════════════════

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
            self.last_error = f"引擎加载失败: {model_id}"
            log.warning("ensure_loaded 引擎加载失败: %s", model_id)
            return False

        with self._loaded_lock:
            self._loaded[model_id] = {
                "category": cat, "path": path, "vram_gb": required,
                "priority": _EVICTION_PRIORITY.get(cat, 0),
                "loaded_at": time.time(),
                "engine": type(engine).__name__,
                "backend": backend_tag,
                "load_seconds": round(time.time() - t0, 2),
            }

        # 审计 P0-2 接线 engines.vram_manager / memory_manager：
        # 真实跟踪显存分配 + 注册内存块（供 MEMORY_PRESSURE 策略压缩）。
        try:
            from ...engines.vram_manager import get_vram_manager
            get_vram_manager().track_alloc(model_id, required * 1024.0)
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

    def unload_model(self, model_id: str) -> bool:
        """从 GPU 卸载模型并释放记账显存。"""
        with self._loaded_lock:
            entry = self._loaded.pop(model_id, None)
        if entry is None:
            return False

        # 经 _get_engine 解析（懒加载接线）：引擎类别尚未实例化时现场
        # 获取共享单例，否则自动装载登记的模型会出现「台账已清、真实
        # 管线引用未释放」的脱节
        engine = self._get_engine(entry.get("category", ""))
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

    def get_loaded_models(self) -> List[dict]:
        """返回当前已加载模型列表（含类别/路径/显存/优先级/加载时间）。"""
        with self._loaded_lock:
            return [dict(model_id=mid, **info) for mid, info in self._loaded.items()]

    # ═══════════════════════════════════════════════════════════════
    #  ML 预测预加载（委托 FeaturePredictor）
    # ═══════════════════════════════════════════════════════════════

    def record_feature_switch(self, from_feature: str, to_feature: str) -> None:
        """记录功能切换事件（供 ML 预测学习）。"""
        self.predictor.record_event(from_feature, to_feature)

    def predict_next_feature(self, current_feature: Optional[str] = None) -> dict:
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
_manager_instance: Optional[ModelManager] = None
_singleton_lock = threading.Lock()


def get_model_manager() -> ModelManager:
    """获取 ModelManager 全局单例。"""
    global _manager_instance
    if _manager_instance is None:
        with _singleton_lock:
            if _manager_instance is None:
                _manager_instance = ModelManager()
    return _manager_instance
