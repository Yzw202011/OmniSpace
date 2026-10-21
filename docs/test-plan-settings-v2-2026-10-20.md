# OmniSpace 设置页严酷测试计划 v2——仿真人 E2E + 后端链路监控·战后深化版（2026-10-20）

> 被测对象：`/settings` 六签全量（通用/外观/AI 服务/数据/插件/维护）。
> **与 v1 的关系**：v1（`test-plan-settings-2026-10-19.md`，258 条）已在六轮战役+窗口批中
> 实弹 256 条、揪出并修复 11 个缺陷（BUG-01~10 + 启动页死锁），报告六节全部入库
> （`docs/test-report/2026-10-19-settings-round1.md`）。**本 v2 为战后深化版**：
> v1 已验证项转为回归基线收进各组；新增 **Q 组「跑通链」（端到端 pass-through）**
> 与 **R 组「修复回归矩阵」（11 缺陷防复发）** 两个全新维度；全表扩编至 **346 条**
> （P0×94 / P1×159 / P2×93），A~R 十八组。
>
> 纪律真源沿用 v1 §三/§四（仿真人十条纪律+熔断线）与环境/工具链（v1 §三），
> 本文档只列增量与差异。报告落 `docs/test-report/`。

---

## 一、v1 战役后的现状基线（本计划所有「基线比对」的真源，2026-10-20 实测）

| 项 | 现值/语义 |
|---|---|
| 全局设置 | theme=dali-light（**用户自切，测后勿动**）/ font_size=14 / launch_mode=browser / ui_performance=full |
| 云端连接 | 5 条真实 MagicAI（中转 GPT-6 系/国产系/生图/Grok 视频/视频生成），全部启用、外部可达 |
| 工位绑定 | 7 槽全绑定：dialog.text=国产系·glm-5.3 / manga.text=中转·gpt-5.2 / novel.text=中转·gpt-6-astra / paint.image·keyframe.image·asset.image=生图·grok-imagine-image-2.0 / manga.video=视频生成·grok-imagine-video-1.5 |
| 插件 | chat-skill-demo（用户·含源码，enabled）+ rust-coding（出厂·已审查）；**rust-coding 永远只读** |
| 备份 | data/backups/ 手动/自动分池、全 .enc（BUG-10 修复后调度器备份也已加密）；「保留 3 份」=手动按钮 |
| 审计 | ACTION_LABELS 13 种中文映射；audit_clear 自留痕语义 |
| 已修复 | BUG-01 深链接同步 / BUG-02 值白名单 / BUG-03 插件报错译净 / BUG-04 capability 落册 / BUG-05 恢复撤销 / BUG-06 导出信封 / BUG-07 db_size / BUG-08 保留文案 / BUG-09 装载异常收口 / BUG-10 备份加密统一 / 死锁根修 / 主题回写 1.2s 节流 |
| 已知边界 | 导出响应 archive=裸文件名（绝对路径=data/generated/exports/）；导入参数名 **file_path**；插件 enable=挂旗懒装载（load 才真装载）；槽位真名=paint.image/keyframe.image/asset.image；主题连点审计增量≈1（节流已修）；IAB 无文件选择能力 |

## 二、严格度/判定符号/纪律（沿用 v1 §1.2/§1.3/§四，增量三条）

- 🔁 **回归基线项**：v1 已实弹通过，本轮重跑防复发（R 组全部、各组带 🔁 标注的项）；
- 💰 烧钱项：云端真实计费，逐单拍板（Q-05/Q-06 等）；
- ⚠️窗口项：涉及重启/升级实弹/审计清空，拍板后开窗执行。

## 三、测试指标体系（KPI，v1 S1~S20 沿用 + 战后实测基线回填）

| # | 指标 | v1 目标 | **六轮实测基线（v2 通过线）** |
|---|---|---|---|
| S2 | PUT settings 保存往返 | <800ms | 实测毫秒级 → **<600ms** |
| S3/S4 | 主题/字号 DOM 生效 | <150ms | 实测即时 → **<100ms** |
| S8 | 立即备份（12MB 库） | <5s | 实测 ~60ms+加密 → **<3s** |
| S13 | 审计 200 条渲染 | <1.5s | 实测 <1s → **<1s** |
| S15/S16 | 假成功/假失败 | =0 | 维持 =0 |
| S18 | 审计留痕率 | 100% | 维持 100%（13 种动作全覆盖） |
| S19 | Key 泄漏面 | 0 | 维持 0 |
| S21（新） | 主题连点审计增量（节流回归） | — | **≤2/10 连点** |
| S22（新） | 跑通链单链耗时 | — | Q 组各链 ≤ 其环节预算之和 ×1.5 |

## 四、测试矩阵总览（346 条）

| 组 | 模块 | 条数 | P0 | P1 | P2 |
|---|---|---|---|---|---|
| A | 骨架与导航 | 12 | 3 | 5 | 4 |
| B | 通用签·全局设置 | 22 | 5 | 9 | 8 |
| C | 外观签 | 20 | 4 | 9 | 7 |
| D | 云端连接管理 | 34 | 9 | 16 | 9 |
| E | 工位绑定 | 26 | 7 | 12 | 7 |
| F | 联网搜索 | 12 | 3 | 6 | 3 |
| G | 存储盘点与清理 | 12 | 3 | 5 | 4 |
| H | 备份与恢复 | 24 | 9 | 9 | 6 |
| I | 项目导出导入 | 16 | 5 | 7 | 4 |
| J | 省钱账本 | 10 | 2 | 5 | 3 |
| K | 插件 | 26 | 7 | 12 | 7 |
| L | 硬件健康与体检 | 12 | 2 | 6 | 4 |
| M | 软件升级 | 22 | 6 | 10 | 6 |
| N | 操作审计 | 16 | 4 | 8 | 4 |
| O | 账实对账 | 22 | 6 | 10 | 6 |
| P | 漏洞攻击面 | 28 | 6 | 14 | 8 |
| Q | **跑通链·端到端（新）** | 14 | 5 | 6 | 3 |
| R | **修复回归矩阵（新）** | 18 | 8 | 10 | 0 |
| **合计** | | **346** | **94** | **159** | **93** |

---

## 五、分组用例明细

### A组 · 骨架与导航（12 条）

