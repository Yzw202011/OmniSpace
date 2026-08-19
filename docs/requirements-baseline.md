# OmniSpace AI 需求基线（仓库内规格章节 · RTM 附录 A）

> **来源**：从《极致细粒度全量文档E》（修正版）与《程序开发计划文档B》抽取——两文档均在仓库外（根目录 txt，不入库）。
> **地位**：需求-矩阵对应关系的**仓库内真源**。RTM 各条目的「来源§」自此指向本基线章节号，仓内即可独立校验对应关系，无需翻阅外部文档。
> **收录规则**：只收录**仍有效**的需求；已豁免（⚪）/裁剪项标注裁定出处；降级实现（🟨）如实标注降级路径。
> **抽取时间**：2026-08-20（TASK-P2-04）｜ 状态图例与 `requirements-traceability.md` 一致（✅/🟦/🟨/❌/⚪）

---

## A.1 产品定义与核心约束（B §1.1 / E §1.4）

**产品定义**：OmniSpace AI v2.3.1——AIGC 桌面创作工具 + 自主学习 AI 助手。为影视创作者提供离线化、智能化、一站式 AIGC 创作流，通过自主学习引擎实现个性化模型进化与创作辅助。运行环境 Windows 10 21H2+ / Win11 x64；分发形态：完整离线安装包或基础包 + 用户手动导入模型。

**核心约束**（仍有效）：

| 约束 | 基线要求 | 现状对应 |
| --- | --- | --- |
| 硬件底线 | RTX 3060 12GB / RTX 4060 8GB 起，推荐 4090 24GB+；内存 ≥32GB | 实机 RTX 5070 Ti 16GB；模型路由表按显存分 8 档（部署手册 §3.2） |
| 网络要求 | 核心功能 100% 离线可用；仅网页抓取与可选遥测需网络 | ✅ 全链路本地推理 |
| 安全边界 | 浏览器进程沙箱隔离；训练数据仅存本地；禁止未授权模型外传 | A-03 ✅ 回环强制；A-02 ✅ 敏感字段 AES-256-GCM（P2-05） |
| 隐私 | 用户数据本地加密存储 | A-02 ✅（schema v3 存量迁移完成，P2-05） |

**裁定过的口径修正**（有效基线 ≠ 原文照抄）：

- 模型清单 "~392GB" 为文档口径；实测磁盘基线 = `models/` 193.25GB / 24 目录（RTM P0-04 实测）。模型需求以 RTM M-01~M-16 为准。
- 推理栈 vLLM / ComfyUI-Core / Python 3.12 → 实际 HF transformers / diffusers / 内嵌 Python 3.10（离线单机 + 16GB 显存约束下的等价替代，功能需求不变、实现栈降级）。
- Celery+Redis 四队列 → ⚪ 进程内功能锁 + P0-P5 优先级等价替代（RTM F-13 豁免裁定）。

## A.2 六大核心模块功能需求（E §1.2）

### A.2.1 AI 对话 OmniChat（§1.2.1）

功能清单：多模态对话（文本+图像输入、流式输出）、剧本润色、分镜描述生成、Agent 工具调用、上下文记忆（多轮+会话持久化）、RAG 增强（对话前检索 Top-5）、被动补全（知识缺口→联网学习）、流式输出。
**RTM 对应**：F-01（被动补全 ❌）、F-10（显存路由 ✅）、M-01/M-16（模型）。多模态对话/RAG/记忆为主线已实现，端点证据见 `docs/design/api-endpoints.md` §对话域。Agent 工具调用：❌ 无 Function Calling 协议实现（38 项清单 #29）。

### A.2.2 AI 绘画 OmniDraw（§1.2.2）

功能清单：文生图（中文 Prompt）、图生图（相似度可调）、ControlNet 姿态控制、IP-Adapter 风格迁移（多图权重混合）、局部重绘、超分辨率（Real-ESRGAN 4x）、Prompt 优化（对话扩写）、批量生成。
**RTM 对应**：M-02（FLUX 🟦 接线排期）、M-16（ControlNet/IP-Adapter ❌ 未下载）。已实现：SDXL 文生图/图生图（paint_engine，sampler 三映射 + seed 复现）；超分 🟨（Real-ESRGAN 缺失时 PIL LANCZOS 降级并标注）；局部重绘 ❌（F-11 附录B 端点未实现）。

