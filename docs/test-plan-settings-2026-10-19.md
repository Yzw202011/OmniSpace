# OmniSpace 设置页严酷测试计划——仿真人 E2E + 后端链路监控（2026-10-19）

> 被测对象：`/settings` 设置页全部六签（通用 / 外观 / AI 服务 / 数据 / 插件 / 维护），
> 对应 `frontend/src/components/Settings.tsx` 及其六张子卡，后端
> `src/api/system.py`（2480 行）+ `cloud.py` + `plugins.py` + `upgrade.py`。
>
> 目标：以「仿真人操作点击」为主轴、后端与链路监控为第二轴，做一次极严、极细、
> 可复现、可取证的验收级测试。共 **258 条用例**（A~P 十六组，P0×62 / P1×118 / P2×78）。
> 本文档为测试计划与步骤手册，**不改动任何业务代码**；执行产出的报告落 `docs/test-report/`。
>
> 配套纪律真源：CLAUDE.md §16、`docs/test-plan-fullstack-e2e-2026-10-18.md`（总纲纪律沿用）。

---

## 一、总纲

### 1.1 测试目的
1. 验证设置页六签全部交互的**功能正确性**：每一下点击都有真实后端效果，无假按钮/假成功；
2. 用仿真人点击暴露「真人才会踩到」的缺陷：焦点丢失/受控组件/竞态/静默失败/文案误导/确认门缺失；
3. 用后端五源监控验证「前端看到的」与「后端发生的」**账实一致**（KV/DB/磁盘/审计/日志五方对账）；
4. 量化设置页性能指标（保存往返/主题切换/备份耗时/列表加载），锁定稳定性边界；
5. 主动攻击漏洞面：路径穿越/注入/XSS/Key 泄漏/伪造升级包/插件包/并发写/竞态恢复。

### 1.2 严格度分级
| 级别 | 含义 | 执行要求 |
|---|---|---|
| **P0 阻断级** | 设置丢失/数据损坏/Key 泄漏/恢复错库 | 每轮必跑，任一失败=本轮判废 |
| **P1 严重级** | 功能失效或误导用户（假成功/假失败） | 每版必跑，失败须修复或挂账 |
| **P2 完整级** | 边角/体验/性能/文案 | 发版前全量跑一遍 |

### 1.3 判定符号
- ✅ 通过：步骤全走完且全部预期成立，有取证；
- 🔶 有条件通过：主路径成立但命中已知缺口对照表（须注明 GAP-xx 编号）；
- ⚠️ 未测/证据不足：不得替用户宣称通过；
- ❌ 失败：任一预期不成立，附取证与初判根因。

---

## 二、被测对象解剖（代码级地图，写用例的事实依据）

### 2.1 六签 × 卡片 × 后端端点对照（实测 2026-10-19）

| 签 (URL ?tab=) | 卡片 | 主要交互 | 后端端点（真源） |
|---|---|---|---|
| 通用（无参数） | 全局设置 | KV 表展示 + 重置/保存；launch_mode/ui_performance 单选 | GET/PUT `/system/settings`（system.py:123/142） |
| 通用 | 功能互斥规则 | 静态展示四功能阻断矩阵 | 前端 `FEATURE_SWITCH_RULES`（types） |
| 通用 | 局域网访问（已下线） | **应无任何可交互控件**（`{false &&}` 死代码） | — |
| 通用 | 硬件信息 | 只读展示 | `/hardware/*`（hardware.py） |
| 外观 (?tab=appearance) | 主题/字号 | 6 主题卡 radiogroup + 小/中/大 | localStorage + PUT `/system/settings`{theme}（fire-and-forget 回写，useAppStore.ts:196-212）+ ui_prefs 镜像（:223-226） |
| AI 服务 (?tab=ai) | 云端 API 服务 | 连接 CRUD/测试/拉模型/启停 + 工位绑定 7 槽 | `/cloud/providers*`、`/cloud/bindings*`（cloud.py，11 端点） |
| AI 服务 | 联网搜索 | 开关 + 通道 + 触发时机 + 保存 | GET/PUT `/system/web_search`（system.py:192/199） |
| 数据 (?tab=data) | 存储清理中心 | 盘点 + 白名单清理 | `/system/storage/inventory`、`/system/storage/cleanup`（system.py:443/474） |
| 数据 | 数据管理 | 立即备份/恢复/项目导出/导入 | `/system/backup`:246、`/system/restore/list`:320、`/system/restore`:729、`/system/project/export`:1274、`/system/project/import`:1344 |
| 数据 | 本地算力·省钱账本 | 只读统计 | `/system/local-savings`（system.py:2416） |
| 插件 (?tab=plugins) | 插件 | 导入 .CuteMamen/含源码确认门/启停/删除 | `/plugins` CRUD + `/import` + `/enable|disable|reload`（plugins.py，14 端点） |
| 维护 (?tab=maintenance) | 硬件健康中心 | 四仪表 + 保护动作 | `/system/hardware/health`:632、`/system/modules/health/recover`:1066 |
| 维护 | 软件升级 | 导入 .upg/验签徽章/开始升级/删包/开文件夹 | `/upgrade/status|packages|import|start|open-folder`（upgrade.py，6 端点） |
| 维护 | 操作审计 | 刷新/清空/200 条滚动 | `/system/audit/list`:2135、`/system/audit/clear`:2144 |
| 维护 | 体检与修复 | 一键体检/修复/诊断 | `/system/health-check`:1083、`/system/health-repair`:1105、`/system/diagnose`:1127 |

### 2.2 持久化真源（监控取证的「账」）

| 数据 | 真源位置 | 说明 |
|---|---|---|
| 全局设置 KV | 主库 `system_settings` 表 | theme/font_size/launch_mode/ui_performance 等；**PUT 为整包替换语义**（历史坑，见 GAP-12） |
| 主题双写 | localStorage `omnispace.theme`（即时）+ system_settings（启动页读库回放） | 两处不一致 = 启动页闪主题跳变 |
| 字号双写 | localStorage `omnispace.fontSize` + `/system/ui_prefs` 镜像 | 换端口启动 localStorage 丢 → 后端为跨会话真源 |
| 云端连接 | `cloud_provider_service` 持久化（prov_* ID）+ Key 打码 `api_key_masked` | 编辑表单 Key 留空=保持原值（CloudApiSettings.tsx:126） |
| 工位绑定 | `/cloud/bindings` 槽位表（dialog.text/manga.text/novel.text/paint.image/manga.keyframe/manga.asset/manga.video） | 删连接顺带清悬空绑定 |
| 备份 | `data/backups/`：手动=`omnispace_*.db`，自动=`*_auto_*.db.enc` | 文案「保留最近 3 份」vs 实测列表 5 条 → H 组对账（GAP-04） |
| 插件 | `data/plugins/imported/` + `user_registry.json` + `factory_overrides.json`；产出 `data/plugins/output/` | 信任四档 repo_curated/user_data/user_source/user_installed |
| 升级包 | `updates/*.upg`（basename 强制，目录固定） | 验签 Ed25519 + 兼容判定；升级期 updater 在 5890 伺服 state.json |
| 审计 | 主库 `audit_log` 表（id/ts/module/action/target/result/detail，懒建） | settings_update 的 target=改动键排序串截断 180 字符（system.py:162） |
| 联网搜索 | KV `web_search` | 默认关；provider=browser/searxng/bocha；trigger=auto/always/off |

### 2.3 已知缺口对照表（判定 🔶 时的挂账依据）

| 编号 | 缺口 | 定性 |
|---|---|---|
| GAP-01 | 局域网访问 2026-09-20 用户拍板下线，`{false &&}` 死代码保留仅历史兼容展示 | 设计如此，测「不可交互」而非「功能在」 |
| GAP-02 | 省钱账本：keyframes 云端分支无引擎标记，暂计入本地（system.py 注释自认边界） | 已知口径偏差，宁多报风险点 |
| GAP-03 | 省钱账本：写作台/漫剧文字链路无归因字段 v1 不计 | 保守口径设计 |
| GAP-04 | 备份文案「保留最近 3 份」vs 恢复列表实测 5 条（手动/自动疑似分开保留） | 本轮 H 组账实对账定谳 |
| GAP-05 | MagicAI billing 端点无余额口子（404 实锤）→ 须优雅降级不炸卡 | 已知外部限制 |
| GAP-06 | 非文本模型警示 `warnedRef` 同一次表单会话只提醒一次，再点保存即放行 | 设计如此（2026-09-21 严格管理语义） |
| GAP-07 | updater 硬编码 py310（09-20 审计：发行包升级链死）| 开发版实弹升级受限，验证降级为流程级 |
| GAP-08 | 恢复生效须重启窗口；升级 start 为危险动作 | 实弹须用户拍板开窗口 |
| GAP-09 | audit API 支持 module 筛选参数但前端未暴露 | 记录不修 |
| GAP-10 | 插件信任徽章 2026-09-17 origin→trust 查表勘误史 | 回归点：橙徽章「用户·含源码」必须真渲染 |
| GAP-11 | 通用签 KV 表 theme=dali/font_size=14 为只读静态值，与外观签联动 | 联动一致性测试点 |
| GAP-12 | PUT /system/settings 整包替换语义：并发/滞后保存可能覆盖他键 | O 组并发对账重点 |
| GAP-13 | 对话远程推理旧键（remote_dialog_*）已迁云端卡，通用表不应再出现 | 回归点 |
| GAP-14 | 省钱账本 scope_note 前端必须原样展示（口径诚实） | 文案一致性测试点 |
| GAP-15 | 测试连接可打任意 base_url（SSRF 面）——单机产品定位下风险可控但须记录行为 | P 组定性 |

