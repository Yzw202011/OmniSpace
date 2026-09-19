# 全身极致审计报告 — round-2026-09-19（极端细致逐层扫透）

> **元信息**
> - 审计日期：2026-09-19 晚；基线提交：`6764ac1`（本次审计前置动作：README 对账勘正并已 push origin）。
> - 方法：七路专项深度扫描（许可/批1~6增量/服务层/引擎层/数据层+API安全/前端/启动链+工具闸）+ **全部 15 条 P1 逐条人工亲核行号证据**（零误报确认）+ 机械闸基线实测 + 关键 P2 抽验（另实锤 6 条）。
> - 验证状态标注：✅ = 主审亲核（读盘上代码原文确认）；🔶 = 专项代理实锤（附代码摘录，主审抽验通过/未见矛盾）；⚠️ = 疑似（触发条件依赖特定环境，建议修复前先复现）。
> - 工作区注意：审计时工作区存在并发会话未提交改动（.githooks/pre-commit、frontend/src/router.tsx、src/engines/vllm_service.py、tools/convert_klein4b_to_comfy.py、tools/run_tests.py、CLAUDE.md、docs/INDEX.md、docs/未完成清单.md、tests/unit/test_turnaround_done_broadcast.py），全部按**盘上当前版**审计。
> - 覆盖声明：后端 src/ 全部 .py 经模式扫描 + 24~36 个热点文件全文精读每路；前端 160 个 ts/tsx/css 六类模式全量 grep + 62 文件精读；发行两台（packaging_console/license_console）按范围冻结令只扫自身 bug。未实弹项：GPU 推理链、真实出包链、真浏览器手测（本次为纯只读审计）。

---

## 一、执行摘要

**总账：P0 × 0 ｜ P1 × 15 ｜ P2 × 40 ｜ P3 × 62（另附亲测专项 5 项）**

无 P0（数据损毁/可白屏级）。P1 的 15 条全部经人工逐行复核确认，最危险的主题是三条暗线：

1. **闸门在假绿**：mypy.ini 已损坏（DuplicateOptionError 实锤复现），mypy 静默弃用整个配置，而 run_mypy.py 不查 mypy 退出码 → 第 4 闸在"无配置错误宇宙"里空转，当前基线 238 条即产自这个失效宇宙；torch 契约预检的主链判定键 `'py310' in label` 在契约真源已改 py312 后永不命中 → 主链 torch 漂移只黄灯不阻断。
2. **启动链的"只清自己"承诺失守**：stop.py 用路径前缀子串匹配 + 按进程名全机认杀；launcher 端口冲突处理用 `'omnispace' in cmdline` 子串判定——同盘存在任何 `OmniSpace*` 兄弟目录（发行包拷贝最常见）时，A 副本的 stop/启动会杀掉 B 副本正在推理的引擎与冷启动中的后端。
3. **许可防线的三块板**：回拨守卫锚点随每次成功校验滑动（25h 容忍窗 > 单日回拨量 → 时限码可永久续命）；回拨锚点/解绑墓碑/吊销名单三样全部寄生在无签名明文文件上，删除即失效（fail-open）；解绑令牌的"HMAC"无任何秘密成分，持码者可离线伪造换绑。

**亮点（同样重要）**：SQL 注入新增 0（60 处动态 SQL 全部参数化核过）；10 个上传端点白名单/魔数/有界读全部在位；插件包 tar 穿越面归零、升级包 zip-slip/验签闭环；历史病历（vLLM 准入时序、comfy 误杀判据、feature_lock 跨线程竞态、stats 时区、加密读写对称性）复核**全部在位未复发**；XSS 面干净（零 dangerouslySetInnerHTML）。

---

## 二、机械闸基线（实测）

| 闸 | 结果 | 备注 |
|---|---|---|
| L0 文档对账闸 | ✅ PASS（24/24，warn 1） | warn = 端点哨兵漂移：**实测 378 vs 断言 376**（见 §六-1） |
| L1 pytest | ✅ PASS | tests/unit 收集 **1152** 条（py310 实测），全量绿 |
| L2 vitest | ✅ 101/101 实跑绿 | 但 run_tests.py 入口经 npm.cmd 依赖 PATH 上的 node → 本机**必然假挂**（见 §六-2） |
| ruff | ✅ All checks passed | — |
| mypy 第4闸 | ⚠️ "无新增"（假绿） | ini 损坏整配置被弃用 + 不查退出码（P1-14）；基线存量 234→**238**（见 §六-3） |

---

## 三、P1 清单（15 条，全部 ✅ 亲核）

### 许可/激活域

**P1-1 ✅ 回拨守卫锚点随校验滑动，时限码可被逐日回拨永久续命**
`src/license_gate_impl.py:344`（守卫）+ `:376`（`_touch_state`）+ `:108`（`_ROLLBACK_TOLERANCE_S = 25*3600`）。
守卫条件 `now < last_check - 25h`，而每次校验成功 `_touch_state` 把 `last_check` 改写为当前（回拨后）时间——每次回拨 ≤24h 即永不触发守卫且锚点跟着回退。触发：时限码用户每日回拨 24h 重启。修法方向：锚点只允许前进（`last_check = max(last_check, now)`）或锚点入签名/DPAPI 保护。