### A.2.3 漫剧创作 OmniComic（§1.2.3）

七步流程：①创建项目（名称唯一性/模板）→ ②导入剧本（DSL 上传 ≤10MB / 粘贴 / 自由文本 / NLP 语义分析）→ ③分镜（自动逐镜 + 手动四式 + 拖拽排序 + 模式切换）→ ④资产图（角色 4096×1024 四视图 / 场景 / 道具 + 音色绑定两层）→ ⑤分镜描述词（AI 辅助 + 风格一致性）→ ⑥导演台（3D 编排 + 自动资产拉取 + 四分镜合一 + WebGL 降级 2D）→ ⑦视频生成（三路输入，<60s/5s 片段）。
**RTM 对应**：F-06（分镜四端点 ✅）、F-07（导演台 /export ❌）、F-08（解说漫剧 ✅）、A-04（Three.js ✅）、M-03/M-13（视频模型 ✅）。出图规格铁律：资产图/分镜图统一 2560×1440（SDXL 1280×720 + LANCZOS 2x 上采样，2026-08-14 用户裁定，代码 manga.py IMG_TARGET_W/H）。

### A.2.4 知识学习 OmniLearn（§1.2.4，v2.3 新增）

功能清单：内置浏览器网页解析、资料结构化、Agent 自动摘要、LoRA 增量训练触发；输入 URL/本地文档/剪贴板 → 结构化知识库/LoRA 权重。
**RTM 对应**：F-01~F-05（学习闭环五断点 ❌）、F-02（触发器框架 ❌）、F-03（LoRA 自动微调 ❌）。已实现：知识入库/RAG 检索/FTS/图谱（knowledge 域 647 行 + graph_store + fts_store）、手动训练 POST /learn/train。

### A.2.5 模型管理 ModelManager（§1.2.5）

功能清单：模型库索引、三方协同调度、Hash 校验、热加载/冷卸载。
**RTM 对应**：M 域全部 + 工程主线。已实现：ensure_loaded（显存检查→驱逐→加载）、get_gpu_status（pynvml 降级容错）、互斥矩阵、驱逐优先级、force_unload 热切换（38 项 #26/#27 ✅，P1-05 后探测失败显式降级不造假）。

### A.2.6 视频风格 VideoStyle（§1.2.6）

功能清单：视频风格 LoRA 训练、推理应用、帧间一致性处理；输入视频片段/参考图 → 风格化视频/LoRA 权重。
**RTM 对应**：F-11（style apply/merge/stop/metrics 端点 ❌——注意 merge 已单独实现 POST /style/merge，F-11 按附录B 口径整体计 ❌）。已实现：style_lora_service（版本管理 rollback/merge/get_current）、帧间一致性 🟨（无光流法，靠 LoRA 风格约束）。

## A.3 五大智能引擎需求（E §1.3）

| 引擎 | 基线要求 | 现状 | RTM |
| --- | --- | --- | --- |
| LTX-2 LoRA 微调 | 显存分块训练，24GB 可训 720p LoRA | 🟨 lora_training_service（gradient_checkpointing ✅；基座降级 SD1.5/SDXL） | —（主线） |
| ML 预加载预测 | XGBoost 时序预测，预测准确率>85% | 🟨 predict_next_feature/record_feature_switch 存在（简化预测，非 XGBoost） | —（主线） |
| 协同调度历史学习 | PPO 强化学习优化资源分配，效率+20% | ❌ scheduler/history.py 仅决策落库+回归分析 | F-15 |
| AV1 硬件编码 | NVENC AV1 API（RTX50 >120fps / RTX30-40 >80fps），软编 SVT-AV1 兜底 | 🟨 ffmpeg codec 参数支持 AV1（默认 h264），无 NVENC AV1 专用路径 | —（主线） |
| 自主学习引擎 | RAG + Agent 决策树 + 增量学习闭环，创作延迟增加<5% | ❌ 五断点（被动补全/触发器/自动训练/LoRA 注入/进度推送全缺） | F-01~F-05 |

## A.4 功能协调需求（E §2.1/§2.3/§2.5/§2.6）

