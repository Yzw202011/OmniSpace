# OmniSpace 系统日志页严酷测试计划——仿真人 E2E + 后端链路监控 + 日志覆盖遗漏审计（2026-10-19）

> **被测对象**：`/logs` 系统日志页五签全部功能（事件时间线 / 执行流程 / 技术日志 / 资源曲线 / 作品文件），
> 对应 `frontend/src/components/logs/`（LogsPage 666 行 + FlowPanel + FlowTraceView + FlowGraph + FlowTimeline
> + FileGallery + LogSupportWidgets，共 3022 行），后端 `src/api/logs.py`（460 行）
> + `src/services/event_log.py`（526 行）+ `src/services/flow_trace.py`（748 行）
> + `src/services/resource_sampler.py`（190 行）+ `src/middleware/event_log_auto.py`（260 行）
> + `src/api/system.py` 画廊端点（:2011-2056）。
>
> **目标**：以「仿真人操作点击」为主轴、后端与链路监控为第二轴、**日志覆盖遗漏审计为第三轴**
> （用户点名命题：*功能的更新和增加，但是系统日志没有实时更新*），做一次极严、极细、可复现、
> 可取证的验收级测试。共 **174 条用例**（A~N 十四组，P0×48 / P1×85 / P2×41）。
> 本文档为测试计划与步骤手册，**不改动任何业务代码**；执行产出的报告落 `docs/test-report/`。
>
> 配套纪律真源：CLAUDE.md §16、`docs/test-plan-fullstack-e2e-2026-10-18.md`（总纲纪律沿用）、
> `docs/test-plan-settings-2026-10-19.md`（分组格式沿用）。

---

## 一、总纲

### 1.1 测试目的

1. **功能跑通**：五签全部交互真实有效——每一下点击都有真实后端效果，无假按钮、无假成功、无假数据；
2. **仿真人缺陷**：用真人点击暴露「真人才会踩到」的问题：Tab 切换状态保持/竞态/空态误导/文案失真/禁用态错误/刷新死角；
3. **账实一致**：前端展示的每个数字（统计卡/计数/曲线/文件数）都能与后端三本账
   （事件 jsonl / flow_trace.db / usage jsonl）+ 原始日志文件**对得上**；
4. **日志覆盖遗漏审计（本轮核心命题）**：把最近上线的功能逐个真实触发一遍，验证「功能在跑、
   日志有痕、15 秒内可见、文案是人话、模块标签正确」——专抓**功能已更新/新增、但事件日志
   没落、落了但标签裸露英文、落了但前端不实时刷新**三类遗漏；
5. **稳定性量化**：自动刷新长跑、事件风暴、日志轮转、后端重启窗口、跨天日切下的行为边界与 KPI；
6. **漏洞攻击面**：路径穿越/任意文件读/XSS/伪造上报刷屏/导出脱敏失效/SQL 注入/zip-slip。

### 1.2 严格度分级

| 级别 | 含义 | 执行要求 |
|---|---|---|
| **P0 阻断级** | 日志丢失/账实不符/任意文件读/XSS/数据破坏 | 每轮必跑，任一失败=本轮判废 |
| **P1 严重级** | 功能失效或误导用户（假实时/标签裸露/数字僵化） | 每版必跑，失败须修复或挂账 |
| **P2 完整级** | 边角/体验/性能/文案 | 发版前全量跑一遍 |

### 1.3 判定符号

- ✅ 通过：步骤全走完且全部预期成立，有取证；
- 🔶 有条件通过：主路径成立但命中已知缺口对照表（须注明 BUG-xx / GAP-xx 编号）；
- ⚠️ 未测/证据不足：不得替用户宣称通过；
- ❌ 失败：任一预期不成立，附取证与初判根因。

---

## 二、被测对象解剖（代码级地图，写用例的事实依据）

### 2.1 五签 × 组件 × 后端端点对照（实测 2026-10-19）

| 签（tab 值） | 前端组件 | 主要交互 | 后端端点（真源） |
|---|---|---|---|
| 事件时间线（events，默认） | LogsPage 内联（:414-574）+ EventRow + ErrorSummaryCard | 四统计卡 / 24h 趋势 / 模块分布 / 级别五按钮 / 模块下拉 / 天数 1·7·30 / 关键词 Enter / 卡片「技术详情」展开 | GET `/logs/events`、`/logs/stats`、`/logs/modules`、`/logs/errors/summary`（logs.py:58/143/149/244） |
| 执行流程（flows） | FlowPanel（459 行）+ FlowTraceView（537 行）+ FlowGraph + FlowTimeline | 子签「实时追踪/事件聚合」、状态七筛选（running/stalled/success/error/cancelled/orphan）、流程详情节点链、10s 自动刷新（FlowPanel.tsx:47、FlowTraceView.tsx:48） | GET `/logs/flows`、`/logs/flows/trace`、`/logs/flows/trace/{flow_id}`、`/logs/flows/trace-stats`（logs.py:75-140） |
| 技术日志（raw） | LogsPage 内联（:583-655） | 六文件签、关键词 Enter ±3 行、级别 ERROR/WARNING/INFO | GET `/logs/raw`（白名单 6 文件，logs.py:48-55/172-225）、`/logs/files` |
| 资源曲线（resource） | ResourceChart（LogSupportWidgets.tsx:208-277） | 显存/内存折线、30s 轮询、断点诚实渲染 | GET `/hardware/resource-samples`（resource_sampler.py 30s 采样环形 2h） |
| 作品文件（gallery） | FileGallery（141 行） | 四类过滤 / 48 每页分页 / 灯箱 / 下载 / 只读 | GET `/system/files/gallery`（system.py:2011-2056），媒体回读走 `/manga/media/{rel}` 白名单 |

**页级全局控件**（LogsPage.tsx:330-412）：导出诊断包弹窗（GET `/logs/export`，logs.py:324-459）、
自动刷新勾选（15s，仅事件时间线签生效，:281）、刷新按钮（flows/resource/gallery 三签禁用，:389）、
清理过期日志（POST `/logs/cleanup`）。

### 2.2 数据流与「三本账」（后端监控取证的账本）

```
① 事件账  logs/events/events-YYYYMMDD.jsonl（按天切分，30 天保留）
   写入方 A：业务埋点 log_event（src/services/event_log.py:96-144，永不抛错）
            调用点实测 24 个文件（api/system、engines/vllm_service、services/model_manager、
            scheduler/decision、resource_guard、knowledge_checkup、lora_training_service…）
   写入方 B：API 自动兜底中间件 EventLogAutoMiddleware（middleware/event_log_auto.py）
            三条件防刷屏：POST/PUT/DELETE/PATCH 变更 ∪ 任意失败信封 ∪ 耗时≥3000ms
            （config.yaml:255-256 event_log.slow_request_ms=3000）；GET 成功不记。
② 流程账  logs/flow_trace.db（SQLite 独立库，表 flow_executions + flow_nodes，实测 .tables 确认）
   写入方：flow_trace.start_flow —— 全仓调用点仅 5 处：api/dialog.py:866、api/draw.py:273、
            api/manga/video.py:1343、api/manga/comic_asset.py:77、services/browser_agent_service.py:1465
   语义：STALL_THRESHOLD_S=120s 无心跳→stalled（flow_trace.py:77）；启动时 recover_orphans
            （main.py:342，上一进程 running → orphan，错误码 PROCESS_EXIT）
③ 资源账  logs/usage/usage-YYYYMMDD.jsonl（30s 一条，环形 2h + 落盘 30 天）
   写入方：resource_sampler.py 守护线程；巡检越界（RAM85%/VRAM90%/DISK90%）边沿触发
            广播 resource_alarm 事件（:133-162）
```

