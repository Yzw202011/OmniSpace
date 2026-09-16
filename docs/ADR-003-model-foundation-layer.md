# ADR-003：模型服务通用底座——统一引擎协议、注册表真源与生命周期

- 状态：**已接受（Accepted，2026-08-29 用户批准）**
- 实施状态：**P1 已落地（2026-08-29）**——manifest v3（33 模型族磁盘真源）+ `src/data/model_registry.py` 唯一读者 + HARDWARE_TIER_TABLE 幽灵型号清零 + startup_check 第 25 项升级为 manifest↔磁盘一致性校验（实跑"33 条记录，磁盘一致"）+ `tests/unit/unit/test_model_registry.py` 6 用例锁定；pytest 136 passed / 5 skipped；ruff 本机不可用，以 py_compile + 活函数实跑替代（诚实记录）。
- 实施状态：**P2 已落地（2026-08-29）**——`src/services/inference/base_engine.py`（BaseEngine ABC + ENGINE_MODULES 品类注册表 + register/unregister/resolve_engine）；dialog/paint/video/voice 四引擎继承协议（勘察实证四引擎既有鸭子契约完整，协议为正式化而非改造；voice 补 unload_model + get_voice_engine 单例 getter）；`ModelManager._get_engine` if/elif 迁移为注册表驱动（行为等价，新增品类仅注册即接入）。协议只固化生命周期与可观测面，推理接口不进协议（防过度抽象）；is_ready 跟随既有 @property 形状。`tests/unit/unit/test_base_engine.py` 8 用例。
- 实施状态：**P3 已落地（2026-08-29），P1-P3 全部完成**——`base_engine.py` 增加 `EngineState` 统一状态机（UNAVAILABLE/UNLOADED/BOOTING/READY/SLEEPING/STOPPING/ERROR，前四值与既有引擎 state 字符串逐字兼容）+ `derive_state` 单一口径 + 统一钩子 `state`/`health_check`/`graceful_stop`/`vram_reclaim_gb`；vLLM 子进程全链确定性迁移日志（grep「vLLM 状态:」可回放 booting→ready→stopping→unloaded→sleeping，验收②达成）；dialog `_unified_state` 并入 vLLM 子进程事实（预热窗口/权重装载/失联降级如实可观测）；`release_for_module` 终止 vLLM 后经 unload_model 链路同步台账与引擎态（修复面板 stale bug，验收①达成）；E2E 实测（验收③）：预热情 booting → ready(197s) → 绘画切换 stopping/unloaded → 台账 loaded_count=0/预留量归零 → SDXL 出图成功 → dialog 如实 unloaded。`test_lifecycle_state.py` 12 用例；pytest **155 passed / 5 skipped**。P3 诚实边界：sleeping/wake 的 live 触发属 comfy/关键帧协商路径（同日志机制，单测覆盖），E2E 走的是 release_for_module 终止路径。
- 日期：2026-08-29
- 决策者：待用户批准
- 关联：ADR-001（单进程单体拓扑）/ ADR-002（浏览器 + launcher 交付形态）/ docs/model-deployment-plan.md（模型主题唯一权威版）/ RTM M 域 / CLAUDE.md §1/§4
- 证据：本文全部论断经五轮代码级验证（V1 协议面 / V2 注册表 / V3 生命周期与任务层 / V4 规范 / V5 反证轮），可复跑审计脚本见根目录 `_tmp_adr_v2_manifest_audit.py`；所有 文件:行号 均为 2026-08-29 实测。

## 1. 决策问题

项目模型资产已覆盖对话/图像/视频/语音/工具/嵌入六大类（磁盘 90 个含权重或配置标记的模型目录），但承载它们的"模型服务层"由三套机制拼成，四个关键面各自分裂：

