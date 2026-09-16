"""OmniSpace 插件运行时 · 宿主标准（OSP v1.1 统一版，2026-09-16 拍板）。

本模块是进程内插件的**宿主标准**（标准名 `omnispace.plugin`）：
插件源码写 `from omnispace.plugin import ExpertPlugin, PluginContext`，
由 loader 在加载前把本模块注入 sys.modules（见 loader._ensure_host_module）。

**基类统一（P2 拍板）**：此前 OSP v1 自带一套最小基类，与
src/cutemamen/plugin.py 的内核基类同名不同物——同一插件在
「内核直连」「宿主 exec」两条加载路径下拿到不同基类，isinstance
不稳定、记忆模型两套。现统一为**单一真源 = src/cutemamen/plugin.py**：

- ExpertPlugin / PluginContext / PluginMemory 全部转发内核版；
- PluginContext 双构造形态等价（内核 bus=... / 宿主 emit_fn=...），
  emit() 优先总线、宿主桥兜底、两者皆无静默降级；
- PluginMemory.restore 容错 archive 列表形态与宿主 loader 的
  {key: value} 字典形态（loader._parse_pkg_entry 产出）；
- ExpertPlugin 新增 runtime_meta 宿主附加位（OSP 登记信息）。

OSP v1 的三级键值记忆 API（set/get 带 level、snapshot）随统一退役，
调用方一律使用统一后语义（见 tests/unit/test_plugin_runtime.py）。
本文件 py310/py312 双兼容（发行运行时 py310.11 / 开发主链 py312）。
"""
from __future__ import annotations

from src.cutemamen.plugin import (  # noqa: F401 - 统一转发（单一真源）
    ExpertPlugin,
    PluginContext,
    PluginMemory,
)

# 兼容别名：OSP v1 时期的记忆层级常量（统一后语义不变）
MEMORY_LEVELS = PluginMemory.LEVELS
