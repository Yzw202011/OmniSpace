"""OmniSpace AI v2.1 任务分发模块（规格 §5.1 分发层）。

负责执行具体的调度动作（全部接线真实执行体，非记账空操作）：
  - migrate_to_cpu: 将指定层迁移到 CPU（置 ModelManager CPU offload 标记，
    供引擎下次加载参考；另附缓存显存释放）
  - migrate_to_gpu: 将指定任务迁回 GPU（清除 CPU offload 标记）
  - degrade: 降低精度并写入 ModelManager 精度策略（引擎加载时读取）
  - preload: 预加载模型（真实接线 ModelManager.ensure_loaded；
    功能锁占用期间跳过，避免与活跃任务争抢显存）
  - compress_cache: 压缩缓存（真实接线 ModelManager L2 缓存 +
    engines.MemoryManager 不活跃块压缩）
  - force_unload: 强制卸载模型（真实接线 ModelManager.unload_model，
    按 功能<->类别 映射保留指定功能）

硬件安全（审计 P0-1）：调度执行层不再是空操作。preload / force_unload
直接调用 ModelManager 契约；ModelManager 内部做显存检查与驱逐，
任何失败仅记录日志，不向调度循环抛异常。
"""

from __future__ import annotations

import gc
import logging
from typing import Any

from ...config import CACHE_COMPRESSION

logger = logging.getLogger("omnispace.scheduler.dispatcher")


def _release_cached_memory() -> None:
    """即时释放缓存内存：多轮 gc + 清空 CUDA 缓存（容错，不抛异常）。"""
    for _ in range(2):
        gc.collect()
    try:
        import torch  # type: ignore
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
    except Exception as exc:  # noqa: BLE001 - torch 缺失/CUDA 异常均忽略
        logger.debug("CUDA 缓存清理跳过: %s", exc)


# 功能名 -> 模型类别（ModelManager.ensure_loaded 的 category 入参）
# 与 predictor.FEATURE_MODEL_CATEGORY 口径对齐（manga 复用视频模型）
_FEATURE_TO_CATEGORY = {
    "dialog": "dialog",
    "paint": "vision",
    "video": "video",
    "video_gen": "video",
    "manga": "video",
    "voice": "voice",
    "training": "dialog",
}

# 模型类别 -> 功能名（ModelManager._loaded 记账 category 的归一）
_CATEGORY_TO_FEATURE = {
    "dialog": "dialog", "language": "dialog",
    "vision": "paint", "paint": "paint", "image": "paint",
    "video": "video_gen", "video_gen": "video_gen",
    "training": "training",
}


