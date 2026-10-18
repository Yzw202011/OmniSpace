# 训练中心全链路测试计划（前端仿真人操作 + 后端链路监控）

> **版本**：v1.0（2026-10-18 制定，待用户拍板后执行）
> **范围**：/training 训练中心三页签（知识训练 / 风格训练 / 人物训练）+ 统一任务队列 + LoRA 版本面板 + 其背后的全部后端链路（队列/调度/上传/抽帧/版本库/回滚/生图链挂载）
> **性质**：测试计划文档（未动任何代码）。所有「现状事实」均带 `文件:行号` 证据，取自 2026-10-18 双路代码探查。
> **关联记忆**：h3-quality-techlines（GPU 活动门）、learning-g1-test-run（IAB 坐标点击坑）、gpu-test-activity-gate（测试前必核后端实时任务）

---

## §0 背景与病灶（为什么测、重点测什么）

用户提供的 5 张截图（训练中心三页签 + LoRA 版本面板）暴露的**现成病灶**，本计划必须逐一对账：

| # | 截图病灶 | 代码层根因（已探查实锤） | 对应测试组 |
|---|---------|------------------------|-----------|
| S1 | 统一任务队列显示 28 项 | 三表（train_tasks + style_tasks + char_lora_tasks）全量倒序合并，无分页无上限（src/api/training.py:65-75） | B 组 |
| S2 | 知识训练任务成批「失败 0%」（models/qwen3-vl-4b） | 自动触发=LearningScheduler._maybe_auto_finetune（src/services/learning_scheduler.py:807-897）；失败只写 status="error" **无 error 列**（train_tasks 表 src/data/database.py:277-291 无该列；_TRAIN_TASK_COLUMNS 白名单还会丢弃 error 字段，src/services/lora_training_service.py:236-239）；失败文本只在 WS 瞬时帧 + events.jsonl + backend.log | C/H/L 组 |
| S3 | 风格页 LTX-2 基座未就绪警告横幅 + 训练/预览置灰 | base_ready=false（models/ltx-2 缺 model_index.json 或权重，src/services/style_lora_service.py:251-267）→ 80010；训练按钮另被 D3 冻结**无条件置灰**（frontend/src/components/style/StylePage.tsx:503-510） | D 组 |
| S4 | 数据集「样本不足（1/4）」 | MIN_STYLE_SAMPLES=4（style_lora_service.py:69），不足时后端拒训练返回 80011 | D 组 |
| S5 | 人物页角色 15 个、多行「未练」「5 张图」训练按钮可点 | 人物链是**唯一活的训练链**（trainable = n_imgs>=4，src/api/character_lora.py:86-87；提交按钮未被 D3 冻结） | E/I 组 |

**三条测试主线**：
1. **仿真人操作**（前端）：真鼠标移动/点击/输入/拖拽/键盘，模拟老年手抖（双击、连点、误触）与快速操作（切页签风暴），验证按钮态、弹窗、toast、轮询刷新。
2. **后端与链路监控**：每一步前端操作，同步在 WS 帧、events.jsonl、backend.log、三张 DB 表、磁盘产物五个观测点取证，验证状态一致性与时延。
3. **漏洞与稳定性**：上传闸、路径穿越、无鉴权面、CSWSH、UI 冻结与 API 开放的双源不一致、轮询泄漏、断连恢复。

---

## §1 被测对象事实卡（测试定位器与端点真源）

### 1.1 前端组件与文案（仿真操作的定位器）

| 区域 | 组件（行数） | 关键定位器/文案 |
|------|-------------|----------------|
| 页面骨架 | frontend/src/components/training/TrainingPage.tsx（223） | 标题「训练中心」(:177)；副标题「所有训练一个屋檐下：知识 · 风格 · 人物」(:180)；页签按钮「知识训练」「风格训练」「人物训练」(:30-32，role="tab") |
| 统一队列卡 | TrainingPage.tsx 内 UnifiedQueueCard | 「统一任务队列」(:64)；刷新按钮 aria-label="刷新训练队列"(:76-78)；「仅显示最近 8 项（共 N 项…）」(:142)；QUEUE_DISPLAY_CAP=8(:27) |
| 知识页签 | frontend/src/components/learn/LearnView.tsx（374） | 「+ 创建训练任务」/「收起表单」(:149)；表单卡字段：任务名称/基础模型/LoRA Rank/LoRA Alpha/学习率/训练轮次/数据集路径(:160-243)；提交按钮「开始训练（维护中）」**disabled**(:259-262)；「删除记录」(:360)；「取消」(:339) |
| 知识版本面板 | frontend/src/components/learning/LoRAVersionManager.tsx（182） | aria-label="LoRA 版本管理"(:96)；表头 版本/质量分/样本数/状态/操作(:114)；「当前」徽章(:122)；「回滚」(:136)；「版本对比」(:149) |
| 风格页签 | frontend/src/components/style/StylePage.tsx（808，embedded） | 拖拽区 aria-label="上传视频或图片素材"(:296-333)；「数据集列表」(:349)+刷新(:350)；「样本不足（N/4）」(:390)；「开始训练（维护中）」**disabled**(:510)；「训练进度」卡含 暂停/恢复/取消(:536/544/552)；「LoRA 版本」(:667) 含融合/克隆/导出/回滚 |
| 人物页签 | frontend/src/components/training/CharacterTrainPanel.tsx（218） | 「人物 LoRA 训练」(:106)；徽章「未练」/「已练 X」(:134)；「N 张图」+「（需≥4张：先升级四视图）」(:139)；「训练」/「入队中…」(:161)；历史按钮 aria-label="查看 X 的版本"(:167)；「回滚到此版」(:205) **无确认弹窗** |
| 新手引导 | frontend/src/components/common/ModuleGuide.tsx（194） | moduleKey="training"(:14/182)，标题「训练中心 · 新手引导」(:74) |

### 1.2 后端端点清单（API 直测对象，全局前缀 /api/v1）

