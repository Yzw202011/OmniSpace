# 未完成功能修复计划（对照《修正版E》）

- 日期：2026-08-08
- 输入：基于《OmniSpace_AI_v2.3.1_极致细粒度全量文档（修正版E）》的缺口梳理 + 代码库现状复核
- 现状修正：Round2 审计的 7 条 P1（被动补全/触发器/自动微调/LoRA接入推理/学习进度WS/分镜4端点/导演台export）与 8B 路由、/system/info、§4.4 默认值**均已修复**；本计划仅覆盖复核后确认仍存在的缺口
- 工作量单位：人日（d），含自测与文档同步

---

## 一、缺口总表（复核后）

| 编号 | 缺口 | 类型 | 优先级 | 工作量 |
|---|---|---|---|---|
| F-01 | TripoSR 单图 3D 生成接线 | 随包未接线 | P0 | 1.5d |
| F-02 | SAM ViT-H 分割接线 | 随包未接线 | P0 | 1d |
| F-03 | MiDaS 深度估计接线 | 随包未接线 | P0 | 1d |
| F-04 | YOLOv8 目标检测接线 | 随包未接线 | P0 | 1d |
| F-05 | §4.3 学习策略自适应 3 项 | 代码缺失 | P1 | 2d |
| F-06 | 用户数据本地加密（要求#36） | 安全阉割 | P1 | 3d |
| F-07 | AnimateLCM 图生视频接线 | 随包未接线 | P2 | 3d |
| F-08 | GPT-SoVITS 语音克隆接线 | 随包未接线 | P2 | 4d |
| F-09 | /system/update + 附录B 端点裁剪 | 契约漂移 | P2 | 1d |
| F-10 | GPU 利用率>95% 在途任务降参 | 近似实现 | P2 | 1.5d |
| F-11 | PPO 协同调度引擎（§1.3.3） | 未实现 | P3 | 6d（或文档降级 0.5d） |
| F-12 | Tauri 2.x 桌面壳（要求#38） | 架构阉割 | P3 | 8d（或形态确认 0.5d） |
| F-13 | vLLM 推理后端（§1.2.1） | 技术选型偏差 | P3 | 5d（或文档降级 0.5d） |
| F-14 | 缺失模型资产扩展包（FLUX/Kolors/ControlNet/IP-Adapter/LTX-2/Wan2.1/CogVideoX/8B/语言基座/3D三件套/Real-ESRGAN/CLIP/Blip2/CosyVoice3 等） | 资产缺失 | P3 | 视体积决策 |

合计：纯代码修复（F-01~F-10）约 **19d**；架构项（F-11~F-13）若全做约 **19d**，若走文档降级路线仅 **1.5d**。

---

## 二、P0 —— 随包未接线模型（4 项，~4.5d）

**共性背景**：权重已在 `models/`、manifest 已声明能力、推理依赖已齐备（pydeps 含 `triposr_src`、`ultralytics 8.4.115`、`onnxruntime 1.23.2`、transformers 内置 SamModel），唯后端零调用。属"花小钱兑现已有资产"。

**统一落地模式**：在 `backend/services/inference/` 各建一个引擎模块（懒加载 + feature_lock 互斥 + degraded 标记），在 `backend/api/` 暴露端点，manifest 能力→端点一一对应。

### F-01 TripoSR 单图 3D 生成（1.5d）
- 文档依据：§1.7 3D 清单（TripoSR 在列）、manifest `image_to_3d`
- 资产：`models/3d/TripoSR/model.ckpt`（1.6GB）；依赖 `pydeps/triposr_src` 已在
- 方案：新建 `backend/services/inference/triposr_engine.py`（`tsr.system.TSR.from_pretrained` → `load_image` → `extract_mesh` 导出 glTF/OBJ 至 `data/assets/3d/`）；新增 `POST /api/v1/art/image-to-3d`（`backend/api/draw.py` 或独立 `asset3d.py`）；前端 DirectorStage 增加"图片生成 3D 资产"入口（消费 glTF）
- 验收：上传 PNG → 60s 内（RTX 40 系）返回 .glb 路径；Three.js 端可加载展示
- 涉及：新建 1 引擎 + 1 端点 + 前端 1 入口

### F-02 SAM ViT-H 分割接线（1d）
- 文档依据：§1.7 辅助清单、manifest `segmentation`
- 资产：`models/sam-vit-h/model.safetensors`（2.4GB）；transformers 4.57 内置 `SamModel/SamProcessor`
- 方案：`backend/services/inference/segment_engine.py`（点/框提示 → mask）；端点 `POST /api/v1/art/segment`；首要用途：为 Inpainting 遮罩与漫剧角色抠图供能
- 验收：点提示分割返回 mask PNG，mIoU 抽测达标