---

## 三、测试环境与工具链

### 3.1 被测环境（固定）
| 项 | 值 |
|---|---|
| 机器 | 本机 RTX 5070 Ti 16GB / RAM 64GB / Windows |
| 启动链 | 启动器 → 启动页(5850) → boot.py → 后端(5800)；**同机只跑一套** |
| 被测页面 | `http://127.0.0.1:5800/#/settings`（六签 hash + ?tab=） |
| 数据基线 | 现存 5 条云端连接（MagicAI 五件套）、插件 2 个（chat-skill-demo 用户含源码 / rust-coding 出厂）、备份 ≥5 份、审计 ≈200 条 |
| 显存口径 | 实际空闲 ≈13.4GB（勿用标称 16GB） |

### 3.2 仿真人工具链（browser-use IAB）
| 环节能手 | 用途 |
|---|---|
| domSnapshot | 一切定位的先行动作（禁盲坐标起步） |
| role/text 正则定位 | 按可访问名锚定，**count()==1 才允许点击** |
| click / fill / 键盘 | 受控组件一律 `fill`，**填后必 blur 再断言** |
| select 下拉 | 原生 `<select>` 用 selectOption 语义点击 |
| 截图 emitImage | 视觉旁证；**截图超时即改 DOM 断言**（历史坑） |
| CUA 坐标兜底 | role-click 超时的最后手段（学习页 G1 教训） |

### 3.3 后端监控五源（评测只信这五源）
1. **真 Bash/python requests** 直打 API（Git Bash curl 中文 body 必炸 → python 优先）；
2. **DB 只读**：system_settings / audit_log / cloud 持久化 / user_registry.json；
3. **日志**：logs/backend.log 尾随（断言无未捕获堆栈）；启动页 HUD；
4. **磁盘**：data/backups/、updates/、data/plugins/ 的路径/大小/mtime/SHA256；
5. **git diff**：测试前后工作区对照（防误改）。

### 3.4 实时监控点
| 监控点 | 采集方式 | 节奏 |
|---|---|---|
| 后端健康 | GET /health + backend.log 尾随 | 30s |
| RAM 提交余量 | psutil | 10s（<4GB 告警，<2GB 熔断） |
| audit_log 行数 | 只读 COUNT | 每条写操作用例前后各一次 |
| data/backups 目录 | ls + mtime | 备份/恢复用例前后 |
| Toast ↔ 端点对账 | IAB 抓 toast 文本 × 后端 5 源 | 每条用例 |

---

## 四、仿真人操作纪律（违者该用例判无效）

1. **先 snapshot 后动手**：点击前必须有 DOM 快照证据，定位表达式唯一（count==1）；
2. **受控组件 fill + blur**：设置表单类控件不失焦=没进状态，禁止 type 直灌；
3. **每次点击必须断言效果**：toast 文本 + DOM 变化 + 后端落账**三方对齐**才算点中；
4. **重渲染窗口期不点击**：列表加载/保存中状态先等 loading 消失再操作；
5. **视觉结论 DOM 量测优先**：主题/字号断言用 `<html>` 属性与 computed style，截图仅旁证；
6. **人类节律**：连续操作间隔 300~800ms 随机化；连点压力用例除外（该类用例本身就是要暴露防抖缺陷）；
7. **不碰真实数据红线**：不删真实云端连接的**绑定关系**（MagicAI 五件套正在服务对话/写作/绘画/视频工位）——凡涉及删真实连接的用例，一律先建 `TESTAUTO-` 前缀替身连接操作；真实插件 rust-coding 只读不动；审计只增查不清空（清空用例用专表验证后靠审计自留痕恢复确认）；
8. **危险动作预拍板**：标 ⚠️窗口 的用例（恢复生效重启、升级 start、audit 清空实弹）须用户拍板后开窗口执行；
9. **GPU 纪律**：涉及硬件健康卡保护动作的用例，开跑前必查后端实时任务；绝不 kill 后端，退出走 splash /api/quit 正规链；
10. **失败不停手**：单条失败记录后继续，除非触发熔断。

### 熔断纪律（立即停整轮）
- RAM 提交余量 < 2GB（WER RADAR_PRE_LEAK_64 静默死亡线）；
- GPU 温度 ≥ 90°C 持续 2 分钟；
- /health 连续 3 次超时（后端静默死亡）→ 取证后走正规退出链；
- 发现并发会话在飞任务 → 停手记录等窗口。

---

## 五、测试指标体系（KPI 与基线）

| # | 指标 | 基线参考 | 通过阈值 | 采集方式 |
|---|---|---|---|---|
| S1 | 设置签首屏加载（进签→数据可交互） | — | <1.5s（P95<2.5s） | IAB 时间戳 + API duration_ms |
| S2 | PUT /system/settings 保存往返 | — | <800ms（P95<1.5s） | meta.duration_ms |
| S3 | 主题切换 DOM 生效（点击→html data-family/data-theme 变更） | — | <150ms | IAB evaluate 前后时差 |
| S4 | 字号切换 DOM 生效 | — | <150ms | computed font-size 量测 |
| S5 | 云端卡列表加载（providers+bindings 并行） | — | <1.5s | IAB + duration_ms |
| S6 | 测试连接响应（云端可达） | — | <10s（不可达须 ≤10s 给出明确失败文案） | API 计时 + toast 文本 |
| S7 | 拉取模型列表（MagicAI 实测 62+ 模型） | — | <15s | API 计时 |
| S8 | 立即备份耗时（12.1MB 库） | 秒级 | <5s | API 计时 + 文件 mtime |
| S9 | 恢复安排（staging+预备份） | — | <5s | API 计时 + 目录出现两个新文件 |
| S10 | 项目导出（12MB 级） | — | <30s | API 计时 |
| S11 | 插件清单加载 | — | <1s | IAB |
| S12 | 升级包列表扫描（updates/ 空） | — | <2s | API 计时 |
| S13 | 审计 200 条加载渲染 | — | <1.5s | IAB |
| S14 | RAM 提交余量下限（全程） | 9.4GB | >4GB（<2GB 熔断） | psutil 10s 采样 |
| S15 | 假成功率（前端 toast 成功 × 后端无落账） | 0 | **=0** | 每写操作用例对账 |
| S16 | 假失败率（后端成功 × 前端报错） | 0 | **=0** | 同上 |
| S17 | 100 次签切换内存增长 | — | <+50MB 且无可堆叠 DOM 泄漏 | psutil + DOM 计数 |
| S18 | 审计留痕率（应留痕操作 × 实际落库） | — | **100%**（写操作全覆盖） | audit_log 对账 |
| S19 | Key 泄漏面（列表/编辑回显/日志/审计四处） | 全打码 | **0 泄漏** | P 组专测 |
| S20 | 恢复流程幂等（重复安排不叠加损坏） | — | pending 标记单一、重启后恢复一次 | ⚠️窗口用例 |

---

## 六、测试矩阵总览

| 组 | 模块 | 条数 | P0 | P1 | P2 |
|---|---|---|---|---|---|
| A | 页面骨架与导航 | 10 | 2 | 4 | 4 |
| B | 通用签·全局设置与互斥展示 | 20 | 4 | 8 | 8 |
| C | 外观签·主题与字号 | 18 | 4 | 8 | 6 |
| D | AI 服务·连接管理 | 30 | 8 | 14 | 8 |
| E | AI 服务·工位绑定 | 22 | 6 | 10 | 6 |
| F | AI 服务·联网搜索 | 10 | 2 | 5 | 3 |
| G | 数据·存储盘点与清理 | 10 | 2 | 4 | 4 |
| H | 数据·备份与恢复 | 16 | 6 | 6 | 4 |
| I | 数据·项目导出导入 | 12 | 4 | 5 | 3 |
| J | 数据·省钱账本 | 8 | 1 | 4 | 3 |
| K | 插件 | 20 | 5 | 9 | 6 |
| L | 维护·硬件健康与体检修复 | 10 | 1 | 5 | 4 |
| M | 维护·软件升级 | 18 | 5 | 8 | 5 |
| N | 维护·操作审计 | 12 | 3 | 6 | 3 |
| O | 后端链路账实对账 | 18 | 5 | 8 | 5 |
| P | 漏洞攻击面 | 24 | 4 | 14 | 6 |
| **合计** | | **258** | **62** | **118** | **78** |

---

## 七、分组用例明细

> 每条格式：**ID | 级别 | 仿真人操作步骤 | 后端/链路监控取证 | 预期**。
> 组内共享前置只写一次；「取证」列给出最小证据集，「预期」列全部满足才可判 ✅。

