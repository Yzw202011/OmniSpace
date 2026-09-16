# 全项目逐层实码审计·根因版（2026-09-16）

> 口径：用户令「不依赖文档、逐层扫透、挖掘到根部」。全程只读取证（代码/日志/系统事件/git），零改动零 commit。
> 证据标记：✅ 已实证（附 文件:行号 / 日志行）/ 🔶 部分实证 / ⚠️ 推断（标注候选机制）。
> 机械闸基线：ruff ✅ / pytest 883 passed+9 skipped ✅ / tsc ✅ / vitest 71/71 ✅ / mypy ❌ 基线外 1 条。

---

## 一、崩溃簇根因链（P0，今晨 08:57~09:11 五连死 + 09-14 20:13 一轮）

**时间线（全部 ✅ boot.log / heartbeat_history.jsonl / 事件日志 events-20260916.jsonl / Windows System 事件日志）**：

1. 08:26:14 一毫秒内 6 个 `POST /draw/generate` 齐发 + 故意 404/参数错探针（events 当分钟 198 条）——自动化脚本/并发会话在实弹打真后端；
2. 批量 paint 任务排队执行，42s/步 × 36 步，08:55~08:57 显存 97→98% 持续越线（resource_guard 13 次告警，无空闲模型可卸）；
3. **08:56:38 WHEA-Logger 17：PCIe AER「已更正硬件错误」（PCI Express Root Port）**；65 秒后——
4. **08:57:43 nvlddmkm 事件 153（NVIDIA 驱动层）＝第一死精确时刻**；任务死于 56% 进度；无 Python traceback、无 WER Event 1000、无 Resource-Exhaustion 实际事件（RAM 耗尽说否定）；
5. 看门狗 5 连重启（09:01/09:04/09:07/09:11，间隔 ~200s）；期间**用户仍在操作**（09:03:50 release-for-module、09:04:13 comic/asset/adopt、09:05 爆 143 事件）——每轮死亡都砸掉用户正在做的事；
6. 09:11:06 第 5 次打满 `max_restart_attempts`，系统躺尸至 11:06 人工拉起（现进程 pid 4304 稳定）。

**根因定性**：表层=显存 98% 叠载触发 PCIe AER + 驱动层事件 → 进程原生猝死（Python 层无法留痕）。深层结构性缺陷两个 ✅：
- **看门狗热重启路径零清理**：ComfyUI 残留清理只在 boot.py 冷启动/退出链（boot.py:21、:305）；launcher.py:583 `start()`（热重启用）不清理任何引擎子进程 → 孤儿进程占显存 + 新实例 CUDA 初始化撞墙的候选机制 ①；候选 ②=TDR 后驱动状态未恢复。⚠️ 二者需下一次死亡现场抓证据分辨。
- **看门狗不记退出码**：launcher.py:746-773 只记「第 N 次」，`process.poll()` 的返回值被丢弃——取证断链，根因迟迟无法定性的直接原因。
- 同案底：09-14 20:13~20:14 一轮 10 秒 3 连死打满上限；本机 08-29 蓝屏根因即 PCIe AER+显存叠载（同一条线）。

## 二、P1 级问题（代码级，全部亲眼复核）

| # | 问题 | 位置 | 根因 |
|---|------|------|------|
| 1 | 功能锁跨线程竞态：`acquire_sync/release_sync` 无锁 read-modify-write `_holders/_hold_counts`，与 async 路径的 `self._lock` 互不同步 | src/middleware/feature_lock.py:202-242（调用方 lora_training_service.py:805/822、style_lora_service.py:664/681） | 训练 worker 线程与事件循环共享锁状态但无 threading 原语（docstring 自认「批1 接管旧路径」的过渡态未收尾） |
| 2 | 启动回放超时保护是死代码：AbortController 建了、signal 没传 | frontend/src/services/uiPrefs.ts:25-27（main.tsx 阻塞式 await） | systemApi.ts 封装未暴露 timeout/signal 参数，api.ts:147-149 本身支持 |
| 3 | 备份断档 41.9h（主库 131MB 天天写，最新备份 09-14 18:54） | data/backups/ | **无自动备份机制**：main.py lifespan 周期巡检只有 log-cleanup/flow-trace-cleanup/knowledge-checkup（src/main.py:289、:329），备份仅 api/system.py 手动端点 |
| 4 | 运行时写脏 git 跟踪文件 | src/cutemamen/kernel.py:127-132 → pkg.py:143（`DEFAULT_PKG_DIR="cutemamen_pkgs"` kernel.py:32） | 安装介质与运行时存档目录合一设计缺陷；当天 a/echo/x 三包全脏 |