**P1-2 ✅ 回拨守卫+解绑墓碑寄生在用户可删的明文 JSON，删除即归零（fail-open）**
`src/license_gate_impl.py:216-223`（`_load_state` 对 OSError/ValueError 静默返回全零态）。`data/license.state.json` 无 HMAC/签名/DPAPI：删除文件 → 守卫与墓碑消失；配合 P1-1 时限码永生；解绑前备份 `license.bin` + 解绑后还原 → 已解绑旧码复活。修法方向：state 纳入 DPAPI 加密 + 写入注册表副本双锚 + 缺失时降级策略明示。

**P1-3 ✅ 吊销名单未签名明文，删除即整条拦截线消失（fail-open）**
`src/license_gate_impl.py:66-71`（`_read_revocation_file` 缺失/损坏静默返回 `{}`）。包根 `revocations.json` 与 `data/license_revocations.json` 均无签名。出包侧持有 Ed25519 密钥，可对名单签名后由 impl 验签 + 缺失时拒绝激活（或宽限期降级并强提示）。

### 批5/批6 增量域

**P1-4 ✅ 人物 LoRA 版本删除端点函数级 import 指向不存在的模块 → 必 500**
`src/api/character_lora.py:139-142`：`from .character_lora_service import VERSIONS_ROOT` —— `src/api/character_lora_service.py` 不存在（真身在 `src/services/`，ls 实证）；`# type: ignore[import-untyped]` 恰好把 mypy 的真实报错压掉。`DELETE /character/lora/versions/{version}` 一调即 ModuleNotFoundError → 500，"containment/训练中拒/current 拒"三闸从未有机会执行。修法：改为 `from ..services.character_lora_service import`。

**P1-5 ✅ 批5 把建表函数插进 `/system/apikeys` 装饰器之下，密钥列表端点变死码**
`src/api/system.py:1978-1979`：`@router.get("/system/apikeys")` 现挂在 `def _ensure_audit_table()` 头上；原 `def api_keys_list()`（:2139，grep -B2 实证其上方无装饰器）永不路由。`GET /system/apikeys` 现返回 `data:null`。前端当前未消费故静默，API 契约已断。修法：装饰器归位。

### 服务层/队列域

**P1-6 ✅ 统一队列本地 worker 丢失唤醒竞态，静默系统中单个提交可永久滞留**
`src/services/task_queue.py:247-270`（`_worker_loop`）：出队判空（第一段 `with self._cond`）与 `self._cond.wait()`（第二段抢锁）**不在同一锁窗口**——两段之间 submit 完成 `append+notify` 时 worker 尚未进入 wait，通知落空后 wait() 无超时永久睡眠。图像/视频队列共用此核心。对照同文件 `_cloud_dispatcher_loop`（出队与 wait 同锁，正确）。触发：队列空闲后恰好一次生图/生视频提交落进微秒窗（其后无其他队列事件）→ 任务 pending 永不跑，直到下一次任意队列事件惊醒。修法：出队与 wait 合并进同一锁窗口（对齐云道写法）。

**P1-7 ✅ 云端结果下载把服务商 API Key 发往 OSS/CDN 结果主机（跨域凭据外泄面）**
`src/services/inference/cloud_video_client.py:185-187`：`headers = {"Authorization": f"Bearer {ep.api_key}"}` → `requests.get(video_url, ...)`，`video_url` 来自任务结果载荷（DashScope 等的结果 OSS 域 ≠ `ep.base_url`）；`cloud_image_client.py:182`（`_download_image(url, ep.api_key)`）同型。Key 进入第三方对象存储的访问日志；且结果 URL 通常自带签名，鉴权头纯属多余。修法：结果下载一律不带 Authorization 头（或仅对 base_url 同源附加）。

### 引擎域

**P1-8 ✅ llama_service 非重入锁内调 stop() → 自死锁，5.7GB 显存滞留且锁永不释放**
`src/engines/llama_service.py:70`（`self._lock = threading.Lock()`，非重入）+ `:176-178`（start() 持锁内 `if self.is_running(): self.stop()`）+ `:227-232`（90s 启动超时路径锁内 `self.stop()`）+ `:233`（`stop()` 内 `with self._lock:`）。触发：llama-server 冷缓存慢启动超时（HDD 读 5.5GB GGUF/显存被挤）即走超时路径 → 永久死锁，后续一切 unload/stop 排队挂死。修法：对齐 vllm_service（锁内直调 `_kill_locked` 无锁版）或改 RLock。

### 数据/恢复域

**P1-9 ✅ 备份恢复发生在 schema 初始化之后且不重跑迁移——旧版本备份恢复后全线 no such column**
`src/main.py:166`（`db = get_db()` → `_init_schema+_migrate_columns` 已跑完）→ `:180-211`（`_src.backup(_dst)` 页级整库替换，恢复库带着备份当时的 schema/user_version）——恢复后**无任何重迁移钩子**。恢复数月前备份（v10 及更早）→ 后续涉新列（bubbles/stale_reason/face…）端点全报 no such column、写链半截。修法：恢复生效后强制重跑 `_migrate_columns` + 数据迁移（或恢复后重建 Database 单例）。

### 前端域

**P1-10 ✅ 会话/项目切换无竞态守卫，慢响应后到整批覆盖新状态**
`frontend/src/stores/useDialogStore.ts:233-262`（`selectSession` 裸 await 后整批 `set`，无序号守卫/AbortController；`fetchSessions` 每键击一发、SessionList 无防抖）。快速连点会话 A→B：A 的慢响应后到覆盖 currentSession+messages——点了 B 看到 A。同族：`useNovelStore.openProject/selectChapter:149/243`、`manga/projectSlice.openProject:126-155`。修法：模块级 seq 守卫（一行比对）。

