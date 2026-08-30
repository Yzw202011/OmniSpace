# 漫剧创作 ComfyUI 双工作流（分镜图 KF1 + 视频 V8）

> 时效：现行（2026-08-29；KF1 为当日从零搭建并全链验收的增量）。
> 定位：内置 ComfyUI 侧的漫剧创作双工作流——KF1 出分镜图（人物/场景/道具一致性），
> V8 吃分镜图逐镜出带音轨视频；均四分区布局 + 分区大白话便签 + 底部节点词典，
> 全部说明以节点源码与实机 object_info/接线核实（防幻觉）。

## 双工作流落位

| 工作流 | JSON 位置 | 生成脚本（真源） | 状态 |
| --- | --- | --- | --- |
| 分镜图 KF1 | `user/default/workflows/漫剧创作套件/分镜图工作流_KF1.json` | `tools/build_manga_keyframe_workflow.py` | 从零搭建；加载零报错；实跑 214s/张（1280×720@36步）成功 |
| 视频 V8 | `user/default/workflows/H3-分段参考/8、漫剧关键帧锚定版/漫剧分镜关键帧生视频_V8.json` | `tools/build_manga_v8_workflow.py` | V6 骨架分区导览版；GPU 两镜实测全绿 |

## KF1 分镜图工作流（从零，44 节点/48 链/5 分区）

- **① 文本输入区**：正向分镜词/负向词（CLIPTextEncode，Qwen3-8B 中文原生）、
  画幅（ResolutionSelector 0.92MP≈1280×720，multiple=32 对齐 CANVAS_MULTIPLE）、
  步数（36=后端 comfy 引擎实测配方）。
- **② 资产图输入区**：人物/场景/道具三 LoadImage（input 根目录 kf_role/scene/prop）→
  缩放（人物 PuLID 支路 lanczos 0.26MP；参考支路 nearest-exact 0.92MP）→ VAEEncode →
  ReferenceLatent **正负双侧**级联（=后端 reflatent="both" 无 PuLID 档）。
- **③ 流程区**：Klein 9B fp8 →（旁路：一致性 LoRA v2 / PuLID 四件套）→ CFGGuider(4.0)
  → euler + Flux2Scheduler + EmptyFlux2LatentImage → SamplerCustomAdvanced。
- **④ 产出区**：SaveImage → `output/manga_kf/`；与 V8 接力：拷 `shot_NN.png` 进
  `E:/OmniSpace/data/manga_kf/`。
- **诚实设计**：models/pulid 与 insightface 未挂载（组合框仅 `__create_new__`），
  PuLID 四件套 + 一致性 LoRA **默认旁路**（mode=4），挂权重后 Ctrl+B 解除；词典写明
  EVA-CLIP 首跑经 open_clip 自动下载（需 HF 镜像时设 HF_ENDPOINT）。

## 双向集成测试记录（2026-08-29）

1. **项目前端 → ComfyUI**：后端以 `runtime/py310/python.exe -m backend.main`
   启动（5800，约 5s 就绪；直接 `python main.py` 会 relative import 报错）。引擎层
   实测：`h3_available()=True`、`comfy_paint_available()=True`、
   `H3Engine.is_alive()=True`（后端探活并**复用运行中的 8189 外部实例**）；
   **`pulid_available()=False` → 后端关键帧路由（keyframe.py use_comfy 判定）
   当前走本地绘画引擎、不经 ComfyUI**——挂载 PuLID 权重后自动切换。
2. **ComfyUI → 项目前端**：KF1 产物按约定复制 + 注册 keyframes 表
   （is_current=0 测试版本 v7）→ `GET /api/v1/manga/keyframe/list?row_id=…` 返回
   v7 → `GET /api/v1/manga/media/<file_path>` 200（1.6MB PNG）→ 前端漫剧工作台
   「镜 1 详情 → 历史记录 (7)」中 v7 正常显示（截图实证，含回退/删除按钮，
   未覆盖当前 v6）。
3. **协调**：前端打开漫剧页即触发"释放其他模块资源，优先供应漫剧创作"（资源
   协调机制实测生效）；后端与 GUI 共用同一 8189 实例（串行复用语义）；测试后
   `/free` 还原显存。**注意**：手动 GUI 任务与后端任务分属两套锁（前端
   ImpactQueueTrigger / 后端 _gen_lock），人守使用、避免同时排队。

## 遗留与口径

- PuLID 权重挂载后：KF1 解除旁路即得"注意力硬锁"身份一致性；后端关键帧路由
  亦会自动切到 ComfyUI 通道（OMNI_KEYFRAME_PULID_AUTO）。
- 测试注册的 v7 行（keyframe_id 前缀 9c494484…）保留作集成证据，可在前端
  v7 卡片「删除」或 DELETE /manga/keyframe/{id} 清理。
- 12G 显存口径：KF1 的 TE qwen3_8b 为 16.4GB bf16，ComfyUI 自动分页换载，
  12G 卡可跑但文本编码阶段偏慢；V8 视频单镜 124 帧采样需求 ≈10.6GB（后端标定）
  可入 12G，243 帧勿试。