另有：原始技术日志 6 文件（backend.log/error.log/vllm-server.log/boot.log/comfyui_boot.log/launcher.log，
backend.log 轮转 10MB×5）；前端异常采集链 `window.onerror`/`unhandledrejection` → POST
`/logs/frontend-event`（main.tsx:40-56，message 钳 200 字、stack 钳 1000 字，logs.py:288-297）。

### 2.3 预置发现（写计划时静态走查已锁定的嫌疑，执行时逐条实锤/证伪）

> 以下为**计划阶段发现**，只挂账不私改；修复须用户拍板后另行走方案。

| 编号 | 级别 | 描述 | 证据 |
|---|---|---|---|
| **BUG-01** | P1 | **「作品文件」签渲染了「技术日志」原始日志内容**：tab 三元链 events→flows→resource 之后 else 落空，gallery 落入 raw 视图分支，随后 ：657 又追加渲染 FileGallery——用户看到原始日志卡 + 画廊上下两截 | LogsPage.tsx:575-657（三元链 `: tab === 'resource' ? <ResourceChart/> : (raw 视图)` 无 gallery 分支 + `{tab === 'gallery' && <FileGallery />}`）；用户截图 5 实锤（作品文件高亮但内容为「原始日志」） |
| **BUG-02** | P2 | **「最近 7 天异常聚合」卡不参与自动刷新**：仅挂载时拉一次，无轮询无重挂——统计卡 15s 涨、聚合卡数字僵化，同页两处「错误数」不同步；这正是用户感知「日志没有实时更新」的第一嫌疑点 | LogSupportWidgets.tsx:139-148（useEffect 空依赖数组，仅首拉）；对照 LogsPage.tsx:279-284 统计卡 15s 轮询 |
| GAP-01 | P2 | **character 模块无中文映射**：人物 LoRA 路由挂在 `/api/v1/character/lora/*`（character_lora.py:52+），中间件兜底取首段 `character`——前端 MODULE_NAMES（LogsPage.tsx:64-85）与后端 _MODULE_ZH（event_log_auto.py:65-81）**双双缺失**，时间线裸显「character」 | 用户截图 1 底部实锤：「character接口调用失败：asset_id 不合法」 |
| GAP-02 | P2 | **plugins / upgrade 模块同样无中文映射**：两模块在 main.py _API_MODULES 注册（:473-474），但不在中间件 _PREFIX_MODULES 白名单（event_log_auto.py:44-62），落首段兜底 `plugins`/`upgrade`，前端后端映射双缺 | main.py:456-479 × event_log_auto.py:44-81 |
| GAP-03 | P3 | **后端 _MODULE_ZH 缺 training / vllm / frontend**（前端 MODULE_NAMES 有 training='知识训练'）——三处模块字典（前端/后端中间件/流程聚合 _MODULE_CATEGORY_FALLBACK）互不同步，training 事件的大白话文案会裸露英文「training接口…」 | event_log_auto.py:65-81 × LogsPage.tsx:64-85 × event_log.py:241-249 |
| GAP-04 | P2 | **中文动作名登记表 EVENT_LABELS 仅 6 条**（图片生成/模型加载/模型卸载/视频生成，event_log_auto.py:85-92）——其余全部变更操作的事件文案都是通用模板「XX接口 POST /api/v1/... 完成」。近两个月上线的新功能（超分、模型设置、训练中心、学习页、存储清理、备份恢复…）**零中文动作名登记**，日志可读性停留在兜底模板 | event_log_auto.py:83-92 |
| GAP-05 | P2 | **「执行流程·实时追踪」只覆盖 5 条产线**（对话/绘画/漫剧视频/漫剧资产/浏览器代理，见 2.2 start_flow 调用点）——小说生成、训练任务、学习会话、语音、风格、云端任务**不进流程追踪**，用户在执行流程签会看到「功能跑了但这里没记录」 | flow_trace.start_flow 全仓 grep 仅 5 调用点 |
| GAP-06 | P3 | **资源账按 UTC 日切名、事件账按本地日切名**：usage 文件名用 `time.gmtime`（resource_sampler.py:127），events 用本地 `datetime.now()`（event_log.py:89-93）——跨日界 8 小时窗口内两账「同一天」文件名不同天 | resource_sampler.py:127 × event_log.py:89-93 |
| GAP-07 | P3 | **事件查询零缓存零索引**：query_events/event_stats/list_modules 每次请求全量重读 30 天 jsonl 文件（event_log.py:151-184）——当前 7 天 3562 条尚可，30 天×高频轮询（15s 一次三连发）为潜在性能税 | event_log.py:151-205、438-443 |
| GAP-08 | P2 | **自动刷新仅覆盖事件时间线**：技术日志签无自动刷新（筛选变化才拉）、作品文件签无任何刷新且全局刷新按钮禁用（LogsPage.tsx:389）——生成完成后画廊不会自己出现新文件 | LogsPage.tsx:279-294、:389 |
| GAP-09 | P3 | 异常聚合卡（/logs/errors/summary）与统计卡（/logs/stats）刷新时机不同（挂载一次 vs 15s 轮询），数字存在暂时性不一致窗口（同 BUG-02 根因的表象面） | LogSupportWidgets.tsx:142-148 × LogsPage.tsx:279-284 |

### 2.4 用户截图指纹对照（取证基准）

| 图 | 内容 | 对应用例 |
|---|---|---|
| 图1 | 事件时间线：7 天异常聚合（browser×460 file:///C:/Windows/win.ini、learn×34 lora_rank>64、system×33 ../../data/omnispace.db、style×27 zzzz-not-hex、hardware×21、novel×21）；统计 3562/1571/1286/637；底部「character接口调用失败：asset_id 不合法」 | B-11、C-14、L-10、K-02、N-01（**注意：×460/×33 为历史渗透测试流量指纹，验证「如实归档」而非复现**） |
| 图2 | 执行流程·实时追踪：50/250、video-20261019020053-8d615f「中断 PROCESS_EXIT 后端进程重启，流程状态未知（历史遗留）」、多条 0 节点 0ms 同秒重复项、资源行「每0% 内存12.6G GPU 1.3/16G·3%·40°C」（**「每0%」疑为字段渲染异常**） | E-09、E-12、E-15、M-02 |
| 图3/图5 | 技术日志签与作品文件签内容**完全相同**（原始日志 backend.log） | BUG-01 直接证据，A-12/I-01 复验 |
| 图4 | 资源曲线：显存16%/内存34%，双平线，断点说明文案 | H-01、H-04 |

---

## 三、测试环境与工具链

### 3.1 被测环境（固定）

- 前端入口：`http://127.0.0.1:5800/#/logs`（boot.py 默认链；**同机只跑一套**，先核单实例守卫）；
- 后端：src.main（py312 主链）127.0.0.1:5800；**绝不直杀后端进程**，退出走 splash `/api/quit` 正规链；
- 数据目录：`logs/events/`、`logs/flow_trace.db`、`logs/usage/`、`logs/backend.log` 等六文件；
- 硬件：RTX 5070 Ti 16GB（资源曲线基线：空闲显存 ~16%、内存 ~34%，见图 4）。