### A组 · 页面骨架与导航（10 条）

> 前置：后端健康（GET /health 200），已登录主界面。通用监控：每次切签断言无 console error、backend.log 无新堆栈。

| ID | P | 操作步骤 | 监控取证 | 预期 |
|---|---|---|---|---|
| A-01 | P0 | 打开 `/#/settings`，snapshot | 无 | 六签依次为 通用/外观/AI 服务/数据/插件/维护，默认落在「通用」，页标题「设置」+副题「通用 · 外观 · AI 服务 · 数据 · 插件 · 维护」 |
| A-02 | P0 | 依次点击六签，每签 snapshot | 每签对应卡片区渲染（对照 §2.1 地图） | 每签卡片齐且无空白页；数据签需出现 存储清理中心+数据管理+省钱账本 三卡；维护签出现 硬件健康+软件升级+操作审计+体检 四卡 |
| A-03 | P1 | 点「外观」签后看地址栏 | — | URL 变为 `?tab=appearance`；点回「通用」后 tab 参数移除 |
| A-04 | P1 | 直接打开 `/#/settings?tab=plugins` | — | 直落插件签（deep link 生效，Settings.tsx:106 初始态逻辑） |
| A-05 | P2 | 打开 `/#/settings?tab=不存在值` | — | 回落通用签，不白屏不报错 |
| A-06 | P2 | 六签快速连点 20 次（随机序） | RAM 采样 | 无卡死/错签/重影；RAM 增长 <+20MB |
| A-07 | P1 | 每签进入时观察 loading 态 | API duration_ms | 有「加载中…」过渡（settings-loading），数据到达后替换，无永久 loading |
| A-08 | P1 | 断后端（走正规链停）后进设置页 | /health 失败取证 | 通用签显「暂无可配置项，后端服务可能未就绪」+「重新加载」按钮；云端卡/插件卡各自显错误态而非白屏 |
| A-09 | P2 | 恢复后端后点「重新加载」 | API 200 | 设置重新载入，无需整页刷新 |
| A-10 | P2 | 页面缩放 80%/125% 下过六签 | DOM 量测 | 无布局断裂（横向滚动/控件重叠不算过） |

### B组 · 通用签·全局设置与互斥展示（20 条）

> 前置：通用签已打开。全局设置卡右上角有「重置」「保存」两钮。
> 监控：保存前后各读一次 GET /system/settings；audit_log 应新增 settings_update 行。

| ID | P | 操作步骤 | 监控取证 | 预期 |
|---|---|---|---|---|
| B-01 | P0 | 读全局设置表 | GET /system/settings | 表内键与后端返回一致；ui_performance/launch_mode 渲染为单选控件，theme/font_size 等渲染为静态值（GAP-11） |
| B-02 | P1 | 断言通用表**不含** remote_dialog_* 旧键 | GET /system/settings 对照 REMOTE_SETTING_KEYS | 旧远程推理键已迁云端卡不在此表（GAP-13 回归） |
| B-03 | P0 | ui_performance 点「性能模式（关动态特效）」→ 保存 | PUT body 含 ui_performance=lite；audit 新行 | toast 成功；GET 回读 lite；动态特效（粒子/流光）立即消失（DOM 断言） |
| B-04 | P0 | 改回「完整效果」→ 保存 | 同上 | 回读 full；特效恢复；两次均有审计留痕 |
| B-05 | P0 | launch_mode 点「系统浏览器」→ 保存 | PUT body 含 launch_mode=browser | toast 成功；回读 browser；控件旁显「下次启动生效」字样且本次会话窗口不热切 |
| B-06 | P1 | launch_mode 改回「桌面窗口（推荐）」→ 保存 | 同上 | 回读 shell |
| B-07 | P1 | 不改任何值直接点「保存」 | PUT payload 与 GET 一致 | 幂等保存成功，无报错，审计 target 含本轮全部键 |
| B-08 | P1 | 改 ui_performance 后**不保存**直接点「重置」 | 无 PUT 发出（网络面板） | 控件回落后端当前值，未落库未留痕 |
| B-09 | P2 | 改值→保存→刷新页面 | GET 回读 | 刷新后控件显示保存值（跨刷新持久） |
| B-10 | P1 | theme 静态行显示值 vs 外观签当前主题 | localStorage + GET | 两处一致（GAP-11 联动：外观签改主题后通用表静态行同步） |
| B-11 | P2 | font_size 静态行显示 14 vs 外观签「中」 | 同上 | 映射一致（md=14） |
| B-12 | P1 | 功能互斥规则卡内容断言 | 前端 FEATURE_SWITCH_RULES | 四行：AI 对话↔绘画/视频/训练；AI 绘画↔对话/视频/训练；视频生成↔对话/绘画/训练；训练↔对话/绘画/视频（与截图矩阵一致） |
| B-13 | P2 | 互斥规则卡文案含「规格 §6.1」说明 | — | 描述文案完整无截断 |
| B-14 | P1 | 局域网访问卡断言 | DOM | 标题含「（已下线）」；卡内**无任何输入框/按钮/令牌显示**（GAP-01，`{false &&}` 死代码不渲染） |
| B-15 | P2 | 全局设置表在窄窗口（<900px）下的横排布局 | DOM 量测 | label 不挤压成一字一行（2026-10-05 云端表单同款坑回归） |
| B-16 | P2 | 双开标签页 A 改 ui_performance 保存、B 改 launch_mode 保存（间隔 1s） | 两次 PUT payload + 终态 GET | 终态=后保存者整包覆盖（GAP-12 语义实锤记录）；无 500/无 KV 损坏；两签刷新后显示一致终态 |
| B-17 | P1 | PUT 非法值 launch_mode="" 直打 API | python requests | 后端拒绝或归一为合法值；GET 回读无脏值 |
| B-18 | P2 | audit 留痕内容抽查 | audit_log 行 | action=settings_update、target=改动键排序串（≤180 字符）、result=ok |
| B-19 | P2 | 硬件信息卡（通用签底部） | /hardware API | 数据渲染完整（CPU/GPU/RAM），后端不可达时有错误态 |
| B-20 | P2 | 保存时断网 2s（代理注入失败） | 网络面板 + toast | toast 报错不静默；重试保存成功 |

### C组 · 外观签·主题与字号（18 条）

> 前置：外观签已打开。监控：每次切主题断言 `<html>` 的 data-family/data-theme 属性（useAppStore.ts:62-85 契约）；localStorage omnispace.theme 同步；后端 theme 回写 fire-and-forget。

| ID | P | 操作步骤 | 监控取证 | 预期 |
|---|---|---|---|---|
| C-01 | P0 | 逐一点击 6 张主题卡：Sakura·夜樱/拂晓、Nebula·深空/晨辉、Dali·洱海月/苍山雪 | 每次断言 html data-family（sakura/tech/dali）+ data-theme（亮色四态有 light） | 卡片 active 态正确迁移（aria-checked）；DOM 属性与主题一一对应；色板即变（computed background 变化） |
| C-02 | P0 | 切到 Dali·洱海月（恢复原状） | GET /system/settings{theme} | 回写落库（boot 读库回放真源）；audit 有 settings_update |
| C-03 | P0 | 切主题后**立即刷新页面** | localStorage + GET | 刷新后主题保持所选（不闪回旧主题） |
| C-04 | P0 | 清 localStorage → 刷新 | boot 读库回放路径 | 主题从后端 system_settings 回放（localStorage 丢失不减损） |
| C-05 | P1 | 主题卡 title 提示文案 | DOM | 六卡 title 各为描述文案（如 Dali·洱海月=治愈系靛海 · 我在风花雪月里等你） |
| C-06 | P1 | 亮暗四态（拂晓/晨辉/苍山雪 + 夜樱）下检查正文可读性 | computed color/background | 文字/背景对比无失效（白字白底类缺陷） |
| C-07 | P1 | 字号三档 小/中/大 逐一点击 | computed font-size（13/14/16px） | 即时生效（[data-font-size] 覆盖）；按钮 active 迁移 |
| C-08 | P0 | 字号切「大」后刷新页面 | localStorage omnispace.fontSize + ui_prefs | 保持「大」；ui_prefs 镜像落库（跨端口启动真源） |
| C-09 | P1 | 字号切回「中」 | 同上 | 回落 14px；双写一致 |
| C-10 | P1 | 主题+字号连点各 30 次（随机序，300ms 节律） | html 属性序列录像 | 终态=最后一次点击；无中间态卡死/属性错乱 |
| C-11 | P2 | 主题切换 50 次连续（无节律快连） | RAM + DOM 节点数 | 无监听器泄漏（节点数不随次数线性涨） |
| C-12 | P1 | 切主题后进其他五签各看一眼 | DOM | 全局令牌生效于全页（无残留旧色块/对比失效局部） |
| C-13 | P2 | 主题卡布局：6 卡网格换行 | DOM 量测 | 窄窗下不溢出不重叠 |
| C-14 | P2 | GET /system/settings 不可达时切主题 | 网络面板 | localStorage 即时生效不阻塞（fire-and-forget），console 有 theme-writeback 报错但不白屏 |
| C-15 | P1 | 亮色主题（拂晓）下过六签 | 截图旁证+DOM | 全页无暗色残留岛（卡片/弹窗/toast） |
| C-16 | P2 | confirmDialog 弹窗在四亮色主题下的配色 | DOM | 按钮可辨、文字可读 |
| C-17 | P1 | 主题写入失败注入（PUT 500）后切两次主题 | audit/GET | 后端值最终=最后一次成功写入；无脏中间值 |
| C-18 | P2 | 字号在设置弹窗类浮层（确认弹窗）中的继承 | computed | 浮层字号随档位变化 |