| ID | P | 仿真人操作步骤 | 后端/链路监控 | 预期 |
|---|---|---|---|---|
| A-01 🔁 | P0 | 打开 /#/settings | DOM | 六签序正确、默认通用、标题副题齐 |
| A-02 🔁 | P0 | 逐签点击×6 | DOM | 各签卡片齐（数据=存储+数据管理+账本；维护=硬件+升级+审计+体检） |
| A-03 🔁 | P1 | 点签看 hash | location.hash | ?tab= 写入/移除正确（通用=无参） |
| A-04 🔁 | P0 | **页内原地改 hash ?tab=xxx**（BUG-01 回归主项） | DOM | 即时切签（useEffect 同步生效） |
| A-05 🔁 | P1 | 冷加载深链接 + 跨路由进入 | DOM | 均直达目标签 |
| A-06 | P2 | 非法 tab 参数冷加载 | DOM | 回落通用不白屏 |
| A-07 | P2 | 六签随机快连 40 次 | RAM+DOM | 末态=末次点击、无重影、RAM <+20MB |
| A-08 | P1 | 后端不可达时进签（重启窗口内观察） | API 失败态 | 各卡错误态+重试钮，非白屏（⚠️窗口） |
| A-09 | P1 | 恢复后点重试/重载 | API 200 | 数据回归无整页刷新依赖 |
| A-10 | P2 | 视口 1024×700 过六签 | scrollWidth | 无横向溢出 |
| A-11 | P2 | 页签关闭再开（SPA 状态复位） | DOM | 无陈旧状态残留 |
| A-12 | P2 | 页面刷新连打 5 次 | 网络面板 | 设置 GET 不重复轰炸（有缓存/去重） |

### B组 · 通用签·全局设置（22 条）

| ID | P | 仿真人操作步骤 | 后端/链路监控 | 预期 |
|---|---|---|---|---|
| B-01 🔁 | P0 | 读全局设置表 | GET settings | 键值与 KV 一致；launch_mode/ui_performance 单选、theme/font_size 静态 |
| B-02 🔁 | P1 | 通用表无 remote_dialog_* | GET 对照 | UI 不显示（API 层仍返回=已知边界记录） |
| B-03 🔁 | P0 | 性能模式切换+保存 | PUT+audit+html class | ui-lite 挂/摘、KV 落账、审计一行 |
| B-04 🔁 | P0 | 改回完整效果 | 同上 | 还原 full |
| B-05 🔁 | P0 | launch_mode 切换+保存 | PUT | 回读一致、「下次启动生效」文案在 |
| B-06 | P1 | launch_mode 非法值直打 API | PUT bogus → 4xx | **SYSTEM_PARAM_INVALID（BUG-02 回归）** |
| B-07 | P1 | launch_mode="" 直打 | PUT | 归一为 shell（非脏值入库） |
| B-08 | P2 | ui_performance 同款非法值/空串 | PUT | 同 B-06/B-07 语义 |
| B-09 | P1 | 零改动保存 | PUT+audit | 幂等 200+审计一行 |
| B-10 🔁 | P1 | 改值不保存→点重置 | 无 PUT | 回落后端值（重置不发请求） |
| B-11 | P2 | 刷新后控件显示 | GET | 持久一致 |
| B-12 | P1 | theme 静态行与外观签联动 | DOM | 同步一致 |
| B-13 | P2 | font_size 行=14 与「中」映射 | DOM+data-font-size | 一致 |
| B-14 🔁 | P1 | 互斥规则四行断言 | DOM | 矩阵与规格 §6.1 一致 |
| B-15 🔁 | P1 | 局域网卡不可交互 | DOM | 无任何控件（下线死代码不渲染） |
| B-16 | P2 | 双标签页并发保存 | 双 PUT | last-write-wins、KV 键数守恒 |
| B-17 | P2 | 审计 target 排序≤180 | audit_log | 语义正确 |
| B-18 🔁 | P2 | 硬件信息卡有值 | /hardware | GPU/CPU/RAM/磁盘渲染 |
| B-19 | P1 | 保存时 fetch 注入失败 | monkeypatch | 错误 toast 不静默 |
| B-20 | P2 | 窄窗 label 不挤压 | DOM | 无一字一行 |
| B-21 | P1 | KV 终态与基线全等 | GET vs §一 | 测后零漂移 |
| B-22 | P2 | theme=dali-light 用户态不被测试改写 | 全程 | 测后仍=dali-light（用户自有状态红线） |

### C组 · 外观签（20 条）

| ID | P | 仿真人操作步骤 | 后端/链路监控 | 预期 |
|---|---|---|---|---|
| C-01 🔁 | P0 | 六主题轮切 DOM 契约 | html data-family/data-theme | aria-checked 迁移+属性映射正确（sakura 暗色=无属性为设计） |
| C-02 🔁 | P0 | 切主题→KV 回写 | GET settings | 落库（启动页回放真源） |
| C-03 🔁 | P0 | 清 localStorage→刷新 | 回放路径 | 主题从后端回放 |
| C-04 🔁 | P0 | 字号三档+刷新持久 | computed 13/14/16px | data-font-size+LS+ui_prefs 三写一致 |
| C-05 🔁 | P1 | 主题+字号混合 30 连点 | 每步断言 | 0 失配 |
| C-06 🔁 | P1 | 主题快连 10 次→审计增量 | audit COUNT | **≤2（节流回归，修复前≈10）** |
| C-07 | P2 | 主题快连 50 次终态 | DOM | 末次点击获胜、无属性错乱 |
| C-08 | P1 | 亮色主题过六签 | computed 抽查 | 无暗色残留岛 |
| C-09 | P1 | 主题写回失败注入 | fetch patch | LS 即时生效不阻塞、后端无脏值 |
| C-10 | P2 | 主题卡 title 提示 | DOM | 六卡文案齐 |
| C-11 | P2 | 窄窗主题网格换行 | DOM | 3 行无重叠 |
| C-12 | P1 | 切主题后全签巡检 | DOM | 全局令牌生效 |
| C-13 | P2 | confirm 弹窗主题跟随 | DOM | 按钮可辨 |
| C-14 | P1 | 终态还原 dali-light | GET+LS | 与基线一致（用户态红线） |
| C-15 | P2 | 字号浮层继承 | computed | 弹窗字号随档位 |
| C-16 | P2 | 快切后 1.2s 内关页 | KV | 末次主题不丢（去抖尾沿落库语义） |
| C-17 | P1 | data-font-size 属性序列 | DOM | sm/md/lg 精确迁移 |
| C-18 | P2 | 主题切换 RAM 稳定性 | psutil | 50 次无监听器泄漏 |
| C-19 | P1 | 回写失败后无孤儿定时器 | DOM | 去抖清理由 clearTimeout 保证 |
| C-20 | P2 | 亮暗四态对比度抽查 | computed | 正文可读 |