### 3.2 仿真人工具链（browser-use IAB）

- 主用 browser-use 控浏览器：真点击/真输入/真滚动，禁 evaluate 直改 DOM 代替点击；
- **已知坑（沿学习页教训）**：role 定位器超时时改坐标点击兜底；截图超时改 DOM 断言取证；
- 受控输入组件用真人式键入（type 逐字），Playwright type 绕不过 React 受控组件时改 dispatchEvent 后必须 DOM 断言复核。

### 3.3 后端监控六源（评测只信这六源）

| 源 | 路径/方式 | 看什么 |
|---|---|---|
| 事件账 | `logs/events/events-YYYYMMDD.jsonl`（`tail -f` 实时） | 事件落库时刻/module/event/level/friendly/duration_ms/trace_id |
| 流程账 | `logs/flow_trace.db`：`sqlite3 "select flow_id,module,feature,status,node_count from flow_executions order by started_at desc limit 10"` | 流程状态机/节点链/卡住/orphan |
| 资源账 | `logs/usage/usage-YYYYMMDD.jsonl` | 30s 采样连续性/null 断点比例 |
| 技术账 | `logs/backend.log`、`error.log` 等 6 文件 | 轮转/异常栈/启动链 |
| API 直调 | `curl http://127.0.0.1:5800/api/v1/logs/...` | 信封结构/参数校验/错误码 |
| DevTools Network | 浏览器面板 | 请求时序/15s 轮询周期/响应体与页面渲染对账 |

### 3.4 实时监控点（每组开跑前挂起）

```bash
# 事件账实时流（Git Bash）
tail -f "logs/events/events-$(date +%Y%m%d).jsonl"
# 后端错误流
tail -f logs/error.log
# 流程账最近 10 条
sqlite3 logs/flow_trace.db "select flow_id,module,feature,status,node_count,datetime(started_at,'unixepoch','localtime') from flow_executions order by started_at desc limit 10;"
```

---

## 四、仿真人操作纪律（违者该用例判无效）

1. 每条用例从 `/chat` 或直接 `#/logs` **真实导航**进入，不热接力上条用例的页面状态；
2. 点击必须落在元素真实坐标/role 上；定不到 = 先截图报告，不许盲猜坐标连点；
3. 输入框一律真人键入 + Enter/失焦提交，禁止 JS 直接 set value；
4. 每步操作后「三看」：看页面反馈（toast/列表变化）→ 看 Network 请求 → 看事件账 tail（10s 内应出现对应事件行）；
5. **并发会话纪律**：涉及生成类触发（L-03/04/05、E-05）前必查后端实时任务（近 5 分钟提交记录 + >1GB comfy 进程），撞车立即改期；**绝不重启后端**；
6. **熔断线**（触发任一立即停整轮）：后端进程死亡 / 事件账文件损坏或被误删 / flow_trace.db 写坏 / GPU 温度 >89°C / 出现非本轮制造的未知错误激增。

---

## 五、测试指标体系（KPI 与基线）

| 指标 | 目标 | 测法 |
|---|---|---|
| 事件查询 `/logs/events`（200条/7天） | P95 < 500ms | curl 循环 20 次，DevTools 交叉验证 |
| 技术日志 `/logs/raw`（300 行） | P95 < 800ms；带关键词全扫 < 2s | 同上 |
| 异常聚合 `/logs/errors/summary`（7 天 637 错） | < 1.5s | 同上 |
| 导出诊断包 | 24h 包 < 10s；30 天包 < 60s；zip 完整可解压 | 计时 + 解压校验 |
| 事件落库→前端可见延迟 | 自动刷新 ≤ 15s；手动刷新 ≤ 1s | 操作时刻 t0 与 jsonl ts、页面出现时刻三方比对 |
| 自动刷新周期 | 15s ± 2s（事件时间线）；10s ± 1s（流程追踪）；30s ± 2s（资源曲线） | Network 时序 |
| 时间线渲染 | 200 条首渲染 < 1s；滚动无明显掉帧 | Performance 面板 |
| 资源曲线断点率 | < 2%（诚实断开以 usage jsonl null 比例为准，不伪断） | 24h 挂机取样 |
| 页面 30min 内存漂移 | < 50MB | DevTools Memory 前后快照 |
| 日志覆盖遗漏率（L 组） | 目标 0：每个已上线功能至少一本账有痕 | L 组逐条判定 |

---

## 六、测试矩阵总览

| 组 | 覆盖面 | 条数 | P0 | P1 | P2 |
|---|---|---|---|---|---|
| A | 页面骨架与五 Tab 导航 | 12 | 5 | 4 | 3 |
| B | 事件时间线·统计卡与异常聚合 | 14 | 2 | 7 | 5 |
| C | 事件时间线·筛选/搜索/空态 | 14 | 4 | 7 | 3 |
| D | 事件卡片交互 | 10 | 2 | 5 | 3 |
| E | 执行流程·实时追踪 | 16 | 6 | 7 | 3 |
| F | 执行流程·事件聚合（历史兼容） | 8 | 0 | 4 | 4 |
| G | 技术日志 | 12 | 4 | 5 | 3 |
| H | 资源曲线 | 8 | 2 | 3 | 3 |
| I | 作品文件画廊（含 BUG-01 复验） | 10 | 4 | 4 | 2 |
| J | 全局动作：导出/自动刷新/清理 | 14 | 4 | 8 | 2 |
| K | 后端 API 直测与信封 | 12 | 4 | 6 | 2 |
| L | **日志覆盖矩阵——功能更新×日志遗漏审计** | 20 | 5 | 12 | 3 |
| M | 稳定性与疲劳 | 10 | 1 | 6 | 3 |
| N | 漏洞攻击面 | 14 | 5 | 7 | 2 |
| **合计** | | **174** | **48** | **85** | **41** |

---

## 七、分组用例明细

> 表格列：编号 | 优先级 | 操作步骤（仿真人视角） | 预期结果与取证。
> 「对账」= 前端渲染与 §3.3 六源之一数值一致；「事件账」= `logs/events/events-当日.jsonl`。

### A组 · 页面骨架与五 Tab 导航（12 条）

| 编号 | P | 步骤 | 预期与取证 |
|---|---|---|---|
| A-01 | P0 | 侧栏点「系统日志」进入 /logs | ≤3s 可交互；标题「系统日志」+副标题「全项目运行日志…（仅保留 30 天，超期自动清除）」 |
| A-02 | P0 | 逐一核对五个 Tab 文案与顺序 | 事件时间线/执行流程/技术日志/资源曲线/作品文件，顺序一致，当前项 btn-primary 高亮 |
| A-03 | P0 | 全新打开页面（清 sessionStorage 后直达） | 默认落「事件时间线」签 |
| A-04 | P0 | 核对右上控件 | 导出诊断包 / ☑自动刷新（默认勾选）/ 刷新 / 清理过期日志 四控件齐 |
| A-05 | P1 | 在 #/logs 按 F5 整页刷新 | 仍在本页（Hash 路由），数据重新拉取成功 |
| A-06 | P1 | 五 Tab 乱序快速点击 ×3 轮 | 无报错 toast、无卡死、无双份内容渲染 |
| A-07 | P1 | 依次切到执行流程/资源曲线/作品文件 | 全局「刷新」按钮变禁用，hover title 提示各自独立刷新（LogsPage.tsx:389）；切回事件时间线/技术日志恢复可用 |
| A-08 | P1 | 事件时间线设筛选（级别=错误+关键词）→ 切技术日志 → 切回 | 筛选状态保持不清零（React state 常驻） |
| A-09 | P2 | 核对 ModuleGuide 模块指引 | 页顶存在且不遮挡内容 |
| A-10 | P2 | 窗口 1280px 与 1920px 两档宽度 | Tab 行/工具行 flex-wrap 换行不重叠不截断 |
| A-11 | P2 | 离开 /logs 去 /chat 再返回 | 数据重新拉取（Network 可见新请求），非陈旧缓存 |
| A-12 | P0 | **【BUG-01 复验】**点击「作品文件」Tab | 当前预期（未修复态）：出现「原始日志」卡 + 下方 FileGallery 两截内容——如实截图取证 BUG-01；修复后重跑本条：应只见 FileGallery，无任何原始日志元素 |

