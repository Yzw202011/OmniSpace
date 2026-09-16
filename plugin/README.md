# plugin/ — CuteMamen 插件包

OmniSpace 插件系统（路线图「规划中」）的首批插件包。插件以 `.CuteMamen`
自包含包交付（tar.gz：manifest.json + weights/ + memory/），内核侧零源码
改动即可加载。

## VideoMaking.CuteMamen — 轻量视频生成内核

**定位**：漫剧视频生成的**轻量档位**，与现有主力链路互补：

| 档位 | 引擎 | 资源需求 | 适用场景 |
| ---- | ---- | -------- | -------- |
| 主力 | MiniMax H3 / Wan2.2-ti2v-5b（本地 ComfyUI） | NVIDIA GPU + 大显存 | 高质量多镜成片 |
| 轻量（本插件） | 镜头运动曲线 + 缓动仿射帧合成 | 纯 numpy，零 GPU / 零额外依赖 | 预览、粗剪、低配机器、快速迭代分镜 |

### 能力

- 10 种镜头运动曲线：static / pan / tilt / zoom_in / zoom_out /
  dolly_in / orbit（左右），参数化运镜（归一化平移 + 缩放 + 旋转）
- 缓动：smoothstep / linear / ease_out
- 转场：cut / crossfade（叠化）/ dip_to_black（黑场）
- 输入 = 漫剧关键帧（HxW 或 HxWx3 数组，uint8 / [0,1] 均可）+ 分镜
  spec；输出 = 逐帧 ndarray 序列 + 镜头计划（JSON 安全）
- 三级记忆（working / episodic / semantic）与运动曲线权重随包存档，
  渲染统计（镜头数 / 总帧数）跨会话保留

### 用法（宿主侧示例）

```python
from plugin.video_making import VideoMakingPlugin   # 或从包加载（见下）

plugin = VideoMakingPlugin()
frames, plan = plugin.render({
    "keyframes": [kf1, kf2],        # np.ndarray 列表（漫剧关键帧）
    "fps": 12,
    "shots": [
        {"motion": "zoom_in", "duration_s": 1.5},
        {"motion": "pan_left", "duration_s": 1.0, "transition": "crossfade"},
    ],
})
```

从 `.CuteMamen` 包加载（DistributedFormer 内核宿主，或任何实现
CuteMamen 标准 v2 清单的宿主）：

```python
# DistributedFormer 宿主
from src.cutemamen import load_pkg
plugin, manifest = load_pkg("plugin/VideoMaking.CuteMamen")
result = plugin.on_think({"topic": "video", "data": spec}, ctx)
frames = result["frames"]
```

### 文件

| 文件 | 说明 |
| ---- | ---- |
| VideoMaking.CuteMamen | 插件包（manifest + 运动曲线权重 + 三级记忆存档） |
| video_making.py | 插件源码（与包内权重同源，供评审与二次开发） |

来源：DistributedFormer CuteMamen 插件标准 v2.0.0（内核 ≥ 0.8.7）。