### D组 · 云端连接管理（34 条）

> 红线：真实 MagicAI 五件套只读+测试；增删改全在 TESTAUTO- 替身上。

| ID | P | 仿真人操作步骤 | 后端/链路监控 | 预期 |
|---|---|---|---|---|
| D-01 🔁 | P0 | 读列表五连接 | GET providers+bindings | 名称/类型/打码 Key/已启用/使用中与绑定对账 |
| D-02 🔁 | P0 | Key 打码 | 持久化 vs UI | 全打码无明文（sk-***后 3~4 位） |
| D-03 🔁 | P0 | 添加表单开合 | DOM | 四字段+协议三选+password 型 |
| D-04 🔁 | P0 | 空表单保存 | 无 POST | toast 拦截 |
| D-05 🔁 | P0 | 建 TESTAUTO-文本（UI 全链） | POST | 列表新增+打码+审计 |
| D-06 🔁 | P0 | 假 Key 测试连接 | POST test | 「不可达：/v1/models 返回 HTTP 401」明示 |
| D-07 🔁 | P0 | 编辑 Key 留空保存 | PUT | **Key 保持原值**（回读打码不变） |
| D-08 🔁 | P1 | 改名保存（500ms 节律） | PUT | 「连接已更新」 |
| D-09 🔁 | P0 | 停用 | PUT enabled=false | 已停用+测试/拉取钮禁用 |
| D-10 🔁 | P0 | 启用还原 | PUT | 恢复绿 |
| D-11 🔁 | P0 | 绑定后删除→级联解绑 | DELETE+bindings | 槽自动回「本地引擎（默认）」 |
| D-12 | P1 | 删除取消 | 无 DELETE | 保留 |
| D-13 | P1 | 删除审计 | audit_log | provider_delete 落账 |
| D-14 🔁 | P1 | 拉取模型列表（真实连接只读） | GET models | GPT-6 系 17+剔 5 / 生图 34（外部恢复后基线） |
| D-15 | P1 | 拉取失败（坏地址替身） | GET models | 连接失败明示 |
| D-16 | P1 | 协议切换三套 placeholder | DOM | 文本/图片/DashScope/视频四态文案 |
| D-17 🔁 | P1 | 非文本模型警示+放行 | SUSPICIOUS_RE | 首存警示不发 POST、再存放行+类别过滤 |
| D-18 | P2 | warnedRef 会话内一次 | DOM | 同表单二次保存不再警示 |
| D-19 | P2 | 真实连接测试连接 | POST test | 可达（MagicAI 恢复后基线 ✅） |
| D-20 | P2 | 真实连接模型清单渲染 | DOM | 与截图基线一致无跨类混入 |
| D-21 | P1 | 使用中标注对账 | bindings 反查 | 七槽全对 |
| D-22 | P1 | 编辑取消无副作用 | 无 PUT | 表单关闭 |
| D-23 | P2 | 超长名 300 字 | POST | 接受且布局不炸 |
| D-24 | P1 | XSS 名纯文本渲染 | DOM+window 探针 | 0 注入 0 执行 |
| D-25 | P2 | 重名三条并存 | POST×3 | 不静默合并 |
| D-26 | P1 | 表单取消重开空白 | DOM | EMPTY_FORM 复位 |
| D-27 | P1 | 保存中防重入 | saving 态 | 双击只发一次 |
| D-28 | P2 | 中文/空格/换行 Key | POST | 掩码规则稳定 |
| D-29 | P1 | 快点击竞态观察（300ms 填后即存） | 网络 | 记录行为（v1 观察项，人类节律 500ms+ 无问题） |
| D-30 | P1 | 切签回来重拉列表 | GET | 无陈旧缓存 |
| D-31 | P2 | billing 端点优雅降级 | GET billing | MagicAI 404→不炸卡（GAP-05） |
| D-32 | P1 | 替身清理归零 | DELETE×n | providers 回基线 5 条 |
| D-33 | P2 | 协议徽章三色 | DOM | 文本/图片/视频标注正确 |
| D-34 | P0 | 全程真实连接零改动 | GET providers 快照 | 测后与基线逐字段相等 |

### E组 · 工位绑定（26 条）

| ID | P | 仿真人操作步骤 | 后端/链路监控 | 预期 |
|---|---|---|---|---|
| E-01 🔁 | P0 | 读七槽绑定 | GET bindings | 槽名/当前值与 §一基线全等 |
| E-02 🔁 | P0 | 协议过滤 | 下拉选项 | 文本槽只文本连接、图片槽只图片、视频槽只视频 |
| E-03 | P1 | 停用连接不进下拉 | DOM | enabled 过滤 |
| E-04 | P0 | 无模型连接占位行 | DOM | 「连接名 · 默认模型」 |
| E-05 🔁 | P0 | 替身绑写作台 | PUT novel.text | 「已切换为云端」+回读编码一致 |
| E-06 🔁 | P1 | 解绑回本地 | DELETE binding | 「已恢复本地引擎」 |
| E-07 | P1 | 使用中标注即时更新 | 列表重载 | 同步 |
| E-08 | P0 | 绑定后 /cloud/status 对账 | GET status | 文本承载=绑定值 |
| E-09 | P1 | 槽位 hint 文案 | DOM | 四条专属+兜底齐 |
| E-10 | P2 | 下拉 label 拼接 | DOM | 无 undefined/空尾点 |
| E-11 🔁 | P1 | 换绑不残留 | PUT×2 | 单值替换 |
| E-12 🔁 | P0 | 删连接级联清绑定 | DELETE+GET | 悬空清零 |
| E-13 | P1 | 并发换绑同槽 | 双 PUT×10 轮 | last-write-wins 无脏态 |
| E-14 | P2 | 伪槽直打 | PUT bogus | CLOUD_SLOT_UNKNOWN |
| E-15 | P2 | 协议不匹配直打 | PUT | CLOUD_PROTOCOL_MISMATCH |
| E-16 | P1 | 管道模型名编码安全 | 绑 a\|b | 回读切分正确 |
| E-17 🔁 | P0 | 七槽逐一切换还原 | PUT/DELETE×14 | 终态=基线零差异 |
| E-18 | P1 | 空态下拉（全停用替身） | DOM | 只有「本地引擎（默认）」 |
| E-19 | P2 | label 含特殊字符模型名 | DOM | 渲染不串档 |
| E-20 | P1 | manga.video 绑定跨页展示 | 漫剧模型设置只读 | 展示云端来源（**人工/窗口项**，自动化两次险情叫停） |
| E-21 | P1 | 绑定操作留痕 | audit | 有相应动作或记缺口 |
| E-22 | P2 | 绑定区窄窗布局 | DOM | 无挤压 |
| E-23 | P1 | 双开页绑定并发 | 双 PUT | 一致终态 |
| E-24 | P2 | 下拉打开时列表后台刷新 | DOM | 无选项跳动错选 |
| E-25 | P1 | 快速连切绑定 | PUT 连发 | 终值=末次、审计不洪泛 |
| E-26 | P0 | 全程真实绑定零漂移 | GET vs §一 | 测后零差异（强还原纪律） |