### B组 · 事件时间线·统计卡与异常聚合（14 条）

| 编号 | P | 步骤 | 预期与取证 |
|---|---|---|---|
| B-01 | P0 | 读取四统计卡数字，`curl /logs/stats?days=7` 对账 | 总事件=stats.total；成功/注意/错误=by_level 三项，全等 |
| B-02 | P1 | 天数切「最近 1 天」 | 统计卡与趋势随 days=1 重查，数字 ≤ 7 天口径 |
| B-03 | P0 | 核对「最近 7 天异常聚合」卡 | ≤6 组，每组 ×计数/[模块]/文案单行截断（LogSupportWidgets.tsx:162-171） |
| B-04 | P1 | 聚合卡 total_errors vs 统计卡「错误」 | 同为 7 天 error 口径应相等；不等时记录拉取时刻差并挂 BUG-09/GAP-09 |
| B-05 | P1 | **【BUG-02 实锤】**经 N-05 手法制造 1 条新 frontend 错误 → 等 15s 自动刷新 | 统计卡「错误」+1 而聚合卡 total_errors 不变——同页数字不同步取证；手动 F5 刷新页面后聚合卡才追上 |
| B-06 | P1 | 24h 趋势柱状图 | 24 根柱；hover 出「MM-DD HH:00 · N 条」title |
| B-07 | P1 | 模块分布徽章 | 与 /logs/stats by_module Top12 一致，中文名映射正确 |
| B-08 | P2 | 空库态（临时 OMNISPACE 测试目录或清库窗口） | 四卡全 0、趋势空、列表空态文案「还没有日志事件。用一用 AI 对话…」 |
| B-09 | P2 | total_errors=0 时（新装） | 聚合卡整卡隐藏（return null，LogSupportWidgets.tsx:148） |
| B-10 | P1 | 找一条超长 example 聚合行 | 单行省略 + hover title 全文可读 |
| B-11 | P2 | 核对图1 指纹仍在 | browser「win.ini」、system「omnispace.db」聚合条目如实归档（历史渗透流量不删不改不粉饰） |
| B-12 | P2 | 抽查聚合条目数据 | count 为正整数；last_ts ≥ first_ts（logs.py:268-275 时序修复回归） |
| B-13 | P2 | 窄屏四统计卡 | 2×2 grid 不挤压不换行错乱 |
| B-14 | P1 | DevTools Network 抓 /logs/stats 响应 vs 页面渲染 | 数值逐项一致（渲染层无二次加工失真） |

### C组 · 事件时间线·筛选/搜索/空态（14 条）

| 编号 | P | 步骤 | 预期与取证 |
|---|---|---|---|
| C-01 | P0 | 点级别「错误」 | 列表全部红色错误徽标；Network 请求带 level=error |
| C-02 | P0 | 打开模块下拉 → 选一项（如 browser） | 下拉选项=GET /logs/modules 动态清单；列表只剩该模块事件 |
| C-03 | P0 | 天数切「最近 30 天」 | total ≥ 7 天口径；无超期数据泄漏（≤30 天，event_log RETENTION_DAYS） |
| C-04 | P0 | 关键词输入「清理」回车 | 命中 friendly/detail/event 任一字段（event_log.py:181），Network 带 keyword=清理 |
| C-05 | P1 | 清空关键词回车 | 恢复全量列表 |
| C-06 | P1 | 大小写各搜一次（如 GPU/gpu） | 结果一致（后端 .lower() 包含匹配，event_log.py:182） |
| C-07 | P1 | 组合：级别=错误 + 模块=browser + 关键词 | 三条件 AND 生效，逐条核对 |
| C-08 | P1 | 核对「共 N 条事件（新 → 旧，最多展示 200 条）」 | 文案数字=total；total>200 时列表恰 200 条卡 |
| C-09 | P1 | 核对排序 | 首条 ts ≥ 末条 ts（新→旧） |
| C-10 | P2 | 构造无匹配组合（关键词=乱串） | 空态文案「当前筛选条件下没有匹配的事件，试试放宽筛选」 |
| C-11 | P2 | 200 条满列表滚动 | 滚动区限高 68vh，统计卡/筛选栏不随滚走（09-12 用户反馈回归，LogsPage.tsx:542-553） |
| C-12 | P2 | 任一筛选变化 | Network 立即发新请求（useEffect :274-277），无需回车 |
| C-13 | P1 | 中文关键词（如「模型加载」） | 命中且中文渲染无乱码 |
| C-14 | P1 | **【映射审计】**逐项核对模块下拉中文名 vs LogsPage.tsx:64-85 | system→系统、dialog→AI 对话、manga→漫剧、vllm→推理引擎、training→知识训练…；**character/plugins/upgrade 应无中文映射原样裸显** → GAP-01/02 取证 |

### D组 · 事件卡片交互（10 条）

| 编号 | P | 步骤 | 预期与取证 |
|---|---|---|---|
| D-01 | P0 | 核对卡片结构 | 级别徽标+模块徽标+耗时（有则显）+friendly 主文案+右侧 MM-dd HH:mm:ss 时间 |
| D-02 | P0 | 点「技术详情」展开→再点收起 | 展开：pre 等宽四行（事件/详情/追踪/时间）；再点收起 |
| D-03 | P1 | 找无 detail 且 event 空的事件（若有） | 不渲染「技术详情」按钮（LogsPage.tsx:140 条件渲染） |
| D-04 | P1 | 找 duration_ms=48000 类事件 | 显示「48.0s」（毫秒→秒换算） |
| D-05 | P2 | 四级别颜色 | 错误红/注意黄/成功绿/信息主色，全用 var(--color-*)（走查无硬编码） |
| D-06 | P2 | 核对时间格式 | 10-19 02:24:35（去年份去时区，fmtTime :91-94） |
| D-07 | P1 | 展开 A 卡后展开 B 卡 | A 保持展开，互不影响（独立 useState） |
| D-08 | P2 | 200 条卡片区快速滚动 | 无明显卡顿/白屏/错位 |
| D-09 | P1 | 抽 1 条带 trace_id 的事件 | 展开详情「追踪:」值可在执行流程签/flow_trace.db 关联到同 trace 流程 |
| D-10 | P1 | 抽查 20 条 friendly | 全部大白话中文；技术栈串/JSON 裸串/堆栈只许出现在 detail 折叠区 |