### D组 · AI 服务·连接管理（30 条）

> 前置：AI 服务签已打开，云端 API 服务卡加载完成（现存 5 条真实连接）。
> 红线：对真实 MagicAI 五件套**只读+测试**，一切增删改在 `TESTAUTO-` 替身连接上进行。
> 监控：cloud_provider_service 持久化前后对照；audit 留痕（provider_delete 等）；后端日志无 Key 明文。

| ID | P | 操作步骤 | 监控取证 | 预期 |
|---|---|---|---|---|
| D-01 | P0 | 读连接列表 | GET /cloud/providers + /cloud/bindings 并行 | 5 条连接渲染：名称/类型徽章（文本/图片/视频）/地址/打码 Key（sk-****xxxx）/已启用（绿）；「使用中：工位名」标注与 bindings 对账 |
| D-02 | P0 | Key 打码断言 | 持久化文件 vs 页面 | 页面**任何位置**不出现完整 Key；打码格式=前 3 后 4 |
| D-03 | P0 | 点「添加服务商」 | DOM | 表单展开：名称/连接类型（文本/图片/视频三选）/服务地址/API Key（password 型）/模型列表 textarea + 添加/取消 |
| D-04 | P0 | 空表单直接点「添加」 | 无 POST | toast「请填写名称和服务地址」，不发请求 |
| D-05 | P0 | 建替身连接：名称 TESTAUTO-文本、类型文本、地址 https://api.deepseek.com、Key sk-test-12345678、模型 deepseek-chat | POST /cloud/providers | toast「连接已添加」；列表新增行且 Key 打码；持久化有新 prov_*；audit 无泄露 Key 的字段 |
| D-06 | P1 | 替身连接点「测试连接」 | POST /cloud/providers/test | 假 Key 也应给出明确结果（连通成功/401 类失败文案均算过，不许无响应悬挂） |
| D-07 | P0 | 替身连接点「编辑」→ Key 留空 → 保存 | PUT body api_key="" | 回读持久化 Key 不变（留空=保持原值语义，CloudApiSettings.tsx:126） |
| D-08 | P1 | 编辑名称与地址 → 保存 | PUT | toast「连接已更新」；列表即时刷新 |
| D-09 | P0 | 替身连接点「停用」 | PUT enabled=false | 状态字变「已停用」（红）；该连接从各工位绑定下拉消失 |
| D-10 | P1 | 停用态下「测试连接」「拉取模型列表」按钮 | DOM disabled | 两钮均禁用（disabled={!p.enabled}） |
| D-11 | P0 | 点「启用」恢复 | PUT enabled=true | 恢复绿色；绑定下拉重新可选 |
| D-12 | P0 | 替身连接绑到 AI 对话工位后点「删除」 | heavy 级确认弹窗 | 弹「确定删除连接…指向它的工位绑定会一并解除」；确认后连接消失且 **bindings 中该槽自动清空回本地引擎**（级联解绑） |
| D-13 | P1 | 删除确认弹窗点取消 | 无 DELETE | 连接保留 |
| D-14 | P1 | 删除后 audit 对账 | audit_log | 有 provider_delete（删云端连接）行，target=prov_id，result=ok |
| D-15 | P1 | 替身连接点「拉取模型列表」 | GET /providers/{id}/models + PUT | toast「正在拉取模型列表…」→成功「已更新 N 个模型」；若 MagicAI 返回含非本类模型，toast 附加「已自动剔除 X 个非本类模型」 |
| D-16 | P2 | 拉取失败注入（错 Key/断网） | toast | 「拉取失败：具体原因」或「服务商未返回任何模型」，不静默 |
| D-17 | P1 | 表单连接类型切「图片」→ 地址 placeholder 变 DashScope 形态 | DOM | placeholder 与说明文案随协议切换（三种协议三套文案） |
| D-18 | P1 | 文本连接模型列表填 whisper-1（音频类）→ 点添加 | 前端 SUSPICIOUS_NON_TEXT_MODEL_RE | 首次保存弹 warning toast（疑似音频/向量化/绘画类模型警示），**未发 POST**；再点保存放行（GAP-06 语义） |
| D-19 | P2 | warnedRef 放行后模型确实被类别过滤 | POST 响应 removed_models | toast 显「已按工位类别自动过滤 N 个不匹配模型」（后端双保险） |
| D-20 | P1 | 真实 MagicAI 中转（GPT-6 系）点「测试连接」 | POST test | 「连接成功」（💰 不产生生成计费；仅连通探测） |
| D-21 | P1 | 真实 MagicAI 生图连接模型列表断言 | 列表渲染 | 含 grok-imagine-image-2.0 等（截图基线），无跨类混入 |
| D-22 | P2 | 连接卡片「使用中」标注对账 | bindings 反查 | 每条被绑定连接的使用中工位名与 /cloud/bindings 完全一致 |
| D-23 | P1 | 编辑真实连接（名称不变）→取消 | 无 PUT | 表单关闭无副作用 |
| D-24 | P2 | 名称/地址超长输入（5000 字符）建连接 | POST | 后端接受或明确拒绝；前端不崩、布局不炸（换行/截断） |
| D-25 | P2 | 名称含 `<script>alert(1)</script>` 建连接 | DOM 渲染 | 列表按纯文本渲染（无脚本执行）；React 转义实锤 |
| D-26 | P2 | 重复名称建第二条连接 | POST | 行为记录（允许重名=两条并存且下拉可辨；拒绝=明确报错）——不许静默合并 |
| D-27 | P1 | 表单「取消」后重开「添加」 | DOM | 表单为空白初始态（无残留上一次输入；EMPTY_FORM 重置） |
| D-28 | P1 | 保存中重复快连「添加」钮 | 网络面板 | saving 态禁用按钮，只发一次 POST |
| D-29 | P2 | API Key 含中文/空格/换行 | POST | 后端接受原样存储或明确校验拒绝；测试连接行为可解释 |
| D-30 | P1 | 六签切走再回 AI 服务签 | GET 重发 | 列表重新拉取（不显示陈旧缓存）；加载态正确 |

### E组 · AI 服务·工位绑定（22 条）

> 前置：云端卡工位绑定区可见，7 槽位：AI 对话（dialog.text）/漫剧文字（manga.text）/写作台（novel.text）/绘画页出图（paint.image）/漫剧关键帧生成（manga.keyframe）/漫剧资产图（manga.asset）/漫剧镜头视频（manga.video）。
> 红线：真实槽位当前绑定（对话=国产系、漫剧文字/绘画/资产=GPT-6 系/grok-imagine 等）**测后必须还原**；每条绑定用例后回读 /cloud/bindings 对账。

