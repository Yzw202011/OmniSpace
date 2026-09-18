"""推理引擎统一生命周期协议与品类注册表（ADR-003 P2）。

设计思想（对齐 backends/base.py 的 kernel/编排分层）：
  - 协议只固化 ModelManager 与引擎的**既有真实契约**（2026-08-29 勘察）：
    load_model(model_id) / unload_model() / get_status() / is_ready()——
    四引擎（dialog/paint/video/voice）已全部以鸭子类型实现该面，本模块
    将其正式化为 ABC，不新增引擎必须实现的方法。
  - 推理接口（text2img/i2v/tts/...）**不进协议**：各品类签名天然不同，
    强行统一属过度抽象（ADR-003 §2 非目标：在现有零件上收敛而非重写）。
  - 品类解析从 ModelManager._get_engine 的 if/elif 迁移为 ENGINE_MODULES
    注册表：新增品类调用 register_engine_module() 即可被 ModelManager
    管理（单测锁定），管理器本体零改动。

依赖方向（无环）：
  base_engine ← model_manager（_get_engine 委托 resolve_engine）
  base_engine ← 各引擎（继承 BaseEngine；引擎模块由 resolve_engine 懒导入）
"""
from __future__ import annotations

import importlib
import logging
import threading
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, ClassVar

log = logging.getLogger("omnispace.inference.base_engine")


class EngineState(str, Enum):
    """引擎统一状态机（ADR-003 P3）。

    进程内引擎与子进程引擎（vLLM）共用同一词汇；UNLOADED/UNAVAILABLE/
    READY/ERROR 四值与 dialog/paint 引擎既有 `state` 字符串逐字对齐
    （历史兼容），BOOTING/SLEEPING/STOPPING 为 P3 新增的可观测态：
      - BOOTING:  加载进行中（进程内 load 或子进程冷启动/预热窗口）
      - SLEEPING: 显存已让渡（vLLM sleep level1 或 Windows fallback
                  停进程让渡），wake 后可恢复
      - STOPPING: 协同停止进行中（取消/超时/模块切换终止）
    """

    UNAVAILABLE = "unavailable"
    UNLOADED = "unloaded"
    BOOTING = "booting"
    READY = "ready"
    SLEEPING = "sleeping"
    STOPPING = "stopping"
    ERROR = "error"

    @classmethod
    def coerce(cls, raw: Any) -> EngineState:
        """宽容映射：既有字符串/枚举/None → EngineState（未知 → UNLOADED）。"""
        if isinstance(raw, cls):
            return raw
        try:
            return cls(str(raw))
        except ValueError:
            return cls.UNLOADED


def derive_state(loaded: bool, unavailable: bool = False,
                 error: str = "", booting: bool = False,
                 sleeping: bool = False) -> str:
    """统一状态推导（四引擎 get_status()["state"] 的单一口径）。

    优先级：booting > sleeping > unavailable > ready(loaded) > error > unloaded。
    诚实降级模式（SAPI5 语音/mock 视频）由调用方以 loaded=False +
    unavailable=True 表达——降级态不是 ready，但也不撒谎成 unavailable
    时引擎仍可回退合成，故仅 torch 缺失等真不可用场景才标 unavailable。
    """
    if booting:
        return EngineState.BOOTING.value
    if sleeping:
        return EngineState.SLEEPING.value
    if unavailable:
        return EngineState.UNAVAILABLE.value
    if loaded:
        return EngineState.READY.value
    if error:
        return EngineState.ERROR.value
    return EngineState.UNLOADED.value

# 品类别名组 → (引擎模块全名, 模块级单例 getter 名, 类名兜底)。
# 与 2026-08-29 前 _get_engine 的 if/elif 分组逐条对齐（行为等价迁移）；
# voice 为 P2 新接入（原先恒 None，管理器侧 CPU 记账，现注册共享单例）。
# 未注册品类（auxiliary/embedding/3d 等）→ resolve 返回 None。
ENGINE_MODULES: dict[str, tuple[str, str, str]] = {
    "dialog": ("src.services.inference.dialog_engine",
               "get_dialog_engine", "DialogEngine"),
    "language": ("src.services.inference.dialog_engine",
                 "get_dialog_engine", "DialogEngine"),
    "omni": ("src.services.inference.dialog_engine",
             "get_dialog_engine", "DialogEngine"),
    "paint": ("src.services.inference.paint_engine",
              "get_paint_engine", "PaintEngine"),
    "vision": ("src.services.inference.paint_engine",
               "get_paint_engine", "PaintEngine"),
    "image": ("src.services.inference.paint_engine",
              "get_paint_engine", "PaintEngine"),
    "video": ("src.services.inference.video_engine",
              "get_video_engine", "VideoEngine"),
    "video_gen": ("src.services.inference.video_engine",
                  "get_video_engine", "VideoEngine"),
    "voice": ("src.services.inference.voice_engine",
              "get_voice_engine", "VoiceEngine"),
}

_registry_lock = threading.Lock()