| 端点 | 位置 | 要点 |
|------|------|------|
| GET /training/tasks | src/api/training.py:65-75 | 三源聚合；sources 字段披露单源故障（:57-61） |
| POST /learn/train | src/api/learn.py:78-141 | **UI 冻结但 API 开放**；dataset_path 白名单(:97-104)；入队 priority="low" |
| GET /learn/tasks / {task_id} | learn.py:144-160 / 163-180 | 倒序；详情含 progress |
| POST /learn/tasks/{id}/cancel | learn.py:195-241 | 仅 queued/training/evaluating 可取消 |
| DELETE /learn/tasks/{id} | learn.py:252-283 | 仅终态可删；不动 LoRA 版本 |
| POST /learn/tasks/reorder | learn.py:286-314 | 写 priority 列 |
| POST /learn/dataset/upload | learn.py:321-362 | 50MB 上限(:317-338)；upload_guard(:342) |
| GET /learn/models | learn.py:365-369 | 硬编码 3 基座（qwen3-vl-4b/qwen2-vl-2b/sdxl-base-1.0，:68-75） |
| POST /learn/lora/rollback | learn.py:382-395 | {version}；服务层白名单（lora_training_service.py:1469-1484） |
| GET /learn/lora/versions (+/compare, /{version} DELETE) | learn.py:402-481 | 版本名 regex 三闸(:422-426)；rmtree+audit(:443-444) |
| GET/PUT /learn/train/defaults | src/api/system.py:1720-1736 | kv learn.train_defaults |
| POST /style/upload | src/api/style.py:53-93 | 200MB 限量读取(:65-71)；MEDIA_TABLE 魔数(:73-78) |
| GET /style/datasets / {dataset_id} | style.py:96-113 | dataset_id 32 位 hex regex（style_lora_service.py:1001,386-392） |
| POST /style/train | style.py:120-178 | 80010 基座缺/80011 样本不足/40007 数据集不存在 |
| POST /style/tasks/{id}/pause\|resume\|cancel\|resume-training | style.py:222-250 | STYLE-017/018 |
| GET /style/versions；POST /style/rollback、/preview | style.py:257-334 | preview image_path 必须 is_relative_to(DATA_DIR)(:315-326) |
| GET /style/status | style.py:341-345 | base_ready/base_reason/min_samples |
| POST /style/merge、/export、/clone；GET+POST /style/templates | style.py:409-488 | 版本高级操作 |
| GET /character/lora/assets | src/api/character_lora.py:51-90 | trainable=n_imgs>=4(:86-87)；同名去重 |
| POST /character/lora/train | character_lora.py:93-107 | {asset_id}；同角色在队拒（service:223-226） |
| GET /character/lora/tasks；POST …/cancel；GET /versions；DELETE /versions/{version} | character_lora.py:110-160 | 删除三闸 regex+containment(:143-147) |
| POST /character/lora/rollback | character_lora.py:163-173 | ⚠️ **version 参数无白名单**（service:565-573 仅 is_dir()） |

### 1.3 队列 / 互斥 / 进度通道

- **三条独立串行队列、各自单 worker**：知识 `"lora-training"`（lora_training_service.py:615-628）、风格 `"style-lora-training"`（style_lora_service.py:496-509）、人物 `"char-lora-training"`（character_lora_service.py:260-283，队列空线程退出非常驻）。优先级四级 high>medium>low>background（src/services/priority.py:46-47）。
- **互斥**：三服务共用同一把 "training" 功能锁（src/services/training_common.py:29-79）；feature_lock 四域互斥 dialog/paint/video_gen/training（src/middleware/feature_lock.py:35-48）。冲突返回 `FEATURE_MUTEX_LOCKED`。
- **知识训练准入闸**：空闲显存 ≥10GB（TRAINING_MIN_FREE_GB，src/services/vram_policy.py:130；检查 lora_training_service.py:860-868）、数据 ≥100 条(:836-844)、CUDA 可用(:857-859)、基座权重存在(:823-834)。
- **人物训练准入闸**：素材 ≥4 图、基座 models/paint/flux2-klein-4b+transformer 子目录、无同角色在队；**无显存数值闸**（仅 feature_lock）。
- **WS 进度**：仅知识服务有广播器（main.py:281-305 注入）→ /ws 中枢；事件 training_queued/started/progress/evaluating/completed/failed/cancelled（lora_training_service.py:114-123）；ws_hub 翻译 completed→task_complete、failed→task_error（src/services/ws_hub.py:53-86）。**风格与人物无 WS，只能 REST 轮询**。
- **前端轮询**：统一队列 5s（TrainingPage.tsx:170）；人物面板 5s（CharacterTrainPanel.tsx:53-55）；知识页签有活跃任务时 3s（LearnView.tsx:72-79）；风格进度前台 5s/后台 10s（useStyleStore.ts:25-30）；useTaskStore 陈旧巡检 60s/15min 判完成（useTaskStore.ts:232-247）。
- **训练完成自动挂载**：人物→adapter 拷为资产旁 lora.safetensors 并挂生图链身份硬锁（character_lora_service.py:476-491、524-537；消费端 src/api/manga/keyframe.py:1012-1022、1622-1659）；知识→refresh_knowledge_lora 热挂载（lora_training_service.py:696-703）。
- **版本库**：知识 models/lora/vN 上限 10 版（MAX_VERSIONS:129）；风格 models/style_lora；人物 data/character_lora/{asset_id}/vN 每资产 3 版(:44)。pending_review/registered 由 evaluate() 写 meta.json（质量分 <0.5 或评估不可用→pending_review，lora_training_service.py:1302-1347）。

### 1.4 上传安全闸（漏洞组靶点）

- 扩展名白名单 + 魔数嗅验（src/services/upload_guard.py:77-88）+ **PE/ELF/Mach-O 绝对黑名单**（MZ/\x7fELF/cafebabe，:35-42，拦截 :102-105）+ 文本类首 512 字节无 NUL（:54-57）。
- learn 上传 50MB；style 上传 200MB（限量读取防内存爆）。
- 鉴权现状：训练域全部端点**无用户级鉴权**，防线=TrustedHost 回环白名单（main.py:549-553）+ LAN 令牌（仅非回环绑定时）+ license gate 未激活 403（main.py:514-533）+ CORS 本地 + 限流 300/min（main.py:511-512）+ WS ws_origin_guard。

---

## §2 测试环境与前置检查（P 组，10 项，全部 ✅ 才准开测）

| # | 检查项 | 方法 | 通过判据 |
|---|--------|------|---------|
| P-01 | 后端健康且为 py312 主链 | `curl http://127.0.0.1:5800/api/v1/system/health` + 任务管理器查进程 | 后端就绪；进程解释器=runtime/py312（禁裸 python=py313，见记忆 boot-guard-py313-incident） |
| P-02 | **GPU 活动门**：无并发会话在飞任务 | 查近 5 分钟任务提交 + >1GB comfy 进程 | 无在飞重任务才开测；发现并发立即停手，**绝不重启后端**（CLAUDE.md §16.6） |
| P-03 | 单实例确认 | 5800-5835 端口仅一套监听 | 无第二副本（双栈叠载显存红线） |
| P-04 | DB 基线快照 | 复制 data/omnispace.db（WAL 先 checkpoint）+ 记录三表行数 | 三表行数留底，供每轮对账 |
| P-05 | 磁盘产物基座盘点 | ls models/lora、models/style_lora、data/character_lora | 记录既有版本目录清单（防误删+对账回滚） |
| P-06 | 日志窗口开启 | tail -f logs/backend.log + logs/events/events-*.jsonl | 实时可见，测试中全程录屏取证 |
| P-07 | WS 监听器挂上 | wscat/脚本连 ws://127.0.0.1:5800/ws 并落盘帧 | 能收到 task_progress 系列帧 |
| P-08 | 空闲显存记录 | nvidia-smi（或 /api/v1/hardware） | 记录基线（预期 ~13.4GB 可用口径，见记忆 machine-gpu-stability） |
| P-09 | 基座就绪态记录 | GET /style/status；ls models/qwen3-vl-4b、models/ltx-2、models/paint/flux2-klein-4b | 三基座在/不在照实登记（决定 D3/E2E 组走实弹还是走拒绝分支） |
| P-10 | 仿真人操作工具就绪 | IAB（ZCode 内置浏览器）/browser-use：web-gui-tester 技能 | 页面可达 #/training；注意坑：IAB role-selector 点击超时须改 CUA 坐标点击（记忆 learning-g1-test-run） |

---

## §3 监控体系（每个用例共用的五观测点）