### F组 · 联网搜索（12 条）

| ID | P | 仿真人操作步骤 | 后端/链路监控 | 预期 |
|---|---|---|---|---|
| F-01 🔁 | P0 | 读卡当前值 | GET web_search | UI=KV（基线 enabled=true/browser/auto） |
| F-02 🔁 | P0 | 关→保存→开→保存 | PUT×2 | KV 跟随、toast 正确 |
| F-03 | P1 | 通道三选切换 | PUT | 各档保存回读一致 |
| F-04 | P1 | 触发三档切换 | PUT | 同上 |
| F-05 | P2 | 草稿切签即弃 | GET | 不误存 |
| F-06 | P2 | 隐私声明文案 | DOM | 「只上传查询词不上传聊天历史」在卡 |
| F-07 | P2 | 脏 provider 直打归一 | PUT bogus | 拒绝或归一 |
| F-08 | P1 | bocha_key 不出网明文 | GET 响应 | 空串/打码（基线空） |
| F-09 | P1 | 读失败错误态+重试 | 断链注入 | 不永久 loading |
| F-10 | P2 | SearXNG URL 填写联动 | DOM | 出现地址输入 |
| F-11 | ⚠️窗口 | 对话页搜索实弹 | 对话链 | 引用标注（属对话域，挂账项转正需窗口） |
| F-12 | P1 | 终态还原基线 | GET | enabled=true/browser/auto |

### G组 · 存储盘点与清理（12 条）

| ID | P | 仿真人操作步骤 | 后端/链路监控 | 预期 |
|---|---|---|---|---|
| G-01 🔁 | P0 | 读盘点 | inventory vs du | **字节级一致**（v1 实测 220522587 恒等） |
| G-02 | P1 | 白名单断言 | cleanable 字段 | 生成图片/关键帧/资产不可清理 |
| G-03 🔁 | P0 | 清 temp（零风险目标） | POST cleanup | 「删 0 项，释放 0MB」诚实 |
| G-04 🔁 | P1 | 清理确认门 | DOM | 取消/开始清理双钮 |
| G-05 | P2 | 防重入 | busy 态 | 双击一次 |
| G-06 | P2 | days 参数语义 | API 直打 | 只清旧件 |
| G-07 | P1 | 有内容目标联动盘点刷新 | 前后 inventory | 字节下降反映 |
| G-08 | P2 | 大目录清理耗时展示 | API 计时 | 有反馈不假死 |
| G-09 | P2 | 清理审计留痕 | audit | 有或记缺口 |
| G-10 | P1 | 盘点失败错误态 | 断链注入 | 错误+重试 |
| G-11 | P2 | 只保留 3 份备份按钮 | POST+目录 | 手动触发按池清理（GAP-04 语义回归） |
| G-12 | P2 | 盘点数字格式化 | DOM | 无 NaN/undefined |

### H组 · 备份与恢复（24 条）

> ⚠️窗口：H-13~H-18 涉及重启。**恢复链已在五轮窗口批全线贯通（双跳回滚实锤），本轮为防复发回归+边界深化。**

| ID | P | 仿真人操作步骤 | 后端/链路监控 | 预期 |
|---|---|---|---|---|
| H-01 🔁 | P0 | 立即备份 | POST backup | toast **MB 级**（db_size_bytes）+双件 .enc |
| H-02 🔁 | P0 | 调度器备份加密（BUG-10 回归） | _perform_backup 直调 | 双件 .enc |
| H-03 | P1 | 备份耗时 | API 计时 | <3s |
| H-04 🔁 | P1 | 恢复列表 | GET list | 分池可辨、前端截 5 条 |
| H-05 🔁 | P1 | 保留语义 | 目录 vs 文案 | 「自动清理未启用」文案+手动按钮按池清理 |
| H-06 | P2 | 连备 4 份不自动清 | 目录 | 行为一致（手动制） |
| H-07 🔁 | P0 | 恢复确认弹窗 | DOM | 三行说明+确认门 |
| H-08 🔁 | P0 | 安排恢复 | POST+目录 | pending=True+待恢复徽标 |
| H-09 🔁 | P0 | **撤销安排（BUG-05 回归）** | cancel 端点+UI 钮 | pending=False+徽标消失+幂等拒 |
| H-10 🔁 | P0 | ⚠️窗口：重启生效 | 启动日志+DB | 标记消费、防呆备份 pre_restore_*.enc |
| H-11 🔁 | P0 | ⚠️窗口：数据回退 | 标记行断言 | 回退到备份时点 |
| H-12 🔁 | P0 | ⚠️窗口：双跳回滚 | 恢复防呆件→重启 | 今日数据全量回归 |
| H-13 🔁 | P1 | 双安排后胜 | POST×2 | staged=后者 |
| H-14 🔁 | P2 | 恢复文件名穿越 | POST×4 变体 | 全拒 SYSTEM_PARAM_INVALID |
| H-15 | P2 | 删备份+穿越 | POST delete | 正删+穿越拒 |
| H-16 | P2 | busy 防重入 | DOM | 双击一次 |
| H-17 | P1 | .enc 恢复源解密 | ⚠️窗口 | _stage 解密消费（五轮已验） |
| H-18 | P2 | 小文件损坏备份拒恢复 | POST | <4096 拒 |
| H-19 | P1 | 手动/自动分池滚动（手动按钮） | 目录 | 各池各留 N |
| H-20 | P2 | 备份期间 WS 推送 | 前端 | 无卡顿假死 |
| H-21 | P2 | 恢复安排后 UI 全签可继续用 | DOM | 当前会话不受影响语义 |
| H-22 | P1 | pending 徽标跨签持久 | DOM | 回签仍在 |
| H-23 | P2 | 备份命名时间戳唯一 | 目录 | 同秒不覆盖 |
| H-24 | P1 | 终态目录=基线+本轮新增说明 | ls | 测试产物全清、安全网备份保留说明 |