**P1-11 ✅ Ctrl+K 全局搜索「模型」分组永久缺失：调用死端点 + 吞错双层失效**
`frontend/src/components/common/GlobalSearch.tsx:50-53`：`get('/models/list')` —— 全仓无此路由（src/api/models.py 实际是 `/models`、`/models/readiness`…，grep 实证）；404 信封被 `.catch(() => [])` 静默吞（外层 `Promise.all().catch(reportBgError)` 因每个 job 自带 catch 永不触发=死代码）。违反 errors.ts「禁止吞错」铁律。修法：端点改 `/models`。

### 启动链/工程闸域

**P1-12 ✅ stop.py「只清本启动链」承诺失守：前缀子串 + 按名全机认杀**
`launcher/stop.py:97`（`PROJECT_MARKER in cl`，`PROJECT_MARKER='e:\omnispace'` 无尾分隔符——`E:\OmniSpace-UAT` 等兄弟目录 cmdline 全命中）+ `:157`（`'llama-poc' in ... or name == 'OmniSpace-LLM.exe'` 无路径标记）+ `:175`（`name == 'OmniSpace-Shell.exe'` 同）。A 副本跑 stop.py → B 副本在推理的引擎/已开的壳当场被杀，且三个退出分支都执行这两把扫杀。修法：路径标记改为 `PROJECT_MARKER + os.sep` 前缀匹配；llama/shell 清理补 cwd/路径归属校验（对照 launcher.py `_cleanup_orphan_engines` 的 parent_alive 口径）。

**P1-13 ✅ launcher 端口冲突第一级"清残留"用 `'omnispace' in cmdline` 子串判定，冷启动窗口内他副本后端被当残留强杀**
`launcher/launcher.py:169-172`（`is_omnispace` 三条件，`'omnispace' in cmdline` 命中任何 OmniSpace* 路径）+ `:188-204`（terminate→kill）+ `_probe_existing` 仅认健康实例。B 副本后端冷启动 25~96s 内不健康 → A 启动时 probe 返回 None → 落入杀残留 → B 后端被杀、看门狗重启 → 端口战争。批2-4 加的 copy_id 接管闸防"收编"防不住这条"杀掉重拉"。修法：判定键改 cwd 精确前缀 + copy_id 归属。

**P1-14 ✅【闸门级】mypy.ini 已损坏 → mypy 静默弃用整个配置；run_mypy.py 不查 mypy 退出码 → 第 4 闸 fail-open 假绿**
`tools/mypy.ini:55`（`[mypy-hydra.*]` 节内重复 `follow_imports = skip`，configparser 实测复现 `DuplicateOptionError`）+ `:64/:67`（`[mypy-src.build_info]` 节重复）+ `tools/run_mypy.py`（grep 实证无 `returncode` 检查，只按 `path:line: error:` 正则抽行——mypy 配置崩溃输出不匹配正则 → 错误集为空 → 「✓ 无新增」）。后果：python_version=3.10/check_untyped_defs/全部豁免节均未生效；基线 238 条产自"无配置宇宙"；将来修复 ini 会引爆海量"新增错误"。修法：ini 去重 + run_mypy 检查 rc（rc≥2 直接 FAIL）+ 修复后重对基线（一次性显式重基线，向用户报备增量）。

**P1-15 ✅【闸门级】torch 契约主链阻断判定键 `'py310' in label` 在契约真源改 py312 后永不命中 → 主链漂移只黄灯不阻断**
`src/torch_contract.json` targets = {"主运行时 py312", "vLLM py313", "ComfyUI 便携包"}（无任何 py310 标签）vs `launcher/launcher.py:291-297` 与 `tools/check_torch_contract.py:49-56` 的 `if 'py310' in label: primary_bad = True`。主运行时 torch 被换/漂移 → 落黄灯分支放行（boot 预检不拦），与两文件 docstring「主链不符=阻断」相反；check_torch_contract.py 头注也仍写 py310。修法：判定键改为契约中声明的主链标签（"主运行时"），并同步头注。

---

## 四、P2 清单（40 条）

### 许可/激活域（4）
| # | 条目 | 位置 | 状态 |
|---|---|---|---|
| P2-1 | 解绑令牌"HMAC"无秘密成分（公钥+序列号全公开），持码者可离线伪造换绑码 → 原机被吊销（DoS） | `src/license_gate_impl.py:301-303`（镜像 license_console/crypto_core.py:201-203） | 🔶 |
| P2-2 | 激活门禁中间件 `@app.middleware("http")` 不覆盖 WS——未激活发行包 WS 对话流可直连（dialog.py 全文无 is_activated 检查） | `src/main.py:507`、`src/api/dialog.py`（grep 零命中） | ✅亲核 |
| P2-3 | make_dist 吊销名单导出把 `database is locked` 误判"老库无列" → 静默出空名单升级包 | `tools/make_dist.py:53-56` | 🔶 |
| P2-4 | protect_build 功能哨兵仍写死 runtime/py310 → 发行包已不带 py310，发行链第③步必失败（操作者被迫人工放行=绕过验链） | `tools/protect_build.py:192-195` | 🔶 |

