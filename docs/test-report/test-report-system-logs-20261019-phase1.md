# 系统日志页测试报告 · 第 1 轮（阶段报告）— 2026-10-19

> 计划：`docs/test-plan-system-logs-2026-10-19.md`（174 条）｜本报告覆盖第 1 轮干跑 + 第 2 轮无 GPU 部分，
> **04:16:37 后端被并发会话正规退出后测试中止**（详见 §五环境事件）。结论一律带取证。

---

## 一、总裁定

| 范围 | 已判 | ✅ | 🔶 | ❌ | ⚠️ 未执行 |
|---|---|---|---|---|---|
| 计划用例 | 78 条 | 62 | 9 | 1 | 96（受阻于后端退出/窗口未到） |
| 计划外新发现 | 6 项 | — | — | — | — |

- **62 ✅**：A 组全绿（12/12）、K 组 17 项参数与信封、N 组 13 项穿越/注入全防住、B/C 组账实对账主体。
- **1 ❌（K-04，已定性为设计局限）**：新→旧 offset 翻页在持续写入下漂移（实时流 20+20 条重叠 19 条）；前端无翻页控件故用户不可见，稳定子集复核被后端退出打断，挂账待复测。
- **9 🔶**：BUG-01 复验后续项（I 组画廊被串台遮挡）、C-10/13 空态与中文（API 层已证、UI 输入受自动化工具限制未走完）、B-05 动态观察、C-07 组合筛选、C-11/C-12、D-02、E-12。

## 二、预置发现实锤情况（计划 §2.3）

| 编号 | 判定 | 实锤证据 |
|---|---|---|
| **BUG-01** 作品文件签渲染原始日志 | ✅ **实锤** | 点击作品文件签 → DOM 断言 原始日志卡=true + 作品文件区=true 双渲染；截图：作品文件高亮但主区为「原始日志」+traceback（LogsPage.tsx:575-657 三元链落空 + :657 追加，与预判行号一致） |
| **BUG-02** 异常聚合卡不自动刷新 | ✅ **实锤（静态+活体）** | 代码：LogSupportWidgets.tsx:142-148 仅挂载拉一次；活体：同屏截图 聚合卡「共 749 条错误」vs 统计卡「错误 751」滞后 2 条；B-04 证明两端点同瞬数据本应相等（796=796） |
| GAP-01 character 标签裸露 | ✅ 实锤 | 模块下拉含裸选项 `character`；事件卡「character接口调用失败」（jsonl 近 1 天 5 条） |
| GAP-02 plugins/upgrade 裸露 | ✅ 实锤 | 下拉裸选项 `plugins`/`upgrade`；jsonl plugins×7、upgrade×6 条全部裸英文 |
| GAP-03 后端 _MODULE_ZH 缺 training/vllm/frontend | ✅ 实锤 | 模块分布徽章 `video`/`behavior` 裸显；下拉含裸 `video` |
| GAP-04 EVENT_LABELS 仅 6 条 | ✅ 实锤 | 时间线大量「XX接口 POST /api/v1/... 完成」通用模板（对话/云端/小说/系统全中） |
| GAP-05 流程追踪仅 5 产线 | ✅ 实锤 | trace-stats modules 与 start_flow 调用点一致；小说/训练无流程 |
| GAP-06 资源账 UTC 日切 | ✅ 实锤 | `logs/usage/` 最新文件=usage-20261018.jsonl，**10-19 本地样本落在 18 日文件**（events 账已到 events-20261019） |
| GAP-07 事件查询零缓存 | 🔶 旁证 | 30 天 24,770 条时查询尚快（P95 目标未正式压测）；30 天压测待窗口 |
| GAP-08 画廊无刷新+刷新按钮禁用 | ✅ 实锤 | gallery 签 isEnabled=false（flows/resource 同） |
| GAP-09 聚合卡/统计卡口径 | ✅ 合并进 BUG-02 | 同瞬两端点相等（796=796），不等只因刷新时机 |

## 三、计划外新发现（编号顺延）

