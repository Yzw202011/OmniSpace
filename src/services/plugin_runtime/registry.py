"""插件运行时编排核心：登记表 + 生命周期 + invoke 通道。

三道运行闸（方案 v1.2 修正案 8/10，深验证据在案）：
1. **登记闸**：未登记的插件名一律拒绝（登记表是治理便利非沙箱，
   信任模型见 base.py）；
2. **RAM 闸**：invoke 前 psutil 可用内存余量 < MIN_FREE_RAM_GB 拒绝
   （POC2-A 实测 1080p 渲染峰值 1.64GB，RAM 静默死亡历史雷）；
3. **插件锁**：每插件一把 RLock——渲染期间锁住权重变更/卸载
   （POC2-B 实测并发渲染 600/600 零崩溃，但权重热换是脏快照）。

软超时（诚实声明）：asyncio.wait_for 超时后工作线程**无法强杀**
（Python 线程天性），超时即把插件标记 faulty 拒绝后续调度，
跑飞的线程任其自然结束——死循环/原生崩溃的硬隔离属子进程档（P3+）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from ...config import ROOT_DIR
from ..offload import run_blocking
from .base import ExpertPlugin, PluginContext
from .bridge import PluginEventBridge
from .loader import (
    PluginLoadError,
    find_plugin_classes,
    frames_to_png_files,
    load_plugin_module,
    read_cutemamen_pkg,
)

logger = logging.getLogger(__name__)

# 插件输出根目录（帧 PNG 落盘；P2 由 ffmpeg 合成后归 video_tasks）
OUTPUT_ROOT = ROOT_DIR / "data" / "plugins" / "output"

# RAM 闸阈值：可用内存低于此值拒绝 invoke（P4 治理时迁 config.yaml）
MIN_FREE_RAM_GB = 3.0

# 默认软超时（1080p 单镜实测 10.4s，12 镜串行留 2 分钟级余量）
DEFAULT_INVOKE_TIMEOUT_S = 180.0

# 出厂预登记（repo_curated = 已过仓库逐行审查档；PR#2 插件实审在案）
_SEED_PLUGINS: dict[str, dict[str, str]] = {
    "video-making": {
        # 单一源码：src/cutemamen/video_making.py（内核直连/宿主 exec 双路导入，
        # 旧 plugin/video_making.py 评审副本已删除）
        "source_py": "src/cutemamen/video_making.py",
        "pkg": "plugin/VideoMaking.CuteMamen",
        "trust": "repo_curated",
    },
}

STATE_UNLOADED = "unloaded"
STATE_LOADED = "loaded"
STATE_FAULTY = "faulty"


@dataclass
class _PluginEntry:
    """单个插件的登记与运行态（lock 保护跨字段一致性）。"""
    name: str
    source_py: Path
    pkg_path: Path | None
    trust: str
    enabled: bool = True
    state: str = STATE_UNLOADED
    instance: ExpertPlugin | None = None
    manifest: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock)


def _display_path(path: Path) -> str:
    """展示路径：仓库内相对化，仓库外（用户自装任意路径）保绝对。"""
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:  # 跨盘/仓库外路径无相对关系
        return str(path)


class PluginRuntimeError(RuntimeError):
    """运行时拒绝/失败（code 供 API 层翻译为语义错误码）。"""

    def __init__(self, code: str, message: str, suggestion: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion


class PluginRuntime:
    """插件运行时（进程内单例；invoke 走 offload 线程池）。"""

    def __init__(self, bridge: PluginEventBridge | None = None) -> None:
        self._bridge = bridge or PluginEventBridge()
        self._registry: dict[str, _PluginEntry] = {}
        self._global = threading.RLock()
        for name, spec in _SEED_PLUGINS.items():
            self._registry[name] = _PluginEntry(
                name=name,
                source_py=ROOT_DIR / spec["source_py"],
                pkg_path=ROOT_DIR / spec["pkg"] if spec.get("pkg") else None,
                trust=spec["trust"])

    # ── 登记（代码级入口；HTTP 暴露属 P4 设置页治理） ──────────
    def register(self, name: str, source_py: Path,
                 pkg_path: Path | None = None,
                 trust: str = "user_installed") -> None:
        """登记新插件（未登记不可加载——治理闸，非安全沙箱）。"""
        with self._global:
            if name in self._registry:
                raise PluginRuntimeError(
                    "PLUGIN_ALREADY_REGISTERED", f"插件已登记: {name}")
            self._registry[name] = _PluginEntry(
                name=name, source_py=source_py, pkg_path=pkg_path, trust=trust)

    def _entry(self, name: str) -> _PluginEntry:
        entry = self._registry.get(name)
        if entry is None:
            raise PluginRuntimeError(
                "PLUGIN_NOT_REGISTERED", f"插件未登记: {name}",
                "仅可调用已登记插件；登记入口属设置页治理（P4）")
        if not entry.enabled:
            raise PluginRuntimeError(
                "PLUGIN_DISABLED", f"插件已停用: {name}",
                "在插件清单中重新启用后再调用")
        return entry

    # ── 清单 ────────────────────────────────────────────────
    def plugins_info(self) -> list[dict[str, Any]]:
        """全部已登记插件的状态清单（展示用，轻量无副作用）。"""
        info: list[dict[str, Any]] = []
        with self._global:
            for entry in sorted(self._registry.values(),
                                key=lambda e: e.name):
                stats: dict[str, Any] = {}
                if entry.state == STATE_LOADED and entry.instance:
                    try:
                        stats = entry.instance.stats()
                    except Exception:  # noqa: BLE001 - 清单不因插件炸
                        stats = {"error": "stats() 调用失败"}
                info.append({
                    "name": entry.name, "trust": entry.trust,
                    "enabled": entry.enabled, "state": entry.state,
                    "source": str(entry.source_py.relative_to(ROOT_DIR)),
                    "capability": entry.manifest.get("capability", ""),
                    "last_error": entry.last_error, "stats": stats})
        return info

    # ── 生命周期 ────────────────────────────────────────────
    def ensure_loaded(self, name: str) -> ExpertPlugin:
        """加载并返回插件实例（幂等；未加载→自动加载，产品铁律）。"""
        entry = self._entry(name)
        with entry.lock:
            if entry.state == STATE_LOADED and entry.instance is not None:
                return entry.instance
            logger.info("插件加载开始: %s (%s)", name, entry.trust)
            try:
                module = load_plugin_module(entry.source_py)
                classes = find_plugin_classes(module)
                if not classes:
                    raise PluginLoadError("模块内无 ExpertPlugin 子类")
                cls = classes[0]
                try:
                    instance = cls()  # type: ignore[call-arg]
                except TypeError:
                    # 宿主标准：优先无参构造；插件类未提供缺省 name 时
                    # 以登记名实例化（基类 __init__ 需要 name）
                    instance = cls(name=entry.name)  # type: ignore[call-arg]
                if entry.pkg_path is not None and entry.pkg_path.is_file():
                    pkg = read_cutemamen_pkg(entry.pkg_path)
                    if hasattr(instance, "load_weights"):
                        instance.load_weights(pkg.weights, pkg.manifest)
                    instance.memory.restore(pkg.memory)
                ctx = PluginContext(
                    emit_fn=lambda topic, payload:
                        self._bridge.emit(name, topic, payload),
                    plugin_name=name)
                instance.on_load(ctx)
                entry.manifest = instance.build_manifest(
                    source=_display_path(entry.source_py),
                    trust=entry.trust, osp_version="1.0")
                entry.instance = instance
                entry.state = STATE_LOADED
                entry.last_error = ""
                logger.info("插件加载完成: %s", name)
                return instance
            except PluginLoadError as exc:
                entry.last_error = str(exc)
                entry.instance = None
                entry.state = STATE_UNLOADED  # 载入失败可重试
                raise PluginRuntimeError(
                    "PLUGIN_LOAD_FAILED", f"插件加载失败: {exc}",
                    "检查插件源码/包完整性后重试 load") from exc

    def unload(self, name: str) -> None:
        """卸载（faulty 复位通道：unload→load 即重建）。"""
        entry = self._entry(name)
        with entry.lock:
            if entry.instance is not None:
                try:
                    entry.instance.on_unload()
                except Exception:  # noqa: BLE001 - 卸载钩子炸不拦卸载
                    logger.warning("插件 on_unload 异常: %s", name,
                                   exc_info=True)
            entry.instance = None
            entry.state = STATE_UNLOADED

    # ── invoke（同步推理唯一入口 run_blocking，§3.1） ─────────
    async def invoke(self, name: str, spec: dict[str, Any],
                     save_dirname: str | None = None,
                     timeout_s: float = DEFAULT_INVOKE_TIMEOUT_S
                     ) -> dict[str, Any]:
        """执行插件任务：返回 JSON 安全摘要；帧本体只落盘不出线。

        - RAM 闸 → ensure_loaded → 插件锁内 on_think；
        - save_dirname 给定时帧序列逐帧 uint8 化写 PNG（内存红线）；
        - 软超时：超时标记 faulty（线程不可强杀，任其自然跑完）。
        """
        entry = self._entry(name)
        if entry.state == STATE_FAULTY:
            raise PluginRuntimeError(
                "PLUGIN_FAULTY", f"插件处于故障态: {name}",
                "先 unload 复位再重新 load 调用")
        avail_gb = psutil.virtual_memory().available / (1 << 30)
        if avail_gb < MIN_FREE_RAM_GB:
            raise PluginRuntimeError(
                "PLUGIN_RAM_LOW",
                f"系统可用内存不足（{avail_gb:.1f}GB < {MIN_FREE_RAM_GB}GB）",
                "关闭其他大内存任务后重试；或用更低分辨率档")
        self.ensure_loaded(name)
        event = {"topic": "invoke", "data": spec}
        # 目录名净化（纵深防御：API 层已剥，运行时层再剥一次——
        # 直调运行时的调用方不经 API 闸）
        if save_dirname:
            save_dirname = Path(save_dirname).name or "run"
        save_dir = (OUTPUT_ROOT / name / save_dirname
                    if save_dirname else None)
        try:
            result = await asyncio.wait_for(
                run_blocking(self._invoke_sync, entry, event, save_dir),
                timeout=timeout_s)
        except asyncio.TimeoutError:
            with entry.lock:
                entry.state = STATE_FAULTY
                entry.last_error = f"invoke 超时(>{timeout_s}s)"
            raise PluginRuntimeError(
                "PLUGIN_TIMEOUT", f"插件执行超时(>{timeout_s}s)：{name}",
                "插件已标记故障态；unload 后重新 load 可复位") from None
        except PluginRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - 插件异常统一收口
            with entry.lock:
                entry.state = STATE_FAULTY
                entry.last_error = str(exc)
            raise PluginRuntimeError(
                "PLUGIN_INVOKE_FAILED", f"插件执行失败: {exc}",
                "插件已标记故障态；unload 后重新 load 可复位") from exc
        return result

    def _invoke_sync(self, entry: _PluginEntry, event: dict[str, Any],
                     save_dir: Path | None) -> dict[str, Any]:
        """线程体内执行：锁内 on_think + 可选帧落盘（帧不出运行时层）。"""
        instance = entry.instance
        if instance is None:
            raise PluginRuntimeError(
                "PLUGIN_INVOKE_FAILED", "插件实例丢失（并发卸载）")
        with entry.lock:
            ctx = PluginContext(
                emit_fn=lambda topic, payload:
                    self._bridge.emit(entry.name, topic, payload),
                plugin_name=entry.name)
            raw = instance.on_think(event, ctx) or {}
        result: dict[str, Any] = {
            "summary": raw.get("summary"),
            "plan": raw.get("plan"),
        }
        frames = raw.get("frames")
        if save_dir is not None and isinstance(frames, list) and frames:
            names = frames_to_png_files(frames, save_dir)
            result["saved_files"] = names
            result["output_dir"] = str(save_dir)
        return result


# ── 进程内单例 ─────────────────────────────────────────────
_runtime: PluginRuntime | None = None
_runtime_lock = threading.Lock()


def get_plugin_runtime() -> PluginRuntime:
    """获取插件运行时单例（懒初始化）。"""
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = PluginRuntime()
        return _runtime