### 批1~6 增量域（6）
| # | 条目 | 位置 | 状态 |
|---|---|---|---|
| P2-5 | 批5 加密备份 `.enc` 不被批4 删除/保留/清理三处匹配 → 永久累积且 UI 删不掉 | `src/api/system.py:554-556,515,1792` | 🔶 |
| P2-6 | 生成物文件库四类中两类必 404：URL 走 /manga/media 白名单缺 `generated/images`、`generated/videos` | `src/api/system.py:2002-2007` + `src/api/manga/video.py:1553-1558` | 🔶 |
| P2-7 | 知识 LoRA「current 版本拒删」闸死代码：`hasattr(svc,'current_version')` 恒 False（真名 get_current）→ 可删生效中版本 | `src/api/learn.py:434-438` | 🔶 |
| P2-8 | 用户态 4xx 失败信封也喂舱壁账本（无严重度过滤）——模型加载中连点 3 次对话/传错 3 次尺寸 → 误判 degraded → 自愈真去重启引擎 | `src/middleware/error_handler.py:496-500` | ✅亲核 |
| P2-9 | 断电清扫跑在备份恢复之前（main.py:99 vs :183）→ 恢复进来的 `generating/pending` 僵尸行无人标记 | `src/main.py:99,180-211` | ✅亲核 |
| P2-10 | 睡眠保护 SetThreadExecutionState 是 per-thread 语义，异步钩子持/同步钩子放跨线程配对 → 保护泄漏或静默失效 | `src/middleware/feature_lock.py:71-94` | 🔶 |

### 服务层域（8）
| # | 条目 | 位置 | 状态 |
|---|---|---|---|
| P2-11 | 云视频成功记账死代码（`return resp.content` 在 try 内，try 后的成功记账永不可达）→ 日志页云端视频只有失败无成功 | `src/services/inference/cloud_video_client.py:198,204-206` | ✅亲核 |
| P2-12 | 小说队列宣称「排队等让位」实际 `acquire_or_raise` 被持锁即秒败 → 功能互斥时小说任务直接进 error | `src/services/novel_service.py:139-146` | 🔶 |
| P2-13 | WS 流式主路径无断连停止：客户端断开后 `_produce` 线程继续烧完整段推理；finally `discard` 还会反清 `/chat/stop` 恰好置入的停止旗标（SSE 路径已修，两通道不一致） | `src/api/dialog.py:2128-2146` | 🔶 |
| P2-14 | dialog_engine 读路径改写状态机：`_maybe_reattach`（由 get_status 触发）不持锁改 `_backend/_state/_model_id` → 与装载/切换竞态「张冠李戴」 | `src/services/inference/dialog_engine.py:1958-2008` | 🔶 |
| P2-15 | 知识入库/删除四写（向量/SQLite/FTS/图谱）无事务无补偿，半成功即永久孤儿，无对账修复路径 | `src/services/knowledge_service.py:855-885,920-958,1244-1267` | ⚠️疑似 |
| P2-16 | 知识批量删除无先备份；向量库（chroma）全链路零备份 → 误删不可恢复 | `src/api/knowledge.py:515-538` | ⚠️疑似 |
| P2-17 | 插件沙箱内存上限/超时硬杀只覆盖直接子进程，插件内部 spawn 的孙进程逃逸监管 | `src/services/plugin_runtime/sandbox.py:78-107` | ⚠️疑似 |
| P2-18 | submit_and_wait 在默认线程池占位至 3600s，与 run_blocking 共池 → 批量关键帧期间可饿死 ensure_loaded/推理卸载 | `src/services/task_queue.py:138-161` | ⚠️疑似 |

### 引擎域（2）
| # | 条目 | 位置 | 状态 |
|---|---|---|---|
| P2-19 | DPAPI unprotect 失败静默回退机器指纹派生钥 → 新旧密文双向不可解（混合密文库），无迁移/标记机制 | `src/data/crypto.py:126-142` | 🔶 |
| P2-20 | gpu_budget.request() 判定与记账非原子（锁外 snapshot→锁内 reserve），硬闸翻转后两任务可同时 GRANTED 超订阅显存 | `src/services/inference/gpu_budget.py:303-370` | ⚠️疑似 |

### 数据/API 安全域（7）
| # | 条目 | 位置 | 状态 |
|---|---|---|---|
| P2-21 | v3 数据迁移（明文字段补加密）在 user_version 置位**之后**跑 → 中途失败即永久跳过，存量明文永不补加密且无提示 | `src/data/database.py:611-628` | 🔶 |
| P2-22 | `remote_dialog_api_key` 明文落 system_settings（同表 cloud provider 的 key 已 AES-GCM）且随全量导出外带 | `src/data/models.py:1010` + `src/api/system.py:166-167` | 🔶 |
| P2-23 | `/system/export` 全量导出未加密未脱敏（对照 /system/backup 双闸），含 DB 热备副本，且 exports 目录在 /manga/media 白名单内可回读下载 | `src/api/system.py:1907-1913` | 🔶 |
| P2-24 | 限流桶停止访问永不清除（清理只删空桶）→ 随机 URL 段打非归一化端点可慢性内存增长 | `src/middleware/rate_limit.py:52-59` | 🔶 |
| P2-25 | 插件包导入全量读入内存后才验 64MB（违背同仓已有「有界读」修法）；source 文件同 | `src/api/plugins.py:373-379` | 🔶 |
| P2-26 | voices_clone 无大小上限全量读 + 客户端可控扩展名落盘（.exe/.bat 可落）+ 无魔数校验 | `src/api/manga/voice.py:321-329` | 🔶 |
| P2-27 | LAN 令牌无过期/无轮换/无失败锁定/`?token=` 明文入 URL 日志；泄露后终身有效 | `src/middleware/lan_auth.py:45-89,130-134` | 🔶 |

