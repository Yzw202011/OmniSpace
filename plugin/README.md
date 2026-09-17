# plugin/ — CuteMamen 插件包

OmniSpace 插件系统的插件包目录。插件以 `.CuteMamen` 自包含包交付
（tar.gz：manifest.json + weights/ + memory/ 三级记忆 + 可选
source/plugin.py 内嵌源码 v2.1）。产品加载方 =
`src/services/plugin_runtime/`（OSP v1，`plugin/` 为种子包登记目录）；
`src/cutemamen/` 内核经 2026-09-16 接线后亦可装载。插件源码单一真源 =
`src/cutemamen/rust_coding.py`。

| 插件包 | 能力 | 资源需求 |
| ------ | ---- | -------- |
| RustCoding.CuteMamen | Rust 编译错误分类（move/borrow/lifetime/type/ok），携带主模型迁移知识（502 段真实语料训练的线性读出层） | 纯 numpy，零 GPU |

> 2026-09-17：VideoMaking.CuteMamen（单镜运镜快速预览插件）与
> `/manga/video/preview` 产品链接用户令整链移除；`plugin/` 目录自此
> 仅登记 RustCoding。

## RustCoding.CuteMamen — 主模型 Rust 知识迁移

「新阶段 · 分布式架构」：主模型（`src/core/distributedformer.py`
冻结 CubeGPT 水库）的 Rust 开发知识经 `src/training/readout.py`
线性读出层蒸馏进插件，宿主无需加载主模型即可推理：

```python
from src.cutemamen import load_pkg

plugin, manifest = load_pkg("plugin/RustCoding.CuteMamen")
result = plugin.on_think({"topic": "rust", "data": {"code": "fn f() { let s = String::from(\"x\"); let t = s; let u = s; }"}}, ctx)
# → {"label": "move", "confidence": ..., "source": ...}
```

插件源码：`src/cutemamen/rust_coding.py`。
插件标准：CuteMamen v2.0.0+（内核 ≥ 0.8.7，见 `src/cutemamen/pkg.py`）。
