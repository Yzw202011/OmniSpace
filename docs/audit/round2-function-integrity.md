# 第二轮审计：功能完整性与需求符合性（后端）

- 日期：2026-08-07
- 范围：`e:\OmniSpace\backend\` 对照需求文档逐条核查（只做审计，未改动任何代码）
- 基线文档：文档B《OmniSpace AI v2.3.1中文版（修正版B）》（可信基线）；文档E《极致细粒度全量文档（修正版E）》（细粒度任务清单，冲突时以 B 为准）
- 既有结论：Round0/Round1 已修复项（BK-002/010/011/012/013/014/018/023/027/028/041/042/046/047/048 等）不重复报告；"诚实降级"（明确 degraded 标记）计为符合。

---

## 一、API 端点完整性比对

### 1.1 文档B §7.1 基线比对（权威）

| 模块 | 文档B 要求端点 | 代码现状 | 结论 |
|---|---|---|---|
| /chat | send, history, clear, stream | 全部存在（另有 sessions CRUD/favorites/rating/stop/WS 超集） | ✅ |
| /paint | generate, img2img, upscale, history | 全部存在（另有 result/models/status/controlnet preview） | ✅ |
| /storyboard | create, list, reorder, ai-describe, preview | create ✅；**list / reorder / ai-describe / preview 缺失** | ❌ 见 R2-B06/B07 |
| /director | scene, render, export | panorama≈scene、screenshot-4in1≈render（功能近似、命名不同）；**export 缺失** | ⚠ 见 R2-B08 |
| /video | generate, progress, export | generate ✅；{task_id}/status≈progress ✅；{task_id}/download≈export ✅ | ✅（命名差异） |
| /models | list, load, unload, health | 全部存在（另有 status/predict/vram/usage/import/verify/select 超集） | ✅ |
| /style | train, preview, versions | 全部存在（另有 upload/datasets/tasks/rollback/status 超集） | ✅ |
| /system | info, hardware, update | hardware 在独立 /hardware 模块（功能等价）；**info、update 缺失** | ❌ 见 R2-B09 |
| /learn（§7.1.2 共 22 条） | — | 21/22 存在；**WS /learn/session/progress 缺失** | ❌ 见 R2-B05 |
| /browser（§7.1.3 共 6 条） | — | 6/6 存在（另有 handback 超集） | ✅ |

### 1.2 文档E 附录B 细粒度比对（B 基线未列出的额外缺失，汇总为 R2-B11）

- chat：/complete、/context（POST+DELETE）、/models、/usage
- art（E 前缀）：/inpaint、/ip-adapter、/styles、任务取消 DELETE、历史删除 DELETE
- model（E 前缀）：/download、/download/{task_id}、/config PUT
- style：/{id} DELETE、/{id}/apply、/{id}/metrics、/{id}/merge、/train/{id}/stop
- system：/metrics、/logs、/restart、/gpu、/disk、/config（GET/PUT）
- learn：/knowledge/import/url、/lora/{id}/metrics、/lora/{id} DELETE
- comic（E 前缀）：scene CRUD/reorder/render/export 系列（与 B 基线 director 差距重叠）

### 1.3 代码有而文档无（超集，不计问题）

/hardware/*（info/realtime/synergy/WS）、/system/diagnose、/system/backup、/system/project/import|export、/models/predict|usage|select|import、/dialog/* 与 /chat/* 双前缀别名、/knowledge/* 与 /behavior/* 顶层别名等——均为前端契约所需或兼容别名，属合理超集。

---

## 二、问题清单

### P1（功能不完整或偏离文档）

---

**R2-B01 对话"被动补全"流程缺失**
- 文档依据：文档B §7.1.6.1 步骤4「被动补全：检测不确定性标记 → 提取关键词 → 快速搜索1~3页 → 补充上下文重新推理」；§7.1.5 ChatService 职责含"被动补全"
- 代码现状：全后端无"被动补全/不确定性/passive"任何实现（`backend/api/dialog.py:224` dialog_send 流程为 RAG注入→组装→推理→落库，无补全分支）；`learning_scheduler.py:106` 仅定义 `TRIGGER_DIALOG_GAP` 常量，无触发逻辑
- 差距：对话发现知识缺口后即时补搜重推理的能力整体缺失
- 严重度：**P1**
- 修复建议：在 dialog 推理后对回复做不确定性标记检测（如"我不确定/没有相关信息"），命中时提取关键词调 browser_agent 快速搜索 1~3 页，注入上下文重推理一次；同时 fire `dialog_gap` 触发器沉淀长期学习

---

**R2-B02 学习触发器框架未接线（5 个触发器仅"手动"生效）**
- 文档依据：文档B TASK-014/学习时机「手动/定时/空闲>5分钟/项目驱动/对话缺口」
- 代码现状：`backend/services/learning_scheduler.py:153/170` 定义了 `register_trigger`/`fire_trigger`，但**全后端无任何调用方**；`is_user_idle()`（:194）与 `notify_user_activity()`（:185）同样无调用方；`evaluate()` 仅被 `/learn/quota` API（`learning.py:553`）用于展示，无后台周期循环驱动
- 差距：定时/空闲/项目驱动/对话缺口 4 个自动学习触发不生效，学习会话只能手动 POST /learn/session/start 启动
- 严重度：**P1**
- 修复建议：在 main 启动时注册 5 触发器回调（空闲触发调 `evaluate()` 的 start 动作）；增加后台协程周期评估；在对话/绘画 API 入口上报 `notify_user_activity()`

---

**R2-B03 LoRA 自动微调未接线（设置存在但无执行器）**
- 文档依据：文档B line 170「训练数据 ≥ 100条 → 触发LoRA微调」；line 229「GPU 空闲且数据够 → 触发 LoRA 微调」；§4.4「自动微调频率」设置项
- 代码现状：`browser_agent_service.py:190` 有 `auto_finetune_frequency: "daily"` 设置；`lora_training_service.py:113` 有 `MIN_TRAINING_SAMPLES=100` 与充分性检查（:293/:484），但**没有任何后台任务在数据达标或到频时自动调用训练**；训练仅由 POST /learn/train、/learn/lora/train 手动触发
- 差距："数据积累 → 自动进化"闭环的第一环缺失
- 严重度：**P1**
- 修复建议：增加后台调度（可挂在 scheduler tick 或独立协程）：按 auto_finetune_frequency 检查频率窗口 + `prepare_training_data()["total"] >= 100` + GPU 空闲（feature_lock 无持有者）→ 自动入队训练；训练前经 §4.1 阈值门控

---

**R2-B04 训练产出的知识 LoRA 未接入推理（"全部功能可使用"闭环断裂）**
- 文档依据：文档B line 116「对话：RAG+LoRA」；line 171「微调完成 → 新LoRA注册 → 全部功能使用」
- 代码现状：`PeftModel` 仅出现在 `lora_training_service.py`（:839 续训加载、:1120 评估加载）；`dialog_engine.py` 无任何 lora/adapter/PeftModel 引用，推理永远只用基座权重
- 差距：LoRA 训练、版本管理、回滚都真实存在，但训练成果从不被对话（及依赖对话模型的浏览器决策、分镜拆分）加载使用，学习进化闭环在"应用"环节断裂
- 严重度：**P1**
- 修复建议：dialog_engine `load_model` 后在存在已注册 LoRA 版本时 `PeftModel.from_pretrained(model, active_version_dir)` 挂载 adapter；版本切换/回滚时热卸载旧 adapter；保持无 adapter 时的纯基座回退

---

**R2-B05 学习进度 WebSocket 推送端点缺失**
- 文档依据：文档B §7.1.2 line 761「/api/v1/learn/session/progress WS 实时进度 WebSocket推送」
- 代码现状：`backend/api/learning.py` 全部为 HTTP 端点；全后端 WS 仅 /hardware/realtime（`hardware.py:279`）与对话流（`dialog.py:843`）；前端只能轮询 GET /learn/session/status（`learning.py:379`）
- 差距：学习过程无实时推送通道，进度/日志只能轮询，与文档契约不符
- 严重度：**P1**
- 修复建议：新增 `@router.websocket("/learn/session/progress")`，复用 browser_agent_service 的进度回调向连接端推送（参照 hardware/realtime 的 2s 推送模式）

---

**R2-B06 分镜列表与排序端点缺失（/storyboard/list、/reorder）**
- 文档依据：文档B §7.1.4 line 788「漫剧分镜 /create, /list, /reorder, /ai-describe, /preview」；TASK-COMIC-001 验收「分镜CRUD+拖拽排序」
- 代码现状：`backend/api/manga.py` 有 create（:235）/get（:252）/行更新（:270）/整体保存（:310）/auto-split/import/export，但**无全量分镜列表端点**；`StoryboardRowUpdate`（`data/models.py:357`）**不含 sort_index 字段**，行序无法通过任何 API 持久化
- 差距：前端无法枚举已有分镜项目；拖拽排序结果无法保存
- 严重度：**P1**
- 修复建议：新增 GET /storyboard/list（联 projects 表分页）；PUT /storyboard/{project_id}/reorder（接收 row_id 有序数组批量更新 sort_index）

---

**R2-B07 分镜 AI 描述生成与预览端点缺失（/ai-describe、/preview）**
- 文档依据：文档B §7.1.4 line 788；§7.1.5 StoryboardService「分镜CRUD+AI描述」
- 代码现状：`manga.py:348` auto-split 仅支持"整段剧本 → 批量拆分生成行"（部分等价 AI 描述）；**无单行级描述再生成端点，无分镜预览端点**
- 差距：用户修改某行台词/场景后无法单独让 AI 重生成该行画面描述；分镜无快速视觉预览通道
- 严重度：**P1**（auto-split 记为部分实现）
- 修复建议：新增 POST /storyboard/{project_id}/rows/{row_id}/ai-describe（单行上下文调对话模型重生成 description）；/preview 可复用 director screenshot-4in1 管线按行出图

---

### P2（次要偏差）

---

**R2-B08 导演台 /export 缺失；scene/render 命名与文档不符**
- 文档依据：文档B §7.1.4 line 789「3D导演台 /scene, /render, /export」
- 代码现状：`manga.py:546` /director/panorama≈scene 搭建、:561 /director/screenshot-4in1≈render（功能近似，命名不同）；**无导演台导出端点**（视频导出依附 /video/{task_id}/download）
- 严重度：**P2**
- 修复建议：补 POST /director/export（4in1 截图/全景图打包导出）；或在文档侧确认 panorama/screenshot-4in1 为正式命名并同步

---

**R2-B09 /system/info 与 /system/update 缺失**
- 文档依据：文档B §7.1.4 line 793「系统设置 /info, /hardware, /update」
- 代码现状：`system.py` 有 settings/backup/diagnose/project 导入导出/version（:759 部分等价 info）；硬件信息在 /hardware/info（功能等价、模块位置不同）；**软件更新端点完全缺失**
- 严重度：**P2**
- 修复建议：合并 version+hardware 摘要为 GET /system/info；/system/update 若为免安装 RC 有意省略，应在文档侧标注删除

---

**R2-B10 对话模型未按硬件等级路由（tier 表 dialog 列不可达）**
- 文档依据：文档B §4.2 六档映射（5090/4090→Qwen3-VL-8B，4070Ti→4B，3060/RX6600→2B）；§7.1.6.1 步骤3「根据GPU显存选择模型（8B/4B/2B）」
- 代码现状：`data/models.py:112` HARDWARE_TIER_TABLE 六档齐全且已用于硬件 API/学习配额；但 `dialog_engine.py:43` `DIALOG_MODEL_CANDIDATES` 仅 (qwen3-vl-4b, qwen2-vl-2b) 两项，**不查询 tier 表，qwen3-vl-8b 永不入选**
- 差距：高端显卡用户无法自动路由到 8B 对话模型
- 严重度：**P2**
- 修复建议：`_pick_model` 先调 `detect_hardware_tier()` 取 dialog 列模型为首选，再按候选表回退；或把 8b 纳入候选并标注显存门槛

---

**R2-B11 文档E 附录B 细粒度端点缺失合集**
- 文档依据：文档E 附录B B.1~B.7
- 代码现状：见本报告 §1.2 清单（chat/complete、/context、/models、/usage；art/inpaint、/ip-adapter、/styles、任务取消、历史删除；model/download、/config；style apply/merge/stop/metrics/delete；system metrics/logs/restart/gpu/disk/config；learn import/url、lora metrics 等）
- 差距说明：上述端点均未被文档B 基线要求，属 E 文档独有的细化契约；其中 model/download 与离线 RC 定位冲突（有意省略合理）
- 严重度：**P2**
- 修复建议：按前端实际需要裁剪补齐；不需要的端点在文档E侧标注废弃，消除双文档漂移

---

**R2-B12 §4.3 学习策略自适应 3 项未实现**
- 文档依据：文档B §4.3「知识库接近10万条→去重阈值0.85→0.8」「LoRA训练连续3次质量下降→暂停自动训练并通知」「用户长时间未使用某功能→降低相关学习优先级」
- 代码现状：`knowledge_service.py` 去重为固定 simhash 相似度判定（:263），无随容量调整阈值逻辑；`lora_training_service.py` 无连续质量下降计数/暂停；`predictor.py` 仅统计使用频率做预加载预测，不反哺学习优先级
- 严重度：**P2**（第4项「行为数据<50条不触发微调」已由 MIN_TRAINING_SAMPLES=100 更严格满足，计符合）
- 修复建议：知识入库前读取当前总量，≥90k 时去重阈值 0.85→0.8；train_tasks 记录质量分，连续3次下降置自动训练暂停旗标并写入通知表

---

**R2-B13 §4.4 学习设置默认值三处与文档不符**
- 文档依据：文档B §4.4「自动微调频率 默认每周（每日/每周/每月/手动）」「敏感内容处理 默认标记待审核（跳过/标记/停止）」「显示浏览器 默认开启」
- 代码现状：`browser_agent_service.py:187-190`：`auto_finetune_frequency="daily"`（文档：每周；枚举缺"每月"）、`sensitive_content_handling="skip"`（文档：标记待审核；枚举缺"停止"）、`show_browser=False`（文档：开启）
- 严重度：**P2**
- 修复建议：默认值对齐文档（weekly / mask / True），枚举补齐 monthly 与 stop；若 RC 有意调整，修订文档侧

---

**R2-B14 端口与 WebSocket 路径和文档契约不符**
- 文档依据：文档B line 1298-1299「API基础URL：http://localhost:8000/api/v1」「WebSocket：ws://localhost:8000/ws/{module}」
- 代码现状：`config.yaml` port=5800；WS 挂在 /api/v1/hardware/realtime 与对话流端点，非 /ws/{module}
- 严重度：**P2**（5800 已是 RC 交付事实，前后端同源自洽）
- 修复建议：以代码事实修订文档B；或 config 增加 8000 监听别名（不建议，避免双端口混乱）

---

**R2-B15 Celery+Redis 四队列架构未实现（进程内等价替代）及两处流程细节偏差**
- 文档依据：文档B line 837「Celery任务队列（high/medium/low/background四队列）」、line 708/745 Redis；§4.1「GPU利用率>95%持续10s→降低生成质量参数」；§7.1.6.1 步骤6「触发行为记录（后台异步）」
- 代码现状：无 Celery/Redis 依赖，任务并发由"功能锁互斥 + 后台线程 + Priority P0-P5 让行"（`services/priority.py`）进程内等价实现；GPU 高利用率动作由 dispatcher 精度降级阶梯（`dispatcher.py:166`）近似，**不调整在途生成的步数/分辨率参数**；`dialog.py` 推理后**未异步触发行为记录**（行为事件仅由前端 POST /behavior/event 驱动）
- 严重度：**P2**（架构级偏差，功能语义大体等价；离线单机场景下进程内方案可合理化）
- 修复建议：文档侧将 Celery/Redis 标注为"可选横向扩展方案，当前进程内实现"；dialog 落库后异步写 behavior_logs（P4 优先级）

---

**R2-B16 文档E 附录C users 表与 assets 表未建**
- 文档依据：文档E 附录C C.1 users、C.3 assets
- 代码现状：实际 26 表（`data/database.py` 14 + graph/fts 3 + 各服务自建 9）覆盖对话/分镜/导演台/音色/视频/训练/学习/行为/知识/图谱/FTS；**无 users 表**（文档B §7.1.6.1 明确"本地应用，无需Token"，可合理化）；**无独立 assets 表**（资产以文件系统+JSON 引用存储，如 director_characters.character_assets）
- 严重度：**P2**
- 修复建议：users 在文档E侧标注删除；assets 若前端需要统一资产管理再建表，否则文档标注"文件系统+引用"方案

---

## 三、智能引擎实现程度（文档声称五引擎）

| 引擎 | 文档依据 | 实现程度 | 结论 |
|---|---|---|---|
| 1. LTX-2 LoRA 微调引擎 | 文档B R32/TASK-STYLE-001 | `style_lora_service.py` QLoRA 4bit 管线完整（帧序列 VAE latent → DiT 训练 → 评估 → 版本/回滚）；基座权重未随包时如实 80010 门控（:248），不伪造进度 | ✅ 符合（诚实门控） |
| 2. 模型预加载 ML 预测引擎 | 文档B line 1225/§3.4 | `model_manager/predictor.py` 马尔可夫链+时间特征融合（<50ms、SQLite 持久化、预热），engine 字段如实上报 "markov+time"；XGBoost 迁移路径保留并诚实标注 | ✅ 符合（诚实回退） |
| 3. 协同调度历史学习引擎 | 文档B 第四章引擎3 | `scheduler/history.py` 每次模式切换落库（触发源/等待/成功率），支持阈值自调整（显存预警线 -5pt） | ✅ 符合 |
| 4. AV1 硬件编码引擎 | 文档B TASK-013/§3.5 | `encoder_service.py` 启动探测 5 编码器，按 GPU 分代路由（50系→av1_nvenc / 30-40系→h264_nvenc / AMD/CPU→libsvtav1），AV1→H.264 失败降级链 | ✅ 符合 |
| 5. 自主学习引擎 | 文档B v2.3 新增 | `browser_agent_service.py` 决策循环完整（搜索→点击→阅读→提取，分级决策 <5s/<500ms 双路径、撞墙检测、检查点续学）；**但自动触发未接线（R2-B02）** | ⚠ 核心符合、触发残缺 |

## 四、调度与互斥核查（§4.1/§5.1/§8.4.2）

| 核查项 | 文档要求 | 代码现状 | 结论 |
|---|---|---|---|
| 功能互斥 | 对话/绘画/视频/训练同时仅一类 | `feature_lock.py` 四类互斥 + 热保护门控，占用时 40007 + 原因 | ✅ |
| 显存 >90% | 降精度/卸载非活跃 | 阈值 0.90（config.yaml），dispatcher degrade/force_unload 真实接线 | ✅（Round1 BK-023） |
| GPU 利用率 >95% 持续 10s | 降低生成质量参数 | analyzer 持续 10s 判定 ✅；动作为精度降级阶梯（下次加载生效），不调整在途生成参数 | ⚠ 近似（R2-B15） |
| CPU >90% | 暂停后台学习 | learning_scheduler 真实采样判定（:301-305） | ✅ |
| 内存 >85% | 清理缓存 | mem_critical_ratio 0.15（可用<15% 等价）+ 缓存释放 | ✅ |
| 磁盘 IO >80% | 延迟非关键写入 | 调度周期置 disk_io_busy 旗标并向状态/API 暴露 | ✅ |
| 模式切换滞回 | 30 秒 | decision.py hysteresis_seconds=30s，ALL_TENSE 紧急直通 | ✅ |
| 优先级 P0-P5 | 用户>预载>训练>浏览学习>行为>清理 | priority.py 六级定义；学习 P3 让行逻辑真实（_FEATURE_PRIORITY 比较） | ✅ |
| 学习配额矩阵 | 空闲/创作/推理/断网/电池/低内存 六态 | get_resource_quota 纯函数六态 + 硬件等级标签封顶 | ✅ |

## 五、数据层与配置符合性

- **SQLite**：26 表，WAL 模式；业务覆盖面超出文档E 附录C（users/assets 两表未建，见 R2-B16）
- **ChromaDB**：知识容量上限 100,000 条（`knowledge_service.py:47` MAX_KNOWLEDGE_COUNT），超限清理最旧 100 条（:800 _enforce_capacity）✅
- **FTS5**：`data/fts_store.py` trigram 中文滑窗索引 + BM25 排序 + LIKE 兜底（FTS5 编译缺失时整表降级）✅
- **图存储**：`data/graph_store.py` kg_entities/kg_edges 两表等价实现（有意不引入 networkx，代码注释已说明）✅ 偏差已标注
- **混合检索**：injection_service 向量+FTS5 双路 RRF 融合 ✅
- **Redis 缓存**：未实现（进程内等价，R2-B15）
- **config.yaml 阈值**：五项临界阈值与文档B §4.1 全部一致（Round1 BK-023 已对齐）✅；端口/WS 路径偏差见 R2-B14；学习设置默认值偏差见 R2-B13

## 六、功能完整性统计表（按文档B 基线条目计）

| 模块 | 文档要求条目 | 已实现 | 部分实现 | 缺失 |
|---|---|---|---|---|
| AI 对话 | 11（4端点+7步流程） | 8 | 1（显存选模无8B） | 2（被动补全、异步行为记录） |
| AI 绘画 | 6（4端点+ControlNet+Prompt优化） | 6（含ControlNet诚实降级） | 0 | 0 |
| 漫剧创作 | 13（分镜5+导演台3+视频3+配音2） | 6（含TTS/视频诚实降级） | 3（ai-describe/scene/render 近似） | 4（list/reorder/preview/director export） |
| 知识学习 | 34（22端点+5触发器+闭环3+§4.3四项） | 24 | 0 | 10（WS进度+4自动触发+自动微调+LoRA应用+3项自适应） |
| 模型管理 | 9（4端点+注册/校验/调度/显存/预测） | 9 | 0 | 0 |
| 视频风格 | 5（3端点+训练管线+版本回滚） | 5（含LTX-2诚实门控） | 0 | 0 |
| 系统管理 | 3（info/hardware/update） | 1 | 1（version 部分等价 info） | 1（update） |
| 浏览器控制 | 9（6端点+决策循环+分级+检查点） | 9 | 0 | 0 |
| 调度与互斥 | 14（6阈值+互斥+滞回+优先级+配额5档） | 13 | 1（生成质量降参以精度降级近似） | 0 |
| 数据层 | 6（SQLite/ChromaDB上限/FTS5/图/Redis/备份） | 5 | 0 | 1（Redis） |
| 配置符合性 | 12（端口+WS路径+§4.4十项默认） | 7 | 0 | 5（端口、WS路径、微调频率、敏感内容、显示浏览器） |
| **合计** | **122** | **93（76.2%）** | **6（4.9%）** | **23（18.9%）** |

## 七、总体评价

后端主体骨架与文档B 的符合度良好（76.2% 完全符合，且 ControlNet/语音/LTX-2 视频等未随包能力全部以 degraded 标记诚实呈现，**本轮未发现 P0 级虚假实现**）。五智能引擎中四个（LTX-2 管线、ML 预测、调度学习、AV1 编码）真实完整；调度互斥、阈值、滞回、优先级、配额矩阵经 Round1 修复后与 §4.1/§8.4.2 全参数一致；数据层 ChromaDB 上限、FTS5、图存储均真实落地。

主要缺口集中在**知识学习闭环的最后两环**：自动触发器框架（R2-B02）与自动微调执行器（R2-B03）未接线，且训练产出的 LoRA 从不被对话推理加载（R2-B04）——三者叠加导致"学习→进化→应用"的自主进化主线目前只能手动走通，这是与文档愿景差距最大的一处。其次是分镜模块的 list/reorder/ai-describe/preview 四个端点缺失（R2-B06/B07）直接影响前端分镜管理可用性，以及学习进度 WS 推送缺失（R2-B05）。建议下一轮修复按 P1 七条优先（R2-B01~B07），P2 中优先对齐 §4.4 默认值（R2-B13）与双文档端点漂移（R2-B11）。