| ID | P | 操作步骤 | 监控取证 | 预期 |
|---|---|---|---|---|
| E-01 | P0 | 读绑定区 | GET /cloud/bindings | 7 槽全渲染；当前值下拉与真实绑定一致（对话=MagicAI 国产系…等截图基线） |
| E-02 | P0 | 槽位协议过滤断言 | bindOptions 逻辑 | 文本槽下拉只列文本连接；图片槽只列图片连接；视频槽只列视频连接（stop 混类） |
| E-03 | P1 | 停用连接不出现在任何下拉 | — | disabled 连接被过滤（enabled 校验） |
| E-04 | P0 | 无模型连接（models=[]）在下拉中显示 | DOM | 显示「连接名 · 默认模型」占位行 |
| E-05 | P0 | 替身连接绑到写作台槽 | PUT /cloud/bindings/novel.text | toast「已切换为云端」；回读绑定值=prov|model 编码一致 |
| E-06 | P1 | 写作台槽选回「本地引擎（默认）」 | DELETE bindings/novel.text | toast「已恢复本地引擎」；回读空 |
| E-07 | P1 | 绑定后连接卡「使用中」标注更新 | 列表重载 | 使用中列表含新槽位名 |
| E-08 | P0 | 绑定后全链路生效抽测（写作台云端槽） | novel 链路一次真实请求（廉价短文）或 status 端点 | /cloud/status 文本引擎承载=新绑定（按窗口决定实弹深度；至少 status 对账） |
| E-09 | P1 | 漫剧镜头视频槽 hint 文案 | DOM | 显「绑定后生成视频走云端（该分镜须已有关键帧；首帧图会上传给服务商，费用按条计）」 |
| E-10 | P1 | 绘画/关键帧/资产三槽 hint | DOM | 均含「绑定后该功能出图走云端…恢复本地=选回本地引擎」类说明 |
| E-11 | P2 | 绑定下拉 label 格式 | DOM | 「连接名 · 模型名」拼接正确（无 undefined/空串尾点） |
| E-12 | P1 | 换绑（A→B） | PUT | 旧绑定被替换不残留；回读单值 |
| E-13 | P0 | 删除已绑定连接（D-12 场景复用） | bindings 对账 | 悬空绑定清零（cloud.py DELETE 顺带清绑定语义实锤） |
| E-14 | P1 | 双开标签页同槽并发换绑 | 两次 PUT | last-write-win；无脏中间态；两签刷新一致 |
| E-15 | P2 | 直打 API 绑 nonexistent slot | PUT /cloud/bindings/ bogus | 后端拒绝（明确错误码），不静默建槽 |
| E-16 | P2 | 直打 API 绑协议不匹配连接（文本连接绑视频槽） | PUT | 后端校验拒绝（与前端 protocolMatchesSlot 同源语义） |
| E-17 | P1 | 全部 7 槽逐一切本地再逐一还原 | PUT/DELETE ×14 | 每步回读一致；终态与起始快照完全一致（强还原纪律） |
| E-18 | P1 | 绑定区在无任何连接时的空态 | DOM（临时停用全部替身） | 下拉只有「本地引擎（默认）」；不空白不报错 |
| E-19 | P2 | 绑定值编码含 `|` 的模型名 | 回读 | provider_id|model 切分正确（indexOf 首个管道）不串档 |
| E-20 | P1 | manga.video 绑定生效前置抽测 | 漫剧页状态（只读） | 绑云端后漫剧模型配置的视频槽展示云端来源（跨页联动，只读断言） |
| E-21 | P2 | 槽位说明文案随槽位正确匹配（SLOT_HINTS 四条 + 兜底文案） | DOM | dialog/manga.text/novel.text/manga.video 四条专属 hint；其余槽显兜底 |
| E-22 | P1 | 绑定操作审计 | audit_log | 绑定/解绑留痕（如有该 action；无则记录为缺口候选） |

### F组 · AI 服务·联网搜索（10 条）

> 前置：联网搜索卡可见。默认关。监控：GET/PUT /system/web_search KV 前后对照。

| ID | P | 操作步骤 | 监控取证 | 预期 |
|---|---|---|---|---|
| F-01 | P0 | 读卡当前值 | GET /system/web_search | 开关=关（默认）、通道=浏览器自搜、触发=智能判定；控件与 KV 一致 |
| F-02 | P0 | 勾「启用联网搜索」→ 保存 | PUT | toast「联网搜索配置已保存」；KV 回读 enabled=true |
| F-03 | P1 | 通道切 SearXNG / 博查 | PUT | 各自保存成功；博查档显「需自带 Key」提示 |
| F-04 | P1 | 触发时机三档切换 | PUT | auto/always/off 各保存成功，回读一致 |
| F-05 | P1 | 关闭开关→保存 | PUT | KV enabled=false |
| F-06 | P2 | 不点保存直接切签再回 | GET | 显示已保存值而非未保存草稿（无草稿态=切换丢弃草稿，行为记录） |
| F-07 | P1 | 对话页发起含「今天/最新」问题（联网开） | 对话页只读抽测 | 回复带【1】【2】引用标（按窗口决定；至少配置链路 KV 生效） |
| F-08 | P2 | PUT 异常值 provider=bogus 直打 | python requests | 后端拒绝或归一；GET 无脏值 |
| F-09 | P2 | 卡片文案断言 | DOM | 「默认关闭；只上传查询词不上传聊天历史」隐私声明在卡上 |
| F-10 | P1 | 读失败注入（后端 500） | DOM | 显错误+重试（2026-09-15 审计修复：不许永久「加载中…」） |

### G组 · 数据·存储盘点与清理（10 条）

> 前置：数据签，存储清理中心卡（StorageCard）。监控：/system/storage/inventory 输出 vs 磁盘 du 对账。

| ID | P | 操作步骤 | 监控取证 | 预期 |
|---|---|---|---|---|
| G-01 | P0 | 打开数据签读盘点卡 | GET /system/storage/inventory | 各目录占用数字与磁盘实况量级一致（±20%）；渲染无 NaN/undefined |
| G-02 | P1 | 盘点加载态 | — | loading→数据替换；失败有错误态 |
| G-03 | P1 | 白名单清理项清单断言 | cleanup 白名单 | 只列白名单目录；无项目媒体/模型等高危目标 |
| G-04 | P0 | 执行一项最小清理（如临时目录） | POST cleanup + 磁盘对照 | toast 成功；对应目录字节下降；inventory 刷新 |
| G-05 | P1 | 清理确认弹窗（如有） | DOM | 有确认门，不一键直删 |
| G-06 | P2 | 清理耗时展示 | API 计时 | 大目录清理有进度/耗时反馈，不假死 |
| G-07 | P2 | _safe_clear_dir older_than 语义 | 直打 API（days 参数） | 仅清旧文件，新产物保留 |
| G-08 | P1 | 清理期间再点清理 | 按钮 busy | 防重入（busy 态禁用） |
| G-09 | P2 | 盘点在备份新增 100MB 后刷新 | 前后 inventory | 数字增长反映 |
| G-10 | P2 | 清理后 audit 留痕（如适用） | audit_log | 记录行为（无留痕则记缺口候选） |

### H组 · 数据·备份与恢复（16 条）

> 前置：数据管理卡。备份目录 data/backups/ 起始快照（文件名+SHA256）。
> ⚠️窗口：恢复生效需重启——H-10~H-12 须用户拍板开窗口。

| ID | P | 操作步骤 | 监控取证 | 预期 |
|---|---|---|---|---|
| H-01 | P0 | 点「立即备份」 | POST /system/backup + 目录 | toast「备份完成（约 12xx xKB）」；data/backups/ 新增 omnispaces_YYYYMMDD_HHMMSS.db；mtime=now |
| H-02 | P0 | 备份文件可用性 | sqlite3 只读打开新备份 | 库完整可查（integrity_check ok），非空文件 |
| H-03 | P1 | 备份耗时计测 | API 计时 | <5s（KPI S8） |
| H-04 | P1 | 恢复列表渲染 | GET /system/restore/list | 手动 .db 与自动 .db.enc 分列可辨；大小/时间格式正确；前端 slice(0,5) 显示最近 5 条 |
| H-05 | P1 | 保留策略账实对账 | 目录文件数 vs 「保留最近 3 份」文案 | 定谳 GAP-04：实测保留规则（手动/自动分开各 3 份或别的），文案与行为不一致则记缺口 |
| H-06 | P1 | 连续备份 3 次 | 目录 | 生成 3 个新文件；超限旧文件按实际策略清理（对账 H-05 结论） |
| H-07 | P0 | 点某备份「恢复」 | medium 确认弹窗 | 弹窗三行说明（还原数据库/下次重启生效/恢复前自动再备份）+「恢复」钮 |
| H-08 | P0 | 确认恢复 | POST /system/restore + 目录 | toast「恢复已安排——下次重启后生效」；目录**新增一份当前库的自动备份**（可回退）；列表头显「待恢复（重启后生效）」徽标 |
| H-09 | P1 | pending_restore 标记 | GET restore/list | pending_restore=true；再次进签徽标仍在 |
| H-10 | P0 | ⚠️窗口：正规链重启后端 | 启动页/重启后 GET | 库=所选备份内容（抽 1 个特征行验证）；pending 标记清除；重启前自动备份仍在 |
| H-11 | P0 | ⚠️窗口：恢复到旧备份后数据回退正确性 | DB 特征行 | 恢复时间点之后新增的数据消失（按测试脚本埋的特征行验证） |
| H-12 | P1 | ⚠️窗口：恢复的回退链（恢复前自动备份）可再恢复 | 重复 H-07/H-10 | 可恢复回「恢复前」状态（两跳回滚） |
| H-13 | P1 | 恢复安排后再次安排另一个备份 | POST ×2 | 第二次覆盖第一次 pending（单一 pending 语义）；不叠加损坏 |
| H-14 | P2 | restore filename 穿越攻击（P 组前置） | POST filename=..\..\evil | 后端 _resolve_safe_path 拒绝；无文件被读 |
| H-15 | P2 | backups/delete 端点（如有 UI 入口则走 UI） | POST /system/backups/delete | 删指定备份成功；列表刷新；不可删正在 pending 的备份（行为记录） |
| H-16 | P2 | 备份期间（busy 态）重复点「立即备份」 | DOM | 防重入（busy 禁用，Loader2 旋转） |

### I组 · 数据·项目导出导入（12 条）

> 前置：用 `TESTAUTO-` 前缀新建或选定一个测试项目（漫剧或漫画），内含 ≥1 分镜行 + ≥1 资产 + ≥1 关键帧。真实项目（微微一笑等）只读不动。