### 前端域（7）
| # | 条目 | 位置 | 状态 |
|---|---|---|---|
| P2-28 | 批6 气泡五形 CSS 整段重复粘贴：`.comic-bubble:hover` 与空格连接成永不匹配的后代选择器（hover 样式丢失）；`.comic-bubble-resize` opacity .85 覆盖 .25（把手常显，设计失效） | `frontend/src/styles/app.css:2359-2366` | 🔶 |
| P2-29 | generateVideo 失败路径无条件释放 video_gen 功能锁+压平状态（绕过 busy 检查）→ 另一分镜生成中互斥被误放行 | `frontend/src/stores/manga/videoSlice.ts:277-281` | 🔶 |
| P2-30 | ws.ts onclose 不校验 `this.socket === sock` → 重连竞态下旧连接回调抹掉新连接引用 → 同 URL 双活连接、消息重复 | `frontend/src/services/ws.ts:157-163` | 🔶 |
| P2-31 | LicenseGate 5s 轮询 useEffect 闭包捕获首帧 open=false 且依赖被 disable → 定时器永空转（后端慢就绪时指纹区卡「等待后端就绪」） | `frontend/src/components/common/LicenseGate.tsx:57-59` | 🔶 |
| P2-32 | 断流兜底只覆盖「曾 open 后断开」：WS 从未 open（发送瞬间后端宕机）永不起计时器 → generating 恒 true 只能手点停止 | `frontend/src/stores/useDialogStore.ts:591-613` | 🔶 |
| P2-33 | 批6 端点零 Zod：tasks_center（`as` 强转+cards 直渲染进 ErrorBoundary 面）、QuotaCard 裸 as、FileGallery 无 parse、批删三件套裸泛型 `res.deleted_ids.forEach` | `TopBar.tsx:136`、`QuotaCard.tsx:24`、`FileGallery.tsx:49`、`dialogApi:136/mangaApi:487/learnApi:354` | 🔶 |
| P2-34 | 四处裸 fetch 绕过 api.ts 不带 X-Omni-Token（logs/export、pagehide keepalive、videoSlice 取图、ArtSegmentDialog）→ LAN 远程模式下鉴权失败 | `LogSupportWidgets.tsx:62`、`DialogPage.tsx:109`、`videoSlice.ts:68`、`ArtSegmentDialog.tsx:40` | ⚠️疑似 |

### 启动链/工具域（6）
| # | 条目 | 位置 | 状态 |
|---|---|---|---|
| P2-35 | 8189 兜底清理无归属校验：probe 承诺复用的外部/用户 ComfyUI 在 boot 退出/stop 兜底时被反手 kill | `launcher/boot.py:306-313` + `launcher/stop.py:123-139` | 🔶 |
| P2-36 | dir_audit 退出码漏计引擎树违规（报告打 ✗ 但 exit 0）→ CI/人工按返回值判断漏报 | `scripts/dir_audit.py:285-288 vs 315-318` | 🔶 |
| P2-37 | archive_debug_debris 把轮转代日志（`*.log.1`）与 crash_forensics/ 整目录当残留搬走 → 取证断链 | `tools/archive_debug_debris.py:24-32,44-56` | 🔶 |
| P2-38 | 双 boot 近同时冷启都过守卫（守卫只认已 LISTEN splash）→ 后到者杀先到者冷启动后端（P1-13 的单副本形态） | `launcher/boot.py:712-751` | ⚠️疑似 |
| P2-39 | build_rc.py robocopy `/MIR` 镜像删除无任何 dest 护栏（无防呆/无确认，--dest 任意路径） | `tools/build_rc.py:58-76,100-113` | 🔶 |
| P2-40 | 发行入口桩 omnispace_exe.c 仍硬编码 py310（发行包已无 py310）→ 源码与产物双失配，下次出包即暴露 | `launcher/omnispace_exe.c:22-31` | 🔶 |

---

## 五、P3 清单（62 条，紧凑口径）

### 许可/激活（8）
1. `src/license_gate.py:24-34` 转发分支漏转发 `PUBKEY_HEX_X`（stub 有、真身无）——开发机 import 即 AttributeError，与"两态等价"自我声明矛盾。✅摘录核过
2. `src/license_gate_impl.py:375-379` `license_sha256` 完整性锚只写不读（死代码）——license.bin 被替换无检测（与 P1-2 叠加）。
3. `tools/make_dist.py:408-413` `--pubkey` 无 hex/长度校验；漏传则静默出门禁关闭的发行包，无哨兵拦截。
4. `src/api/license.py:53-55` `/license/status` 在未激活白名单内向本机任意进程泄露三枚指纹+序列号+异常原文（伪造解绑令牌的材料免费送）。
5. `src/license_gate_impl.py:246-249` 激活码无长度上限直接 b32decode（约 5/8 内存放大，低危资源 DoS）。
6. `src/main.py:516` async 中间件内同步 `is_activated()`——指纹缓存过期时持 RLock 跑 PowerShell 子进程（1~30s）阻塞事件循环。
7. `license_console/app.py:257-277`（冻结范围）`/api/rebind` 不检查 `old["status"]`——已作废序列号凭有效解绑码换绑复活洗白。
8. `src/license_gate_impl.py:198-199` 公钥双藏 XOR 垫是随二进制同存常量——对有二进制 patch 能力者无增量安全。