| 观测点 | 采集方式 | 断言用途 |
|--------|---------|---------|
| M1 WS 帧 | ws://…/ws 连接落盘（type/task_id/percent/ts） | 知识链事件时序与到达延迟 |
| M2 事件日志 | logs/events/events-YYYYMMDD.jsonl 过滤 module="training"（training_started/completed/failed/cancelled） | 失败原因文本的唯一持久侧通道（S2 病灶） |
| M3 技术日志 | logs/backend.log | 堆栈与异常分支 |
| M4 DB | sqlite3 只读查 train_tasks / style_tasks / char_lora_tasks / comic_assets / audit_log | 状态机、error 列（风格/人物有、知识无）、审计行 |
| M5 磁盘产物 | ls models/lora、models/style_lora/datasets、data/character_lora/{asset_id}、data/training/uploads | 版本目录、adapter 文件、抽帧产物、上传落盘 |

**通用取证规矩**：每个用例执行后截屏（前端态）+ 五观测点各一条记录，归档到 `logs/test-runs/training-center-2026-10-18/TC-编号/`；论断必须标 ✅/🔶/⚠️（CLAUDE.md §16.2）。

---

## §4 测试用例主体（12 组，共 169 条；另有 §2 前置 P 组 10 项，总计 179 项）

> 列格式：**编号｜用例｜仿真操作/步骤｜预期与判据｜监控/指标**。
> 默认前置：§2 P 组全绿；浏览器已打开应用并登录到 /training。

### A 组·页面加载与导航仿真（15 条）

| # | 用例 | 步骤 | 预期与判据 | 监控/指标 |
|---|------|------|-----------|----------|
| A-01 | 冷加载路由直达 | 地址栏输入 `#/training` 回车 | 页面渲染：标题「训练中心」+副标题+三页签+统一队列卡 | 首屏可见 <3s（本地）；Network 无非 2xx 资源 |
| A-02 | 旧书签重定向 | 地址栏 `#/style` 回车 | 跳转到 /training（router.tsx:74 重定向），落知识页签 | URL 哈希=training |
| A-03 | 默认页签态 | 冷加载后观察 | 知识页签选中（aria-selected=true、training-tab active），其余未选 | DOM 断言 |
| A-04 | 页签逐一切换（真人节奏） | 鼠标移到「风格训练」→停 300ms→单击；再切「人物训练」；再回「知识」 | 每次点击 ≤300ms 出现对应面板；页签高亮随动 | 切换 P95<300ms |
| A-05 | 页签快速风暴 | 2s 内交替点击三页签 10 次 | 无白屏、无面板错位（知识面板与版本面板同显、风格独占、人物独占）；最终态=最后点击的页签 | 控制台零未捕获异常 |
| A-06 | 页签键盘可达性 | Tab 聚焦页签列表→方向键/Enter | role="tab" 可聚焦可激活（TrainingPage.tsx:191-207） | focus ring 可见 |
| A-07 | 新手引导首访 | 清 localStorage 后重载 | ModuleGuide「训练中心 · 新手引导」出现（moduleKey="training"）；可跳过 | 跳过后不再出现（KV 持久） |
| A-08 | 全局导航互斥置灰 | 在人物页签发起一次训练（activeFeature=training）后看左侧导航 | 对话/绘画/视频入口置灰+提示（App.tsx 权重映射）；训练完成恢复 | 截屏前后对比 |
| A-09 | 离开页面轮询清理 | 打开 DevTools Network，离开 /training 到 /chat | 5s 队列轮询停止（clearInterval，TrainingPage.tsx:171）；无残留请求 | 离开后 15s 内零 /training/tasks 请求 |
| A-10 | 页面刷新状态还原 | 在风格页签按 F5 | 回落后落在**知识**页签（本地 state 不持久——按现状断言并记录为 UX 缺陷候选） | 现状如实记录 ⚠️ |
| A-11 | 窗口失焦/复焦 | 切到别的窗口 30s 再回来 | 页面不假死；失焦期间轮询按设计继续（统一队列无 visibility 门控）或降频（style-poll 后台 10s） | Network 计数对账 |
| A-12 | 前后翻页/滚动 | 长任务列表下滚到底再回顶 | 无横向滚动条异常、无粘性头部错位；10k 级不要求（本页量级 28 项） | 目测+截屏 |
| A-13 | 系统浏览器兼容 | 用 Chrome（用户日常 D:\Chrome）+ Edge 各跑一遍 A-04 | 两浏览器表现一致 | 截屏对比 |
| A-14 | 主题族下渲染 | 切换 data-family（sakura/tech）后重进 | 无样式崩坏；CSS 全走 var(--color-*)（哨兵已有，抽查视觉） | 目测 |
| A-15 | 断网降级 | DevTools Offline 10s 再恢复 | 队列卡显示「队列拉取失败：…」（TrainingPage.tsx:84-86）；恢复后自动续上；**不许白屏** | 恢复后 ≤2 轮轮询自愈 |

### B 组·统一任务队列面板（14 条）

| # | 用例 | 步骤 | 预期与判据 | 监控/指标 |
|---|------|------|-----------|----------|
| B-01 | 三源聚合正确性 | 对 DB 三表手工核对当前条数（P-04 基线+本轮增量） | 队列「N 项」=train_tasks+style_tasks+char_lora_tasks 总和（training.py:65-75） | M4 对账 |
| B-02 | 排序规则 | 检查列表顺序 | created_at 倒序；缺时间戳项沉底（trainingApi.ts:59-66） | M4 对账 |
| B-03 | 8 项截断 | 构造/确认 >8 条任务 | 恰显示 8 条+「仅显示最近 8 项（共 N 项；完整历史在对应页签里看）」（QUEUE_DISPLAY_CAP=8） | 文案含真实 N |
| B-04 | 名称兜底链 | 找无 name 的风格任务 | 显示 style_prompt→base_model→id 兜底（trainingApi.ts:43-45） | M4 抽 1 行对账 |
| B-05 | 状态色映射 | 列表含 running/queued/done/error 各态 | 训练中=绿、排队中=黄、已完成、失败红（TrainingPage.tsx:36-49） | 截屏 |
| B-06 | 5s 轮询节奏 | Network 观察 1 分钟 | 稳定 ~12 次/分钟；loading 中刷新按钮 disabled | 间隔 5s±1s |
| B-07 | 手动刷新按钮 | 真人点击「刷新」 | 立即拉取；按钮禁用态随 loading；无重复并发请求 | Network |
| B-08 | 单源故障 fail-soft | 临时把 style 源搞挂（如停 DB 写权限/模拟——不改码方式优先：可用大量并发占满造成超时替代） | 显示「『风格/知识』源暂不可用（…），其余任务照常显示。」（sources 披露，training.py:57-61） | 🔶 视可行性，做不到则记 API 直测替代（G-12） |
| B-09 | 空队列态 | 三表清空后的隔离环境（或开发分支环境） | 「暂无训练任务。选一个页签，上传材料开始训练。」（TrainingPage.tsx:95） | 截屏 |
| B-10 | kindLabel 正确 | 知识+风格任务混排 | 分别标「知识训练」「风格训练」；**人物任务注意**：training.py:69-71 聚合三源但 kindLabel 覆盖检查——若人物任务无标签/错标记 P2 缺陷 | M4+截屏 ⚠️ |
| B-11 | 进度百分比归一 | 有活跃任务时观察 | percent 0-100 单调不减（WS percent 0-1→0-100 归一，useTaskStore.ts:155-184） | M1 帧序列 |
| B-12 | 失败任务展示 | 指向 S2 病灶任务 | 状态=失败、进度 0%；**列表不显示失败原因**（无 error 通道）——如实记录（缺口 G-01） | 截屏+M2 对照 |
| B-13 | 大列表压力 | 三表灌到 100+ 行（INSERT 只读环境慎用——改为查现有 28 项渲染帧率即可） | 渲染不卡顿（滚动 60fps 量级目测）；API 响应 P95<300ms | M4 行数+Network 耗时 |
| B-14 | 队列卡与页签数据一致性 | 同一时刻对比队列卡与知识页签任务列表 | 两处同一任务状态/进度一致（允许一个轮询周期差 ≤5s） | M4 双查 |