### F-03 MiDaS 深度估计接线（1d）
- 文档依据：§1.7 辅助清单、manifest `depth`
- 资产：`models/depth/midas_small.onnx` + `.pt`；onnxruntime 已在
- 方案：`backend/services/inference/depth_engine.py`（ORT CUDA Session）；端点 `POST /api/v1/art/depth`；用途：ControlNet-depth 前置、3D 导演台自动布景
- 验收：输入图 → 深度图 PNG，<3s

### F-04 YOLOv8 目标检测接线（1d）
- 文档依据：§1.7 辅助清单、manifest `detection`
- 资产：`models/detect/yolov8n.pt`；`ultralytics` 包已在 pydeps
- 方案：`backend/services/inference/detect_engine.py`；端点 `POST /api/v1/art/detect`；用途：分镜角色定位、学习页图像内容理解
- 验收：返回检测框 JSON（class/conf/bbox），单帧 <500ms

---

## 三、P1 —— 闭环修补与安全（2 项，5d）

### F-05 §4.3 学习策略自适应 3 项（2d）
- 文档依据：§4.3（修正版E §4.3 / 文档B §4.3）
- 缺口：
  1. 知识库 ≥90k 条时去重阈值 0.85→0.8（`knowledge_service.py` 现为固定 simhash 阈值）
  2. LoRA 训练连续 3 次质量分下降 → 暂停自动微调并通知（`lora_training_service.py` 无计数）
  3. 长期未用功能 → 降低相关学习主题优先级（`predictor.py` 不反哺学习侧）
- 方案：①入库前读 ChromaDB count 动态取阈值；②`train_tasks` 记录 quality_score，learning_scheduler._maybe_auto_finetune 前置检查连续下降计数 → 置 `auto_train_paused` 旗标 + 写通知表；③predictor 输出冷门功能清单，learning_scheduler 评估时降权匹配主题
- 验收：三项各有单测覆盖；阈值切换、暂停通知、降权日志可观测

### F-06 用户数据本地加密（3d）
- 文档依据：要求#36「本地加密存储，敏感数据不可读」、§1.4 安全边界
- 现状：`data/database.py` 明文 SQLite（SQLCipher shim 已移除并诚实标注）
- 方案（二选一，建议 A）：
  - **A. 字段级加密**（1.5d）：对敏感列（对话内容、知识条目正文、行为日志）采用 AES-GCM，密钥派生自机器指纹（DPAPI 保护，免用户口令）；改动集中在 `data/database.py` 读写层
  - B. SQLCipher 全库加密（3d）：需引入 sqlcipher 二进制依赖，离线包体积 +30MB，迁移成本高
- 验收：落库数据文件级不可读；功能回归全绿；启动性能回退 <5%

---

## 四、P2 —— 中等工程（4 项，9.5d）

### F-07 AnimateLCM 图生视频接线（3d）
- 文档依据：§1.7 视频清单、manifest `img2video`；当前 video_engine 仅 Ken Burns 降级
- 资产：`models/video_gen/AnimateLCM/AnimateLCM_sd15_t2v.ckpt`（1.7GB）
- 方案：diffusers `MotionAdapter.from_pretrained`（或 `from_single_file` 解析 ckpt）+ SD1.5 底座（需补 ~4GB SD1.5 权重，或复用 SDXL 之外的轻量底座）+ `LCMScheduler`；挂入 `video_engine.py` 管线探测链（优先级：LTX-2/Wan/CogVideoX 缺失 → AnimateLCM → Ken Burns）；产出真实 2~4s 短 clip 后再接 Ken Burns 扩展时长
- 风险：AnimateLCM 官方为 diffusers 布局，单文件 ckpt 需转换脚本验证；SD1.5 底座未随包（需下载或降级 t2v 不可用只做 i2v 运动化）
- 验收：静帧 → 2s 真实运动 clip（非推拉帧），响应保留 degraded 分级（animatelcm/kenburns）

### F-08 GPT-SoVITS 语音克隆接线（4d）
- 文档依据：§1.7 语音清单、manifest `tts/voice_clone`；当前 voice_engine 仅 CosyVoice3/ChatTTS/SAPI5
- 资产：`models/gpt-sovits/`（hubert/roberta/gsv-v2final/v4/v2Pro/sv，2.6GB）
- 难点：pydeps **无** GPT-SoVITS 推理代码包（GPT-SoVITS 为独立 repo 非 pip 包）；需内嵌其推理链路（hubert 特征 → roberta 文本前端 → s2G 语义 → s2D/vocoder 波形）约 600 行适配代码，或裁剪引入官方 `GPT_SoVITS/inference` 子树
- 方案：voice_engine 新增 `sovits` 引擎类型（探测顺序 cosyvoice → chattts → **sovits** → sapi5 → silent）；参考音频克隆音色；manga.py 配音管线接入
- 验收：3s 参考音频克隆音色合成 24kHz WAV；`get_status().tts_backend` 如实上报 `sovits`