| ID | P | 操作步骤 | 监控取证 | 预期 |
|---|---|---|---|---|
| I-01 | P0 | 输入测试项目 ID 点「导出」 | POST /system/project/export | toast「导出完成：路径（x.xMB）」；归档文件落盘（.omnispace） |
| I-02 | P0 | 归档内容解包断言 | python zipfile 清点 | manifest + 分镜/资产/关键帧 bundle 齐（_collect_project_bundle 语义）；JSON 可解析 |
| I-03 | P1 | 空 ID 点导出 | 无 POST | toast「请填写要导出的项目 ID」 |
| I-04 | P0 | 不存在的项目 ID 导出 | POST | 后端明确报错（PROJECT_NOT_FOUND 类），toast 错误文案，不假成功 |
| I-05 | P1 | 导出耗时（12MB 级） | API 计时 | <30s（KPI S10） |
| I-06 | P0 | 输入归档绝对路径点「导入」 | medium 确认 → POST import | toast「导入成功：项目 新ID」；DB 出现新 project 且 **ID 全部重映射**（_fresh_id，不与源项目撞号） |
| I-07 | P0 | 导入后项目完整性 | DB 对照源项目 | 分镜行数/资产数/关键帧数与归档 manifest 一致 |
| I-08 | P1 | 导入确认弹窗取消 | 无 POST | 无副作用 |
| I-09 | P1 | 空路径点导入 | 无 POST | toast「请填写 .omnispace 归档文件路径」 |
| I-10 | P2 | 不存在的路径导入 | POST | 明确报错文案 |
| I-11 | P2 | 非法后缀文件路径（.txt 伪装）导入 | POST | 后端校验拒绝，明确错误 |
| I-12 | P2 | 导入含同名项目时的行为 | DB | 新 ID 独立并存（不覆盖同名旧项目）——行为记录并断言不损坏旧项目 |

### J组 · 数据·省钱账本（8 条）

> 前置：数据签底部账本卡。基线（截图）：2.9万 tokens / 5 条 / 306 张（关键帧 67/漫画资产 32/绘画 207）/ 52 条 / ¥139.43。

| ID | P | 操作步骤 | 监控取证 | 预期 |
|---|---|---|---|---|
| J-01 | P0 | 读账本五格 | GET /system/local-savings + DB 直查四表 | 五格数字与 SQL 直查一致（dialog_messages 排 cloud:: 前缀 / keyframes+comic_assets+paint_history 有产物行 / video_tasks 排 cloud:） |
| J-02 | P1 | 金额重算 | prices（¥8/M tokens、¥0.2/张、¥1.5/条） | 手算=cny_est（±0.01）；展示两位小数 |
| J-03 | P1 | tokens 万位缩写格式 | DOM | ≥1 万显「x.x 万 tokens」，否则原数 |
| J-04 | P1 | 口径声明展示 | DOM | scope_note 原样展示 + 参考价一行（GAP-14） |
| J-05 | P2 | 云端回复不计入 | 造 1 条 model_used=cloud::xxx 的 assistant 行（测试库）→ 刷新 | 不增（GAP-02/GAP-03 口径验证）；测后清理 |
| J-06 | P2 | 本地生图三分项和=总数 | DOM | 关键帧+漫画资产+绘画=306 类推恒等 |
| J-07 | P2 | 空库新装机口径 | 单测/桩 | 全 0 不 NaN；懒建表（paint_history 未建）计 0 不炸 |
| J-08 | P1 | 后端不可达时账本卡 | DOM | 显「账本数据暂不可用：重启后端后此处会显示本地算力统计」错误态 |

### K组 · 插件（20 条）

> 前置：插件签。现存 chat-skill-demo（用户·含源码，启用中）与 rust-coding（出厂·已审查）。
> 红线：rust-coding 只读不动；chat-skill-demo 启停可测（测后还原启用态）；删除/导入用测试包。

| ID | P | 操作步骤 | 监控取证 | 预期 |
|---|---|---|---|---|
| K-01 | P0 | 读插件清单 | GET /plugins + user_registry.json | 2 条渲染：名称+信任徽章+能力描述+启停钮；chat-skill-demo 显「用户·含源码」徽章 |
| K-02 | P0 | 徽章颜色断言（GAP-10 回归） | DOM computed color | repo_curated=蓝（accent）/user_data=绿/user_source=**橙 warning**（2026-09-17 勘误后必须真渲染） |
| K-03 | P0 | chat-skill-demo 点「停用」 | POST disable | toast「已停用」；钮变「启用」；/plugins enabled=false |
| K-04 | P0 | 再点「启用」 | POST enable | toast「已启用」；还原成功 |
| K-05 | P1 | 启停后插件技能清单联动 | GET /plugins/skills | 停用后其技能消失；启用恢复 |
| K-06 | P0 | rust-coding 行**无删除按钮** | DOM | origin=factory 无删除钮（只有启停）——防删出厂件 |
| K-07 | P0 | 造测试插件包 TESTAUTO.CuteMamen（纯数据）→ 选择→导入 | POST /plugins/import | toast「插件已导入：名称（用户·纯数据）」；清单+1；user_registry.json 更新 |
| K-08 | P0 | 造含源码测试包 → 导入 | POST 回 PLUGIN_SOURCE_CONFIRM_REQUIRED | 前端弹橙框确认门：「内含源码…将在软件内部直接运行…请只安装信任来源」+「我信任此来源，确认导入」/「取消」 |
| K-09 | P0 | 确认门点「取消」 | 无二次 POST | 未导入；门收起 |
| K-10 | P0 | 确认门点确认（带 confirm 重试） | POST confirm_source=true | 导入成功；徽章=用户·含源码（橙） |
| K-11 | P1 | 测试插件点「删除」 | heavy 确认（此操作不可恢复）| 确认后清单-1；data/plugins/imported/ 目录移除；user_registry 更新 |
| K-12 | P1 | 删除取消 | 无 DELETE | 保留 |
| K-13 | P1 | 启停 10 轮循环 | /plugins 每轮快照 | 状态翻转一致；无故障态残留；终态还原 |
| K-14 | P2 | 重复导入同名包 | POST ×2 | 覆盖更新或明确报错（行为记录），不产生双行 |
| K-15 | P2 | 伪造包（随机字节改后缀 .CuteMamen）导入 | POST | 明确报错（格式/解析失败），后端不崩（/health 存活） |
| K-16 | P2 | 包内路径穿越条目（../evil.py） | 包构造 + POST | 后端解包拒绝穿越（落点白名单内）；磁盘无逃逸文件 |
| K-17 | P1 | 故障态显示 | 构造 faulty 插件（或桩） | 显「故障态」红字 + 「上次错误：…」详情（state/last_error 渲染） |
| K-18 | P2 | 清单加载失败→「重试」 | 断链注入 | 显「插件清单加载失败，请重试」+重试钮可用 |
| K-19 | P2 | 无能力描述插件 | registry 造空 capability | 显「（无能力描述）」占位 |
| K-20 | P1 | 卡头文案三徽章说明 | DOM | 蓝=出厂已审查/绿=纯数据/橙=含源码警示语完整（含「请只安装信任来源」加粗） |

### L组 · 维护·硬件健康与体检修复（10 条）

> 前置：维护签。GPU 纪律：跑前查后端实时任务。

| ID | P | 操作步骤 | 监控取证 | 预期 |
|---|---|---|---|---|
| L-01 | P0 | 读硬件健康中心四仪表 | GET /system/hardware/health | 显存/温度/利用率/RAM 四仪表有值且量级正确；等级色（_level 阈值）与数值匹配 |
| L-02 | P1 | 仪表刷新 | 轮询/手动 | 数值更新；无冻结假数（对照 pynvml 直读，警惕 py313 无 pynvml 假空闲事故回归） |
| L-03 | P1 | 模块健康恢复按钮（如可见） | POST /system/modules/health/recover | 空闲时调用成功或明确「无需恢复」；不在任务在飞时误触发（纪律 9） |
| L-04 | P1 | 体检卡一键体检 | POST /system/health-check | 各探针项（DB 连接/WAL/加密/对话模型/绘画模型/视频模型/voice/目录可写/任务队列/WS Hub/调度器/功能锁/续跑扫描/云端）逐项出结果；无永久 loading |
| L-05 | P1 | 体检结果分级展示 | DOM | ok/warn/fail 三态可辨；fail 项有解释文案 |
| L-06 | P2 | 修复动作（health-repair） | POST | 空闲环境执行成功或「无可修复项」；不误伤在飞任务 |
| L-07 | P2 | 诊断导出 | POST /system/diagnose | 产出诊断包；audit 留痕 diagnostics_export |
| L-08 | P2 | 体检期间快连两次 | busy 态 | 防重入 |
| L-09 | P2 | RAM 紧张时仪表告警色 | 桩/观察 | 低于阈值变警示色（阈值对照 config） |
| L-10 | P2 | 硬件卡在后端高负载时的加载表现 | 生成中打开 | 超时降级文案而非白屏（同步 def 走线程池不堵事件循环验证） |

### M组 · 维护·软件升级（18 条）

> 前置：维护签软件升级卡，当前版本 v2.5.0（开发版），updates/ 目录起始快照。
> 红线：**升级 start 实弹（M-16）须用户拍板开窗口**；其余用例全部无副作用可日常跑。