### C 组·知识训练页签（20 条）

| # | 用例 | 步骤 | 预期与判据 | 监控/指标 |
|---|------|------|-----------|----------|
| C-01 | 创建表单开合 | 点击「+ 创建训练任务」 | 内联表单卡展开（非弹窗）；按钮变「收起表单」；再点收起 | DOM 断言 |
| C-02 | 基础模型下拉 | 展开基座 select | 恰 3 项：qwen3-vl-4b/qwen2-vl-2b/sdxl-base-1.0（learn.py:68-75）；未就绪项标「（未就绪）」且 disabled（LearnView.tsx:181-183） | M5 盘点佐证 |
| C-03 | Rank 滑条边界 | 拖 LoRA Rank 到 1 和 64 | 限 1-64（LearnView.tsx:189-199）；越界不可达 | DOM value |
| C-04 | Alpha 滑条边界 | 同上 1-128 | 限 1-128 | DOM value |
| C-05 | 学习率选项 | 展开学习率 select | 5 档：1e-5（保守）/5e-5（温和）/1e-4（标准）/3e-4（激进）/1e-3（实验）（LearnView.tsx:221-225） | 截屏 |
| C-06 | Epochs 滑条 | 拖 1-20 | 限 1-20 | DOM value |
| C-07 | 提交按钮冻结态（D3） | 填全表单后 hover 提交按钮 | **disabled**+title「知识训练维护中：训练依赖修复后恢复」+文案「开始训练（维护中）」（LearnView.tsx:259-262）；真人点击无反应、零请求 | Network 零 POST |
| C-08 | 表单重置 | 填脏表单后收起再展开 | 默认值恢复（DEFAULT_FORM，LearnView.tsx:23-31；如不恢复如实记录） | DOM value ⚠️ |
| C-09 | 任务列表加载 | 观察任务卡渲染 | meta 行含 模型/Rank/Alpha/LR/Epochs/创建（LearnView.tsx:293-299） | 截屏 |
| C-10 | 「自动」徽章真相 | 对照一张手动创建过的历史任务 | 凡列表拉回非本会话创建的任务一律「自动」（learnApi.ts:71 客户端推断 priority 兜底 'auto'）——**徽章≠后端真源**，如实记录（缺口 G-05） | M4 priority 列对照 |
| C-11 | 失败任务卡「错误:」行 | 指向 S2 病灶任务 | 若 DB 无 error 可展示则无「错误:」行（LearnView.tsx:331 依赖 error 字段）——现状=知识任务永远无错误行（缺口 G-01） | M4 查 train_tasks 无 error 列佐证 |
| C-12 | 活跃任务 3s 轮询 | 制造/找到 queued|training 任务 | 轮询 3s（LearnView.tsx:72-79）；无活跃任务时停 | Network 计数 |
| C-13 | WS 进度合并 | 知识任务活跃时观察卡片进度 | /ws task_progress 帧驱动实时更新，与 3s 轮询互补 | M1 vs Network 时序 |
| C-14 | 取消活跃任务 | 真人点击任务卡「取消」 | 仅 queued/training/evaluating 可取消（learn.py:195-241）；取消后状态→已取消；WS training_cancelled 帧 | M1+M4 |
| C-15 | 取消终态任务被拒 | 对 done/error 任务尝试取消（按钮应不显示） | 终态卡无取消按钮（LearnView.tsx:336-342 条件渲染） | DOM |
| C-16 | 删除记录确认弹窗 | 真人点击终态任务「删除记录」 | heavy 级确认弹窗：标题含任务名+「此操作不可恢复」+按钮「删除」（LearnView.tsx:350-355） | 截屏 |
| C-17 | 删除落库 | 确认删除 | DELETE /learn/tasks/{id} 200；DB 行消失；UI ≤6s（下轮轮询）内消失；**不动 LoRA 版本目录**（M5 对照） | M4+M5 |
| C-18 | 手滑取消删除 | 弹窗出现后点「取消」 | 记录保留，零请求 | Network |
| C-19 | API 绕过 UI 直建任务 | curl POST /learn/train（合法 payload，基座 qwen3-vl-4b） | **UI 冻结但 API 开放**：任务入队（priority=low）→train() 前置闸失败→复现 S2「失败 0%」；WS training_failed 帧带原因；events.jsonl training_failed 带原因 | M1+M2 帧文本=失败根因（基座缺失/数据不足/显存<10GB 之一） |
| C-20 | dataset_path 穿越拒绝 | API 直打 dataset_path=`../../Windows` | 白名单双保险拒绝（learn.py:97-104；lora_training_service.py:514-528），错误信封语义码 | 响应体断言 |

### D 组·风格训练页签（22 条）

