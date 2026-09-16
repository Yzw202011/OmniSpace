# plugin/ — CuteMamen 插件包

OmniSpace 插件系统的插件包目录。插件以 `.CuteMamen` 自包含包交付
（tar.gz：manifest.json + weights/ + memory/ 三级记忆），由
`src/cutemamen/` 内核加载与路由，源码与插件系统规范都在
`src/cutemamen/`（扁平化重构后不再在 plugin/ 重复存放源码副本）。

| 插件包 | 能力 | 资源需求 |
| ------ | ---- | -------- |
| RustCoding.CuteMamen | Rust 编译错误分类（move/borrow/lifetime/type/ok），携带主模型迁移知识（502 段真实语料训练的线性读出层） | 纯 numpy，零 GPU |
| VideoMaking.CuteMamen | 轻量视频生成内核：关键帧 + 镜头运动曲线 → 帧序列 + 转场 | 纯 numpy，零 GPU |

## RustCoding.CuteMamen — 主模型 Rust 知识迁移

「新阶段 · 分布式架构」：主模型（`src/core/distributedformer.py`
冻结 CubeGPT 水库）的 Rust 开发知识经 `src/training/readout.py`
线性读出层蒸馏进插件，宿主无需加载主模型即可推理：

```python
from src.cutemamen import load_pkg

plugin, manifest = load_pkg("plugin/RustCoding.CuteMamen")
result = plugin.on_think({"topic": "rust", "data": {"code": "fn f() { let s = String::from(\"x\"); let t = s; let u = s; }"}}, ctx)
# → {"label": "move", "confidence": 0.97, "source": "main-model-readout", ...}
```

## VideoMaking.CuteMamen — 轻量视频生成内核

漫剧视频生成的**轻量档位**，与 MiniMax H3 / Wan2.2-ti2v-5b 主力链路
（本地 ComfyUI，需大显存）互补：预览、粗剪、低配机器、快速迭代分镜。

- 10 种镜头运动曲线（static / pan / tilt / zoom / dolly / orbit）
- 缓动：smoothstep / linear / ease_out
- 转场：cut / crossfade / dip_to_black

```python
from src.cutemamen import load_pkg

plugin, manifest = load_pkg("plugin/VideoMaking.CuteMamen")
result = plugin.on_think({"topic": "video", "data": {
    "keyframes": [kf1, kf2],   # np.ndarray 列表（漫剧关键帧）
    "fps": 12,
    "shots": [
        {"motion": "zoom_in", "duration_s": 1.5},
        {"motion": "pan_left", "duration_s": 1.0, "transition": "crossfade"},
    ],
}}, ctx)
frames = result["frames"]
```

插件源码：`src/cutemamen/rust_coding.py` / `src/cutemamen/video_making.py`。
插件标准：CuteMamen v2.0.0（内核 ≥ 0.8.7，见 `src/cutemamen/pkg.py`）。