**互斥规则**（§2.1，✅ 已实现于 `MUTUAL_EXCLUSION_MATRIX`，与基线一致）：对话/绘画/漫剧视频/训练四类重量级功能两两互斥；浏览器学习与行为学习为后台低优先级，不互斥任何功能（manga/video 同义归一为 video_gen）。

**加载优先级**（§2.3 P0-P5）：P0 用户当前操作（不可抢占）→ P1 生成任务 → P2 长任务（视频）→ P3 后台训练 → P4 附属模型（随基座卸载）→ P5 常驻服务。✅ 实现为 `_EVICTION_PRIORITY`（auxiliary/embedding=0 最先驱逐 → dialog=5 最后），数值方向相反、语义一致。

**后台服务优先级**（§2.5）：用户操作 > 预加载 > LoRA 训练 > 浏览器学习 > 行为记录（<10ms）> 知识清理。✅ 功能锁 + 调度器实现等价语义。

**知识反哺**（§2.6）：网络学习→ChromaDB 即时可用；训练数据 ≥100 条触发 LoRA 微调；微调完成新 LoRA 注册全功能可用；旧 LoRA 保留可回滚。❌ 触发与应用两端断裂（F-02/F-03/F-04），仅数据积累端（≥100 条判定）在位。

## A.5 三十八项核心要求清单 ↔ RTM 映射（E §1.5 / B §1.5）

> 「RTM」列为空 = 主线实现未单列入矩阵（端点证据可查 `docs/design/api-endpoints.md`）；「—」= 该项整体豁免或归属既有条目。

| # | 需求（验收标准） | RTM | 状态 |
| --- | --- | --- | --- |
| 1 | Qwen3-VL 多模态对话（首字<800ms，显存<12GB） | M-01, F-10 | ✅ 8B/4B/2B 三档接线（vLLM→transformers 降级） |
| 2 | FLUX.1-dev 文生图 FP8（1024² <15s） | M-02 | 🟦 flux2-klein-4b 已下载，接线排期 P1 |
| 3 | Kolors 2.1 中文优化（中文准确率>95%） | M-16 | ❌ 未下载（重裁清单候选） |
| 4 | SDXL+ControlNet 联动（姿态误差<2px） | M-16 | SDXL ✅；ControlNet ❌ 未下载 |
| 5 | IP-Adapter 风格迁移（SSIM>0.85） | M-16, F-11 | ❌ 未下载，端点未实现 |
| 6 | 分镜表 DSL 解析（成功率 100%） | F-06 | ✅ storyboard 四端点 + reorder 落库 |
| 7 | 3D 导演台资产加载（USDZ/glTF，10万面<3s） | M-14, A-04 | ✅ TripoSR（.glb）+ Three.js r170 |
| 8 | LTX-2 视频生成（720p/30fps，5s<60s） | M-03 | ✅ ltx-video-0.9.5 接线 |
| 9 | Wan2.1 视频生成（FVD<200） | M-16 | ❌ 未下载 |
| 10 | CogVideoX 长视频（接缝不可见） | M-16 | ❌ 未下载 |
| 11 | 内置浏览器 CEF（沙箱，泄漏<50MB/h） | — | 🟨 已实现为 playwright（CEF→playwright 降级，7xxxx 错误码体系完整） |
| 12 | AI Agent 知识抽取（召回率>90%） | — | 🟨 knowledge 管线（LangChain→自研抽取降级） |
| 13 | LoRA 进化训练（无灾难性遗忘） | F-03 | ❌ 自动触发缺失（仅手动 /learn/train） |
| 14 | 模型库索引（392GB 索引<30s） | — | ✅ models 域 926 行 + 磁盘扫描 + 路径提示表 |
| 15 | 三方协同调度（冲突率<1%） | — | ✅ model_manager 显存/内存协调 + 功能锁 |
| 16 | 视频风格 LoRA 训练（一致性>0.9） | — | ✅ style 域 + lora 版本管理 |
| 17 | LTX-2 微调引擎（24GB 训 720p） | — | 🟨 gradient_checkpointing ✅；基座降级 SD 系 |
| 18 | ML 预加载预测（准确率>85%） | — | 🟨 简化预测在位，非 XGBoost |
| 19 | 历史学习调度 PPO（效率+20%） | F-15 | ❌ 无策略网络与 PPO 更新 |
| 20 | AV1 硬编 RTX50（>120fps） | — | 🟨 ffmpeg codec 参数级支持，无 NVENC AV1 专用路径 |
| 21 | AV1 硬编 RTX30/40（>80fps） | — | 🟨 同上 |
| 22 | AV1 软编 SVT-AV1（CPU<80%） | — | 🟨 ffmpeg 传递，无预设调优 |
| 23 | 自主学习引擎（延迟增加<5%） | F-01~F-05 | ❌ 学习闭环五断点 |
| 24 | 离线包完整性（SHA256 校验） | E-07 | 🟨 build_rc --verify 为 robocopy 差异比对（非逐文件 SHA256） |
| 25 | 基础包模型导入（自动识别注册） | — | ✅ 磁盘扫描 + _MODEL_PATH_HINTS |
| 26 | 显存监控与保护（OOM 自动卸载） | — | ✅ vram_manager + 20013 闸门 + 驱逐（P1-05 探测降级可观测） |
| 27 | 多模型热切换（UI 不卡顿） | — | ✅ force_unload + 异步加载 |
| 28 | 知识库 RAG 检索（延迟<100ms） | M-08 | ✅ ChromaDB + bge-large-zh（bge-m3 🟦 升级候选） |
| 29 | Agent 工具调用（成功率>98%） | — | ❌ 无 Function Calling 协议实现 |
| 30 | 3D 场景渲染 Three.js 浏览器内 | A-04 | ✅ three 0.170，无独立渲染进程 |
| 31 | 视频帧间一致性（闪烁率<1%） | — | 🟨 LoRA 风格约束，无光流法 |
| 32 | LoRA 权重合并（可用性验证） | F-11 | ✅ POST /style/merge（adapter 加权和 + merged_from 溯源） |
| 33 | 训练 Checkpoint 管理（Top-K 清理） | — | ✅ checkpoints 目录 + rollback/merge/get_current |
| 34 | 编码码率自适应（CRF/VBR） | — | 🟨 ffmpeg 参数级 |
| 35 | 浏览器安全策略（CSP+隔离） | A-03 | ✅ 回环绑定强制 + 非回环拒绝（P0-05） |
| 36 | 用户数据隐私（本地加密） | A-02 | ✅ crypto.py 接入 DB 层 + schema v3 存量迁移（P2-05 完成） |
| 37 | 错误日志结构化（JSON+堆栈） | — | ✅ 统一错误信封 {success,error{code,message,detail,suggestion}} |
| 38 | Tauri 2.x 桌面壳（安装包<500MB） | A-01 | ⚪ 裁剪：浏览器+launcher 为正式形态（ADR-002，2026-08-20） |