### E组 · 执行流程·实时追踪（16 条）

| 编号 | P | 步骤 | 预期与取证 |
|---|---|---|---|
| E-01 | P0 | 切「执行流程」签 | 子签「实时追踪/事件聚合（历史兼容）」+说明文案「每次功能执行唯一流程 ID…卡住定位」 |
| E-02 | P0 | 核对列表项结构 | 状态图标+时间+功能名+[module][feature]+耗时+节点数；头部「N/M」 |
| E-03 | P0 | 状态七筛选按钮 | 全部/运行中/卡住/成功/失败/取消/遗留，各带状态计数徽章（计数来自 /logs/flows/trace-stats） |
| E-04 | P0 | 点选一条流程 | 右侧详情：触发源/输入/错误码/资源快照/flow_id（图2 版式） |
| E-05 | P0 | **真弹一发**：提交一次小画图（或漫剧短生成，先过并发纪律） | ≤10s 列表顶部出现 running 流程；完成后 status=success 且节点链 ≥1（flow_trace.db flow_executions+flow_nodes 双表对账） |
| E-06 | P1 | 展开完成流程节点链 | 每节点 seq/node/module/status/started_at/耗时/输入输出摘要齐全 |
| E-07 | P1 | 选中 running 流程等 10s | 详情随轮询自动更新心跳（FlowTraceView.tsx:273-282 keepFlow running 分支） |
| E-08 | P1 | 卡住语义（观察既有 stalled 或构造长任务暂停） | running 且 >120s 无心跳 → stalled 徽标「卡住 Ns」+ stall_analysis（flow_trace.py:77、:637） |
| E-09 | P1 | **后端正规重启窗口**（splash /api/quit 链） | 重启后原 running → orphan，错误码 PROCESS_EXIT、文案「后端进程重启，流程状态未知（历史遗留）」（图2 指纹；main.py:342 recover_orphans） |
| E-10 | P1 | 模块/功能/关键词筛选逐个用 | trace 端点参数逐一生效（module/feature/status/keyword/days） |
| E-11 | P2 | days 参数换算 | 前端 days → start=now-days*86400s 时间戳（logApi.ts:290-292），Network 可验 |
| E-12 | P2 | 核对「50/250」头部 | 展示 50（limit 固定 FlowTraceView.tsx:258）/ 总数 250 语义正确 |
| E-13 | P1 | 自动刷新时选中保持 | 原选中还在则不跳选；消失则自动选最新一条（:276-278） |
| E-14 | P2 | 详情拉取失败（断网模拟） | 保留旧详情不白屏（:280 catch） |
| E-15 | P1 | 资源快照字段渲染 | 内存 GB/GPU MB·利用率%·温度°C 逐字段可读；**专查图2「每0%」疑似乱码字段**（CPU 字段渲染取证） |
| E-16 | P0 | **【覆盖审计 GAP-05】**跑一次小说生成/训练任务 → 回本签 | 「实时追踪」**无**对应新流程（仅 5 产线接入）——如实取证挂账：小说/训练/学习/语音/风格/云端任务不在流程追踪覆盖面 |

### F组 · 执行流程·事件聚合（历史兼容）（8 条）

| 编号 | P | 步骤 | 预期与取证 |
|---|---|---|---|
| F-01 | P1 | 切「事件聚合（历史兼容）」子签 | 流程卡列表，summary 格式「主类型 · N 模块 · N 步 · 全部成功/有注意项/N 个错误」 |
| F-02 | P1 | 制造两条相距 >120s 的事件（两次设置保存） | 聚合为两个独立流程（_FLOW_GAP_S=120 切分，event_log.py:217） |
| F-03 | P1 | 核对状态推导 | 流程内含 error→error；含 warning→warning；否则 ok |
| F-04 | P2 | 联动 M-04 风暴场景 | 单流程事件 >120 条时截断到 120（_FLOW_MAX_EVENTS，event_log.py:220） |
| F-05 | P2 | 噪声过滤 | vram_shed_no_candidate 类心跳不进流程不粘连（_NOISE_EVENTS :214） |
| F-06 | P1 | 流程图/时间轴组件（FlowGraph/FlowTimeline） | 渲染正常无 NaN 坐标/无限高容器 |
| F-07 | P2 | flow_id 格式 | flow-{时间串} 格式稳定可复制 |
| F-08 | P2 | 历史兼容 | 无 trace_id 的旧事件仍能按时间邻近性聚合成流程 |

### G组 · 技术日志（12 条）

| 编号 | P | 步骤 | 预期与取证 |
|---|---|---|---|
| G-01 | P0 | 核对六个文件签 | backend.log/error.log/vllm-server.log/boot.log/comfyui_boot.log/launcher.log；默认 backend.log |
| G-02 | P0 | 核对渲染 | pre 等宽、自动换行、560px 限高内滚（LogsPage.tsx:628-651） |
| G-03 | P0 | 切换文件签 | Network GET /logs/raw?name=对应文件&lines=300（LogsPage.tsx:253） |
| G-04 | P0 | 关键词输入回车 | 命中行带 ±3 行上下文（logs.py:211-217） |
| G-05 | P1 | 级别过滤 ERROR/WARNING/INFO | 按行内 [LEVEL] 标记过滤，无漏无串 |
| G-06 | P1 | 关键词+级别组合 | 双条件叠加正确 |
| G-07 | P1 | 关键词命中 0 行 | 文案「（无命中行——换个关键词或级别试试）」 |
| G-08 | P2 | comfyui_boot.log（引擎冷启动前） | toast「日志文件 comfyui_boot.log 还不存在（可能后端刚启动）」+ exists:false 渲染不崩 |
| G-09 | P2 | 脚注 | 「技术日志面向开发者：含后端/启动链/引擎日志，同样遵循 30 天自动清除。」 |
| G-10 | P1 | 与磁盘对账 | `tail -n 300 logs/backend.log` 与页面行一致（轮转竞态容差 ±5 行） |
| G-11 | P2 | 内容语义抽查 | boot.log=启动链、launcher.log=壳日志、vllm-server.log=引擎日志，语义对应正确 |
| G-12 | P1 | UI 面攻击面确认 | 六签外无任何自定义文件名输入口（API 层穿越归 N 组） |

### H组 · 资源曲线（8 条）

| 编号 | P | 步骤 | 预期与取证 |
|---|---|---|---|
| H-01 | P0 | 切「资源曲线」签 | 标题「近 2 小时，每 30 秒采样」+图例显存/内存+最新百分比（图4 版式） |
| H-02 | P0 | Network 对账 | GET /hardware/resource-samples?limit=480，页面曲线与响应 samples 一致 |
| H-03 | P1 | 挂 70s 观察 | 30s ± 2s 自动刷新一次（setInterval 30_000，LogSupportWidgets.tsx:224） |
| H-04 | P1 | 断点诚实 | 采集失败段曲线断开不连线（buildPath null 分段 :187-206）+「曲线断开处=当次采集失败（诚实零读数，不伪造）」文案 |
| H-05 | P2 | 空数据态（新装） | 「暂无采样数据（采样器随后端启动…）」 |
| H-06 | P2 | 网格线 | 25/50/75% 三条网格线 |
| H-07 | P1 | 与资源账对账 | 抽 3 个采样点与 logs/usage/usage-*.jsonl 对应行 ±1% |
| H-08 | P2 | 数值钳制 | 曲线 0-100% 不越界（buildPath :201 min/max 钳制）；图4 基线（显存16%/内存34%）量级合理 |