| # | 用例 | 步骤 | 预期与判据 | 监控/指标 |
|---|------|------|-----------|----------|
| D-01 | 基座横幅判定 | 进入风格页签 | base_ready=false 时红边横幅，含 base_model 与 base_reason（StylePage.tsx:286-288，兜底文案「权重文件缺失」） | GET /style/status 字段对照 |
| D-02 | 横幅文案诚实性 | 对照 GET /style/status base_reason | 横幅文本=API 返回值逐字（不得前端编造） | 文本 diff |
| D-03 | 拖拽上传图片 | 真人拖一张 PNG 进虚线区 | dragOver 高亮→上传中 spinner「正在上传素材…」→成功 toast「素材「x.png」上传成功（N 帧样本）」+「已上传：X（可重新上传覆盖）」 | M5 落盘+datasets 增行 |
| D-04 | 点击上传（键盘可达） | Tab 到拖拽区（role=button）回车 | 唤起文件选择器；选择 MP4 后同 D-03 | 同上 |
| D-05 | 视频抽帧链 | 上传一个 ~10s MP4 | FFmpeg fps=1 均匀采样、最多 32 帧、q:v 3、120s 超时（style_lora_service.py:334-360）；toast 帧数=抽得帧数 | M5 帧文件数；耗时记录（10s 视频 <30s） |
| D-06 | 超长视频超时保护 | 上传 >120s 抽帧耗时的长视频（或低码率大文件） | 抽帧 120s 超时被杀，任务如实报错不悬挂 | M3 日志含 timeout |
| D-07 | 格式白名单-拒绝面 | 拖入 .gif / .txt / .bmp | accept 列表外：前端不选入；API 直打伪造扩展名被 upload_guard 拒 | 双层都验 |
| D-08 | 伪装可执行文件 | 把 .exe 改名 .png 上传（API 直打 multipart） | 魔数嗅验 MZ 头→PE 黑名单拒绝（upload_guard.py:35-42,102-105） | 错误信封；M5 无落盘 |
| D-09 | ELF/Mach-O 黑名单 | 构造 \x7fELF 头文件上传 | 同上拒绝 | 同上 |
| D-10 | 200MB 大小上限 | 上传 >200MB 视频 | 限量读取拒绝（style.py:65-71），**不许整读进内存**（测试机 RAM 安全） | 进程内存曲线平稳 |
| D-11 | learn/style 上限差异 | 同文件打 /learn/dataset/upload（>50MB） | learn 侧 50MB 上限拒绝（learn.py:317-338）——两域上限不同都要守住 | 响应断言 |
| D-12 | 数据集列表渲染 | 上传后点「刷新」 | 列表行含名称+「N 帧样本」+徽章；选中行标「训练集」（StylePage.tsx:386） | 截屏 |
| D-13 | 样本不足徽章 | 找 frame_count<4 的数据集 | 「样本不足（N/4）」warning 徽章（:390）；≥4 显示「样本充足」（:388） | min_samples 来自 /style/status |
| D-14 | 数据集详情 | GET /style/datasets/{id} 合法 id | 200 信封含 frame_count 等 | API |
| D-15 | dataset_id 非法注入 | GET /style/datasets/../../etc | 32 位 hex regex 拒（style_lora_service.py:1001,386-392） | 404/400 信封 |
| D-16 | 训练按钮冻结态（D3） | hover「开始训练」 | **disabled**+title「风格训练维护中：基座与消费链恢复后开放」（StylePage.tsx:503-510）；点击零请求 | Network |
| D-17 | API 绕过-基座缺失 | curl POST /style/train（合法 dataset_id） | 80010 STYLE_BASE_NOT_READY 信封（style.py:142-147），文案含 models/ltx-2 | 错误码断言 |
| D-18 | API 绕过-样本不足 | dataset 帧数 <4 再 POST /style/train | 80011 信封，文案「至少需要 4 帧」（style.py:148-155） | 错误码断言 |
| D-19 | 预览按钮门控 | 无版本+基座未就绪时看「生成预览」 | disabled（previewBusy‖无版本‖!base_ready，StylePage.tsx:617-628） | DOM |
| D-20 | preview image_path 穿越 | curl POST /style/preview image_path=`../../models/lora/v1/xx` | resolve 后必须 is_relative_to(DATA_DIR) 拒（style.py:315-326） | 错误信封 |
| D-21 | 参数预设与显存预估 | 切换 4 个预设模板、拖 Rank | 参数联动填充+「恢复默认」复原；data-testid="vram-estimate" 数值变化（StylePage.tsx:425-497） | DOM |
| D-22 | 版本高级操作按钮态 | 无版本时看融合/克隆/导出/回滚 | 不可用态明确（disabled 或空态文案），无裸异常 | DOM+截屏 |

### E 组·人物训练页签（16 条）

| # | 用例 | 步骤 | 预期与判据 | 监控/指标 |
|---|------|------|-----------|----------|
| E-01 | 角色列表加载 | 进入人物页签 | GET /character/lora/assets；行=徽章+名+「N 张图」+训练按钮+历史钟（CharacterTrainPanel.tsx:106-170）；同名资产去重（character_lora.py:51-90） | M4 comic_assets 对照 |
| E-02 | 未练/已练徽章 | 对照 current_version 字段 | null→「未练」；有值→「已练 vX」（:134） | M4 |
| E-03 | trainable 判定 | 找 <4 图与 ≥4 图角色各一 | <4：训练按钮 disabled+后缀「（需≥4张：先升级四视图）」（:139,158）；≥4：可点 | API trainable 字段对照（n_imgs>=4，character_lora.py:86-87） |
| E-04 | 四视图素材收集核对 | 对一个主图+四视图齐全的角色发起训练前查 collect_images 逻辑 | 收 comic_assets.file_path 主图+meta.views 四视图（list 或 {front/side/back/closeup}），仅存盘文件、去重保序（character_lora_service.py:160-196） | M5 训练日志素材清单 |
| E-05 | 训练入队反馈 | 真人点击「训练」 | 按钮变「入队中…」→toast「「X」已入队训练（完成后自动挂到生图链）」（:65）；行下出现活跃任务条+「取消」 | M4 char_lora_tasks 增行 status=queued |
| E-06 | 同角色重复入队拒绝 | 对已在队角色再点训练（API 直打） | 拒绝（service:223-226 同角色在队拒） | 错误信封 |
| E-07 | 基座缺失拒绝 | 临时改名 models/paint/flux2-klein-4b/transformer（**测完立即改回**）或 API 直打 | 基座目录+transformer 子目录校验拒绝（service:199-205） | ⚠️ 环境改动须双人复核恢复 |
| E-08 | 训练进度轮询 | 观察活跃任务条 | 5s 轮询（:53-55）；进度推进；**无 WS**（缺口 G-03）——断连感知延迟=轮询周期 | Network |
| E-09 | 取消训练 | 点击「取消」 | POST /character/lora/tasks/{id}/cancel；状态→已取消；worker 停 | M4 |
| E-10 | 历史版本面板 | 点击钟表图标 | 内联展开「版本（最近 3 个）」（:179）；空态「还没有版本——先练一次。」（:189） | GET /character/lora/versions?asset_id= |
| E-11 | 版本保留上限 | 连续训练 4 次（或 API 造版本目录） | 每资产只留 3 版，最老被清（service:44） | M5 目录数 |
| E-12 | 回滚交互**无确认弹窗** | 真人点击非当前版本「回滚到此版」 | **直接发请求**+toast「已回滚到 vX（生图链即刻生效）」（:201-206,95）——无确认弹窗（缺口 G-04：误触风险） | Network+截屏 |
| E-13 | 回滚生效链 | 回滚后到 AI漫画页生成该角色关键帧 | 生图链消费资产旁 lora.safetensors（keyframe.py:1012-1022 登记kind="lora"、1622-1659 挂载 klein-4b+PuLID 置零） | M5 adapter 文件 mtime + 出图 |
| E-14 | 回滚 version 白名单缺失 | API 直打 version=`../<兄弟asset_id>/v1` | ⚠️ **现状无格式校验仅 is_dir()**（service:565-573）——测试只**验证行为并记录**（是否真的能越出 asset 根目录），不得破坏兄弟目录数据；此为漏洞靶点 G-02 | 响应+M5 目录对照（只读断言） |
| E-15 | 删除版本三闸 | API DELETE 非法 version=`../`、不存在版、当前版 | regex+containment 拒（character_lora.py:143-147）；成功删除写 audit_log（action=char_lora_version_delete） | M4 audit 行 |
| E-16 | 空态引导 | 无角色资产的环境 | 「还没有角色资产。去 AI漫画 或 漫剧创作 的资产库创建角色（建角色时勾选四视图）。」（:121） | 截屏 |

### F 组·知识域 LoRA 版本面板（10 条）