| ID | P | 操作步骤 | 监控取证 | 预期 |
|---|---|---|---|---|
| M-01 | P0 | 读升级卡 | GET /upgrade/status + /upgrade/packages | 显「当前版本：v2.5.0（开发版）」；updates 目录空时显「updates 目录暂无升级包」 |
| M-02 | P1 | 点「打开 updates 文件夹」 | POST /upgrade/open-folder | 资源管理器打开 updates/（取证：弹窗或目录前置存在） |
| M-03 | P0 | 造合法签名 .upg（或用既有测试包）→「导入升级包」 | POST /upgrade/import | toast「已导入 vX 升级包，可开始升级」；卡片区出现包条目 |
| M-04 | P0 | 包条目徽章断言 | GET packages | 签名 ok+兼容 → 绿「可升级」 |
| M-05 | P0 | 篡改字节的伪包导入 | POST | 徽章红「签名无效」+toast「签名验证失败：包可能被篡改或非官方出品」 |
| M-06 | P0 | 版本不兼容包（to_version 回退/跨大版本）导入 | POST | 徽章黄「版本不兼容」+原因文案 |
| M-07 | P1 | 签名无效包不出现「开始升级」可用态 | DOM | 危险动作被禁用（后端 start 也有三道预检双保险） |
| M-08 | P1 | 包条目展开详情 | DOM（U5） | 显包大小（fmtMB）/日期/更新说明 |
| M-09 | P1 | 删除包条目 | DELETE /upgrade/packages/{name} | 条目消失；updates/ 目录文件删除 |
| M-10 | P2 | 非 .upg 后缀文件导入（前端拦截） | doImport 前置校验 | toast「请选择 .upg 升级包文件」，不发请求 |
| M-11 | P2 | >2048MB 假包导入 | 前端拦截 | toast「文件超过 2048MB 上限，疑似不是升级包」 |
| M-12 | P2 | .exe 改名 .upg 导入 | POST | 后端验签/格式校验拒绝（basename 强制 + 魔数/签名层） |
| M-13 | P1 | 拖拽 .upg 到卡片 | dragover 视觉反馈（U1） | 拖入高亮；松手走导入流程 |
| M-14 | P1 | 导入中重复操作 | importing 态 | 防重入 |
| M-15 | P2 | 直打 start 无包/无效包 | POST /upgrade/start | 后端三道预检（签名/兼容/磁盘）拒绝；任务忙时拒绝（错误码明确） |
| M-16 | P0 | ⚠️窗口：合法包「开始升级」实弹 | 全程监控 | 确认弹窗（U5 增强）→ 全屏等待页阶段文案流转（U2：正在启动升级…备份…替换…验证…完成）；完成后自动 reload 新版本号；旧版本备份存在 |
| M-17 | P0 | ⚠️窗口：升级中断电/杀 updater 模拟（可选） | state.json | 走断电自愈/回滚链（rolled_back 阶段文案）；旧版可启动 |
| M-18 | P2 | 升级等待页 10 分钟超时（U3） | 计时桩 | 显「重开指引」不再永久等待；后端下线时读 5890 updater 伺服阶段（Bug1 修复路径） |

### N组 · 维护·操作审计（12 条）

> 前置：维护签审计卡，当前 ≈200 条。红线：清空实弹（N-08）用「先备份数据库或拍板窗口」；默认轮次只验证确认门。

| ID | P | 操作步骤 | 监控取证 | 预期 |
|---|---|---|---|---|
| N-01 | P0 | 读审计卡 | GET /system/audit/list?limit=200 | 标题「操作审计 N 条（删除/导出/设置/云端/激活留痕）」；条目=时间+动作徽章+target 摘要 |
| N-02 | P1 | 动作徽章中文映射抽查 | ACTION_LABELS | project_delete=删除作品/settings_update=更新设置/provider_delete=删云端连接/audit_clear=清空审计 等 10 种；**未知 action 显示原文**不空白 |
| N-03 | P1 | 失败结果红标 | 构造 result!=ok 行（或桩） | 显红色 result 徽章 |
| N-04 | P1 | 触发一条新设置保存→点刷新钮 | audit 行数 +1 | 新行置顶出现；刷新钮有效 |
| N-05 | P2 | 条目 hover title | DOM | 显完整 detail（ellipsis 截断有 title 兜底） |
| N-06 | P2 | 滚动容器 260px 上限 | DOM | maxHeight 生效可滚动；200 条不撑爆页 |
| N-07 | P0 | 点「清空审计」 | medium 确认弹窗 | 弹「将删除全部审计记录（不可恢复）」；**先取消**——取消后无 DELETE |
| N-08 | P0 | ⚠️拍板：确认清空实弹 | POST audit/clear + COUNT | 清空成功 toast；列表空显引导文案；**audit_log 新增一条 audit_clear 自留痕**（清空动作本身留痕，system.py:2149） |
| N-09 | P1 | 清空后新操作重新留痕 | 做一次设置保存 | 新 audit 行出现（闭环） |
| N-10 | P2 | limit 边界直打 | GET limit=1 / limit=1001 | 1≤limit≤1000 校验（ge/le），1001 拒绝 422 类 |
| N-11 | P2 | module 筛选参数直打 | GET ?module=system | API 支持筛选（GAP-09：前端未暴露，记录） |
| N-12 | P1 | 审计与五类敏感操作联动抽测 | 各做一次删测试项目/导出/改设置/云端操作 | 每类均有留痕（S18 留痕率 100%） |

### O组 · 后端链路账实对账（18 条）

> 定位：不点 UI，纯五源对账——验证「前端展示的每一句话后端都真成立」。

| ID | P | 对账项 | 取证方法 | 预期 |
|---|---|---|---|---|
| O-01 | P0 | 设置 KV 全量 vs UI | GET /system/settings vs 通用签静态行 | 逐键一致；无 UI 演示了但 KV 没有的键 |
| O-02 | P0 | 主题三处真源一致 | localStorage + system_settings + html 属性 | 三处同值（启动页读库=boot 回放链路前提） |
| O-03 | P0 | 字号两处真源一致 | localStorage + ui_prefs | 同值 |
| O-04 | P0 | 云端连接持久化 vs UI | 持久化文件 vs 列表 | 条数/名称/启停/模型列表一致 |
| O-05 | P0 | 绑定表 vs 下拉选中值 | /cloud/bindings vs 每槽 select value | 7 槽全一致 |
| O-06 | P1 | 打码一致性 | 持久化 Key vs api_key_masked | 打码=前3后4；不同长度 Key 规则稳定 |
| O-07 | P1 | 备份目录 vs 恢复列表 | ls data/backups vs GET restore/list | 列表=目录实际（含 .db.enc）；大小误差 0 |
| O-08 | P1 | pending_restore 机制 | POST restore 后 GET | 标记位真；重启窗口后清除（联 H-10） |
| O-09 | P1 | 插件注册表 vs 清单 | user_registry.json + factory 清单 vs GET /plugins | 名称/信任档/启停一致 |
| O-10 | P1 | updates/ 目录 vs 升级包列表 | ls vs GET packages | 文件名/大小/数量一致 |
| O-11 | P1 | audit_log 表 vs UI 卡 | COUNT + 最新 5 行 vs 卡内容 | 条数与内容一致（limit 200 截断语义正确） |
| O-12 | P1 | web_search KV vs 控件 | KV vs 三控件 | 全一致 |
| O-13 | P1 | 省钱账本 SQL 复算 | 手跑四段 SQL | 五格全对（联 J-01） |
| O-14 | P2 | 硬件仪表 vs pynvml 直读 | 双读 | 量级一致（防假数） |
| O-15 | P2 | storage inventory vs du | du -s 对照 | ±20% 内 |
| O-16 | P1 | 日志零未捕获堆栈 | 全轮次 backend.log 扫描 Traceback | 设置页全部操作产生 0 个未捕获异常 |
| O-17 | P1 | 审计 target 截断语义 | 造 30+ 键改动 | target ≤180 字符且排序 |
| O-18 | P2 | 假成功/假失败总对账 | 汇总全轮次 | S15=S16=0 |

### P组 · 漏洞攻击面（24 条）

> 定位：主动攻击。单机产品定位下部分风险定性为「低危可接受」，但仍须**记录实际行为**；任何造成后端崩溃/数据损坏的发现一律 P0 上报。