### I组 · 作品文件画廊（10 条）

> **前置**：BUG-01 未修复时，原始日志卡会同时渲染在画廊上方——先滚动越过它再操作画廊；
> BUG-01 修复后重跑 I-01 确认无残留。

| 编号 | P | 步骤 | 预期与取证 |
|---|---|---|---|
| I-01 | P0 | 记录作品文件签的完整渲染现状 | BUG-01 实况取证（原始日志卡+画廊双截）；修复后本条改为：无原始日志残留 |
| I-02 | P0 | 四类过滤 seg 按钮 | 全部/生成图片/关键帧/视频/导出成品；切换后 total 与卡片同步（system.py:2020-2027 roots 拼装） |
| I-03 | P0 | 分页 | 48/页；首页「上一页」禁用、末页「下一页」禁用；「N~M / total」文案正确 |
| I-04 | P0 | 点图片缩略图 | 灯箱放大；关闭恢复 |
| I-05 | P1 | 视频卡 | <video> 预览；mp4/webm/gif 判定 is_video（system.py:2034） |
| I-06 | P1 | 下载链接 | a[download] 指向 /api/v1/manga/media/{rel}；点击下载成功可打开 |
| I-07 | P1 | 与磁盘对账 | data/generated/images 等四目录文件总数 ≥ 页面 total；抽 1 个文件 mtime 与卡片日期一致 |
| I-08 | P2 | 只读确认 | 无删除按钮；文案「共 N 个文件（只读——删除请到各模块的正规入口）」 |
| I-09 | P2 | 空态 | 「暂无文件——生成的图片/视频/导出件会出现在这里」 |
| I-10 | P1 | 翻到第 2 页后点刷新 | 保持 offset 重拉（FileGallery.tsx:81 load(offset)），不跳回首页 |

### J组 · 全局动作：导出/自动刷新/清理（14 条）

| 编号 | P | 步骤 | 预期与取证 |
|---|---|---|---|
| J-01 | P0 | 点「导出诊断包」 | 弹窗：时间范围 4 档（1h/24h/7天/30天）/包含 4 勾选（事件/失败流程/原始日志/硬件快照）/脱敏勾选/取消+导出 zip |
| J-02 | P0 | 全默认导出 | zip 下载成功；内含 events.json+events.csv+failed_flows.json+raw/ 六文件+hardware.json+manifest.txt |
| J-03 | P0 | 读 manifest.txt | 生成时间/版本/时间范围/包含项/失败流程数/脱敏标记/事件数逐行正确（logs.py:433-446） |
| J-04 | P1 | 勾「脱敏」再导一包 | 包内 prompt/description/content/api_key/token 系字段值=前4***后4 或 ***（logs.py:302-310）；与不脱敏包 diff 验证 |
| J-05 | P1 | 四个包含项全取消 → 导出 | toast「至少勾选一项内容」，不发请求 |
| J-06 | P1 | events.csv 检查 | 列=全部事件字段并集（logs.py:407）；UTF-8 中文 Excel/文本打开不乱码 |
| J-07 | P1 | 选历史崩溃日（如 10-16 RAM 崩溃）时段导出 | manifest 醒目行「注意：该时段内检测到异常退出…」（logs.py:362-365 crash_note） |
| J-08 | P1 | 取消勾「自动刷新」挂 35s | Network 零轮询请求；重勾恢复 15s 周期；**该开关只管事件时间线签**（LogsPage.tsx:281） |
| J-09 | P0 | **清理实弹**（须拍板窗口：向 logs/events/ 放一个 31 天前日期的测试 events-YYYYMMDD.jsonl） | 点「清理过期日志」→ toast「清理完成：删除 N 个过期文件，释放 X MB」+文件消失+事件账新增 system/logs_cleaned 事件 |
| J-10 | P1 | 无过期文件时点清理 | toast「删除 0 个过期文件」；不产 logs_cleaned 事件（event_log.py:502 if deleted 才记） |
| J-11 | P1 | 清理中快速连点 | 按钮 busy 防连点（cleanupBusy） |
| J-12 | P2 | 清理完成后 | 事件列表自动重拉（LogsPage.tsx:306 fetchEvents） |
| J-13 | P2 | 导出动作审计对账 | 设置页·维护·操作审计可查 logs/diagnostics_export 记录（logs.py:452-456 log_audit） |
| J-14 | P1 | 后端停止时点导出 | toast「导出失败：后端服务可能未启动」，弹窗不卡死 |

### K组 · 后端 API 直测与信封（12 条）

| 编号 | P | 步骤 | 预期与取证 |
|---|---|---|---|
| K-01 | P0 | `curl /api/v1/logs/events?limit=5` | 信封 success:true/data{items,total,has_more}/meta{request_id,timestamp,duration_ms}（ADR-01 HTTP 恒 200） |
| K-02 | P0 | 参数边界：limit=0/1001、days=0/31、level=debug、offset=-1 | 全部拒绝：Query 校验或 SYSTEM_PARAM_INVALID；level 非法信息含原值（图1 ×33 同族语义） |
| K-03 | P0 | `GET /logs/raw?name=../../data/omnispace.db` | SYSTEM_PARAM_INVALID + 回显白名单可选清单（logs.py:187-188）；响应无任何文件内容泄漏 |
| K-04 | P1 | offset 翻页 | offset=0 与 offset=200 两页无重叠且拼接连续（新→旧游标） |
| K-05 | P1 | `/logs/flows` 筛选语义 | module/level/keyword 为**流程级**判定（流程含该元素即整条保留，不切断内部事件，event_log.py:374-390） |
| K-06 | P1 | `/logs/flows/trace/{不存在}` 与超长/特殊字符 flow_id | 前者 SYSTEM_NOT_FOUND；后者安全拒绝无异常栈泄漏 |
| K-07 | P1 | trace-stats 计数对账 | by_status 各值=列表对应状态筛选 total |
| K-08 | P1 | `/logs/errors/summary` | groups 按 count 降序；example ≤160 字；limit 默认 20 |
| K-09 | P2 | `/logs/modules` | 每个模块名都能在 30 天 jsonl 中 grep 到（抽查 3 个） |
| K-10 | P2 | `/logs/files` | size_mb 两位小数、modified ISO 格式、retention_days=30 |
| K-11 | P1 | POST /logs/frontend-event 三连：空 message / 500 字 message / kind=foo | 空→SYSTEM_PARAM_INVALID；超长→钳 200；kind 非法→钳 error |
| K-12 | P0 | POST /logs/cleanup 连发两次 | 均成功；第二次 deleted_count=0；幂等不报错 |

### L组 · 日志覆盖矩阵——功能更新×日志遗漏审计（20 条，本轮核心）

> **统一取证模板**：记录操作时刻 t0 → 打开事件账 tail → 等 15s（自动刷新周期）看页面 →
> `grep 当日 jsonl` 验证落库时刻/module/event/level → 三方（操作/落库/可见）时刻差 ≤15s 判过。
> 判「遗漏」标准：功能真实执行成功（业务侧有产物/状态变化）但事件账 60s 内无痕 = ❌ 覆盖遗漏。

