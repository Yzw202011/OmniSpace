"""安全盾宿主适配层（把 DF SecurityMonitor 接进 OmniSpace 调用链）。

与 security/ 包（忠实移植、与上游同构）分工：本模块做三件产品侧的事：

1. **单例持有**：全进程一个 SecurityMonitor（指纹基线/审计环形共享）；
2. **config 总闸**：``plugins.security_gate``（默认 true；读取失败保持开
   ——初始规则集与现网插件名零交集，闸开着零行为变化，关闸只作逃生口）；
3. **两条调用链接入**：
   - registry.PluginRuntime.invoke → gate_plugin_call(name, spec)
   - kernel_gateway（think 网关补丁）→ gate_kernel_event(event)

判定语义：ALLOW 放行（回填指纹基线 + 审计留痕）；REVIEW/DENY 由
**调用方**翻译为 PluginRuntimeError（本层不 import registry，避免环）。
门禁自身异常 fail-open（记错误日志放行）——拦截面故障不引入新产品
故障点；真威胁只在规则命中时存在，而规则路径是纯 dict 遍历。
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from .security import BehavioralFingerprint, SecurityMonitor

logger = logging.getLogger(__name__)

# 审计留痕的参数裁剪上限（spec 可能含长代码串；熵值与留痕同源同口径）
_PARAMS_CLIP = 2000

# 熵阈值部署调参（DF 原版 4.0 为短工具名口径）：自然代码/英文文本熵约
# 4.2~4.9 bit/字符，随机 base64/密文 ≥6——取 5.5 只抓真随机载荷，
# 避免 rust-coding 等代码类插件的每次调用都打 high-entropy 噪声标记
_ENTROPY_THRESHOLD = 5.5

_monitor: SecurityMonitor | None = None
_monitor_lock = threading.Lock()


def security_gate_enabled() -> bool:
    """总闸（config plugins.security_gate，默认 true；读取失败保持开）。"""
    try:
        from src.config import get_config
        raw = (get_config().get("plugins") or {}).get("security_gate", True)
        return bool(raw)
    except Exception:  # noqa: BLE001 - 配置异常保持开（默认规则零拦截）
        return True


def get_security_monitor() -> SecurityMonitor:
    """获取组合监控面单例（懒初始化；熵阈值按部署调参，见模块头）。"""
    global _monitor
    if _monitor is None:
        with _monitor_lock:
            if _monitor is None:
                _monitor = SecurityMonitor(fingerprint=BehavioralFingerprint(
                    entropy_threshold=_ENTROPY_THRESHOLD))
    return _monitor


def reset_security_monitor() -> None:
    """复位单例（测试用：清指纹基线与审计环形）。"""
    global _monitor
    with _monitor_lock:
        _monitor = None


def _clip(text: str, limit: int = _PARAMS_CLIP) -> str:
    return text if len(text) <= limit else text[:limit] + f"...<+{len(text) - limit}>"


def gate_plugin_call(name: str, spec: dict[str, Any],
                     task: str | None = None) -> tuple[bool, str, str]:
    """invoke 通道安全门禁：返回 (放行, 判定, 拒绝理由说明)。

    ALLOW 回填指纹基线；REVIEW/DENY 由调用方翻译错误码——
    REVIEW=fail-closed（挂起等审核，无审核者默认不放行）。
    """
    action = {"tool": name, "params": _clip(str(spec))}
    return _gate(action, task)


def gate_kernel_event(event: dict[str, Any],
                      task: str | None = None) -> tuple[bool, str, str]:
    """内核 think 通道安全门禁（topic 作 tool 名，语义同上）。"""
    action = {"topic": str(event.get("topic", "")),
              "params": _clip(str(event.get("data", "")))}
    return _gate(action, task)


def _gate(action: dict[str, Any],
          task: str | None) -> tuple[bool, str, str]:
    if not security_gate_enabled():
        return True, "GATE_OFF", ""
    monitor = get_security_monitor()
    try:
        ok, audited, _review = monitor.gate(action, task=task)
    except Exception:  # noqa: BLE001 - 门禁故障 fail-open + 留错误日志
        logger.error("安全门禁内部异常，放行并留痕", exc_info=True)
        return True, "GATE_ERROR", ""
    outcome = audited.verdict.verdict.value
    if not ok:
        detail = "; ".join(audited.verdict.reasons) or outcome
        logger.warning("安全门禁拦截: %s → %s（%s）",
                       action.get("tool") or action.get("topic"),
                       outcome, detail)
    return ok, outcome, "; ".join(audited.verdict.reasons)


def gate_stats() -> dict[str, Any]:
    """监控面统计（四件套各自口径，诊断/产品面暴露用）。"""
    if _monitor is None:
        return {"active": False}
    return {"active": True, **get_security_monitor().stats()}