| # | 用例 | 步骤 | 预期与判据 | 监控/指标 |
|---|------|------|-----------|----------|
| F-01 | 版本列表加载 | 知识页签滚到版本面板 | GET /learn/lora/versions；表：版本/质量分/样本数/状态/操作（LoRAVersionManager.tsx:114）；与 models/lora/vN 目录一一对应 | M5 对照 |
| F-02 | 空态 | 无版本时 | 「暂无版本——完成一次微调训练后生成。」（:109） | 截屏 |
| F-03 | 「当前」徽章 | 对照响应 current 字段 | v.version===current 的行显示绿徽章「当前」（:121-123） | API 对照 |
| F-04 | 状态列原样渲染 | 找 pending_review 与 registered 版本 | 直接显示英文状态串（:127，无中文映射——记录为 UX 待办） | 截屏 ⚠️ |
| F-05 | 回滚确认弹窗（medium 级） | 真人点击非当前版「回滚」 | 弹窗：标题「确定回滚到 vX？」+「当前生效版本将被替换。」+按钮「回滚」（:75-82）——**三域中唯一带确认** | 截屏 |
| F-06 | 回滚落盘 | 确认回滚 | POST /learn/lora/rollback；current.json 指针变更；列表「当前」徽章迁移；dialog_engine refresh_knowledge_lora 生效（lora_training_service.py:696-703） | M5 current.json diff |
| F-07 | 回滚非法版本 | API 直打 version=v999 / `../` | 白名单拒（service:1469-1484） | 错误信封 |
| F-08 | 当前版回滚按钮态 | 看 current 行 | disabled（:133） | DOM |
| F-09 | 版本对比 | 选 A/B 两版点「对比」 | GET /learn/lora/versions/compare；表：字段/A/B/差值（:146-179）；默认选末两版；compareRef 闭包陈旧修复不回退（:29-47） | API+DOM |
| F-10 | 删除版本三闸+审计 | API DELETE：regex 违例/训练中/当前版 各一次 | 三种拒绝信封分明（learn.py:422-426）；合法删除 rmtree+audit_log 行（:443-444） | M4 audit |

### G 组·后端 API 契约直测（16 条）

| # | 用例 | 步骤 | 预期与判据 | 监控/指标 |
|---|------|------|-----------|----------|
| G-01 | 成功信封形状 | GET /training/tasks 等只读端点 | `{success:true,data,meta:{request_id,timestamp,duration_ms}}`（error_handler.py:585-588） | 响应 JSON schema |
| G-02 | 失败信封形状 | POST /learn/train 缺 base_model | `{success:false,error:{code,message,…}}`，HTTP 恒 200（ADR-01） | 同上 |
| G-03 | 参数校验-越界 | lora_rank=200 / lr=1e-2 / epochs=100 | TrainTaskCreate 拒（models.py:973-988：rank 4-64、lr≤1e-3、epochs 1-50） | 422/错误码 |
| G-04 | 任务详情 404 | GET /learn/tasks/不存在id | 语义错误码信封 | 响应 |
| G-05 | 删除终态外任务被拒 | 对 queued 任务 DELETE | 拒绝（learn.py:252-283 仅终态） | M4 行仍在 |
| G-06 | reorder 写入 | POST /learn/tasks/reorder 乱序数组 | train_tasks.priority 按新序落库 | M4 |
| G-07 | 上传-learn 白名单 | 上传 .pdf 到 /learn/dataset/upload | 拒（upload_guard） | 信封 |
| G-08 | /learn/models 一致性 | GET /learn/models | 3 基座与硬编码清单一致（learn.py:68-75） | diff |
| G-09 | 训练默认值读写 | GET 后 PUT /learn/train/defaults 改 epochs 再 GET | kv learn.train_defaults 持久+回读一致（system.py:1720-1736） | M4 system_settings |
| G-10 | /style/status 全字段 | GET /style/status | base_ready/base_reason/min_samples/base_model 齐 | schema |
| G-11 | /style/tasks 列表 | GET /style/tasks | style_tasks 有 error 列可回看（与知识域差异对照，缺口 G-01 反面佐证） | M4 |
| G-12 | 统一队列 sources 披露 | GET /training/tasks 观察 sources 字段 | 三源 {ok:true} 各一；构造单源超时时该源 {ok:false,error} 其余照常 | 响应 |
| G-13 | 限流阈值 | 对同一端点连打 >300 req/min | 被限流拦截（main.py:511-512） | 429/限流信封；⚠️ 用只读端点打，避开副作用 |
| G-14 | license gate 未激活面 | OMNISPACE_FORCE_LICENSE_PAGE 预览模式下打训练端点 | 业务 API 403（main.py:514-533） | 响应 |
| G-15 | 回环绑定纪律 | 检查 uvicorn 绑定地址 | 仅 127.0.0.1 监听（非回环需 OMNISPACE_ALLOW_LAN=1） | netstat |
| G-16 | API 与 UI 双源一致性总账 | 汇总 C-07/C-19、D-16/D-17 | **UI 冻结但 API 开放**记为缺口 G-06（决策面割裂：要么 API 也封，要么 UI 解封） | 报告条目 |

### H 组·队列与调度（12 条）

| # | 用例 | 步骤 | 预期与判据 | 监控/指标 |
|---|------|------|-----------|----------|
| H-01 | 优先级排序 | API 造 high/low 两任务 | high 先执行（priority.py:46-47 四级） | M4 执行顺序 |
| H-02 | 单 worker 串行 | 同时提交 2 个人物训练（不同角色） | 串行执行，第二个排队；**不允许并行**（三队列各自单 worker） | M4 时间线 |
| H-03 | 训练↔对话互斥 | 人物训练在飞时发起对话 | 对话侧 FEATURE_MUTEX_LOCKED（feature_lock.py:35-48）；导航置灰（A-08） | 错误码 |
| H-04 | 训练↔绘画互斥 | 同上对绘画 | 同上 | 同上 |
| H-05 | 训练↔视频互斥 | 同上对视频生成 | 同上 | 同上 |
| H-06 | 互斥释放 | 训练终态后重试对话 | 自动恢复可用；导航解灰 | 时延<2s |
| H-07 | 知识训练显存闸 | 空闲显存 <10GB 时（用绘画占住显存）API 建 /learn/train 任务 | 前置闸拒绝（TRAINING_MIN_FREE_GB，vram_policy.py:130） | M2 失败原因含显存 |
| H-08 | 人物训练无显存闸记录 | 低显存下人物训练行为观察 | 现状=仅 feature_lock 无数值闸（service:209-226）——记录 RAM/显存风险（16GB 卡+klein-4b QLoRA 实测边界） | 🔶 视环境 |
| H-09 | 队列空 worker 退出 | 人物队列清空后查线程 | char worker 线程退出非常驻（service:265-270）；再次提交能重建消费 | M3 线程日志 |
| H-10 | 取消竞态 | 任务刚进 running 瞬间点取消 | cancel 落库 cancelled+request_cancel 中断标志（learn.py:195-241）；无僵尸训练进程 | M4+任务管理器 |
| H-11 | 失败不传染 | 一任务失败后另一任务照常执行 | 队列继续消费（记忆教训：失败不传染但后端崩溃全队陪葬——本组只验前者） | M4 |
| H-12 | 自动微调节流 | 观察 LearningScheduler 自动触发周期 | _maybe_auto_finetune 30s 节流入口、每天/每周周期判定（learning_scheduler.py:807-897,499-526）；自动任务 priority=background | M2 事件时间戳 |

### I 组·端到端链路跑通（8 条）