| 编号 | P | 触发动作（仿真人） | 预期事件（module/event/level）与审计点 |
|---|---|---|---|
| L-01 | P0 | 对话页发一条消息（模型热态） | dialog api_call success；文案含「对话」；duration 真实 |
| L-02 | P0 | 模型管理页加载→卸载一个模型 | models「模型加载/卸载完成，用时 X 秒」（EVENT_LABELS 已登记中文，event_log_auto.py:88-89） |
| L-03 | P0 | 绘画页生成一张图 | paint「图片生成」完成事件 + duration 真实秒数 + 执行流程签同步出现绘画流程 |
| L-04 | P0 | 漫剧视频生成 1 段（短产线，先过并发纪律） | manga「视频生成」事件 + 实时追踪 running→success 跨视图联动 + 节点链完整 |
| L-05 | P1 | 漫剧四视图/资产生成 | manga api_call + comic_asset 流程（start_flow comic_asset.py:77） |
| L-06 | P1 | 小说页生成一章 | novel api_call success——**取证文案形态**：预期通用模板「小说接口 POST /api/v1/novel/... 完成」（GAP-04：无中文动作名） |
| L-07 | P1 | 学习页提交一次学习会话 | learn api_call（learning 前缀映射到 learn，event_log_auto.py:48-49） |
| L-08 | P1 | 知识库入库/检索一次 | knowledge api_call |
| L-09 | P1 | 训练中心提交/查询训练任务 | training api_call——**取证 friendly 是否裸露英文「training」**（GAP-03 后端映射缺失） |
| L-10 | P1 | 人物 LoRA：提交训练或取消任务（或触发一次 asset_id 校验失败） | character api_call/api_failed——**直接复现图1 底部指纹**「character接口调用失败」（GAP-01 实锤） |
| L-11 | P1 | 风格页训练/预览 | style api_call |
| L-12 | P1 | 云端「测试连接」用假 key | cloud **api_failed error** 事件含 suggestion（失败信封必记——GET 失败也记，event_log_auto.py:229-239） |
| L-13 | P2 | 语音转写或合成一次 | voice api_call |
| L-14 | P2 | 插件加载/卸载一次 | plugins api_call——**取证标签裸露「plugins」**（GAP-02） |
| L-15 | P2 | 升级包导入（验签失败包即可） | upgrade api_failed——**取证标签裸露「upgrade」**（GAP-02） |
| L-16 | P1 | 激活语义核对 | GET status 成功不记（防刷屏语义）；POST 激活失败必记 license api_failed——验证「GET 成功不记」不被滥用成「关键动作无痕」 |
| L-17 | P1 | 前端报错链路：DevTools 制造一次未捕获异常 | ≤15s 时间线出现 frontend console_error（main.tsx:40-56 → /logs/frontend-event 全链） |
| L-18 | P1 | 系统动作双账：设置页保存一项 + 存储清理一次 | system api_call ×2；存储清理另有业务事件 storage_cleanup（system.py:547）——**业务事件与 API 兜底事件双轨并存不重复** |
| L-19 | P1 | 调度切换：跑一个 GPU 任务结束后转空闲 | scheduler「模式切换：gpu_primary → all_idle」类事件（图3 指纹 ：104/:221） |
| L-20 | P0 | **【新功能专项】近两周上线功能过堂**（对照 git log 10-03~10-19）：①漫剧超分列 SR 按钮 ②模型设置·超分模型下拉切换（manga.sr_model KV 保存）③训练中心人物链 ④学习页 G1 操作 ⑤设置页六签保存 | 每项操作后查三本账：事件账/流程账/资源账**至少一本有痕**；全部无痕 = ❌ P0 遗漏缺陷「功能已上线、日志零埋点」；有痕但文案为裸路径模板 = 🔶 GAP-04 挂账 |

### M组 · 稳定性与疲劳（10 条）

| 编号 | P | 步骤 | 预期与取证 |
|---|---|---|---|
| M-01 | P1 | 页面挂 30min（自动刷新开） | 轮询 120 次无失败堆积；DevTools Memory 漂移 <50MB；无 toast 抖动 |
| M-02 | P0 | **后端正规重启窗口**（splash /api/quit，绝不直杀） | 页面存活不白屏；后端恢复后 ≤15s 数据自愈；重启前 running 流程转 orphan PROCESS_EXIT（联动 E-09） |
| M-03 | P1 | backend.log 轮转（10MB×5）观察 | 技术日志签 tail 正常无重复行无截断乱码 |
| M-04 | P1 | 事件风暴：脚本 5s 写 300 条事件（测试脚本直调 log_event，须 PYTEST 隔离豁免窗口或专用目录） | 页面不卡死；列表 200 截断生效；聚合单流程 120 上限生效（联动 F-04） |
| M-05 | P2 | 跨天日切：23:58-00:02 跨界操作 | 事件落当日新文件；days=1 查询可见；ts 为 aware ISO（event_log.py:122-124） |
| M-06 | P1 | 测试隔离回归：跑一轮 pytest 后回看错误面板 | 无测试流量污染（PYTEST 态 events-test 分流，event_log.py:60-62；09-17 审计回归） |
| M-07 | P2 | 刷新竞态：轮询在途时改筛选 | 渲染不错序不闪烁（queryRef 快照 :217） |
| M-08 | P1 | 资源曲线 24h 挂机 | 断点率 <2%（以 usage jsonl null 占比为诚实基准，前端断点数与之相符） |
| M-09 | P1 | 30 天大包导出（hours=720） | <60s；zip 完整可解压；导出期间 UI busy 态正常 |
| M-10 | P2 | 周期任务存活 | log-cleanup（24h）/resource-sampler（30s）/flow-trace cleanup 守护线程日志心跳可查（periodic daemon） |

### N组 · 漏洞攻击面（14 条）

| 编号 | P | 步骤 | 预期与取证 |
|---|---|---|---|
| N-01 | P0 | 路径穿越复显：`GET /logs/raw?name=../../data/omnispace.db` 及 `..\\\\..\\\\` 变体 | SYSTEM_PARAM_INVALID，无文件内容泄漏（图1 ×33 指纹复验；白名单唯一真源 logs.py:48-55） |
| N-02 | P0 | 穿越变体全套：绝对路径 D:/、file:///、%2e%2e 编码、大小写 Backend.log、尾点`backend.log.`、尾空格、NULL 截断 | 全部拒绝；错误信息只回显白名单清单，不泄漏文件系统结构 |
| N-03 | P1 | lines 边界：2000（上限ok）/2001/0/-1/99999 | 上限内正常；超界与非法值拒绝（Query ge=1 le=2000） |
| N-04 | P1 | keyword 注入面：10KB 超长/换行/emoji/%00/正则元字符 `.*[]()` | 纯字符串包含匹配（logs.py:203-206）——不崩、不注入、响应仍为合法信封 |
| N-05 | P1 | frontend-event 伪造刷屏：脚本连发 >300 次/分 | 全局限流 300 req/60s 生效（429/限流信封）；msg/stack 钳制生效（logs.py:288-297） |
| N-06 | P0 | 存储型 XSS：经 N-05 注入 `<img src=x onerror=alert(1)>` 入 friendly | 时间线卡片/聚合卡/技术详情 pre 全部文本转义，脚本不执行（React 默认转义；走查确认无 dangerouslySetInnerHTML） |
| N-07 | P0 | 导出脱敏有效性：同窗口导 sanitize=true 与 false 两包 diff | api_key/token/authorization/remote_dialog_api_key 等键值全打码（logs.py:302-306）；false 包明文仅限本机自用（知情确认） |
| N-08 | P1 | 导出参数 fuzz：hours=720 边界/721/负数、include=events,foo、failed_flows=0/51 | 边界内成功；越界拒绝；未知 include 项忽略不崩 |
| N-09 | P1 | gallery 参数攻击：`/system/files/gallery?module=../../etc` | roots 拼装不命中→空 items total=0；不抛错不穿越（system.py:2020-2027） |
| N-10 | P0 | 媒体回读穿越：`/api/v1/manga/media/../..//data/omnispace.db` 及编码变体 | 白名单拒绝（复用漫剧 media 防护回归）；gallery 的 url 永远落在四白名单目录内 |
| N-11 | P1 | /logs/cleanup 与 /logs/export 无 CSRF token 评估 | 回环绑定闸门+TrustedHost 下风险论证记录（设计豁免）；LAN 开关打开时本条升级重测 |
| N-12 | P1 | flow_id SQL 注入：`' OR '1'='1`、`%;DROP TABLE--` | SQLite 参数化（flow_trace._query 绑定）——按字面量查无结果 SYSTEM_NOT_FOUND，表结构无损 |
| N-13 | P2 | 临时文件清理：导出后查 %TEMP% | 无 omnidiag_*.zip 残留（BackgroundTask unlink，logs.py:457-459）；异常路径（J-14 失败）也不残留 |
| N-14 | P2 | zip-slip：检查诊断包内部路径 | raw/ 内文件名固定六名（writestr 固定键 :419-428），无 ../ 路径元件 |