### 批1~6 增量（6）
9. `src/api/system.py:346` 为嗅 6 字节头把每个备份文件全量读进内存（GB 级 DB 备份），应 `read(64)`。
10. `src/api/system.py:2078-2081` 任务中心风格训练卡片 SQL 未选 name 列却 `r.get("name")` → 恒显兜底名（批6"列名勘误"未修全）。
11. `src/api/character_lora.py:154` current.json 版本命中用子串判断（"v1" 被 "v10" 误拒），与 learn.py 等值比较不一致。
12. `src/api/novel.py:154-181`、`comic.py:443`、`dialog.py:1357` 批删逐条 DELETE 无事务包裹，中途异常留半截态（novel 批删亦不避生成中章节）。⚠️疑似
13. `src/services/novel_service.py:985-989` `_JOB_PROGRESS` error 条目永不回收（无界慢性泄漏）。
14. `src/services/novel_service.py:772-773` 大纲重建无条件删全部 outline → 已完成章节链接悬挂，无确认无备份。

### 服务层（11）
15. `src/services/inference/dialog_engine.py:2208-2215` reset_instance 的 watchdog `join(1.0)` 过短——复位恰逢巡检执行卸载时旧线程仍在飞（测试污染残余窗口）。
16. `dialog_engine.py:1481-1498,1966-2008` degraded_from 与收养重挂模型不一致时 meta 帧标注错位。⚠️疑似
17. `src/api/dialog.py:609` 候选模型清单显存估算硬编码 "vl" 口径 → vllm/gguf 档 fits_local 失真（前端置灰误判）。
18. `src/api/dialog.py:1248-1253` SSE 收尾 n.output 复制粘贴重复两遍。
19. `src/api/dialog.py:2080-2089` WS 主路径同步 DB+AES 直跑事件循环（历史模式遗留，高频叠加卡顿）。
20. `task_queue.py:354-372` 云道 dispatcher 锁内做 sqlite 读拉长队列锁；建池失败任务已出队登记但永不执行。⚠️疑似
21. `image_queue.py:243-263`、`video_queue.py:323-347` 功能锁 acquire/release 固定 10s 结果超时 → 循环忙 >10s 时误失败/锁残留（B0 已知面）。
22. `video_queue.py:128-131` 孤儿回收旗标 check-then-set 非原子（幂等故轻）+ 回收同步 DB 扫描跑在事件循环。
23. `plugin_runtime/bridge.py:29-36` 注释宣称「事件不带大数组」实际 emit 通道无任何大小闸（大 payload 全量广播）。
24. `knowledge_service.py`（全文）见 P2-15 四写无补偿——另 `_enforce_capacity` 反向删除半失败态列表可见检索不可达。
25. `src/services/offload.py:27-49` 唯一卸载入口无独享池/并发闸（与 P2-18 同根，设计文档自认可两路共用——登记为架构债）。

### 引擎/内核（12）
26. `src/engines/llama_service.py:168-175` 准入拒绝置 error 但旧健康进程仍在服务（状态与 VRAM 事实脱节）。
27. `llama_service.py:197-203` 唯一未绑 Job Object 的子进程引擎（vllm/comfy 均绑 KILL_ON_JOB_CLOSE）→ 强杀后端留孤儿持显存。
28. `src/engines/vllm_service.py:859-868` 子进程异常退出路径不清 `_served_name/_model_dir/_started_at` → status 报旧值 uptime 虚增。
29. `vllm_service.py:1001-1011` stop() 拿不到锁即 return True 的假设面（并发双 stop/收养微窗口）。⚠️疑似
30. `vllm_service.py:24,202-205` 超时口径文档漂移（头注 240s vs 实际 START_TIMEOUT_S=420）。
31. `comfy_proc.py:383-391` shutdown 的 taskkill TimeoutExpired 未捕获 → 空闲巡检线程死亡，自动关静默失效。⚠️疑似
32. `comfy_proc.py:351-355` spawn 失败路径泄漏 `_log_fp`；`:139-147` Job 句柄失败分支泄漏（危害轻）。
33. `src/middleware/feature_lock.py:130-134` instance() 无锁单例（训练线程与循环并发首触可 split-brain）。⚠️疑似
34. `src/engines/vram_manager.py:353-358`、`memory_manager.py:246-251` 单例无锁初始化（与 gpu_budget 双检锁口径不一致）。
35. `memory_manager.py:161-163` 压缩后 access() 无解压路径（当前无消费方，潜伏数据损坏）。⚠️疑似
36. `src/core/distributedformer.py:1097,1628` 环路由用进程盐化 hash()（同文件感受野刻意用 crc32 保复现）→ 跨进程可复现承诺在路由维度失效。
37. `distributedformer.py:643,660 vs 869-874` 标量与向量化前向输出时序语义漂移；`:944-997,1613` dim≠16 全链路形状崩（当前恒 16，潜伏）。