| ID | P | 攻击面 | 步骤 | 预期（行为须记录；加粗=不可逾越红线） |
|---|---|---|---|---|
| P-01 | P0 | 恢复文件名路径穿越 | POST /system/restore {filename:"..\\..\\windows\\win.ini"} 等 6 变体 | **拒绝执行**；无文件读取；错误码明确（_resolve_safe_path 闸） |
| P-02 | P0 | 项目导入路径穿越/UNC | import path=\\\\attacker\\share\\x.omnispace、..\\ 等 | **拒绝或明确失败**；无外联请求发出 |
| P-03 | P0 | project_id SQL 注入 | export project_id=`' OR 1=1--`、`"; DROP TABLE projects;--` | 参数化查询实锤：返回 NOT_FOUND 类错误；projects 表完好 |
| P-04 | P1 | 连接名/项目名 XSS 向量 | 名称=`<img src=x onerror=alert(1)>` | 纯文本渲染无执行；删除该测试行 |
| P-05 | P1 | base_url SSRF 探面 | test base_url=http://169.254.169.254/、file:///、http://127.0.0.1:5800/api/v1/health | 行为记录（能打回环=单机定位可接受但记录；**file 协议与云元网段须拒绝或失败**）（GAP-15） |
| P-06 | P0 | Key 泄漏四查 | 列表/编辑回显/后端日志/audit target 四处 grep Key 明文 | **0 泄漏**（S19） |
| P-07 | P1 | 超长输入轰炸 | name 5000 字/models 1 万行/models_text 1MB | 不崩；接受或明确拒绝；响应时间 <10s |
| P-08 | P1 | 空值/纯空格 | name="   " 提交 | 前端 trim 校验拦截（!form.name.trim()） |
| P-09 | P2 | 重复名/同名覆盖语义 | 建同名×3 | 行为记录（D-26 结论引用） |
| P-10 | P0 | 双标签页并发 PUT settings | A/B 同时保存不同键 ×10 轮 | 整包替换语义下无 KV 损坏（键数守恒）；GAP-12 风险实锤记录 |
| P-11 | P1 | 恢复安排与备份竞态 | restore 安排后 0.5s 内 backup | 备份照常成功；pending 不被破坏 |
| P-12 | P0 | 升级包名路径注入 | DELETE /upgrade/packages/..%2F..%2Fconfig.yaml | **basename 强制**实锤：只删 updates/ 内；无目录逃逸 |
| P-13 | P0 | 升级 zip 内路径穿越条目 | 构造含 ../../evil 的 .upg | 解包落点白名单；无逃逸文件 |
| P-14 | P1 | 任务忙时强发升级 start | 生成任务在飞时直打 start | 后端「任务忙拒绝」预检生效（不杀任务不升级） |
| P-15 | P1 | 插件名路径注入 | DELETE /plugins/..%2Ffactory_override 等 | **拒绝**；出厂目录无恙 |
| P-16 | P1 | 含源码确认门绕过尝试 | 直接带 confirm_source=true 首次 POST（跳过弹门） | 后端行为记录：只要最终导入必经显式确认参数（门在后端有闸，非纯前端）；若后端无条件接受=缺口上报 |
| P-17 | P2 | audit 无限增长 | 造 1000+ 行后读 limit=1000 | 分页/上限生效；UI 卡 200 条截断正常 |
| P-18 | P2 | web_search KV 脏值 | PUT provider="javascript:" 等异常值 | 拒绝或归一；对话链不因脏值崩 |
| P-19 | P2 | ui_prefs 任意键写入 | PUT {evil_key:"x"} | 行为记录：接受（宽松 KV）或拒绝；不污染设置主表 |
| P-20 | P1 | 绑定悬空引用 | 直改持久化删连接不删绑定（模拟外部损坏）→ 读 providers/bindings | 读路径不崩；UI 不渲染幽灵绑定（或自动清扫） |
| P-21 | P2 | 备份目录只读/满盘注入 | 临时 ACL 只读 → 立即备份 | 诚实报错（不假成功）；恢复 ACL |
| P-22 | P2 | .omnispace 伪造归档 | manifest 缺失/坏 JSON/空 zip | 明确报错；不产生半截项目行 |
| P-23 | P1 | 并发导入同一归档 ×3 | 同时 POST | 产生 ≤3 个独立新项目或 1 个+2 拒绝；**无 ID 撞号**（_fresh_id 并发安全） |
| P-24 | P1 | 设置页全接口未鉴权访问面 | 无 token 直打全部 40+ 端点 | 行为记录：单机回环绑定下全部可达=产品定位已知；**确认回环绑定闸未开 LAN**（非 127.0.0.1 拒绝启动闸在位） |

---

## 八、测试稳定性规程（每轮次必做）

### 8.1 重复性
- **P0 用例全部重复 3 遍**：3/3 稳定过才判 ✅；偶发过=❌ 待修（竞态嫌疑）；
- 失败用例复跑 2 次定位可复现性：必现/偶现/单次，分别标注。

### 8.2 压力与疲劳
| 项 | 脚本 | 通过线 |
|---|---|---|
| 签切换疲劳 | 六签随机连点 100 次 | 无卡死；RAM 增长 <+50MB；DOM 节点数稳态 |
| 主题快连 50 次 | 无节律连点 | 终态=末次；无属性错乱 |
| 保存连发 20 次 | 通用签保存钮 20 连发 | 全部 200；KV 无脏值；audit 20 行 |
| 备份恢复循环 5 轮 | 备份→恢复安排→（窗口）重启验证 | 每轮库完整；备份文件递增正常 |
| 插件启停 10 轮 | 见 K-13 | 终态还原 |
| 设置页挂机 30min | 开六签轮询（模拟忘记关页面） | RAM 平稳；无 WS 误连；无内存爬升 |

### 8.3 环境扰动
- 断网注入（测试连接/拉模型/保存各一次）：错误文案全部可达、可重试；
- 后端正规链重启后：设置页全部数据自动恢复显示（无永久错误态）；
- 换端口启动（5800 被占顺延）：字号/主题从后端真源回放成功（localStorage 丢失场景，联 C-04/C-08）。

### 8.4 熔断与窗口需求汇总
| 项 | 需求 |
|---|---|
| ⚠️窗口 A | 恢复生效重启验证（H-10~H-12、O-08 尾巴） |
| ⚠️窗口 B | 升级 start 实弹（M-16/M-17）+ audit 清空实弹（N-08） |
| 💰烧钱项 | E-08 写作台云端实弹（按最短文计费）、D-20 连接测试（仅探测不计费）——开跑前逐单拍板 |
| 熔断线 | RAM<2GB / GPU≥90°C 持续 2min / /health 连续 3 次超时 / 并发会话在飞 |

---

## 九、执行顺序建议

1. **第 1 轮（无窗口，全量功能）**：A→B→C→F→G→J→K→L→N(除 N-08)→D→E（强还原纪律）→I→H(除窗口)→M(除实弹)；
2. **第 2 轮（对账）**：O 组全量 + P 组除需窗口项；
3. **第 3 轮（窗口批，拍板后）**：H-10~H-12、M-16/M-17、N-08；
4. **第 4 轮（稳定性）**：§8.2 压力疲劳 + §8.3 扰动；
5. 报告落 `docs/test-report/2026-10-19-settings-round1.md`，格式沿用 10-18 报告（逐条 ✅/🔶/⚠️/❌ + 取证 + 缺口对照表更新）。

## 十、附录

### 10.1 取证命令模板
```bash
# 健康与设置读（python 优先，Git Bash curl 中文 body 必炸）
python -c "import requests;print(requests.get('http://127.0.0.1:5800/api/v1/system/settings').json())"
# 审计对账
python -c "import sqlite3;c=sqlite3.connect('file:data/omnispace.db?mode=ro',uri=True);print(c.execute('SELECT COUNT(*) FROM audit_log').fetchone())"
# 备份完整性
python -c "import sqlite3;c=sqlite3.connect('data/backups/<file>.db');print(c.execute('PRAGMA integrity_check').fetchone())"
# 日志尾随
tail -f logs/backend.log | grep -iE "traceback|error"
# RAM 采样
python -c "import psutil;print(psutil.virtual_memory().percent, psutil.Process().memory_info().wset)"
```

### 10.2 端点真源速查（本轮涉及 40+）
`system.py`：settings 123/142 · ui_prefs 176/183 · web_search 192/199 · backup 246 · restore/list 320 · storage 443/474 · backups/delete 541 · hardware 567/632 · restore 729 · modules/health 1055/1066 · health-check 1083 · health-repair 1105 · diagnose 1127 · project/export 1274 · project/import 1344 · audit 2135/2144 · local-savings 2416；
`cloud.py`：providers 52/69/90/122 · test 133 · models 172 · billing 190 · bindings 204/212/221 · status 227；
`plugins.py`：list 72 · skills 78 · load/unload 94/106 · invoke 117 · kernel 144 · import 363 · enable/disable 383/394 · reload 405 · delete 419；
`upgrade.py`：status 92 · packages 103 · import 114 · start 160 · delete 232 · open-folder 243。

### 10.3 本计划的证据链声明
- 全部交互步骤均以 2026-10-19 工作区代码实测为据（Settings.tsx 640 行 / CloudApiSettings.tsx 491 行 / DataManagementSection.tsx 245 行 / PluginSection.tsx 272 行 / UpgradeSection.tsx 447 行 / system.py 2480 行 / cloud.py 236 行 / plugins.py 436 行 / upgrade.py 252 行）；
- 截图八张（六签实况）与代码逐一对齐；基线数字（5 连接/2 插件/306 张图/¥139.43/审计 200 条）取自截图实测；
- ⚠️ 未验证项：KPI 阈值均为目标值非实测基线，首轮执行后回填实测值。
