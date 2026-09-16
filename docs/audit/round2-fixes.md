# 第二轮审计修复记录：功能完整性与需求符合性

- 日期：2026-08-07
- 对应审计：`round2-function-integrity.md`（R2-B01 ~ R2-B16）
- 原则：所有修复均以文档B《中文版（修正版B）》为基线；无法完整实现的能力以 degraded 标记诚实呈现，禁止伪造。

---

## 一、P1 问题修复（7/7 全部完成）

### R2-B01 对话被动补全流程缺失 → ✅ 已修复
- **改动文件**：
  - `src/api/dialog.py`：新增被动补全完整链路——`_UNCERTAINTY_MARKERS`（24 个中文不确定性标记）、`_detect_uncertainty()`（首 800 字 + 尾 400 字探测）、`_extract_keywords()`（无 NLP 依赖的轻量提取：拉丁/数字词 + 停用片段过滤后的中文段）、`_quick_search_supplement()`（浏览器池快速搜索 ≤3 页、25s 时间预算、每页截 1500 字/总量 3000 字上限，仅直接导航搜索 URL 不填表单，遵守 TC-S-012）、`_passive_reinfer()`（补充上下文重推理一次）、`_maybe_passive_completion()` 编排（全程容错，任何子步骤失败回退原回复）。
  - 三条推理路径全部接入：非流式 `dialog_send`（响应携带 `passive_completion` 字段）、SSE `_stream_response`（[DONE] 前追加改进回复 token + `passive_completion` 事件）、WS `_ws_handle_message`（追加 token 推送后落库，meta 标注）。
  - `src/services/learning_scheduler.py`：注册 `TRIGGER_DIALOG_GAP` 默认动作 `_default_dialog_gap_trigger`——缺口关键词沉淀为学习主题（source=auto），后续由空闲/定时触发器按正常门控拾起学习；不同步启动会话（对话 P0 进行中学习 P3 必须让行，§8.4.2）。
- **验证**：`_detect_uncertainty("我不确定…")=True`、肯定句=False；`_extract_keywords("请问 Qwen3-VL 的最大上下文长度是多少？")=['Qwen3','VL','最大上下文长度是']`；三触发器回调注册确认。

