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
import json
import logging
import re
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
from .sandbox import sandbox_enabled

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
        "capability": "轻量视频生成内核：关键帧+运镜曲线→帧序列（纯 numpy 零 GPU）",
    },
    "rust-coding": {
        # Rust 报错分类器（2026-09-16 登记拍板）：342 行逐行审查在案——
        # 纯 numpy 数值分类，无 IO/网络/子进程/eval；审查修复两处：
        # ①on_think 工作记忆写入加宿主形态护栏 ②推理路径相对导入补
        # 双路垫片（对齐 video_making 惯例）。读出层权重随包注入。
        "source_py": "src/cutemamen/rust_coding.py",
        "pkg": "plugin/RustCoding.CuteMamen",
        "trust": "repo_curated",
        "capability": "Rust 编译错误分类器（move/borrow/lifetime/type/ok 五类）",
    },
}

STATE_UNLOADED = "unloaded"
STATE_LOADED = "loaded"
STATE_FAULTY = "faulty"

# ── 用户插件持久化（data/ 运行时区，git 不跟踪、发行包不带） ──
USER_PLUGIN_DIR = ROOT_DIR / "data" / "plugins" / "imported"
USER_REGISTRY_PATH = ROOT_DIR / "data" / "plugins" / "user_registry.json"
FACTORY_OVERRIDE_PATH = ROOT_DIR / "data" / "plugins" / "factory_overrides.json"
MAX_USER_PLUGINS = 32
MAX_SOURCE_BYTES = 256 * 1024
# 插件名规则（manifest.name 与登记名共用；防路径花活）
PLUGIN_NAME_RE = r"^[a-z0-9][a-z0-9_-]{0,63}$"
# 用户档集合（repo_curated 之外都是用户档；user_installed 为 P1 旧别名）
_USER_TIERS = ("user_data", "user_source", "user_installed")
_TRUST_LABELS = {
    "repo_curated": "出厂·已审查",
    "user_data": "用户·纯数据",
    "user_source": "用户·含源码",
    "user_installed": "用户导入",
}


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
    imported_at: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock)


def _rel_or_abs(path: Path) -> str:
    """登记表/清单路径序列化：仓库内相对、仓库外（如测试 tmp）绝对。"""
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


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


def _passthrough_data(raw: dict[str, Any]) -> dict[str, Any]:
    """插件 on_think 结果的通用透传清洗。

    - 剔除 video 形态专用键（frames/summary/plan 已单列）；
    - ndarray → 形状/ dtype 摘要（帧本体铁律不进 JSON）；
    - numpy 标量 → Python 标量；列表截断到 32 项防膨胀；
    - 其余类型原样（str/int/float/bool/None/小 dict）。
    """
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key in ("frames", "summary", "plan"):
            continue
        out[key] = _json_summary(value)
    return out