## 三、P2 级问题（摘要，详见前轮清单）

- 吞错 307 处 `except Exception: pass`（占宽捕获 51%），最险 main.py:381-410 关停链 5 连吞、api/upgrade.py:66/77；根因=硬件探测降级链的习惯写法无统一「静默需带 debug 日志」规约。
- 26 个 async 函数 61 处直调同步 SQLite（novel_service.py:749 十处等）；根因=无 async DB 门面，services 直连 `_db()`。
- 6 处 subprocess 无 timeout（llama_service.py:112/239、vllm_service.py:1078/1186、comfy_proc.py:262/383）——全是 kill/收尸路径。
- CORS 硬编码 5800：src/middleware/cors.py:32/36（端口顺延 5801 即失配，api/system.py:166 注释证实顺延场景真实）。
- 前端吞错守卫只认单行形态（errors.test.ts:46-66），多行 `} catch { /* 忽略 */ }` 全逃逸；~20 处真静默。
- 非豁免 CSS ~136 行硬编码色 + LicenseGate.tsx:115-177 十六处内联色。
- 日志轮转三洞（根因 ✅）：logger.py:140/161 RotatingFileHandler 只管 backend.log 系；boot.log=boot.py 手写无轮转（10.5MB 超限）；backend_stdout/stderr.log=launcher.py:593-594 append 重定向无轮转（9.1MB）；backend.log.4 缺号=疑手动删除。
- 前端 0 AbortSignal 传递（竞态只靠 cancelled 旗标）；localStorage 前缀三态（omnispace./omni./无前缀）。

## 四、流程与仓库卫生（根因层）

| 问题 | 根因 |
|------|------|
| pre-commit 第1步 compileall 扫 `backend launcher tools`，不含 src/ | .githooks/pre-commit 未随 src 扁平化同步（PR#3 漏网），语法闸在扫空目录 |
| mypy 基线外 1 条（launcher/shell_poc.py union-attr） | 基线记账缺口非新回归：文件 09-11 后未动（cfe21ac），基线 09-16 11:02 重生成（cc1f765「内核 37 条」）却 0 条 launcher——rebaseline 的 SCOPE 含 launcher，疑环境差异（pywebview 类型解析）；⚠️ 待复现 |
| backend/ 残留树：全套 __pycache__(310/312/313)+0 字节 data/omnispace.db(8-25)+空目录×3+vendor/sqlcipher3+_planA2_dbg.py+两个 _tts wav | 扁平化重构只删 git 跟踪文件，未扫未跟踪残留 |
| 根目录 启动OmniSpace-开发版.exe（58KB 二进制）被 git 跟踪 | 历史误入库 |
| main 领先 origin/main 3 提交未推送 | 待推送窗口 |
| 隔离区 48.5G | 排期 09-20 真删（purge_quarantine.py 已备未执行，正确） |
| logs/ 153M、45+ 一次性调试日志 | 无归档策略 |
| models 201G 中 _build/diffusion_models/image_gen/segment 未入册 | 历史结构目录，manifest 只登 17 条主模型 |

## 五、文档/记忆漂移实证（印证「不能依赖文档」）