1. **引擎协议只统一了对话侧**。全后端唯一 ABC 是 `DialogBackend`（`backends/base.py:112`，load/unload/chat_stream/count_tokens + 工厂注册 `backends/__init__.py:21`）；paint/video/voice/depth/detect/segment/triposr/h3/comfy 共 9 个主力引擎类全部无共享基类（2026-08-29 全量 class 声明扫描实测，已剔除 StylePack/RouteDecision/流解析器等非引擎类）。`ModelManager._get_engine`（`model_manager/__init__.py:687-726`）为 if/elif 字符串分发，只接 dialog/paint/video 三类，voice/auxiliary/embedding 直接返回 None（由调用方本地加载）——新增品类必须改管理器本体。
2. **注册表分裂且三方失真**。`models/models_manifest.json`（manifest_version=2）仅 9 条目，其 `required:true` 的 qwen2-vl-2b 磁盘已不存在；代码内 `HARDWARE_TIER_TABLE`（`data/models.py:171`）引用的 flux.1-dev-fp8 / ltx-2 / kolors-2.1 / wan2.1-14b-fp8 磁盘同样不存在（幽灵型号）；实际路由（`gen_router.py:231-237` `_available_models`）走 paint_engine 磁盘探测 + 代码内偏好链，manifest 的 capabilities/min_vram_gb 字段不参与任何路由裁决。manifest 现存读者仅 2 处：`startup_check.py:476`（第 25 项检查，只数条目数）与 `api/models.py:704`（仅取 dependencies 兜底）。
3. **生命周期双态未统一**。进程内引擎（diffusers/transformers，模块级懒加载单例）与 vLLM 子进程（py313，`vllm_service.py:84-697` 完整 start/stop/_kill_locked/_reap_orphans/sleep_for_paint/wake_from_paint 睡眠换装）语义完全不同；2026-08-29 启动日志实测 vLLM 对话模型预热被模块切换资源释放（→video_gen）交叉终止，处于"启动中→取消"的不可观测态。
4. **任务/流式至少三套并存**。对话 SSE（`api/dialog.py:106` `_sse` + `:716` chat_stream，旧 WS `:1419` 兼容保留）；绘画私有线程队列（`api/draw.py:636` `_dispatch_loop`）；LoRA 训练与风格 LoRA 各一个独立 `PriorityQueue`（`lora_training_service.py:273` / `style_lora_service.py:161`）。

边际成本：每接入一个新模型/新推理框架，需分别摸清上述四面的现状约定；注册表失真则直接误导选型与启动检查。

## 2. 决策

**建立显式的模型服务底座层（Model Foundation Layer），在现有零件上收敛而非重写**，范围三项：

1. **统一注册表真源（manifest v3）**：补齐磁盘全部模型族；schema 固化为 capabilities / min_vram_gb / min_ram_gb / resident / dependencies / lifecycle（inproc|subprocess）；`HARDWARE_TIER_TABLE` 的 models 映射与 gen_router 偏好链改为读 manifest。
2. **统一引擎协议（BaseEngine）**：仿 `DialogBackend` 定义 load/unload/generate/status/is_ready/last_error 协议与工厂注册；paint/video/voice 三主力引擎首批接入；`_get_engine` 改注册表驱动（CATEGORY → FACTORY 映射）。
3. **统一生命周期**：子进程形态封装为与进程内同签名钩子（health_check / graceful_stop / vram_reclaim），睡眠换装语义保留但纳入同一状态机与可观测状态。

**非目标（诚实排除）**：

- 不做云端底座形态（多租户 / 服务化 / 分布式调度 / 统一 KV-cache 服务）——ADR-002 已裁定单用户单机 127.0.0.1 交付，无此需求；
- 不改调度器 / feature_lock / offload 既有语义，只收敛其接入面；
- 不引入新外部组件（无 Redis / Celery / 新消息中间件）；
- 不一次性重写引擎实现——协议为适配层，现有 `get_xxx_engine()` 单例 getter 保持兼容，渐进迁移。

## 3. 理由