def _json_summary(value: Any, _depth: int = 0) -> Any:
    """递归 JSON 安全化（深度/列表长度封顶）。"""
    import numpy as np
    if isinstance(value, np.ndarray):
        return {"shape": list(value.shape), "dtype": str(value.dtype)}
    if isinstance(value, (np.floating, np.integer, np.bool_)):
        return value.item()
    if isinstance(value, dict) and _depth < 6:
        return {str(k): _json_summary(v, _depth + 1)
                for k, v in list(value.items())[:64]}
    if isinstance(value, (list, tuple)):
        items = [_json_summary(v, _depth + 1) for v in value[:32]]
        if len(value) > 32:
            items.append(f"...(共 {len(value)} 项，截断)")
        return items
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


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
                trust=spec["trust"],
                manifest={"capability": spec.get("capability", "")})
        self._load_user_registry()
        self._load_factory_overrides()

    def _load_factory_overrides(self) -> None:
        """出厂插件停用覆盖（2026-09-17：此前出厂档停用重启即复活，
        UI 却提供开关——审计四轮 P2）。只记偏离默认（停用）的条目。"""
        try:
            raw = json.loads(FACTORY_OVERRIDE_PATH.read_text("utf-8"))
        except Exception:  # noqa: BLE001 - 无文件/坏文件=无覆盖
            return
        if not isinstance(raw, dict):
            return
        for name, enabled in raw.items():
            entry = self._registry.get(str(name))
            if entry is not None and entry.trust not in _USER_TIERS:
                entry.enabled = bool(enabled)

    def _save_factory_overrides(self) -> None:
        """回写出厂停用覆盖（锁内调用；全部恢复默认则删文件）。"""
        overrides = {e.name: e.enabled for e in self._registry.values()
                     if e.trust not in _USER_TIERS and not e.enabled}
        try:
            USER_PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
            if overrides:
                tmp = FACTORY_OVERRIDE_PATH.with_suffix(".json.tmp")
                tmp.write_text(
                    json.dumps(overrides, ensure_ascii=False), "utf-8")
                tmp.replace(FACTORY_OVERRIDE_PATH)
            else:
                FACTORY_OVERRIDE_PATH.unlink(missing_ok=True)
        except OSError:
            logger.warning("出厂停用覆盖回写失败", exc_info=True)

    def _load_user_registry(self) -> None:
        """启动时读用户登记表（容错：坏文件备份改名后空表起步）。"""
        try:
            raw = json.loads(USER_REGISTRY_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            corrupt = USER_REGISTRY_PATH.with_suffix(".json.corrupt")
            try:
                USER_REGISTRY_PATH.replace(corrupt)
            except OSError:
                logger.debug("_load_user_registry: 降级忽略", exc_info=True)
            logger.warning("用户插件登记表损坏，已备份为 %s，从空表起步",
                           corrupt)
            return
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            source = ROOT_DIR / str(item.get("source") or "")
            pkg_raw = item.get("pkg")
            pkg = ROOT_DIR / str(pkg_raw) if pkg_raw else None
            trust = str(item.get("trust") or "")
            if (not re.match(PLUGIN_NAME_RE, name)
                    or trust not in _USER_TIERS or not source.is_file()):
                logger.warning("用户插件登记跳过（不合格）: %r", name)
                continue
            self._registry[name] = _PluginEntry(
                name=name, source_py=source, pkg_path=pkg, trust=trust,
                enabled=bool(item.get("enabled", True)),
                imported_at=str(item.get("imported_at") or ""))

    def _save_user_registry(self) -> None:
        """用户登记表原子回写（锁内调用；只写用户档条目）。"""
        items = []
        for entry in self._registry.values():
            if entry.trust not in _USER_TIERS:
                continue
            items.append({
                "name": entry.name,
                "source": _rel_or_abs(entry.source_py),
                "pkg": _rel_or_abs(entry.pkg_path) if entry.pkg_path else None,
                "trust": entry.trust,
                "enabled": entry.enabled,
                "imported_at": entry.imported_at,
            })
        USER_PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        tmp = USER_REGISTRY_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(USER_REGISTRY_PATH)

    # ── 登记（导入端点/代码级入口；HTTP 导入闸见 api/plugins.py） ──
    def register(self, name: str, source_py: Path,
                 pkg_path: Path | None = None,
                 trust: str = "user_installed",
                 imported_at: str = "") -> None:
        """登记新插件（未登记不可加载——治理闸，非安全沙箱）。"""
        with self._global:
            if name in self._registry:
                raise PluginRuntimeError(
                    "PLUGIN_ALREADY_REGISTERED", f"插件已登记: {name}")
            if trust != "repo_curated" and trust not in _USER_TIERS:
                raise PluginRuntimeError(
                    "PLUGIN_TRUST_INVALID", f"未知信任档: {trust}")
            if trust in _USER_TIERS:
                user_count = sum(1 for e in self._registry.values()
                                 if e.trust in _USER_TIERS)
                if user_count >= MAX_USER_PLUGINS:
                    raise PluginRuntimeError(
                        "PLUGIN_LIMIT_REACHED",
                        f"用户插件已达上限（{MAX_USER_PLUGINS} 个）",
                        "在设置页删除不再使用的插件后重试")
            self._registry[name] = _PluginEntry(
                name=name, source_py=source_py, pkg_path=pkg_path,
                trust=trust, imported_at=imported_at)

    def save_user_registry(self) -> None:
        """显式持久化用户登记表（导入流程收尾调用）。"""
        with self._global:
            self._save_user_registry()

    def is_registered(self, name: str) -> bool:
        with self._global:
            return name in self._registry

    def set_enabled(self, name: str, enabled: bool) -> None:
        """启停用户/出厂插件（停用即卸载释放内存；用户档持久化）。"""
        with self._global:
            entry = self._registry.get(name)
            if entry is None:
                raise PluginRuntimeError(
                    "PLUGIN_NOT_REGISTERED", f"插件未登记: {name}")
            entry.enabled = enabled
            if not enabled and entry.instance is not None:
                try:
                    entry.instance.on_unload()
                except Exception:  # noqa: BLE001 - 卸载钩子炸不拦停用
                    logger.warning("插件 on_unload 异常: %s", name,
                                   exc_info=True)
                entry.instance = None
                entry.state = STATE_UNLOADED
            if entry.trust in _USER_TIERS:
                self._save_user_registry()
            else:
                self._save_factory_overrides()

    def remove(self, name: str) -> None:
        """删除用户插件（卸载+清登记+删文件；出厂档拒删）。"""
        with self._global:
            entry = self._registry.get(name)
            if entry is None:
                raise PluginRuntimeError(
                    "PLUGIN_NOT_REGISTERED", f"插件未登记: {name}")
            if entry.trust not in _USER_TIERS:
                raise PluginRuntimeError(
                    "PLUGIN_FACTORY_PROTECTED",
                    f"出厂插件不可删除: {name}",
                    "出厂插件随软件版本管理")
            if entry.instance is not None:
                try:
                    entry.instance.on_unload()
                except Exception:  # noqa: BLE001
                    logger.warning("插件 on_unload 异常: %s", name,
                                   exc_info=True)
            self._registry.pop(name, None)
            # 只删 data/plugins/imported/ 区内文件（防误删仓库/系统文件）
            try:
                zone = USER_PLUGIN_DIR.resolve()
                for f in (entry.source_py, entry.pkg_path):
                    if f is None:
                        continue
                    p = f.resolve()
                    if p.is_relative_to(zone):
                        p.unlink(missing_ok=True)
            except OSError:
                logger.warning("用户插件文件清理失败: %s", name,
                               exc_info=True)
            self._save_user_registry()

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
                    "trust_label": _TRUST_LABELS.get(entry.trust, entry.trust),
                    "origin": ("user" if entry.trust in _USER_TIERS
                               else "factory"),
                    "imported_at": entry.imported_at,
                    "enabled": entry.enabled, "state": entry.state,
                    "source": _rel_or_abs(entry.source_py),
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
        # 分级隔离（2026-09-17 终态=A，用户拍板）：含源码档走子进程
        # 沙箱——宿主进程不 exec 其源码（ensure_loaded 一并跳过），
        # 超时/内存超限可硬杀、崩溃不连坐后端；出厂/纯数据档零改动
        if entry.trust == "user_source" and sandbox_enabled():
            return await self._invoke_sandboxed(entry, spec, timeout_s)
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

    async def _invoke_sandboxed(self, entry: _PluginEntry,
                                spec: dict[str, Any],
                                timeout_s: float) -> dict[str, Any]:
        """含源码档的沙箱 invoke：子进程执行 + 结果按进程内同构出线。

        事件/结果只走 JSON（ndarray 不直传——帧类插件属出厂档进程内
        直跑，不受此限）；失败统一置 faulty 与进程内路径同语义。
        """
        from .sandbox import SandboxError, invoke_sandboxed
        event = {"topic": "invoke", "data": spec}
        try:
            raw = await run_blocking(
                lambda: invoke_sandboxed(
                    entry.name, entry.source_py, entry.pkg_path, event,
                    timeout_s=min(float(timeout_s), 600.0)))
        except SandboxError as exc:
            with entry.lock:
                entry.state = STATE_FAULTY
                entry.last_error = str(exc)
            raise PluginRuntimeError(
                "PLUGIN_SANDBOX_FAILED", f"沙箱插件执行失败: {exc}",
                "插件已标记故障态；停用→启用（或 unload）可复位") from None
        raw = raw if isinstance(raw, dict) else {"value": raw}
        return {"data": _passthrough_data(raw), "sandboxed": True}

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
        # 通用数据透传（基类统一 2026-09-16）：非 video 形态插件的结果键
        # 原样出线（如 rust-coding 的 label/confidence）；帧本体仍被拒之
        # 门外，ndarray 压成形状摘要防 JSON 膨胀
        result["data"] = _passthrough_data(raw)
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