### 数据/API 安全（10）
38. `src/api/draw.py:72-82` vs `paint_engine.py:188-197`（browser_agent_service 同型）paint_history 等 DDL 双份维护且游离迁移体系 → 复刻 08-14 work_mode 事故的温床。
39. `src/api/system.py:2427-2435` 统计端点对密文列做 `LENGTH()` → tokens 估算系统性高估 1.5~2 倍。
40. `src/api/manga/voice.py:250` voices/upload 无魔数嗅验（白名单+大小在位）。
41. `src/api/license.py:21-50` 激活失败锁是全局单桶进程内存态：LAN 邻居 5 次失败可锁全机激活 60s；重启计数清零。
42. `src/api/upgrade.py:130`、`system.py:1504`、`deps.py:126` 等 20+ 处业务信封 detail 回传 `str(exc)` 原文（内部绝对路径/用户名侦察面）。
43. `src/api/knowledge.py:87-94` + `behavior_service.py:100,122-144` /behavior/event 字段无长度上限+事件队列无界（300/min 限流下单请求仍可注入巨串）。
44. `src/services/data_prune.py:39-47` 后台修剪线程 sqlite3 直连主库执行 DELETE，绕过全局写锁（WAL busy_timeout 兜底，纪律破坏）。
45. `src/main.py:632-652` + `rate_limit.py:205` 限流中间件不覆盖 WebSocket：对话流 WS 与 /ws hub 消息洪泛无速率闸。
46. `src/api/models.py:977-1013` /models/import 接受任意绝对路径（黑名单 best-effort，自述治本在加载侧）——已知接受项登记在案。
47. `src/services/page_guard.py:170-171` `WsHub.instance()` 在 try 外 → 初始化异常任务静默死亡，全页关闭自动退出失效。⚠️疑似

### 前端（6）
48. `src/App.tsx:266-292` 全局快捷键只豁免 isContentEditable 未豁免 input/textarea/select → 输入框内 Alt+N 仍切路由。⚠️疑似
49. `BottomStatusBar.tsx:108` `as never` 类型谎言 → degraded 芯片 title 拼出 undefined、key=undefined。
50. `useConfirmStore.ts:41-47` 并发 ask() 顶掉前一个 pending → 其 Promise 永不 resolve，调用方 await 悬挂。⚠️疑似
51. `src/App.tsx:12` 头注仍写「8 个一级路由」（router.tsx 已勘正为 10+1）——文档漂移；懒加载 chunk 失败无路由级 errorElement。
52. 硬编码颜色存量：tsx 57 处/app.css 128 处（豁免主题族文件外）——LicenseGate GitHub-dark 色板（已登记豁免但未进清单）、ModelManager/MangaLibrary 樱花粉 rgba、OmniLightbox/GlobalSearch/ConfirmDialog 遮罩黑、DaliParticles/TechParticles 画布色板整组等 → 切主题不随色。
53. `useDialogStore.ts:287-305` 批删会话分批循环中批抛错整体抛出：已删批次服务端生效但 done 集丢失、本地列表不收敛（幂等兜底故轻）。⚠️疑似

### 启动链/工具（9）
54. `launcher/launcher.py:780-798,905` start() 失败后心跳盲窗（process=None 走不进崩溃判定+180s 宽限反复豁免）→ Popen 失败时 ~16 分钟零重启零取证。
55. `tools/run_mypy.py:52-59` `--staged` 读工作区而非暂存内容 + follow_imports 把未暂存依赖错误计入 fresh（与 doc 闸同类，另一闸）。⚠️疑似
56. `launcher/boot.py:259-260,378-380` /api/comfy 处理无异常包裹：logs/ 不存在时 open('ab') 抛错 HUD 无响应；log_fp 常驻不关（fd 泄漏）。
57. `launcher/shell.py:127,141-143` 壳单实例守卫「标题含 omnispace + python 宿主」即可命中 → 用户第三方 python 窗口被顶替。⚠️疑似
58. `launcher/make_shortcut.py:86-97` PowerShell 单引号字符串内插路径不转义（路径含单引号即创建失败，报错退出非静默）。
59. `tools/brand_exe.py:328-336` 新鲜度检查 src 缺失时 src.stat() 未捕获 → 整批品牌化中止（出包中断）。
60. `scripts/codemod_except_log.py:26,52-55` `_best_ln` 从未赋值（死代码）+ 单行 except 整行替换毁掉 except 头（复跑必 SyntaxError 中止）。
61. `scripts/comfy_link/comfy_mount.py:172-179` 孤儿清理 `st_nlink<=1` 即 unlink → 中央库删除权重后映射槽位硬链（唯一剩余引用）随之消失，无备份无确认。⚠️疑似
62. `tools/make_dist.py:380-383` 输出目录清场 rmtree 仅由名字构造护栏（--out 错位时同名子目录被无确认删除）。

---

## 六、主审亲测专项（5 项，本次审计过程亲手实测）

1. **端点哨兵漂移**：`grep -rE "@router\.(get|post|put|delete|patch)\(" src/api` 实测 **378**，doc_claims.json `http-endpoint-count` 断言 376（L0 warn 在案，README 提交时闸自身亦报）。差 2 需对账：要么哨兵漏更、要么有两端点未登记。修正后同步 CLAUDE.md §4。
2. **run_tests.py L2 层假挂**：`npm.cmd run test` 依赖 PATH 上的 node（本机 node 不在 PATH，实测报「'node' 不是内部或外部命令」）——"三层测试体系唯一入口"的 L2 在无全局 node 的开发机/CI 必假挂；vitest 直跑 101/101 真绿。修法：对齐 PY 的做法直调 `runtime/node-v20.20.2-win-x64/node.exe node_modules/vitest/vitest.mjs`。
3. **mypy 基线暗涨**：基线存量 234（09-05 冻结口径）→ 现 238（2f22bd4 递减 248→238 后又增？）——结合 P1-14（配置失效宇宙），基线内容不可信，修复 ini 时须一次性显式重基线并报备。
4. **doc_drift_check 两个边角**：①count_matches 把注释/字符串中的命中也算数（当前无注释装饰器，纯设计余量）；②闸读工作区而非暂存区——`git add` 违规文件后回滚工作区可过闸提交（防漂移不防故意，登记备查）。
5. **convert_klein4b_to_comfy.py（在飞版）三点**：QKV 三件套不齐时**静默丢弃**该组权重（`if all(...)` 无 else 报告）；无"未映射键残留"审计输出（漏网键原样进输出文件）；`save_file` 非原子（转换中途断电=权重文件损坏，工具级可重建故轻）。