def register_engine_module(category: str, module_name: str,
                           getter_name: str = "get_engine",
                           class_name: str = "") -> None:
    """注册（或替换）品类引擎映射——新增品类的唯一接入点。

    模块不在此处导入（resolve_engine 懒导入），故注册本声是零成本的，
    可用于运行时插件注入与测试桩。
    """
    cat = (category or "").strip().lower()
    if not cat:
        raise ValueError("register_engine_module: category 不能为空")
    if not module_name:
        raise ValueError("register_engine_module: module_name 不能为空")
    with _registry_lock:
        ENGINE_MODULES[cat] = (module_name, getter_name, class_name)


def unregister_engine_module(category: str) -> bool:
    """注销品类映射（测试清理用；内置品类注销后按未注册品类降级 None）。"""
    with _registry_lock:
        return ENGINE_MODULES.pop((category or "").strip().lower(), None) \
            is not None


def resolve_engine(category: str) -> Any:
    """按品类解析引擎单例（懒导入 + 容错；未注册/失败 → None）。

    容错语义与 2026-08-29 前 ModelManager._get_engine 逐条对齐：
    getter 缺失回退类名直接实例化，导入/实例化失败记日志返回 None，
    绝不向调用方抛异常。
    """
    cat = (category or "").strip().lower()
    with _registry_lock:
        entry = ENGINE_MODULES.get(cat)
    if entry is None:
        return None
    module_name, getter_name, class_name = entry
    try:
        mod = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - 引擎损坏/缺失容错
        log.warning("引擎模块导入失败 (%s): %s", cat, exc)
        return None
    try:
        getter = getattr(mod, getter_name, None)
        if callable(getter):
            return getter()
        cls = getattr(mod, class_name, None) if class_name else None
        return cls() if isinstance(cls, type) else None
    except Exception as exc:  # noqa: BLE001
        log.warning("引擎实例化失败 (%s): %s", cat, exc)
        return None


class BaseEngine(ABC):
    """推理引擎生命周期协议（ModelManager 管理面的最小真实契约）。

    生命周期：load_model()*N ⇄ unload_model()；并发串行化由各引擎自持
    锁自治（与既有实现一致，协议不重复加锁）。

    ClassVars:
        name: 引擎标识（get_status().engine 兜底、日志定位）
        serves_categories: 服务的品类别名组（声明性元数据；运行时解析
            以 ENGINE_MODULES 注册表为准）
    """

    name: ClassVar[str] = ""
    serves_categories: ClassVar[tuple[str, ...]] = ()

    def __init__(self) -> None:
        self._last_error: str = ""

    def _note_error(self, message: str) -> None:
        """记录最近一次失败原因（last_error() 契约的数据源）。"""
        self._last_error = str(message)

    def last_error(self) -> str:
        """最近一次失败原因（无失败为空串）。"""
        return self._last_error

    @abstractmethod
    def load_model(self, model_id: str | None = None) -> bool:
        """加载模型。返回 False 时原因经 _note_error/last_error 可溯。"""

    @abstractmethod
    def unload_model(self) -> bool:
        """卸载模型并释放引擎持有的管线引用。

        Returns:
            是否确有已加载内容被释放（空载卸载返回 False，非错误）。
        """

    @property
    def is_ready(self) -> bool:
        """引擎是否处于可推理状态（property；默认取 get_status().ready）。

        形状注记：既有四引擎的 is_ready 均为 @property（2026-08-29 实测
        dialog/paint/video/voice 四处定义一致），本协议跟随既有形状；
        未覆盖 is_ready 的引擎（测试桩/新品类）走本默认实现。
        """
        try:
            return bool(self.get_status().get("ready"))
        except Exception:  # noqa: BLE001 - 状态异常按未就绪
            return False

    @property
    def state(self) -> EngineState:
        """统一状态机取值（ADR-003 P3）。

        引擎侧事实源是 get_status()["state"]（dialog/paint 既有字段，
        P3 起四引擎统一携带）；本属性做宽容映射，缺省 → UNLOADED。
        """
        try:
            return EngineState.coerce(self.get_status().get("state"))
        except Exception:  # noqa: BLE001 - 状态异常按未装载
            return EngineState.UNLOADED

    def health_check(self) -> dict[str, Any]:
        """存活 + 就绪探测（统一钩子，P3）。

        进程内引擎默认实现足够；子进程形态覆盖以接入真实 /health 探测
        （vLLM 服务自身已探测，状态经 get_status()["state"] 汇入）。
        healthy 语义 = 当前占显存属正常运营态（READY/SLEEPING 均算）。
        """
        st = self.state
        return {
            "state": st.value,
            "ready": self.is_ready,
            "healthy": st in (EngineState.READY, EngineState.SLEEPING),
        }

    def graceful_stop(self, timeout_s: float = 10.0) -> bool:
        """协同停止（统一钩子，P3）：默认卸载；子进程形态覆盖以实现
        「立取消标志 → 限时拿锁 → 进程树终止」的分级协议。"""
        return self.unload_model()

    def vram_reclaim_gb(self) -> float:
        """立即调用 unload 可回收的显存估计（GB；默认 0 = 进程内引擎
        无独立预算记账）。子进程形态可覆盖返回整卡预算。"""
        return 0.0

    def get_status(self) -> dict[str, Any]:
        """状态快照（引擎可覆盖扩展字段；shape 对齐既有四引擎 dict）。"""
        return {
            "engine": self.name or type(self).__name__,
            "model": "",
            "ready": False,
            "last_error": self._last_error,
        }
# 本项目仅供学习使用，商业授权请+Q 3559331368