### I组 · 项目导出导入（16 条）

> 参数名=file_path；导出响应 archive=裸文件名（绝对路径=data/generated/exports/）。

| ID | P | 仿真人操作步骤 | 后端/链路监控 | 预期 |
|---|---|---|---|---|
| I-01 🔁 | P0 | 导出 comic 项目（BUG-06 回归） | POST export | 成功+归档五件套（manifest/project/storyboard/director/assets） |
| I-02 🔁 | P0 | 导出 manga 项目 | 同上 | 成功（双类型回归） |
| I-03 🔁 | P0 | 导入回环 | POST import(file_path) | 新 ID+行数一致 |
| I-04 | P1 | 二次导入独立 ID | POST | 不撞号（_fresh_id） |
| I-05 | P1 | 导入确认弹窗取消 | 无 POST | 无副作用 |
| I-06 | P0 | 不存在项目导出 | POST | SYSTEM_RESOURCE_NOT_FOUND 干净 |
| I-07 | P2 | 空 ID/空路径前端拦截 | 无 POST | toast |
| I-08 | P2 | 坏路径/伪装后缀 | POST | 干净 4xx 类 |
| I-09 | P1 | 导出耗时 | 计时 | <30s |
| I-10 | P1 | 导入耗时 | 计时 | <30s |
| I-11 | P1 | 导入后项目完整性 | DB 对照 | 分镜/资产/关键帧计数=manifest |
| I-12 | P1 | 并发导入 ×3 | 同时 POST | 全成功 ID 全独立（v1 已验回归） |
| I-13 | P2 | 伪归档三连 | POST | PROJECT_FILE_CORRUPTED 精确原因 |
| I-14 | P1 | 导入后删除清理 | DELETE project | 级联净、TEST6grid 等原项目零损伤 |
| I-15 | P2 | 同名项目并存 | 导入×2 | 独立不覆盖 |
| I-16 | P0 | 原项目只读红线 | 快照对照 | 全程源项目零改动 |

### J组 · 省钱账本（10 条）

| ID | P | 仿真人操作步骤 | 后端/链路监控 | 预期 |
|---|---|---|---|---|
| J-01 🔁 | P0 | 读账本五格 | API+SQL 直查 | 全对（六轮基线口径） |
| J-02 🔁 | P1 | 金额重算 | 手算 | 与 cny_est 两位小数吻合 |
| J-03 | P2 | 万位缩写 | DOM | ≥1 万显 x.x 万 |
| J-04 🔁 | P1 | scope_note 原样展示 | DOM | 口径诚实 |
| J-05 | P2 | cloud:: 排除 | 造行→刷新 | 不计入（测后清理） |
| J-06 | P2 | 三分项和恒等 | DOM | kf+ca+ph=总数 |
| J-07 | P2 | 空库懒建表容错 | 桩/单测 | 全 0 不炸 |
| J-08 | P1 | 后端不可达错误态 | DOM | 「暂不可用」提示 |
| J-09 | P2 | 本地生成→数字增长环 | 造 1 条本地 assistant 行 | 五格联动+1（测后清理） |
| J-10 | P2 | 参考价展示 | DOM | 三价与 prices 一致 |

### K组 · 插件（26 条）

| ID | P | 仿真人操作步骤 | 后端/链路监控 | 预期 |
|---|---|---|---|---|
| K-01 🔁 | P0 | 读清单 | GET /plugins | 双原生渲染+徽章+按钮 |
| K-02 🔁 | P0 | 橙徽章渲染（GAP-10 回归） | computed color | user_source=橙 warning |
| K-03 🔁 | P0 | chat-skill-demo 停用/启用 | POST×2 | 状态翻转+技能清单联动 |
| K-04 🔁 | P0 | rust-coding 无删除钮 | DOM | 出厂防删 |
| K-05 🔁 | P0 | 含源码确认门 API | POST 无 confirm | PLUGIN_SOURCE_CONFIRM_REQUIRED |
| K-06 🔁 | P0 | 确认导入 | confirm=true | user_source 落册 |
| K-07 🔁 | P0 | **capability 落册（BUG-04 回归）** | GET list | 「最简示例：回显输入」在清单 |
| K-08 🔁 | P0 | 重名拒 | POST×2 | PLUGIN_ALREADY_REGISTERED |
| K-09 🔁 | P0 | 垃圾字节拒 | POST | PLUGIN_PACKAGE_INVALID（魔数） |
| K-10 🔁 | P0 | 穿越条目净拒（BUG-03 回归） | POST | PLUGIN_PACKAGE_INVALID「包条目不合规」无内部错误 |
| K-11 🔁 | P0 | 装载异常收口（BUG-09 回归） | load 雷插件 | PLUGIN_LOAD_FAILED+last_error 落册+UI「上次错误」 |
| K-12 | P1 | UI 删除 heavy 确认 | DOM | 取消/删除双门 |
| K-13 | P1 | 删除后三方净 | 目录+registry+list | 全净 |
| K-14 | P2 | 启停 10 轮 | 快照 | 终态还原 |
| K-15 | P2 | 重名覆盖语义 | POST | 行为记录 |
| K-16 | P2 | 64MB 上限 | POST | PLUGIN_PACKAGE_INVALID |
| K-17 | P1 | .py 源码分离上传旧流程 | POST source | 兼容路径可用 |
| K-18 | P2 | 源码安检禁用清单 | 造违规源 | PLUGIN_SOURCE_FORBIDDEN+清单 |
| K-19 | P2 | 非_UTF8 源码 | POST | PLUGIN_SOURCE_INVALID |
| K-20 | P2 | skills 非法形状拒 | manifest | validate_skills 拒 |
| K-21 | P1 | 用户插件上限 MAX_USER_PLUGINS | 灌满 | PLUGIN_LIMIT_REACHED+建议文案 |
| K-22 | P2 | 故障态 invoke 超时路径 | 桩 | state=faulty 红字（invoke 专用语义） |
| K-23 | P1 | 加载失败可重试 | unload→load | 复位重建 |
| K-24 | P2 | 无能力描述占位 | DOM | （无能力描述） |
| K-25 | P1 | 卡头三徽章说明 | DOM | 蓝/绿/橙警示齐 |
| K-26 | P0 | 终态=双原生 | GET | 测后清单零残留 |