| # | 用例 | 步骤 | 预期与判据 | 监控/指标 |
|---|------|------|-----------|----------|
| I-01 | **人物 LoRA 全链闭环**（旗舰 E2E） | 选 ≥4 图角色→训练→等待完成→版本生成→lora.safetensors 挂资产→AI漫画页出该角色图验证身份硬锁 | 全链 done；出图使用 LoRA（keyframe.py:1622-1659 底座切 klein-4b）；engine/model_used 如实 | 全程录屏+M5；耗时记录（参考：Flux2 QLoRA 4 图量级） |
| I-02 | 人物链回滚闭环 | I-01 后训练 v2→回滚 v1→再出图 | 出图随回滚版本变化；current.json 指针正确 | M5+出图对比 |
| I-03 | 知识链失败侧通道闭环 | API 建 /learn/train（基座缺失必败）→全程追踪 | 「失败 0%」复现；失败原因**只在** WS 瞬时帧+events.jsonl+backend.log 可查（缺口 G-01 的完整取证路径） | M1+M2+M3 三处文本一致 |
| I-04 | 风格链拒绝面闭环 | 上传素材→抽帧→数据集<4 帧→API 训练 | 80010/80011 拒因如实；数据集/帧文件落盘正确 | M5 帧数=toast 数 |
| I-05 | 统一队列三源闭环 | 分别在知识/风格/人物三域各造 1 任务 | 队列卡 3 条新任务，kind/状态/进度各自正确，终态收敛一致 | B 组观测点复用 |
| I-06 | 版本对比闭环 | 知识域两版本 compare | 字段级 diff 正确（版本/质量分/样本数/config…） | API |
| I-07 | 删除→重建闭环 | 删除一个失败任务记录→API 重建同名 | 无主键冲突/无残留影响 | M4 |
| I-08 | 训练默认值→表单闭环 | PUT defaults 改 epochs 默认→重开表单 | 表单默认值随 kv 变化（LearnView DEFAULT_FORM 若不读 kv 则记缺口） | DOM ⚠️ |

### J 组·稳定性与压力（14 条）

| # | 用例 | 步骤 | 预期与判据 | 监控/指标 |
|---|------|------|-----------|----------|
| J-01 | 连点风暴 | 「+ 创建训练任务」连点 10 次 | 表单不开合错乱、无请求风暴（纯本地 toggle） | Network 零请求 |
| J-02 | 刷新按钮连点 | 刷新连点 8 次 | 无重复并发请求（loading 禁用态兜底） | Network |
| J-03 | 上传并发 | 3 个标签页同刻各传 1 文件 | 落盘无冲突（uuid 目录）、列表最终一致 | M5 文件数=3 |
| J-04 | 轮询泄漏巡检 | 在三页签反复切换 50 次后静置 | 离开训练中心后零残留轮询（对照 A-09）；各定时器 useEffect cleanup 全生效 | 15s 窗口 Network 计数 |
| J-05 | WS 断连重连 | 断开网络 10s/60s/5min 三档 | 重连指数退避 1s→30s（ws.ts:54-55,171-186）；恢复后 liveTasks 追平 DB 真值 | M1 帧序+M4 对照 |
| J-06 | WS 陈旧任务收敛 | 杀掉前端 15min 模拟（关页签） | useTaskStore 巡检 60s 周期、15min 无更新判完成（useTaskStore.ts:232-247）——重开后无永久「训练中」幽灵 | M4 vs UI |
| J-07 | 后端重启期间前端行为 | 走 splash /api/quit 重启后端（**绝不 kill 直杀**，CLAUDE.md §16.4） | 重启窗口内 UI 报错如实不白屏；恢复后自动续上 | 全程录屏 |
| J-08 | 长跑稳定性 | I-01 人物训练全程（数十分钟）挂机观察 | 后端无静默死亡（RAM 提交量曲线平稳，参照记忆 backend-silent-death-ram-gate）；UI 进度持续推进 | 任务管理器提交内存 10min 采样 |
| J-09 | 取消风暴 | 训练 10s 后取消→立即再训→再取消 3 轮 | 无锁残留（feature_lock 释放）、无僵尸进程、无半写版本目录 | M5 目录完整性 |
| J-10 | 上传中断恢复 | 上传中途断网 | 无半写数据集残留在列表（或如实标注失败）；重传成功 | M5 |
| J-11 | 双开防呆 | 直接再拉一个 src.main 实例 | 命名互斥体拒绝+指引（single_instance.py）；不影响在测实例 | 进程日志 |
| J-12 | 大 payload 列表 | 三表灌 200 行（隔离环境） | /training/tasks P95<500ms；前端渲染不冻结 | Network |
| J-13 | GPU 温度闸联动 | 训练中温度拉高观察（如可） | ≥90°C 拒新任务（feature_lock 层热保护）；现有任务行为如实 | 🔶 视硬件可行性 |
| J-14 | 24h 挂机（抽测） | 空闲 24h 后回来看队列 | 无重启循环（对照记忆 R15：960 次/24 天教训）；自动任务按节流周期出现 | boot 日志 |

### K 组·安全与漏洞（14 条）

| # | 用例 | 步骤 | 预期与判据 | 监控/指标 |
|---|------|------|-----------|----------|
| K-01 | 无鉴权面盘点 | 从回环外视角列训练域全部写端点 | 现状=回环内零鉴权（设计如此，单机产品）——**报告披露**而非修复项；确认 TrustedHost 生效（G-15） | 报告条目 |
| K-02 | CSWSH 跨站 WS 劫持 | 本地起恶意页面 `new WebSocket("ws://127.0.0.1:5800/ws")` | ws_origin_guard 拒非同源（main.py:657-663） | 浏览器控制台 |
| K-03 | LAN 令牌模式 | OMNISPACE_ALLOW_LAN=1 环境下无令牌直打 | LanAuthMiddleware 拦（lan_auth.py:7-35） | 401/信封 |
| K-04 | 版本名注入三域 | version=`v1/../../`、`v1%00`、超长串、`v${1e9}` | 知识/风格 regex 白名单拒；人物域记录 K-05 | 错误信封 |
| K-05 | 人物回滚穿越（G-02 复核） | 见 E-14 | 行为记录+定级（利用条件=asset_id 存在+目标含 adapter 文件） | 只读断言，禁破坏 |
| K-06 | 上传文本类 NUL 绕过 | 首 512 字节无 NUL 的 .png 伪装文本 | upload_guard 文本规则+魔数表双验（:54-57,77-88） | 拒 |
| K-07 | XSS-任务名 | API 建任务名 `<img src=x onerror=alert(1)>` | React 转义不执行；检查无 dangerouslySetInnerHTML | DOM 断言 |
| K-08 | XSS-风格描述/角色名 | 同上对 style_prompt 与 asset name | 同上 | 同上 |
| K-09 | SQL 注入回归 | task_id/asset_id/dataset_id 注 `' or 1=1 --` | 全参数化拒/无泄漏（既有哨兵背书，抽实测弹） | 响应 |
| K-10 | 路径穿越全表 | 对 §1.4 列出的全部拼接点逐个打 `../`（dataset_path/version/dataset_id/image_path/asset_id） | 全部拒绝；唯人物回滚 version 见 K-05 | 结果矩阵 |
| K-11 | CRLF/头部注入 | dataset_path 含 `%0d%0a` | 拒或无害转义 | 响应 |
| K-12 | 上传落盘路径审查 | 传文件名 `..\\..\\evil.png` | 落盘名净化（uuid/原名白名单），不越出 uploads 目录 | M5 实际路径 |
| K-13 | 审计完整性 | 执行全部删除/回滚类操作后查 audit_log | 每次破坏性动作有审计行（lora_version_delete/char_lora_version_delete 等） | M4 |
| K-14 | 日志敏感信息 | 抽查 backend.log/events.jsonl | 无 key/token/明文对话内容泄漏进训练日志 | 人工抽查 |

