"""CuteMamen 事件总线: 插件间通信与内核事件广播

- 内核在生命周期节点广播: plugin.loaded / plugin.unloaded / plugin.evicted /
  kernel.think (每个被路由的事件)
- 每个插件的 on_think 结果发布到 "plugin.<name>.output", 其他插件可订阅
  实现插件间通信 (专家协作)
- 支持 "*" 通配订阅 (收到全部事件)
- 订阅回调抛出的异常被总线捕获并计入 errors, 不影响发布方
"""

import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


class EventBus:
    """轻量 pub/sub 事件总线 (CuteMamen 事件总线通信)"""

    WILDCARD = "*"

    def __init__(self, history_capacity: int = 256):
        self._subscribers: dict[str, list[Callable[[dict], None]]] = {}
        self.history: deque[dict] = deque(maxlen=history_capacity)
        self.errors: list[dict] = []

    # ── 订阅 ────────────────────────────────────────────────
    def subscribe(self, topic: str,
                  handler: Callable[[dict], None]) -> Callable[[], None]:
        """订阅主题, 返回退订函数"""
        self._subscribers.setdefault(topic, []).append(handler)

        def _unsubscribe() -> None:
            try:
                self._subscribers.get(topic, []).remove(handler)
            except ValueError:
                log.debug("_unsubscribe: 降级忽略", exc_info=True)
        return _unsubscribe

    def unsubscribe(self, topic: str, handler: Callable[[dict], None]) -> None:
        try:
            self._subscribers.get(topic, []).remove(handler)
        except ValueError:
            log.debug("unsubscribe: 降级忽略", exc_info=True)

    # ── 发布 ────────────────────────────────────────────────
    def publish(self, topic: str, payload: Any = None) -> int:
        """发布事件到主题, 返回送达的订阅者数"""
        event = {"topic": topic, "t": time.time(), "payload": payload}
        self.history.append(event)
        delivered = 0
        for sub_topic in (topic, self.WILDCARD):
            for handler in list(self._subscribers.get(sub_topic, [])):
                try:
                    handler(event)
                    delivered += 1
                except Exception as exc:  # 订阅方故障不中断发布方
                    self.errors.append({"topic": topic, "error": repr(exc)})
        return delivered

    # ── 查询 ────────────────────────────────────────────────
    def topics(self) -> list[str]:
        return sorted(self._subscribers)

    def subscriber_count(self, topic: str) -> int:
        return len(self._subscribers.get(topic, []))

    def recent(self, n: int = 10) -> list[dict]:
        return list(self.history)[-n:]

    def stats(self) -> dict[str, Any]:
        return {
            "topics": len(self._subscribers),
            "published": len(self.history),
            "errors": len(self.errors),
        }