class TaskDispatcher:
    """任务分发器——执行具体的资源调度动作。"""

    def __init__(self) -> None:
        # 当前在 GPU 上的层
        self._gpu_layers: list[str] = []
        # 当前在 CPU 上的层
        self._cpu_layers: list[str] = []
        # 当前精度
        self._current_precision: str = "fp16"
        # 已预加载的模型
        self._preloaded: list[str] = []
        # 是否已压缩缓存
        self._cache_compressed: bool = False

    # ── 真实执行体接线 ────────────────────────────────────────────

    @staticmethod
    def _get_model_manager() -> Any:
        """容错获取 ModelManager 单例（不可用时返回 None，调度动作降级为记账）。"""
        try:
            from ..model_manager import get_model_manager
            return get_model_manager()
        except Exception as exc:  # noqa: BLE001
            logger.debug("ModelManager 不可用，调度动作降级为记账: %s", exc)
            return None

    @staticmethod
    def _feature_lock_active() -> bool:
        """检测是否有重量级功能锁被持有（有则预加载会让行，避免争抢显存）。"""
        try:
            from ...middleware.feature_lock import get_feature_lock
            return get_feature_lock().active_feature is not None
        except Exception as exc:  # noqa: BLE001
            logger.debug("功能锁状态探测失败（视为空闲）: %s", exc)
            return False

    # ── 层迁移 ──────────────────────────────────────────────────

    def migrate_to_cpu(self, layers: list[str]) -> None:
        """将指定计算层从 GPU 迁移到 CPU。

        规格 §5.1: 当 GPU 显存压力时，将后处理、VAE 解码等
        非关键层卸载到 CPU 执行，释放显存给核心推理。

        说明：当前推理引擎（transformers/diffusers 整模型加载）不支持
        运行中单层迁移，本方法将调度意图写入 ModelManager 的 CPU offload
        标记，引擎下次加载时据此启用 offload；真实显存保护由
        force_unload/degrade 承担。记账后附带一次缓存显存释放，
        回收可立即归还驱动的部分。

        Args:
            layers: 需要迁移的层名列表，如 ["vae_decode", "postprocess"]
        """
        # 功能锁保护（2026-08-22 e2e OOM 教训）：生成任务运行期间，
        # 本方法附带的 gc×2 + synchronize + empty_cache 会清空
        # allocator 缓存块，去噪下一步大块分配需重新 cudaMalloc，
        # 碎片化下可复用块失效，推向 sysmem fallback（28.13GiB
        # allocated 事故链一环）。整体跳过：记账 + offload 标记 +
        # 缓存释放同进同退，保持与 ModelManager 状态同步。
        if self._feature_lock_active():
            logger.info("功能锁占用中，跳过层迁移: %s", layers)
            return
        for layer in layers:
            if layer in self._gpu_layers:
                self._gpu_layers.remove(layer)
            if layer not in self._cpu_layers:
                self._cpu_layers.append(layer)
        # 真实接线：写入 CPU offload 策略标记
        mgr = self._get_model_manager()
        if mgr is not None:
            try:
                mgr.set_cpu_offload(True, layers=list(self._cpu_layers))
            except Exception as exc:  # noqa: BLE001
                logger.debug("CPU offload 标记写入失败: %s", exc)
        _release_cached_memory()
        logger.info(
            "迁移 %d 层到 CPU (GPU 层: %d, CPU 层: %d, offload 标记已置位)",
            len(layers),
            len(self._gpu_layers),
            len(self._cpu_layers),
        )

    def migrate_to_gpu(self, tasks: list[str]) -> None:
        """将指定任务从 CPU 迁移回 GPU。

        当 GPU 资源恢复时，将关键计算迁回 GPU 以提升性能；
        全部迁回后清除 ModelManager 的 CPU offload 标记。

        Args:
            tasks: 需要迁移的任务/层名列表
        """
        for task in tasks:
            if task in self._cpu_layers:
                self._cpu_layers.remove(task)
            if task not in self._gpu_layers:
                self._gpu_layers.append(task)
        # 真实接线：无残留 CPU 层时清除 offload 标记
        if not self._cpu_layers:
            mgr = self._get_model_manager()
            if mgr is not None:
                try:
                    mgr.set_cpu_offload(False)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("CPU offload 标记清除失败: %s", exc)
        logger.info(
            "迁移 %d 任务到 GPU (GPU 层: %d, CPU 层: %d)",
            len(tasks),
            len(self._gpu_layers),
            len(self._cpu_layers),
        )

    # ── 精度降级 ────────────────────────────────────────────────

    def degrade(self, precision: str = "int4") -> None:
        """降低推理精度以减少显存/内存占用，并写入 ModelManager 精度策略。

        精度阶梯（从高到低）:
          fp16 -> bf16 -> fp8 -> int8 -> int4

        Args:
            precision: 目标精度
        """
        # 功能锁保护（同 migrate_to_cpu，2026-08-22）。边界：策略仅在
        # 模式切换时执行，持锁期间跳过 = 该次降级意图丢弃（当前精度
        # 标记无消费方无实害；未来接线消费方时需锁释放后补执行）。
        if self._feature_lock_active():
            logger.info("功能锁占用中，跳过精度降级: %s", precision)
            return
        precision_order = ["fp32", "fp16", "bf16", "fp8", "int8", "int4"]
        old = self._current_precision
        changed = False
        try:
            old_idx = precision_order.index(old)
            new_idx = precision_order.index(precision)
            if new_idx > old_idx:
                self._current_precision = precision
                changed = True
                logger.info("精度降级: %s -> %s", old, precision)
            else:
                logger.debug("精度不变: %s (请求 %s 不低于当前)", old, precision)
        except ValueError:
            self._current_precision = precision
            changed = True
            logger.info("精度设置: %s", precision)
        # 真实接线：精度策略写入 ModelManager，供引擎加载时参考
        if changed:
            mgr = self._get_model_manager()
            if mgr is not None:
                try:
                    mgr.set_precision_policy(self._current_precision)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("精度策略写入失败: %s", exc)

    @property
    def current_precision(self) -> str:
        """当前推理精度。"""
        return self._current_precision

    # ── 预加载 ──────────────────────────────────────────────────

    def preload(self, features: list[str]) -> None:
        """预加载指定功能模块的模型到内存（真实接线 ModelManager）。

        在 ALL_IDLE 模式下预加载常用模型，减少用户等待时间。
        硬件安全：任一重量级功能锁被持有时整体跳过预加载——
        活跃生成任务期间抢显存会导致正在运行的推理 OOM。

        Args:
            features: 功能列表，如 ["dialog", "paint", "video"]
        """
        mgr = self._get_model_manager()
        if mgr is not None and self._feature_lock_active():
            logger.info("功能锁占用中，跳过预加载: %s", features)
            return
        for feature in features:
            if feature in self._preloaded:
                continue
            model_id = ""
            loaded_ok = False
            if mgr is not None:
                try:
                    category = _FEATURE_TO_CATEGORY.get(feature, feature)
                    gpu = mgr.get_gpu_status()
                    free = gpu["vram_free_gb"] if gpu.get("available") else 0.0
                    # 按路由表 + 当前可用显存选出最合适的模型
                    model_id = mgr.selector.select_for_feature(feature, free)
                    if model_id:
                        loaded_ok = bool(mgr.ensure_loaded(category, model_id))
                        if not loaded_ok:
                            logger.info("预加载未就绪: %s (%s)",
                                        model_id, mgr.last_error or "未知原因")
                    else:
                        logger.debug("功能 %s 无可用模型候选，仅记账", feature)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("预加载异常 (%s): %s", feature, exc)
            self._preloaded.append(feature)
            if loaded_ok:
                logger.info("预加载模型: %s -> %s", feature, model_id)
            else:
                logger.info("预加载模型: %s（记账）", feature)

    # ── 缓存压缩 ────────────────────────────────────────────────

    def compress_cache(self) -> None:
        """压缩模型缓存以释放内存（真实接线缓存/内存压缩接口）。

        规格 §5.1: MEMORY_PRESSURE 模式下使用 LZ4 压缩不活跃的缓存。
        真实执行：
          1) ModelManager L2 模型缓存 compress_inactive()
          2) engines.MemoryManager 不活跃内存块 compress_inactive()
        """
        if not self._cache_compressed:
            self._cache_compressed = True
            mgr = self._get_model_manager()
            if mgr is not None:
                try:
                    n = mgr.cache.compress_inactive()
                    if n:
                        logger.info("模型 L2 缓存压缩: %d 项", n)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("模型缓存压缩失败: %s", exc)
            try:
                from ...engines.memory_manager import get_memory_manager
                n2 = get_memory_manager().compress_inactive()
                if n2:
                    logger.info("内存不活跃块压缩: %d 项", n2)
            except Exception as exc:  # noqa: BLE001
                logger.debug("内存管理器压缩不可用: %s", exc)
            logger.info("缓存已压缩 (算法: %s)", CACHE_COMPRESSION)

    def decompress_cache(self) -> None:
        """解压缓存（资源恢复时）。

        缓存条目在下次访问时惰性解压（ModelCache.get 自动 decompress），
        此处仅翻转状态标记。
        """
        if self._cache_compressed:
            self._cache_compressed = False
            logger.info("缓存已解压（条目访问时惰性解压）")

    # ── 强制卸载 ────────────────────────────────────────────────

    def force_unload(self, except_features: list[str] | None = None) -> None:
        """强制卸载模型，保留指定功能（真实接线 ModelManager.unload_model）。

        规格 §5.1: ALL_TENSE 模式下卸载所有非关键模型，
        仅保留最低限度的功能运行。

        Args:
            except_features: 保留的功能列表
        """
        keep = set(except_features or [])
        # 2026-08-22 VACE 误卸事故：功能锁活动期间，持锁功能的模型是
        # 当前任务的工作集（如 video_gen 生成中的 VACE——ALL_TENSE
        # 强卸致任务回退弱能力 LTX 管线，画面质量投诉根因）。
        # 持锁功能自动纳入保留，强卸只回收真正空闲的模型。
        try:
            from ...middleware.feature_lock import get_feature_lock
            holder = get_feature_lock().active_feature
            if holder:
                keep.add(holder)
        except Exception:  # noqa: BLE001 - 锁查询失败不阻断卸载
            pass
        keep_cats = {_FEATURE_TO_CATEGORY.get(f, f) for f in keep}
        mgr = self._get_model_manager()
        if mgr is not None:
            try:
                for entry in mgr.get_loaded_models():
                    cat = entry.get("category", "")
                    feat = _CATEGORY_TO_FEATURE.get(cat, cat)
                    if feat in keep or cat in keep_cats:
                        continue
                    try:
                        if mgr.unload_model(entry["model_id"]):
                            logger.warning("强制卸载模型: %s (category=%s)",
                                           entry["model_id"], cat)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("强制卸载失败 (%s): %s",
                                       entry.get("model_id"), exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("强制卸载执行异常: %s", exc)
        # 记账同步：移除未保留的预加载记录
        unloaded = []
        for feature in list(self._preloaded):
            if feature not in keep:
                unloaded.append(feature)
                self._preloaded.remove(feature)
        if unloaded:
            logger.warning("强制卸载模型: %s (保留: %s)", unloaded, list(keep))

    # ── 状态查询 ────────────────────────────────────────────────

    def get_state(self) -> dict:
        """返回分发器当前状态。"""
        return {
            "gpu_layers": list(self._gpu_layers),
            "cpu_layers": list(self._cpu_layers),
            "precision": self._current_precision,
            "preloaded": list(self._preloaded),
            "cache_compressed": self._cache_compressed,
        }
