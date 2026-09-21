# 本项目仅供学习使用，商业授权请+Q 3559331368
"""最简 OmniSpace 插件模板——20 行起步。

复制本文件、改类名和 on_think 逻辑，然后用 tools/build_plugin.py 打包。
"""
from __future__ import annotations

from typing import Any

try:
    from .plugin import ExpertPlugin
except ImportError:
    from omnispace.plugin import ExpertPlugin


class HelloPlugin(ExpertPlugin):
    """把输入文本原样返回（ Echo 插件）——改成任何你想要的处理逻辑。"""
    BASE_MODEL = "generic"
    CAPABILITY = "最简示例：回显输入"

    def on_think(self, event: dict[str, Any], ctx: Any) -> dict[str, Any]:
        data = event.get("data") or {}
        text = str(data.get("text") or "").strip()
        return {
            "text": f"Hello! 你说的是：{text or '（空）'}",
            "length": len(text),
        }