### L 组·降级与诚实性（8 条）

| # | 用例 | 步骤 | 预期与判据 | 监控/指标 |
|---|------|------|-----------|----------|
| L-01 | D3 冻结诚实性 | 知识/风格提交按钮 | 置灰+title 说明维护中（LearnView.tsx:259-262 / StylePage.tsx:503-510）——**不许假可用也不许假报错** | DOM |
| L-02 | API 开放 vs UI 冻结 | C-19/D-17/D-18 汇总 | 双源不一致=缺口 G-06 上报待拍板（封 API 或解 UI） | 报告条目 |
| L-03 | 失败原因可见性矩阵 | 对三域失败任务分别找「原因去哪了」 | 知识=仅日志（G-01 缺口）；风格=style_tasks.error 列前端可见（「错误：X」StylePage.tsx:586）；人物=error 列+toast | 三域对照表 |
| L-04 | 坏源不拖垮 | G-12 对照 | 统一队列 fail-soft 语义达成（单源挂，列表照常+披露） | 响应 |
| L-05 | 降级不伪造 | 基座缺失时 /style/status | base_ready=false 如实（不装 true）；训练拒绝码如实 | API |
| L-06 | 进度诚实性 | 复现 S2 | 失败任务 progress=0 不虚标；无「假 99%」悬挂 | M4 |
| L-07 | 「自动」徽章诚实性 | C-10 对照 | 客户端推断的标签标注为已知简化（G-05），不误导测试判读 | 报告条目 |
| L-08 | 英文状态原样 | F-04 对照 | pending_review/registered 直出无翻译——UX 待办非功能缺陷 | 截屏 |

---

## §5 指标与通过标准（KPI）

| 类别 | 指标 | 目标值 | 测法 |
|------|------|--------|------|
| 性能 | 页签切换视觉反馈 | P95 <300ms | 仿真点击+录屏帧分析 |
| 性能 | /training/tasks 聚合响应 | P95 <300ms（28 项基线）；<500ms（200 项压力） | Network 计时 |
| 性能 | 5s/3s 轮询节奏偏差 | ±20% | Network 时间戳 |
| 性能 | 10s MP4 抽帧 | <30s | 上传到 toast |
| 一致性 | WS 帧 vs DB 状态延迟 | <2s（知识链） | M1 vs M4 对拍 |
| 一致性 | UI 状态落后 DB | ≤1 个轮询周期（3-5s） | 删除/取消类用例 |
| 可靠性 | 全部 169 条通过率 | P0 用例（★标记于执行时圈定：A-04/B-01/C-07/C-19/D-08/E-01/E-05/F-06/G-01/G-03/H-03/I-01/J-05/K-02/K-10）100%；整体 ≥95% | 汇总 |
| 稳定性 | 长跑（J-08）后端零死亡 | RAM 提交峰值 <机器 80% | 任务管理器采样 |
| 稳定性 | 轮询泄漏 | 离开页面后残留=0 | Network 15s 窗口 |
| 安全 | K 组全部拒绝面 | 除已申报缺口（G-02/K-05）外 100% 拒绝 | 结果矩阵 |

**签收标准**：P0 全绿 + 缺口清单（§6）逐条有归属（修复/挂起拍板）+ 证据归档完整。

---

## §6 本计划已锁定的问题清单（测试靶点，执行时重点验证并升级为缺陷单）

| # | 问题 | 证据 | 建议 |
|---|------|------|------|
| G-01 | **知识任务失败原因无处落库**：train_tasks 无 error 列+白名单丢弃 error 字段；前端「失败 0%」无线索（S2 病灶直接根因） | database.py:277-291；lora_training_service.py:236-239,734-750 | 列缺失属 P1 级可见性缺陷，修复须拍板（加列或复用 meta） |
| G-02 | **人物回滚 version 无白名单**（三套回滚中唯一） | character_lora_service.py:565-573 | 低利用难度但补 regex 一行事——随下次人物域改动带上 |
| G-03 | 风格/人物训练无 WS 广播，进度全靠轮询（断连感知延迟=轮询周期） | 两服务无 ws_broadcaster | 拍板是否补齐（对齐知识链） |
| G-04 | 风格/人物回滚无确认弹窗（知识域有 medium 级）——误触即换生效版本 | StylePage.tsx:744-750；CharacterTrainPanel.tsx:201-206 | UX 一致性拍板 |
| G-05 | 「自动」徽章为前端推断（priority 兜底 'auto'），非后端真源 | learnApi.ts:71 | 低危记录 |
| G-06 | UI 冻结（D3）但 POST /learn/train、/style/train API 仍开放 | learn.py:78-141 无禁用逻辑 | 双源策略拍板 |
| G-07 | 页面刷新丢页签状态（本地 state） | TrainingPage.tsx:150 | UX 低危 |
| G-08 | 人物训练无显存/RAM 数值准入闸（知识链有 10GB 闸） | character_lora_service.py:209-226 | 16GB 卡实测后拍板 |

---

## §7 执行纪律（红线，源自 CLAUDE.md §16 与项目记忆）

1. **绝不 kill 直杀后端**；一切重启走 splash `/api/quit`（J-07）。
2. **GPU/训练测试前必核后端实时任务**（P-02）；发现并发会话在飞立即停手协调。
3. 环境改动类用例（E-07 改基座目录名）执行后**立即恢复并双人复核**。
4. IAB 仿真点击遇 role-selector 超时→改 CUA 坐标点击；截图超时弃用改 DOM 断言（记忆 manga-asset-dock-collapse / learning-g1）。
5. row_ids 用 32 位全 ID；「卡住」先查 DB 终态再下结论（WS 断连假死常见）。
6. 每轮结束跑 `tools/run_tests.py` L1 回归，确认测试数据未污染真库（记忆 knowledge 金标集教训）。
7. 测试产物（上传文件/临时任务/临时版本目录）收尾统一清理并列清单。
8. 未经用户拍板不 commit；本计划文档本身亦待拍板。

---

## §8 证据归档规范

```
logs/test-runs/training-center-2026-10-18/
├── TC-<编号>/
│   ├── before/ after/ 截屏
│   ├── ws-frames.jsonl      # M1
│   ├── events.jsonl          # M2 过滤后
│   ├── backend.log.txt       # M3 切片
│   ├── db-snapshot.sql       # M4 相关行
│   └── disk-inventory.txt    # M5 目录清单
└── SUMMARY.md                # 通过率矩阵 + 缺口升级清单
```

---

## §9 执行顺序建议（分四批，每批一步一报）

| 批次 | 内容 | 预估 |
|------|------|------|
| 批 1 | P 组前置 + A/B 组（导航与队列，只读零风险） | 半天 |
| 批 2 | C/D/F/G 组（知识+风格+API 契约，含拒绝面） | 一天 |
| 批 3 | E/H 组（人物域+调度互斥，动真训练） | 一天（含训练等待） |
| 批 4 | I/J/K/L 组（E2E 闭环+稳定性长跑+安全+诚实性汇总） | 1-2 天（J-08/J-14 挂机另计） |

> 批 3 起涉及真实 GPU 训练：先跑 I-01 单发确认时长，再排互斥组；批 4 的 J-14 为可选抽测。