### R2-B02 学习触发器框架未接线 → ✅ 已修复（前序会话 + 本次补全）
- 前序已完成：`learning_scheduler.tick()` 周期评估（30s 节流）由调度引擎 1s tick 驱动（`services/scheduler/__init__.py:139`），空闲 >5 分钟 fire TRIGGER_IDLE、学习时段内 fire TRIGGER_SCHEDULED，默认动作 `_default_learn_trigger` 自动为最近活跃主题启动学习会话。
- 本次补全：`src/middleware/request_context.py` 新增 `_report_user_activity()`——变更类 HTTP 请求（POST/PUT/DELETE/PATCH /api/*）视为真实用户操作刷新空闲计时；GET 轮询不计，避免状态栏轮询误判活跃。至此 5 个触发器（手动/定时/空闲/项目驱动/对话缺口）全部接线。

### R2-B03 LoRA 自动微调未接线 → ✅ 已修复（前序会话）
- `learning_scheduler._maybe_auto_finetune()`：消费 `auto_finetune_frequency` 设置（off/daily/weekly/monthly），知识点达阈值 + GPU 空闲 + 过周期窗口 → 排队微调；防重入旗标 + 拒绝后 600s 节流。本次补充 monthly 周期映射（文档B §4.4 四档）。

### R2-B04 知识 LoRA 未接入推理 → ✅ 已修复（前序会话）
- `dialog_engine._attach_knowledge_lora()`：基座加载后查询 current LoRA 版本，`PeftModel.from_pretrained` 挂载（基座匹配校验，失败回退纯基座）；`refresh_knowledge_lora()` 热更新（训练完成回调）；`get_status()` 上报 `knowledge_lora` 字段。

### R2-B05 学习进度 WS 推送端点缺失 → ✅ 已修复（本次）
- `src/api/learning.py`：新增 `@router.websocket("/learn/session/progress")`，每 2 秒推送会话状态快照（与 GET /learn/session/status 同构），无会话推 `{"status":"idle"}`；快照为内存读不阻塞事件循环。路由注册已验证（`/learn/session/progress`）。

### R2-B06 分镜列表/排序端点缺失 → ✅ 已修复（前序会话）
- `GET /manga/storyboard/list`（+顶层别名）、`POST /manga/storyboard/reorder`（row_ids 有序数组批量更新 sort_index）。

### R2-B07 分镜 AI 描述/预览端点缺失 → ✅ 已修复（前序会话）
- `POST /manga/storyboard/ai-describe`（单行上下文调对话模型重生成描述）、`POST /manga/storyboard/preview`（绘画引擎出预览图，引擎未就绪时 degraded 诚实降级）。

---

## 二、P2 问题修复（本次处理 5 项；其余 4 项记入文档侧决策）

### R2-B08 导演台 /export 缺失 → ✅ 已修复（本次）
- `POST /manga/director/export`（+顶层别名）：导出 stage 完整状态 JSON 包（场景元信息 + 角色布局 + 机位列表，`format: omnispace-director-stage/v1`），全部为数据库真实状态。
- **附带诚实性修复**：`director_panorama` 与 `director_screenshot_4in1` 原返回占位 PNG 但**无 degraded 标记**（不诚实）。后端无 3D 渲染引擎（3D 渲染在前端 Three.js 画布执行），两端点已补 `degraded:true` + `degrade_reason` 说明，与全项目降级契约一致。

### R2-B09 /system/info 缺失 → ✅ 已修复（本次）
- `GET /system/info`：版本信息 + 硬件摘要（GPU 名/显存/tier/CPU/内存）聚合；硬件采集失败降级为纯版本信息。`/system/update` 为免安装 RC 有意省略（文档侧标注，见 R2-B14 决策）。

### R2-B10 对话模型未按硬件等级路由 → ✅ 已修复（前序会话）
- `dialog_engine._effective_candidates()`：HARDWARE_TIER_TABLE min_vram ≥12GB 档（RTX 4090/5090）将 qwen3-vl-8b 纳入首选候选，其余档位保持 4b/2b 保守路由。

### R2-B13 §4.4 学习设置默认值三处不符 → ✅ 已修复（本次）
- `browser_agent_service.DEFAULT_LEARNING_SETTINGS`：`auto_finetune_frequency` daily→**weekly**、`sensitive_content_handling` skip→**mask**（标记待审核）、`show_browser` False→**True**；枚举补齐 monthly（周期映射 2592000s）与 stop（键白名单免校验，直接可写）。
- 前端本地回退默认值 `useLearningStore.DEFAULT_LEARN_SETTINGS.show_browser` 同步 false→true。
- 验证：`settings: weekly mask True`；`periods: ['daily','weekly','monthly']`。

### R2-B11/B14/B15/B16 → 文档侧决策（不改代码）
- **R2-B11（文档E 细粒度端点漂移）**：文档E 附录B 独有端点（chat/complete、art/inpaint、model/download 等）未被文档B 基线要求；其中 model/download 与离线 RC 定位冲突。决策：以文档B 为权威基线，文档E 侧标注裁剪，见 `../ADR-001.md` 增补。
- **R2-B14（端口/WS 路径）**：5800 端口与 /api/v1 前缀为 RC 交付事实且前后端同源自洽；以代码事实修订文档B（localhost:8000→127.0.0.1:5800，/ws/{module}→/ws 中枢 + /api/v1/* WS）。
- **R2-B15（Celery/Redis）**：离线单机场景下"功能锁互斥 + 后台线程 + P0-P5 优先级让行"为进程内等价实现；文档侧标注 Celery/Redis 为可选横向扩展方案。
- **R2-B16（users/assets 表）**：users 表与"本地应用无需 Token"（§7.1.6.1）冲突，文档E 侧标注删除；assets 以"文件系统+JSON 引用"方案为准。

### R2-B12 §4.3 学习策略自适应 3 项 → 留待第三轮（性能/策略维度）评估处理
- 去重阈值随容量调整、连续质量下降暂停自动训练、功能久未使用降低学习优先级——属策略优化项，纳入第三轮审计范围统一决策。

---

## 三、验证记录

| 验证项 | 方法 | 结果 |
|---|---|---|
| 改动文件语法 | ast.parse ×7 文件 | ✅ |
| 模块导入 | import dialog/learning/manga/system/learning_scheduler/browser_agent_service/request_context | ✅ |
| 应用装配 | src.main app 创建，11 个路由模块注册 | ✅ |
| WS 端点注册 | learning.router 含 /learn/session/progress | ✅ |
| 触发器注册 | list_triggers 含 scheduled/idle/dialog_gap 回调 | ✅ |
| 不确定性检测 | 标记句=True / 肯定句=False | ✅ |
| 关键词提取 | 疑问句→有效关键词序列 | ✅ |
| §4.4 默认值 | weekly / mask / True + monthly 周期 | ✅ |

## 四、第二轮修复后剩余事项

- R2-B12（§4.3 自适应 3 项）→ 第三轮审计处理。
- 前端轮询 GET /learn/session/status 保留（WS 为增量契约，轮询仍可用）。
- 文档B/E 侧标注项（R2-B11/B14/B15/B16）→ 汇总进 ADR-001 与最终报告。