### F-09 /system/update + 附录B 端点裁剪（1d）
- /system/update：RC 免安装形态下无意义 → 在文档B/E 侧标注废弃（0.2d）；若保留则实现"离线增量包校验+解压替换"占位（不推荐）
- 附录B 漂移端点（chat/complete、art/inpaint、art/ip-adapter、style apply/merge/stop/metrics、model/download 等）：逐个裁定"实现 / 文档废弃"；其中 art/inpaint 可借 F-02 SAM 遮罩 + SDXL inpaint 真实落地（+1.5d，可选）
- 验收：双文档与路由表一致，无漂移

### F-10 GPU 利用率>95% 在途降参（1.5d）
- 文档依据：§4.1.2「GPU 利用率 >95% 持续 10s → 降低生成质量参数」
- 现状：analyzer 判定 ✅，但动作仅"下次加载精度降级阶梯"，不调在途任务
- 方案：绘画/视频生成循环注入 `quality_governor` 回调——每 step 查询调度器降级旗标，命中则动态降步数/切低分辨率中间幅面（diffusers 支持中途截断步数；分辨率切换仅在未开始时生效）；WS 广播降参事件
- 验收：压测下在途任务步数从 30→20 可观测，生成不中断

---

## 五、P3 —— 架构项（3 项，需先决策）

### F-11 PPO 协同调度引擎（6d 或文档降级 0.5d）
- 文档依据：修正版E §1.3.3 明确 PPO（状态/动作/奖励/策略更新）；文档B 仅"记录决策与效果"（现实现已符合 B）
- 选项 A：实现轻量 PPO（torch 手写策略网络，状态=显存/CPU/内存/活跃功能，动作=加载/卸载/降精度，奖励=等待负奖励+利用率正奖励+OOM 大额负奖励），6d，含仿真环境训练
- 选项 B（建议）：修正版E 侧标注降级为"历史回归分析 + 阈值自调整"，0.5d 文档修订

### F-12 Tauri 2.x 桌面壳（8d 或形态确认 0.5d）
- 文档依据：要求#38「Tauri 2.x (Rust) 打包，<500MB」
- 现状：无 src-tauri；交付形态 = 浏览器 + launcher（ADR-02 未落地）
- 选项 A：补 src-tauri（Rust 壳内嵌 WebView2，托管 127.0.0.1:5800，窗口/托盘/自启），8d
- 选项 B（建议）：文档侧确认"浏览器 + 原生 launcher"为正式形态，修订要求#38，0.5d

### F-13 vLLM 推理后端（5d 或文档降级 0.5d）
- 文档依据：§1.2.1「推理后端 vLLM」
- 现状：transformers 直接推理；pydeps 无 vllm（Windows 支持差、体积大）
- 选项 A：引入 vLLM（Windows 需 WSL/特定轮子，离线包 +2GB，风险高），5d
- 选项 B（建议）：文档降级为「transformers + 可选 vLLM」，0.5d

### F-14 模型资产扩展包（视决策）
- 缺失清单：FLUX.1-dev-fp8(~12GB)/Kolors 2.1(~7GB)/ControlNet(~5GB)/IP-Adapter(~2GB)/LTX-2(~9GB)/Wan2.1(~28GB)/CogVideoX(~10GB)/Qwen3-VL-8B(~16GB)/语言基座三件套(~35GB)/3D 三件套(~18GB)/Real-ESRGAN(~0.1GB)/CLIP+Blip2(~4GB)/CosyVoice3+ChatTTS(~10GB)
- 建议路线：**基础包（现状 ~46GB）+ 可选扩展包** 分层发布；Real-ESRGAN/CLIP/IP-Adapter 体积小优先纳入下一 RC；LTX-2 纳入则漫剧/视频风格两大模块同时解锁（收益最大，优先级最高）
- 工作量：下载+SHA256 校验+manifest 注册+管道接通，LTX-2 单项约 2d，其余每项 0.5~1d

---

## 六、执行顺序与里程碑

| 批次 | 内容 | 工作量 | 累计 |
|---|---|---|---|
| M1（P0） | F-01~F-04 四模型接线 | 4.5d | 4.5d |
| M2（P1） | F-05 自适应 + F-06 加密 | 5d | 9.5d |
| M3（P2） | F-07 AnimateLCM + F-08 SoVITS + F-09 契约 + F-10 在途降参 | 9.5d | 19d |
| M4（P3 决策） | F-11/12/13 文档降级或实做；F-14 扩展包 | 1.5d ~ 19d+ | — |

**推荐路径**：M1+M2 全做（高性价比赛道）；M3 中 F-08 按"语音克隆是否为产品卖点"取舍，F-07 依赖 SD1.5 底座下载决策；M4 全部走文档降级（与文档B 对齐），把资源投入 F-14 的 LTX-2 + Real-ESRGAN 扩展包（对六大模块能力解锁收益最大）。

## 七、验证与回归
- 每项修复执行：`startup_check.py` 全量检查 + 对应模块 API 冒烟 + degraded 标记核验
- M1/M2 完成后跑 RC 打包脚本 `pack_rc.ps1` 验证体积与首启
- 文档同步：修复完成后回写《修正版E》对应条目状态，消除双文档漂移