**统计**：✅ 17 ｜ 🟨 10 ｜ 🟦 1 ｜ ⚪ 1 ｜ ❌ 8 ｜ 其中 7 条已有 RTM 追踪（M-16/F-03/F-11/F-15/F-01~F-05），未追踪缺失仅 #29（Agent 工具调用）。

## A.6 附录级需求（E 附录B/附录C/附录D）

- **附录B（API 端点完整清单）**：chat/complete、art/inpaint、art/ip-adapter、style apply/merge/stop/metrics、model/download 等细粒度端点 → **F-11 ❌**（merge 除外，见 #32）。现行 270 端点权威表：`docs/design/api-endpoints.md`。
- **附录C（数据库 Schema）**：users / assets 表 → **F-14 ⚪**（本地单用户豁免）；learning_sessions / knowledge_entries / lora_trainings / model_configs 等 → ✅ 已实现（30 表全表见 `docs/design/database-er.md`）。
- **附录D（15 项用户可调整项）**：实现于 settings 域（前端设置页 + 配置落盘），未单列入矩阵。

## A.7 基线管理规则

1. 本基线是 RTM 的附录 A：RTM「来源§」指向此处章节，此处「RTM」列反向指向矩阵 ID，双向可校验。
2. 需求变更先改本基线（含裁定记录），再改 RTM 状态，同一次提交完成。
3. 豁免/裁剪必须留裁定痕迹（谁裁定、何时、理由），禁止静默删除需求行。
4. 外部文档（E/B）与基线冲突时，以本基线 + RTM 为准（仓库内真源原则）。