- `/api/v1/system/health` 已 404，真路径 `/health`（launcher.py:910 在用）；
- config.yaml 无 `html.ui-lite` 键（271 行全文核过）；
- 「backend/ 仅 2 shim」仅 git 层为真，磁盘另有残留树；
- 测试基线 774→883。

## 六、本次审计自身的坑（诚实记录）

- events jsonl 时间字段是 `ts` 非 `timestamp`——先用错 key 得出「死亡窗口零事件」错误结论，已作废重查（用户当时在操作）；
- vitest 必须在 frontend 目录跑（契约测试用 process.cwd() 定位源码，跨目录跑出 3 个假阳性）；
- PowerShell `$_` 在 bash 双引号内被展开，整条命令须单引号包裹；
- 后台 Bash 任务不继承主 shell 的持久 cwd。

## 七、修复优先级建议（待拍板，未动任何代码）

1. **B 数据安全**（半小时级）：手动触发全量备份；main.py 周期巡检加 db-backup（每日）；修日志轮转三洞。
2. **C 两个 P1 小修**（各 ≤10 行）：feature_lock 加 threading.Lock（两路共用）；uiPrefs 透传 timeout。
3. **A 崩溃根因收口**（需布控）：看门狗记退出码 + 死亡瞬间自动抓显存/引擎子进程快照；热重启路径加引擎清理；下次死亡现场分辨候选 ①/②。
4. **D 大扫除**：backend/ 残留清零；.CuteMamen 存档迁 data/；pre-commit 路径同步 src；mypy 基线缺口复核；根目录 exe 出库。

---

# 第三轮·合并专项（2026-09-16 晚，HEAD=52d4383）

> 重点解剖 PR#3 src 扁平化 + DistributedFormer 内核并入 + 本地插件 P1/P2 三方合并（61cea89）。全程只读。
> 注：审计期间并发会话连落两笔修复（14b0908 七项测试路径 / 52d4383 launcher 两处路径+种子契约），以下不含其已修项。

## 八、打包线整体失守（P0——下次出包即崩或出残废包）✅

| 位置 | 病灶 | 后果 |
|------|------|------|
| tools/make_dist.py:195 | 读 `backend/config.yaml`（已不存在） | 出包即 FileNotFoundError |
| tools/make_dist.py:92 | COPY_DIRS 只拷 backend 壳，**无 src/ 拷贝项** | 出残废包（无后端主体） |
| tools/protect_build.py:38 | TARGETS=["backend","launcher","skills"] | 编译保护打了空壳，src/ 裸奔 |
| tools/check_torch_contract.py:19 | 契约路径指向 backend/torch_contract.json | 契约门禁工具崩（launcher 侧已修，工具侧漏） |
| tools/brand_exe.py:97 | 读 backend/config.yaml | 品牌化崩 |
| launcher/launcher.py:411-412 | 完整性清单引用 backend/core/security.py（不存在） | 完整性校验恒败，src/main.py 不在校验范围 |
| packaging_console/preflight.py:216 | 同款（冻结区，只报不动） | — |

## 九、H3 冻结链带伤（P1）✅

`src/services/inference/h3_chain_engine.py:330` `from backend.config import get_config`（try 内）→ 必 ImportError → `_dual_clock_enabled()` 恒 False → **`src/config.yaml:167` 的 `manga.h3_dual_clock: true` 永远不生效**。不崩、纯静默功能丢失。正确 import 应为 src.config。

## 十、架构层（双体系并存 + 内核死代码）✅