---

## 八、日志覆盖对照总表（L 组的判定底表）

> 判「遗漏」时对照此表：该功能**应至少**落以下一本账；「事件文案形态」列即 GAP-04 的验收基准。

| 模块 | 代表动作 | 事件账 | 流程账 | 文案形态预期 |
|---|---|---|---|---|
| dialog | 发消息 | ✅（API 兜底） | ✅ start_flow | 「对话接口 POST …完成」 |
| models | 加载/卸载 | ✅ 业务+API 双轨 | — | 「模型·模型加载完成」（中文已登记） |
| paint | 生图 | ✅ | ✅ | 「绘画·图片生成完成」（中文已登记） |
| manga | 视频/资产 | ✅ | ✅ video+comic_asset | 「漫剧·视频生成完成」（中文已登记） |
| novel | 生成章节 | ✅（仅 API 兜底） | ❌ 未接入 | 通用模板（GAP-04） |
| learn/learning | 学习会话 | ✅（仅 API 兜底） | ❌ 未接入 | 通用模板 |
| knowledge | 入库/检索变更 | ✅（变更类） | ❌ | 通用模板 |
| training | 训练任务 | ✅ | ❌ 未接入 | 「training接口…」裸英文（GAP-03） |
| character | 人物 LoRA | ✅ | ❌ 未接入 | 「character接口…」裸英文（GAP-01） |
| style | 风格训练 | ✅ | ❌ | 通用模板 |
| cloud | 连接测试/启停 | ✅ | ❌ | 通用模板 |
| voice | 转写/合成 | ✅ | ❌ | 通用模板 |
| plugins | 加载/卸载/invoke | ✅ | ❌ | 「plugins接口…」裸英文（GAP-02） |
| upgrade | 导入/开始升级 | ✅ | ❌ | 「upgrade接口…」裸英文（GAP-02） |
| license | 激活提交 | ✅（仅失败+变更） | ❌ | 通用模板 |
| frontend | 页面异常 | ✅ console_error 链 | ❌ | 「前端异常：…」 |
| system | 设置/清理/备份 | ✅ 业务事件（storage_cleanup/restore_staged/health_*）+API 兜底 | ❌ | 业务事件中文 + 兜底模板 |
| scheduler | 模式切换/策略 | ✅ 业务埋点 | ❌ | 「模式切换：gpu_primary → all_idle」 |
| vllm | 引擎起停 | ✅ 业务埋点 | ❌ | 前端映射「推理引擎」；后端 _MODULE_ZH 缺（GAP-03） |
| browser | 池/代理任务 | ✅ | ✅ browser_agent | 「浏览器…」 |

---

## 九、测试稳定性规程（每轮次必做）

### 9.1 重复性
- P0 全量 ×2 轮背靠背；L 组（覆盖矩阵）×2 轮（第二轮换时段，验证非缓存假象）；
- 两轮结果不一致 = 该用例判 ❌ 不稳定并单独立案。

### 9.2 压力与疲劳
- M-01 30min 长挂 + M-04 风暴 + M-09 大包导出各 1 次/轮；
- 高频筛选操作：C 组筛选连续切换 50 次，观察请求合并与渲染稳定性。

### 9.3 环境扰动
- 后端正规重启 ×1（M-02，需窗口）；系统休眠唤醒 ×1（页面恢复自愈）；
- 跨天日切观察（M-05，夜间挂机自然覆盖）。

### 9.4 窗口与拍板需求汇总
| 事项 | 需要什么 |
|---|---|
| J-09 清理实弹 | 拍板：允许向 logs/events/ 写入一个伪造 31 天前的测试 jsonl（执行完即被清理，无残留） |
| E-09/M-02 重启窗口 | 后端空闲窗口 + splash /api/quit 正规链 |
| L-04/E-05 生成实弹 | GPU 空闲窗口 + 并发会话核查（GPU 测试活动门纪律） |
| M-04 风暴脚本 | 拍板：临时测试脚本直调 log_event（跑完即删，PYTEST 隔离窗口） |
| L-15 升级包 | 一个验签必失败的测试 .upg 包 |

---

## 十、执行顺序建议

1. **第 1 轮（干跑，~2h）**：A→B→C→D→G→H→K（纯页面+API，零生成零风险）+ N-01/02/03（穿越面）；
2. **第 2 轮（实弹，~3h，需 GPU 空闲）**：E→F→I→L-01~L-19（生成类触发按并发纪律串行）+ J；
3. **第 3 轮（审计，~2h）**：L-20 新功能专项 + L 组第二轮复核 + §八底表逐行销账；
4. **第 4 轮（稳定，挂机+窗口）**：M 全组 + E-09/M-02 重启窗口 + N-06/07 安全实弹；
5. 每轮结束即出阶段小报（通过/失败/挂账三清单），全部轮次结束汇总 `docs/test-report/test-report-system-logs-20261019.md`。

---

## 十一、报告与产出台账

- 报告路径：`docs/test-report/test-report-system-logs-20261019.md`（沿用全栈 E2E 报告格式：KPI 实测表 + 分组判定 + 缺陷清单 + 截图取证附录）；
- 缺陷清单必填：BUG-xx/GAP-xx 编号 + 复现步骤 + 取证（截图/请求响应/jsonl 行）+ 初判根因（file:line）；
- **预置发现 BUG-01、BUG-02、GAP-01~09 的修复一律待用户拍板后另行走方案四段制，测试执行不夹带私改**；
- 执行期间发现的新遗漏（功能在跑/日志无痕类）随时补入 §八底表并编号 GAP-10+ 顺延。

---
*本计划基于 2026-10-19 代码实测（行号当日有效，执行时若代码已变以最新代码为准并记录版本）。
仅供学习使用，商业授权请+Q 3559331368*