1. **底座零件已有六成**：ModelManager 单例（注册/显存分配/优先级驱逐/互斥/回滚/预测器）、调度器 + vram_manager + gpu_backend 精度协商矩阵、`DialogBackend` 三后端即插即拔（vLLM/transformers/GGUF，注释已预留 TGI/SGLang 接入路径）、gen_router 风格识别 + 降级链。缺的只是把对话侧已验证的形态推广到全品类。
2. **注册表失真已产生实际误导**：manifest 与硬件分档表各自引用不存在的型号，启动检查第 25 项只统计条目数、无法发现幽灵型号或缺失模型；真源不唯一，任何"模型面板/自动选型"类功能都建立在失真数据上。
3. **即插即用收益立现**：vLLM 子进程本就 OpenAI 兼容，diffusers 家族天然支持本地目录——统一注册表后，新模型"拖进 models/ + manifest 登记"即可被选型/面板/启动检查识别；新推理框架接入降为"实现协议 + 注册工厂"两步。
4. **单卡形态下这是正确抽象层级**：16GB 单卡的底座价值在互斥调度 + 按需换装 + 可替换，而非并发服务；现有 feature_mutex + 优先级驱逐就是单卡下的正确语义，收敛接入面即可，无需引入服务化架构。

## 4. 分期范围与验收口径

| 期 | 内容 | 验收口径 | 风险 |
|---|---|---|---|
| **P1 注册表** | manifest v3 重写（补齐 ~35 模型族 + schema 固化）；HARDWARE_TIER_TABLE models 映射与 gen_router 偏好链改读 manifest；api/models.py、startup_check.py 消费方同步 | ① 启动检查第 25 项升级为 manifest↔磁盘一致性校验（required 在位、孤儿目录告警、幽灵型号清零）；② `_manifest_entry` 消费新 schema 无回归；③ manifest schema 单测锁定；④ ruff + pytest 全绿 | 低（纯数据 + 读取收敛，不触碰加载路径） |
| **P2 引擎协议** | `BaseEngine` ABC + 工厂注册；paint/video/voice 首批接入；`_get_engine` 改注册表驱动 | ① 新增 fake 品类引擎仅靠注册即可被 ModelManager 管理（单测锁定）；② `get_xxx_engine()` getter 兼容零破坏；③ feature_lock/scheduler/offload 语义零变化（既有测试全绿） | 中（触碰引擎实例化路径） |
| **P3 生命周期** | 子进程/进程内统一状态机与钩子；睡眠换装纳入统一状态 | ① 模型面板 loaded_models 对两类形态状态一致可观测；② 启动预热交叉终止场景有确定性状态与日志（消除"启动中→取消"不可观测态）；③ 对话/绘画互斥切换 E2E 回归通过 | 高（触碰 vLLM 加载/睡眠路径） |

**排序建议**：P1 → 一致性主线 P1-b → P2 → P3。P1 与主线无冲突可先行；P2/P3 与主线抢资源，排后。

## 5. 后果

- **收益**：新模型接入成本从"改 3-4 处代码 + 摸现状约定"降为"manifest 登记 + 实现协议"；注册表单一真源消灭幽灵型号；模型面板/启动检查/自动选型可观测性提升。
- **成本（估算）**：P1 约 1-2 天；P2 约 3-5 天；P3 约 3-5 天。
- **风险（诚实记录）**：P2 迁移中引擎单例 getter 被 API 层与 manga 视频工作线程多处引用，须保持签名兼容；P3 触碰 vLLM 睡眠换装，回归面含对话/绘画互斥全链路。
- **放弃的收益（诚实记录）**：不追求云端底座的并发服务能力；多卡调度仅保留 config.yaml 既有 secondary_offload 开关，不做泛化。

## 6. 重启条件（可逆门）

出现以下任一情况时重开本 ADR：

1. 需要多用户/多实例并发服务（超出单机底座形态）；
2. 需要多卡自动并行调度泛化（超出既有 secondary_offload）；
3. 出现长上下文多会话并发，需要统一 KV-cache 服务化。

## 7. 验证方式

- P1：pytest（含新增 manifest schema 用例）+ startup_check 实跑；
- P2/P3：`tools/run_tests.py` 三层测试 + 对话/绘画互斥切换活后端 E2E 手工验收。