### L组 · 硬件健康与体检（12 条）

| ID | P | 仿真人操作步骤 | 后端/链路监控 | 预期 |
|---|---|---|---|---|
| L-01 🔁 | P0 | 四仪表+五道保护线 | GET health | 渲染齐、等级色匹配 |
| L-02 🔁 | P1 | 温度不可测诚实降级 | DOM | 不显假数 |
| L-03 🔁 | P1 | 体检八探针 | GET health-check | 逐项 key/level/friendly |
| L-04 | P2 | 探针级别断言逻辑 | 样本 | ok/warn/fail 分流正确 |
| L-05 | P2 | 修复动作空闲执行 | POST repair | 成功或「无可修复」 |
| L-06 | P2 | 诊断导出+审计 | POST diagnose | 产物+diagnostics_export 留痕 |
| L-07 | P1 | 高负载期加载 | 生成中打开 | 降级文案非白屏 |
| L-08 | P2 | 体检防重入 | busy | 双击一次 |
| L-09 | P2 | 模块健康恢复钮 | POST recover | 空闲成功/无需恢复 |
| L-10 | P1 | pynvml 双读对照 | 直读 vs 卡 | 无假数（py313 事故回归） |
| L-11 | P2 | RAM 告警色阈值 | 观察 | 阈值对照 config |
| L-12 | P2 | 四仪表窄窗布局 | DOM | 不重叠 |

### M组 · 软件升级（22 条）

| ID | P | 仿真人操作步骤 | 后端/链路监控 | 预期 |
|---|---|---|---|---|
| M-01 🔁 | P0 | 读升级卡 | GET status/packages | v2.5.0（开发版）+空目录文案 |
| M-02 🔁 | P0 | 伪包导入 | POST | signature_ok=false+「不是有效的升级包」+UI 签名无效徽章 |
| M-03 | P1 | .exe 伪装 .upg | POST | 收件标无效（设计语义）+start 三道预检兜底 |
| M-04 | P2 | 非 .upg 前端拦截 | DOM | toast 不发请求 |
| M-05 | P2 | >2048MB 拦截 | DOM | toast |
| M-06 🔁 | P1 | start 无确认 | POST | 「缺少升级确认 confirm=UPGRADE」 |
| M-07 🔁 | P1 | 删不存在的包 | DELETE | SYSTEM_RESOURCE_NOT_FOUND |
| M-08 | P1 | 删包目录卫生 | DELETE+ls | updates/ 净 |
| M-09 | P2 | 拖拽视觉反馈 | 合成 dragover | 虚线 outline+文案切换+dragleave 复位 |
| M-10 | P2 | 导入防重入 | importing 态 | 双击一次 |
| M-11 | P1 | 包条目展开详情 | DOM | 大小/日期/说明（有包时） |
| M-12 | P2 | 打开 updates 文件夹 | POST | 资源管理器弹（⚠️扰民，窗口批） |
| M-13 | P0 | ⚠️窗口：合法签包全链 | M-16 | 阶段文案流转→自动 reload→新版本号 |
| M-14 | P0 | ⚠️窗口：回滚链 | M-17 | rolled_back 文案+旧版可启动 |
| M-15 | P2 | 10 分钟超时兜底 | 计时桩 | 重开指引（U3） |
| M-16 | P1 | 5890 updater 伺服阶段 | Bug1 路径 | 后端下线读阶段 |
| M-17 | P2 | updates/state.json 预存件 | ls | 记录不误报 |
| M-18 | P1 | 版本不兼容徽章 | 造跨版包 | 黄「版本不兼容」+原因 |
| M-19 | P2 | 签名无效不可升级态 | DOM | 升级钮禁用 |
| M-20 | P2 | basename 强制（包名穿越） | DELETE ..%2F | 无目录逃逸 |
| M-21 | P2 | 导入响应字段完整 | POST | manifest.to_version/reason 齐 |
| M-22 | P1 | 升级卡与 updates/ 目录对账 | ls vs GET | 零差异 |

### N组 · 操作审计（16 条）

| ID | P | 仿真人操作步骤 | 后端/链路监控 | 预期 |
|---|---|---|---|---|
| N-01 🔁 | P0 | 读审计卡 | GET list?limit=200 | 条数+中文徽章+滚动 260px |
| N-02 🔁 | P1 | 13 种动作映射 | DOM | 含新增 quota_update/restore_staged/restore_cancel |
| N-03 🔁 | P0 | 刷新联动 | 造行→刷新 | 新行置顶 |
| N-04 | P2 | 失败结果红标 | result!=ok 行 | 红徽章（无样本则桩） |
| N-05 | P2 | hover title 兜底 | DOM | detail 可见 |
| N-06 🔁 | P0 | limit 边界 | 1/1001 | 1 行/拒 SYSTEM_PARAM_INVALID |
| N-07 | P2 | module 筛选 API | GET ?module= | 生效（前端未暴露=GAP-09 维持） |
| N-08 🔁 | ⚠️窗口 | 清空实弹 | POST clear | N→1 自留痕 |
| N-09 | P1 | 清空后新操作重新留痕 | 造行 | 闭环 |
| N-10 | P2 | 未知动作显原文 | DOM | 不空白 |
| N-11 | P1 | 五类敏感操作联动 | 各造一次 | 全留痕（S18=100%） |
| N-12 | P2 | 200 条滚动性能 | DOM | 无卡顿 |
| N-13 | P1 | settings_update target 语义 | audit | 键排序+截断 180 |
| N-14 | P2 | 审计表懒建容错 | 新库单测 | 无表自建 |
| N-15 | P2 | 审计与 KV 写失败共存 | 注入 | 备份失败不留痕假象排查 |
| N-16 | P1 | 审计时间序单调 | SQL | ts 非严格递增可接受但 id 单调 |

### O组 · 账实对账（22 条）

