"""插件事件桥：工作线程 emit → 日志 + WS 中枢广播（线程安全）。

POC2-C 实证路线：工作线程不直接碰 asyncio（create_task 必须
在 loop 线程），ws_hub.broadcast 自带 call_soon_threadsafe 通道
（ws_hub.py「任意线程可调用」），本桥只做格式翻译与降级兜底。
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("omnispace.services.plugin_runtime.bridge")


class PluginEventBridge:
    """把插件 emit(topic, payload) 翻译为前端事件并广播。

    事件形态对齐全库通行格式 {"type": ..., "data": {...}}
    （样例 paint_engine.py:414 / main.py:304）：
    {"type": "plugin_event", "data": {"plugin", "topic", ...payload}}。
    WS 中枢不可用时静默降级（只剩日志），不连累插件主流程。
    """

    def emit(self, plugin_name: str, topic: str,
             payload: dict[str, Any]) -> None:
        data: dict[str, Any] = {"plugin": plugin_name, "topic": topic}
        # 只收摘要类 payload（帧本体进总线 = 内存炸弹，POC2-A 实测
        # 1080p float64 帧序列 1.2GB——铁律：事件不带大数组）
        if isinstance(payload, dict):
            data.update(payload)
        log.info("插件事件 %s.%s: %s", plugin_name, topic,
                    {k: v for k, v in data.items()
                     if isinstance(v, (str, int, float, bool))})
        try:
            from ..ws_hub import get_ws_hub
            get_ws_hub().broadcast({"type": "plugin_event", "data": data})
        except Exception:  # noqa: BLE001 - 广播失败不连累插件
            log.debug("插件事件广播降级（WS 中枢不可用）", exc_info=True)


# 模块级单例（桥无状态，单例便于测试替换）
_bridge = PluginEventBridge()


def get_event_bridge() -> PluginEventBridge:
    """获取事件桥单例。"""
    return _bridge