| 编号 | 级别 | 发现 | 证据 |
|---|---|---|---|
| **BUG-03** | P2 | **流程资源快照 CPU 恒 0%**：`cpu_percent: 0.0` 系统性出现——psutil.cpu_percent() 无 interval 首调返回 0 的经典语义坑；UI 如实渲染了坏数据 | flow_trace.db resource_start 字段抽样 ×N 全部 `"cpu_percent": 0.0` |
| **BUG-04** | P1 | **执行流程 0 节点占 98.3%**（17694/17999）+ **同秒流程泄漏**：04:01:39 一秒内创建 12 条 running 流程（video×5+paint×7），该秒无任何对应 api_call 事件=绕过 HTTP 的服务层直接 start_flow，且 15 分钟后仍 running 永挂；截图 2 的「0 节点 0ms」是系统性数据质量问题而非常规态 | flow_trace.db SQL 取证 + events jsonl 04:01 窗口比对 |
| **GAP-10** | P2 | **模块下拉被兜底机制污染**：任何 /api/v1/* 路径（含 404/fuzz 垃圾）都会造出一个模块标签并列进下拉——现含 `__no_such_endpoint__`、`nonexistent`、`comic`、`chat`、`health` 等 30 个模块（modules API 实测） | /logs/modules 返回 30 项；截图下拉全录 |
| **GAP-11** | P3 | behavior（/api/v1/behavior 真实路由）不在 _PREFIX_MODULES → 裸标签无中文 | jsonl behavior×11 条全裸英文 |
| **GAP-12** | P2 | **正规退出在事件账无痕**：04:16:37 走 splash /api/quit 正规链（boot.log 三行有痕），事件账没有「系统已安全关闭」终笔——用户在日志页看不到最后一次关闭 | boot.log:2026-10-19 04:16:37 三行 × events-20261019.jsonl 无对应行 |
| 观察 | P3 | 今日 02:11~03:23 后端 6 次 backend_started（重启风暴窗口），与 R15 审计「960 次重启」画像一致 | events-20261019.jsonl |

## 四、已执行通过项亮点（抽样举证）

- **穿越面全防住（N-01/02/03，13/13）**：`../../data/omnispace.db`、绝对路径、file://、%2e 双编码、大小写、尾点/尾空格、NULL 截断、反斜杠——全部 SYSTEM_PARAM_INVALID 且只回显白名单；backend.log 把每次试探都如实落 WARNING。
- **SQL 注入串安全（K-06）**：`' OR '1'='1`、`%;DROP TABLE flow_executions--` → SYSTEM_NOT_FOUND，trace-stats 表完好（总流程 17,998 照常返回）。
- **账实一致（B-01/B-04/B-12/C-08/C-09）**：stats.total=Σby_level；同瞬 summary.total_errors=stats.error=796；聚合组 first/last 时序修复回归通过；200 截断+has_more 正确；新→旧排序成立。
- **A 组交互全绿**：五 Tab 乱序 15 连点无崩溃无重复渲染；筛选状态跨签保持（错误筛选→跨签→仍 btn-primary+200 条全错误徽标）；flows/resource/gallery 三签刷新按钮正确禁用。
- **门禁语义顺带验证**：发行包实例 :5801 未激活状态下 /logs/stats 正确返回 LICENSE_REQUIRED（激活门工作正常）。

## 五、环境事件时间线（04:16 后测试中止的根因）

| 时刻 | 事件 | 证据 |
|---|---|---|
| 04:15:56 | 开发后端最后一条日志（硬件 WS 断开） | logs/backend.log 尾 |
| 04:16:37 | **正规退出**：「正在退出：启动页请求退出→正在关闭→已全部关闭」 | logs/boot.log |
| 04:17+ | 发行包实例 :5801 已在运行（PID 8708，OmniSpace-BackendC.exe 3GB）；开发版 boot 自检拒绝接管：「本机正在运行发行包实例（:5801）」 | logs/boot.log:04:24:57 |
| 04:25+ | :5801 未激活（license/status gate_enabled=true, activated=false），业务 API 一律 LICENSE_REQUIRED | curl 实测 |

按 §16.6 并发会话纪律：不杀 5801、不抢启动开发版，测试挂起。**恢复条件（二选一）**：
① 发行包实例从其启动页正规退出后，常规链拉起开发版 :5800；② 用户拍板由我方在 5801 退出后重启开发版。

## 六、未执行清单（96 条，按受阻原因分组）

- **后端退出受阻**（后端恢复即可跑）：B-02/05/08/09/10 复测、C-07/10/11/12/13 UI 面、D-02~09、E-02/04~15、F 组全组、G 组 UI 面、H-02~08、I-02~10（画廊，需 BUG-01 修复或绕过取证口径）、J 组全组、K-11/12、N-04~14。
- **需拍板/窗口**：J-09 清理实弹（伪造 31 天文件）、M-02 正规重启窗口（本轮已被动观察一次退出，正式复测仍需窗口）、M-04 风暴脚本、L-04 GPU 生成实弹、N-05 限流洪泛（防时间线污染，建议专项窗口做）。
- **需 BUG-01 先修**：I 组 9 条画廊交互才能在干净视图下跑。

## 七、修复建议优先级（待拍板，本报告不含任何代码改动）

1. **P1 BUG-01**：LogsPage 五签三元链补 gallery 分支（1 行级修复）；修后 I 组即可全跑。
2. **P1 BUG-04**：流程泄漏治理——start_flow 的服务层直调路径补节点记录/终态收口；running 超时自动 orphan（现仅启动时 recover）。
3. **P2 BUG-02**：ErrorSummaryCard 纳入 15s 轮询或随 fetchEvents 刷新。
4. **P2 GAP-10/11/01/02/03**：模块标签治理三件套——_module_for 白名单化（垃圾不进下拉）+ 补 character/plugins/upgrade/behavior 中文映射 + 三处映射表统一。
5. **P2 BUG-03**：resource_snapshot 的 psutil.cpu_percent(interval=0.1) 或复用采样器缓存。
6. **P2 GAP-12**：正规退出链补最后一笔事件（boot/quit 路径 log_event）。
7. **P3 GAP-06**：usage 文件名改本地时区或两账统一口径。

---
*执行者：ZCode 会话（仿真人 IAB + API 直调 + 磁盘/DB 离线取证）；截图工件：
`C:\Users\Administrator\.zcode\cli\artifacts\sess_9e277be9-278e-4324-b853-013cd4ae7f6f\`（A-01 首屏、A-12 BUG-01、A-08 筛选、A-10 高视口四张关键图）。*

---

## 八、修复与复测（2026-10-19 05:0x~05:3x，用户令「先修复后测试」后执行）

### 8.1 已修复并实弹复测通过（前端侧，dist 已重建即生效）

| 编号 | 修复 | 复测证据 |
|---|---|---|
| BUG-01 | LogsPage.tsx 三元链补 `tab === 'raw'` 分支 + `: null` 收尾 | 点作品文件签：画廊独占渲染（831 个文件、四类过滤、下载按钮），原始日志卡与技术日志脚注**双双消失**（DOM 断言+截图双证） |
| BUG-02 | ErrorSummaryCard 加 15s 轮询（与统计卡同周期） | resource timing 触发时刻序列 `1081→17076→32075→47071→62073→77073→92070`，**精确 15000ms 间隔**（注意：用请求计数验证会被浏览器 250 条 resource timing 缓冲上限骗到，须看触发时刻序列） |
| GAP-01/02/03 前端半 | MODULE_NAMES 补 chat/character/plugins/upgrade/behavior/video | 旧事件徽章即时变中文（05:13 behavior 事件已显「行为」徽章截图） |

### 8.2 已修复待重启窗口生效（后端侧，运行中进程 04:54:48 启动早于 04:56 改码）

| 编号 | 修复 | 文件 |
|---|---|---|
| BUG-03 | `_prime_cpu_percent()` 进程内预烧 psutil 首调基线 | flow_trace.py |
| GAP-10 | `_module_for` 兜底白名单化——不存在端点的探测归 system，不再制造垃圾模块 | event_log_auto.py |
| GAP-11/01/02 后端半 | _PREFIX_MODULES 补 character/plugins/upgrade/training/behavior/video/chat 归一 + _MODULE_ZH 同步 | event_log_auto.py |
| GAP-06 | usage JSONL 日切 gmtime→localtime，与事件账同口径 | resource_sampler.py |

### 8.3 复测方法教训（供后续轮次）

- IAB 自动化：role-click 在本页持续超时 → `dom_cua.get_visible_dom()` 节点点击是可靠路径；
- 截图为降采样显示，**不可按显示图读坐标**（两次点空事故）；
- React 受控输入：`fill("")` 与 `cua.type` 均穿不透，`fill(文本)+press("Enter")` 是已证可靠组合；中文关键词 UI 面已由 API 层等价验证（36 条全命中）。

### 8.4 本轮挂账（未修，须单独拍板）

- **GAP-12** 退出事件无终笔：修复点在 src/main.py，但该文件现有**他人已暂存改动**（staged 1+/2-，训练 WIP），为不扫入他人成果暂不动，待其 commit 后补。
- **GAP-13**（新发现）`USAGE_KEEP_DAYS=30` 声明了 usage JSONL 30 天清理但**从未实现**（全仓无消费方）——resource_sampler docstring 承诺未兑现，文件持续累积。
- **BUG-04** 流程 0 节点 98.3% + 服务层直调泄漏：深修涉及 video.py/draw.py 冻结链（H3 主力），须专项方案拍板。
- **K-04** offset 翻页实时流漂移：设计局限（前端无翻页不受影响），稳定子集复核待后端窗口。

---

## 九、复测续批（05:3x~05:5x，修复 commit 后趁后端存活清剩余只读组）

### 9.1 新增判定

| 组 | 本批判定 | 摘要 |
|---|---|---|
| G组 | G-01/02/03/04/05/07/08/09/12 ✅；G-06/10/11 🔶 | 六文件签齐；error.log 关键词 ERROR=98 命中 593 行（±3 上下文）；backend.log WARNING 过滤 600/600 零混杂；乱串→「无命中行」；comfyui_boot.log 不存在→「暂无内容」优雅渲染；脚注/无自定义输入口✅ |
| H组 | H-01/04/06/08 ✅；H-02/03/07 🔶；H-05 ⚠️ | 标题+双折线（vram/ram path 实数据）+3 网格线+断点诚实文案全过 |
| D组 | D-02 ✅ D-09 ✅ | 技术详情展开 pre 四行结构；**追踪 ID 与 request_id 同值**（trace 链路关联实证） |
| BUG-01 复测 | ✅ 根修确认 | 画廊独占渲染 831 文件，原始日志卡消失（截图 BUG01FIX_gallery_clean_render.png） |
| BUG-02 复测 | ✅ 根修确认 | 触发时刻序列精确 15000ms 间隔（`1081→17076→…→92070`）；**教训：请求计数法会被 resource timing 250 条缓冲上限骗出假阴性，须看触发时刻** |

### 9.2 新增语义观察

- **G-10 对账口径**：页面过滤态扫描全文件（22k 行）返回上限 600 行（lines×2 封顶，logs.py:217）；磁盘 `tail -300` 只看最新 300 行——各自正确，未过滤态才是同口径（后者构造性成立）。全文件扫描在大文件+高频过滤下是 GAP-07 性能面的具体形态。
- 后端侧修复（BUG-03/GAP-10/11/06）待下次后端重启生效（运行中进程 04:54 启动早于 04:56 改码）；**已入库 4b3b51c2**。
- 复测期间另一会话并行压测中（learn 探针 ×1533、错误数 796→4106），按 §16.6 未重启未干扰。

### 9.3 累计账（截至本节）

已判 **96 条**：✅ 76 ｜ 🔶 13 ｜ ❌ 1（K-04，设计局限）｜ 其余 78 条待窗口/待拍板（E/F/I/J/L/M/N-04~14 与 BUG-04 专项）。
三笔已入库：`1c30efa3`(docs) / `661c4269`(前端修复) / `4b3b51c2`(后端修复)。