| ID | P | 对账项 | 取证 | 预期 |
|---|---|---|---|---|
| O-01 🔁 | P0 | 设置 KV vs UI | GET vs 表 | 逐键一致 |
| O-02 🔁 | P0 | 主题三真源 | LS+KV+html | 同值 |
| O-03 🔁 | P0 | 字号双真源 | LS+ui_prefs | 同值 |
| O-04 🔁 | P0 | 连接持久化 vs UI | 文件 vs 列表 | 全等 |
| O-05 🔁 | P0 | 绑定 vs 下拉 | API vs select | 七槽全等 |
| O-06 | P1 | 打码规则统一 | 多长度 Key | 前3后4 稳定 |
| O-07 🔁 | P1 | 备份目录 vs 列表 | ls vs API | 含 .enc 全等 |
| O-08 | P1 | pending 生命周期 | arm/cancel/apply | 三态正确 |
| O-09 🔁 | P1 | 插件注册表 vs 清单 | JSON vs API | 全等 |
| O-10 🔁 | P1 | updates/ vs 包列表 | ls vs API | 零差异 |
| O-11 🔁 | P1 | audit 表 vs 卡 | COUNT+行 | 一致 |
| O-12 🔁 | P1 | web_search KV vs 控件 | API vs DOM | 全等 |
| O-13 🔁 | P1 | 账本 SQL 复算 | 四段 SQL | 五格全对 |
| O-14 | P2 | 硬件双读 | pynvml vs 卡 | 量级一致 |
| O-15 | P2 | 盘点 vs du | 抽两目录 | ±20% |
| O-16 🔁 | P1 | 日志零未捕获堆栈（非攻击项） | log 扫描 | 0 |
| O-17 | P2 | 审计 ts/id 单调 | SQL | id 单调 |
| O-18 🔁 | P1 | 假成功/假失败总账 | 汇总 | =0 |
| O-19 | P2 | 文案-行为一致性抽查 | 保留 3 份/下次启动生效/自动清理未启用 | 全部如实 |
| O-20 | P1 | 主题回写节流账务 | 连点 vs audit | 增量≤2 |
| O-21 | P2 | pre_restore 防呆件存在性 | 恢复后目录 | .enc 在 |
| O-22 | P0 | 六轮战渍全清 | 全域快照 | TESTAUTO-*/测试插件/测试备份/测试归档零残留 |

### P组 · 漏洞攻击面（28 条）

| ID | P | 攻击面 | 步骤 | 预期（加粗=红线） |
|---|---|---|---|---|
| P-01 🔁 | P0 | 恢复文件名穿越 | 6 变体 | **全拒**（对照组禁用真名——五轮险情教训入纪律） |
| P-02 🔁 | P0 | 导入路径穿越/UNC | 3 变体 | **拒** |
| P-03 🔁 | P0 | project_id SQL 注入 | 2 向量 | 参数化实锤+表完好 |
| P-04 🔁 | P1 | 连接名 XSS | img/onerror | 纯文本渲染 |
| P-05 | P1 | base_url SSRF 探面 | file://+169.254+回环 | file/元数据失败；回环可达=GAP-15 记录 |
| P-06 🔁 | P0 | Key 泄漏四查 | 列表/回显/日志/审计 grep | **0 泄漏** |
| P-07 | P1 | 超长输入轰炸 | 5000 字/1 万行 | 不崩<10s |
| P-08 | P1 | 空值纯空格 | 提交 | trim 拦截 |
| P-09 | P2 | 重复名语义 | ×3 | 记录 |
| P-10 🔁 | P0 | 并发 PUT settings ×10 | 双线程 | 无 KV 损坏 |
| P-11 | P1 | 恢复与备份竞态 | 0.5s 间隔 | 互不破坏 |
| P-12 🔁 | P0 | 升级包名穿越 | DELETE ..%2F | **basename 强制** |
| P-13 🔁 | P0 | 升级 zip 穿越条目 | 造包 | **白名单拦截** |
| P-14 | P1 | 任务忙强发升级 | 生成中 start | 任务忙拒绝 |
| P-15 🔁 | P1 | 插件名路径注入 | DELETE ..%2F | **拒** |
| P-16 🔁 | P1 | 确认门后端闸 | 首发带 confirm | 门在后端非前端 |
| P-17 | P2 | audit 洪泛上限 | 1000+ 行 | limit 生效 |
| P-18 | P2 | web_search 脏值 | PUT 异常 | 归一 |
| P-19 | P2 | ui_prefs 任意键 | PUT | 行为记录不污染主表 |
| P-20 | P1 | 悬空绑定外部损坏 | 改持久化+重启 | 不崩/自清（⚠️窗口） |
| P-21 | P2 | 备份目录只读 | ACL 注入 | 诚实报错（⚠️窗口，高风险） |
| P-22 🔁 | P2 | 伪 .omnispace 三连 | 空zip/缺 manifest/坏 JSON | PROJECT_FILE_CORRUPTED |
| P-23 | P1 | 归档并发导入 ×3 | 同时 | ≤3 项目零撞号 |
| P-24 | P1 | 全接口未鉴权面 | 无 token 全打 | 回环定位记录+LAN 闸在位 |
| P-25 | P2 | 绑定值超长 model 串 | PUT 10KB model | 不崩 |
| P-26 | P2 | 恢复文件名大小写/尾点变体 | POST | 拒或精确匹配 |
| P-27 | P1 | settings PUT 超大 JSON | 1MB body | 413/4xx 类不崩 |
| P-28 | P1 | 插件包 symlink 条目 | 造链接条目 | 拒或安全解引用 |

### Q组 · 跑通链·端到端（14 条，新维度）

> 每条=一条完整业务链路的「从设置页出发直到业务效果可见」的贯通验证。