- **内核 4800+ 行零产品接线**：src/core（distributedformer.py 1821 行+face_pkg）、src/codec、src/training/readout、src/cutemamen 大部（kernel/bridge/event_bus/face_bridge/migrate/pkg/rust_coding）在产品进程不可达，仅 81 个测试消费；依赖意外干净（numpy+stdlib 零 torch）。
- **双插件体系三处双轨**：双 ExpertPlugin/PluginContext/PluginMemory 基类（cutemamen/plugin.py:34,192,209 vs plugin_runtime/base.py:30,79,103，同名不同 API）；双 .CuteMamen 包读取器（pkg.py:186-217 未带 allow_pickle=False vs loader.py:145 带）；双事件总线（event_bus.py:17 vs bridge.py:15）。
- **记忆格式分歧=静默丢数据雷**：内核 save_pkg 写 episodic `{"events":...}`（pkg.py:162）vs OSP loader 只认 `"entries"`（loader.py:150-156）——内核存的包被 OSP 加载时 episodic 记忆静默丢失；P2 若做回写互毁。
- **cutemamen_pkgs 脏工作区根因=测试污染**：test_cutemamen.py:78/:224 `kernel.unmount("echo"/"x")` 未设 pkg_dir → 写进仓库被跟踪文件；包 base_model="generic" 不在 native_registry，产品侧永不加载=纯死档。
- rust_coding 同名碰撞（src/cutemamen/ vs src/data/）：现无串包，但被 exec 加载时相对导入必炸→静默空语料早退（潜伏，rust-coding 未在 _SEED_PLUGINS）。
- plugin/README.md:5 引用已删的 plugin/video_making.py 且声称由内核加载（实际是 OSP plugin_runtime）；plugin/__pycache__ 残渣。

## 十一、功能/页面/逻辑层（前后端契约全查）✅

**硬断（用户可感知）**：
1. **模型管理「彻底删除」按钮 100% 失败**：前端 DELETE `/models/{id}/files`（modelApi.ts:92、ModelManager.tsx:614，10 秒倒计时确认）→ 后端 models.py 无此路由 → 必 404（报"信封格式非法"）；**已固化进 dist**。合并后唯一真 404 死控件。
2. **风格训练不可暂停/取消**：后端 style.py:225-244 四控制端点前端零调用——训练跑飞只能等或重启。
3. 项目导入导出/诊断 26 项/备份恢复/模型下载/benchmark/学习配额/LoRA 版本管理/音色上传克隆/插件管理 UI——**约 90+ 后端就绪前端没接**的业务端点（双侧就绪只差接线）。
4. 学习进度 WS（learning.py:624）前端零消费，用 3s 轮询替代。
5. 幻影终态：分镜行 `generation_status='skipped'` 前端当终态消费（MangaWorkspace.tsx:250），后端 GenerationStatus 永不返回（data/models.py:75-79）——永不触发的死分支。

**验证为好的消息**（记忆勘误）：attach_lora 已有 1 处生产调用（keyframe.py:1555，09-15 接上，"零调用"旧账已清）；D-LoRA lora.safetensors 约定挂载链完整（keyframe.py:964-970）；zviews 四视图链路完整默认开（comic_gen.py:937-1144）；WS 三通道全对上；信封全覆盖（error_handler.py:485 恒 200 信封 vs api.ts:181-215）；视频/训练/模型状态机三组枚举全对齐；19 路由模块全注册零冲突；内核栈依赖零缺失；dist 新鲜无需重建。

## 十二、三轮合并成总优先级（更新）

| 级 | 事项 |
|----|------|
| P0 | 打包线六件路径修复（§八）——不修则下次出包必炸 |
| P0 | 崩溃簇布控（§一/§七-A） |
| P1 | 模型「彻底删除」404 死控件（前端删钮或后端补路由，改后重建 dist） |
| P1 | h3_chain_engine:330 import 修正（一行） |
| P1 | 备份自动化 + feature_lock 竞态 + uiPrefs 超时（§七-B/C） |
| P2 | 双插件体系归一决策（基类/读取器/事件总线/记忆格式四选一或立桥）；cutemamen_pkgs 迁 data/ + 测试改 tmp dir |
| P2 | 「后端就绪前端没接」清单排期（style 控制件优先——训练不可取消是用户痛点） |
| P3 | 大扫除（backend/ 残留、plugin/README、pre-commit 路径、mypy 基线复核、origin 推送） |