---

## 七、复核无恙面（防复查重复劳动）

- **历史病历全部在位未复发**：vLLM 热切换准入时序（锁内杀后复测闭合）；comfy 空闲 300s 三层防线；feature_lock `_state_lock` RLock 全临界区；stats/event 时区 aware 化；dialog_messages/behavior_logs 加密读写对称（无明文直写路径）；恢复文件名白名单+前后缀限定无穿越。
- **SQL 注入**：全仓 f-string SQL 约 60 处逐一核过——全部「常量条件串+?占位值」或代码内常量表名，新增未参数化 0；FTS5 MATCH 值参数化+双引号包裹。
- **上传面**：10 个上传端点白名单/魔数/有界读全部在位；插件包 tar 条目白名单+64MB/64 条目上限+npz 膨胀闸；升级包 Ed25519 验签前置+safe_join+符号链接拒绝+逐文件 SHA256。
- **文件回读面**：draw/knowledge/manga media/日志导出路径拼接逐一核过，无穿越实锤（manga media 白名单 5 目录 containment 成立——files_gallery 的缺口是**白名单缺目录**而非穿越）。
- **CORS/TrustedHost/回环闸/LanAuth 结构/WS origin guard**：在位；限流键取 TCP 对端（XFF 回环直连下不可伪造）。
- **API Key 处理**：云端 Key AES-GCM 落库+API 面 mask+错误文本无回显（唯 P1-7 下载面与 P2-22 remote key 明文两处例外）。
- **upgrade_service / recovery / data_prune / self_heal 三闸 / module_health 锁 / browser_pool 状态机 / lora worker**：无新发现。
- **前端 XSS**：零 dangerouslySetInnerHTML；markdown 走 react-markdown 默认消毒（无 rehype-raw）。
- **dialog_engine 03cfe5b 新版 reset_instance 本体**：锁序/幂等/PYTEST 闸三契约自洽（仅 P3-15 join 时长残余窗口）。

---

## 八、修复优先级建议（供拍板，未动任何代码）

> **【2026-09-19 深夜更新】第一梯队六项已获用户拍板并全部修复**（验证证据见 git 提交）：
> P1-14（mypy.ini 去重 + run_mypy configparser 预检与退出码 fail-closed，基线 238→229 纯减陈账 0 新增——修复后实测 mypy 真吃配置：探针对照 missing-import 压制）✅；P1-15（launcher.py + check_torch_contract.py 判定键 `'py310'`→`'主运行时'`，健康路径实跑三处对齐）✅；P1-5（apikeys 装饰器归位，路由表断言 GET→api_keys_list）✅；P1-4（import 改 `..services`，真调端点到达 containment 闸）✅；P1-7（两客户端 `_result_headers` 同源才附 Bearer，六探针实弹）✅；P1-8（`_stop_locked` 拆分对齐 vllm 模式）✅。ruff/compileall/全量 pytest 复跑绿。
> 余下第二、三梯队未动。

**第一梯队（闸门与资金安全，建议本周末前）**
1. P1-14 mypy.ini 去重 + run_mypy 查 rc → 一次性重基线报备（闸门诚信根）
2. P1-15 torch 契约判定键（一行改动，恢复主链阻断语义）
3. P1-5 /system/apikeys 装饰器归位（一行）
4. P1-4 character_lora import 改 `..services`（一行）
5. P1-7 云结果下载去掉 Authorization 头（凭据外泄面）
6. P1-8 llama stop 自死锁（对齐 vllm 的 _kill_locked 模式）

**第二梯队（稳定性与误杀面）**
7. P1-6 队列丢失唤醒（出队与 wait 同锁窗口）
8. P1-12/P1-13 stop/launcher 归属判定改精确路径前缀+copy_id
9. P1-9 恢复后重跑迁移；P2-9 清扫挪到恢复之后
10. P1-1/P1-2/P1-3 许可三件（锚点只进不退 + state/名单签名化——建议与激活台一起做成一批）
11. P2-8 舱壁记账加严重度过滤；P2-10 睡眠保护线程归属

**第三梯队（体验与工程卫生）**
12. P1-10/P1-11 前端竞态守卫+死端点；P2-28~P2-34 前端批
13. §六-2 run_tests L2 便携 node 直调；§六-1 哨兵对账；§六-5 转换器补审计输出
14. P3 批量清账（62 条多为低成本）

**本审计（扫描阶段）未动一码一闸**；第一梯队修复见上方更新注记。报告本身为新文件不入闸。第二、三梯队涉及方案四段制的大项（许可三件套、stop/launcher 归属判定等）请拍板后再开工。