| ID | P | 链路 | 步骤 | 监控 | 预期 |
|---|---|---|---|---|---|
| Q-01 | P0 | 设置保存→重启→回放全链 | 改 perf→保存→重启→进签 | KV+DOM | 重启后设置原样回放 |
| Q-02 | P0 | 主题三真源贯通 | 切主题→LS+KV+html+重启 | 四点断言 | 启动页/主界面同肤 |
| Q-03 | P0 | 对话工位云链 | 绑定对话槽→/cloud/status→对话页横幅只读 | status+DOM | 云端承载展示正确 |
| Q-04 | P1 | 写作台云链（廉价实弹💰~0.01 元） | 绑 novel.text→写作台最短生成→还原 | 生成结果+model_used | 云端模型署名 |
| Q-05 | 💰P1 | 绘画云链 | 绑 paint.image→绘画页最短出图→还原 | paint_history.model_used | cloud 署名（拍板后跑） |
| Q-06 | 💰P1 | 漫剧文字云链 | 绑 manga.text→AI 切分最短→还原 | storyboard 生成链 | 云端承载 |
| Q-07 | P0 | 备份→恢复→回退→还原全环 | H-10~12 压缩版 | 标记行三态 | 数据三态全部正确 |
| Q-08 | P1 | 导出→导入→删除→再导入幂等环 | 全 API 环 | ID/计数 | 环闭无渍 |
| Q-09 | P1 | 插件全生命周期环 | 导入→启用→load→invoke→停用→删除 | 各端点 | 全链通 |
| Q-10 | P1 | 升级目录卫生环 | 导入→徽章→删除→目录净 | ls | 环闭 |
| Q-11 | P1 | 审计五类→清空→自留痕环 | 造五类→清空 | COUNT | 环闭 |
| Q-12 | P2 | 账本增长环 | 本地生成→账本+→金额重算 | SQL | 联动正确 |
| Q-13 | P2 | 存储清理→盘点联动环 | 清 temp→盘点刷新 | inventory | 字节反映 |
| Q-14 | P0 | 断链→自愈环 | 重启窗口→错误态→恢复→重载 | WS+API | 全链自愈（A-08/09 集成） |

### R组 · 修复回归矩阵（18 条，新维度）

> 每条=一个已修缺陷的**原始复现步骤**，现在必须呈现「已修复」行为。全组为 🔁 防复发。

| ID | P | 缺陷 | 原始复现步骤（现为回归断言） | 预期（已修复行为） |
|---|---|---|---|---|
| R-01 | P0 | BUG-01 深链接 | 页内改 ?tab= | 即时切签 |
| R-02 | P0 | BUG-02 值校验 | PUT launch_mode=bogus | SYSTEM_PARAM_INVALID |
| R-03 | P1 | BUG-03 插件报错 | 穿越包导入 | PLUGIN_PACKAGE_INVALID 净拒 |
| R-04 | P1 | BUG-04 capability | 导入含能力插件→读清单 | capability 在册 |
| R-05 | P0 | BUG-05 恢复撤销 | arm→cancel | pending=False+幂等拒 |
| R-06 | P0 | BUG-06 导出信封 | 双类型导出 | 成功+五件套 |
| R-07 | P1 | BUG-07 备份体积 | 立即备份看 toast | MB 级非 0KB |
| R-08 | P2 | BUG-08 保留文案 | 读数据管理卡 | 「自动清理未启用」 |
| R-09 | P1 | BUG-09 装载收口 | 雷插件 load | PLUGIN_LOAD_FAILED+last_error |
| R-10 | P0 | BUG-10 备份加密 | 调度器直调+防呆件 | 全 .enc |
| R-11 | P0 | 死锁 | 孤儿 splash+死后端场景 | boot 接管拉起 |
| R-12 | P1 | 主题审计洪泛 | 10 连点 | 审计增量≤2 |
| R-13 | P1 | XSS 渲染 | onerror 名连接 | 0 注入 |
| R-14 | P1 | 审计标签 | restore_staged 行 | 「安排恢复」中文 |
| R-15 | P1 | 深链接三态 | 原地/冷载/跨路由 | 全部正确 |
| R-16 | P1 | 导出双类型 | comic+manga | 双成功 |
| R-17 | P1 | 级联解绑 | 删绑定连接 | 槽回本地 |
| R-18 | P0 | 状态终局还原 | 全域快照 | 与 §一基线零差异 |

---

## 六、稳定性规程（v1 §八沿用+增量）

- P0 全部 3 遍复跑；失败必现/偶现/单次三档标注；
- **新增**：主题节拍 100 次循环（含去抖尾沿关页场景 C-16）；恢复链双跳连跑 2 遍（窗口批）；
- 挂机 30min 六签轮询（RAM 平稳、无 WS 误连）；
- 熔断线不变（RAM<2GB / GPU≥90°C×2min / /health 3 连超时 / 并发在飞）。

## 七、执行顺序

1. **第 1 轮·回归优先**：R 组全量（18）→ 各组 🔁 项 → 无窗口新项（D/E 替身流、F/G/J/K/L/N 常规）；
2. **第 2 轮·深化**：I/H 非窗口边界 + O 对账 + P 攻击（除窗口项）；
3. **第 3 轮·窗口批**（拍板）：H-10~12、M-13/14、N-08、A-08、E-20、F-11、P-20/21；
4. **第 4 轮·烧钱批**（逐单拍板）：Q-04/Q-05/Q-06；
5. **第 5 轮·稳定性**：§六全部；
6. 报告落 `docs/test-report/2026-10-20-settings-v2-round1.md`。

## 八、附录

### 8.1 取证命令模板（v1 §10.1 沿用 + 增补）
```bash
# 导入必须用 file_path（v1 笔误教训）
python -c "import requests;print(requests.post('http://127.0.0.1:5800/api/v1/system/project/import',json={'file_path':r'E:\x.omnispace'},timeout=60).json())"
# 导出响应 archive 是裸文件名，绝对路径在 data/generated/exports/
# 调度器备份直调（BUG-10 回归）
runtime/py312/python.exe -c "import sys;sys.path.insert(0,'.');from src.api.system import _perform_backup;print(_perform_backup())"
```

### 8.2 自动化坑位清单（六轮沉淀，仿真人执行必读）
1. IAB role-click 全超时 → evaluate 兜底点击（tab/单选/卡片通杀）；
2. HashRouter 参数在 location.hash 非 search；
3. 项目卡片**第一个子按钮=删除作品**——严禁 `querySelector('button')` 盲选卡片（六轮险情教训）；
4. 插件 enable=挂旗，load 才真装载（故障态要打 /load）；
5. 造故障插件把雷埋 on_load(ctx)（签名带参）；
6. 恢复对照组禁用真实备份名（五轮拆弹险情）；
7. MagicAI 外部 10054 抖动 → 失败先定性外部再记缺陷；
8. 备份加密判定看 .enc 后缀；恢复链 _stage 按 .enc 分流自动解密。
