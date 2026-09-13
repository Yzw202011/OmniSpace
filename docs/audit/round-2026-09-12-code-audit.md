# 代码审计 round-2026-09-12（全身审计）

> **状态：审计完成，全部发现已并入同日《全量技术改造与升级方案 v2》批0/批2/批3，修复待拍板。**
> 方法：机械闸门实跑（ruff / mypy / tsc / pytest 722 项）+ 安全面快查 + 两路缺陷狩猎（近期变更区 + 全局病灶类）+ 三路架构侦察（后端/前端/资产盘）+ 对最重发现的源码逐行复核。零基点口径：一切结论来自代码与磁盘，不引用文档作为行为依据。
> 历史轮：round-2026-08-28（1P0/27P1/137P2）、round-2026-08-29（竞品对齐）、round-2026-09-10（P1×6 已修入库）。

## 结论摘要

**机械闸门全绿，无 P0，代码级 P1 为零**；但揪出 **P2×5**（两处集中在 09-12 刚上线的对话排队链，一处违反前端吞错铁律）、**P3×10**，以及 **功能/资产级 P1×2**（导播台断链、wan21 空账）与 43G 重复权重等资产问题。近 88 笔提交（09-10 以来）整体质量良好，唯一高危聚集点是新落的排队收尾时序。

## 一、机械闸门（全部实跑）

| 闸门 | 结果 | 证据 |
|------|------|------|
| ruff（backend/launcher/scripts） | ✅ All checks passed | tools/ruff/ruff.exe 0.16.3 |
| mypy 第4闸 | ✅ 新增 0；存量 231 条冻结基线（较 09-04 立闸时 234 净 -3） | tools/run_mypy.py 全量跑 |
| tsc --noEmit（前端 strict） | ✅ 0 错误 | node v20.20.2 + ts 5.6 |
| 前端静默吞错扫描 `.catch(()=>{})` | ✅ 0 处 | grep frontend/src |
| pytest 全量 | 722 collected，结果见 §七回填 | tools/run_tests.py |
| ESLint | 配置在位（no-explicit-any=error 等），本轮未单独实跑 | eslint.config.js |

## 二、安全面快查（干净，附两个加固注意项）

- `shell=True`：0 处。`yaml.load(`：0 处。`eval/exec`：仅 torch `.eval()` 模型求值模式（5 处误报，非内建 eval）。前端 `.catch(()=>{})`：0 处。
- SQL：业务查询全参数化；仅 `data/database.py:459-618` 的 PRAGMA/ALTER TABLE DDL 用内部常量插值（非用户输入面），可接受。
- **P3 加固注意**：① `data/cache.py:82,191` `pickle.loads` 用于 L2 缓存反序列化（内部数据，建议换 JSON 或加来源校验）；② `lora_training_service.py:1592` `torch.load` 未带 `weights_only=True`（加载自训产物，低风险，建议显式声明）。

## 三、代码缺陷（P2×4 + P3×9，全部已源码复核）

### P2（建议尽快修，均给出修法）

| # | 位置 | 问题 | 影响 | 修法 |
|---|------|------|------|------|
| P2-1 | api/dialog.py:1890-1926 | **WS 排队位次登记无 try/finally**——任务取消（CancelledError）或 acquire_or_raise 抛非 ApiError 时 `_DIALOG_LOCK_WAITERS` 条目永不清理 | 位次虚高并持续泄漏（SSE 路径 :825 有 finally，WS 路径漏了） | 排队段包 try/finally，finally 里 `_dialog_queue_exit(sid)` |
| P2-2 | api/dialog.py:1130-1140 | **SSE 收尾「落库在放锁之前」**——`_save_message`（同步 SQLite 写）抛异常（磁盘满/库锁）则 `lock.release("dialog")` 被跳过，且 dialog_send 已置 lock_handed_off 不再兜底 | dialog 功能锁死到重启，后续所有对话请求秒拒 | finally 内先 `await lock.release()`（或 try/except 包落库），落库失败降级为事件日志不阻断放锁 |
| P2-3 | services/inference/comfy_proc.py:291 | **重 spawn/失败路径日志句柄泄漏**——`self._log_fp = open(...)` 直接覆盖旧句柄，无前置 close | fd 泄漏积累（进程长寿） | 赋值前判空 close；或改用统一 ` _open_log()` 帮助函数 |
| P2-4 | api/manga/comic.py:458（同模式 comic_asset.py:810/1084/1169） | **上传先全量读入内存、后验大小**——`await file.read()` 无上限参数，超限文件照样整体进 RAM | 恶意/误传大文件可打爆内存（对照正确写法 knowledge.py:228 限量读） | 改为分块限量读（照 knowledge.py:228 的 `_MAX+1` 模式） |
| P2-5 | frontend/src/components/UpgradeSection.tsx:64 | **`.catch(() => undefined)` 静默吞错**，违反「禁止静默吞错」三分法铁律（errors.test.ts 静态扫描门禁因此红） | 前端 vitest 套件挂红；升级状态初始轮询失败无任何上报 | 改走 `reportBgError`（一行）；若确认可容忍需改扫描白名单而非吞错 |

### P3（列账，随批3 清）

| # | 位置 | 问题 |
|---|------|------|
| P3-1 | api/upgrade.py:151-219 | upgrade_start 无升级自身互斥闸，可双开 updater 并发写安装目录（`_busy_reason` 不含升级态） |
| P3-2 | services/inference/comfy_proc.py:74-76 | Sage 门控配置异常时 fail-open（`except: return True`），与「关不掉」语义相反 ⚠️建议改 fail-close |
| P3-3 | services/inference/comfy_proc.py:278-303 | spawn 全程持锁做重活（psutil 扫描+锁内 sleep 1s+磁盘挂载），阻塞同锁忙碌标记 |
| P3-4 | services/inference/comfy_proc.py:469-484 | 空闲杀进程判定与 mark_busy 之间锁外窗口，理论竞态 ⚠️ |
| P3-5 | api/dialog.py:212-231 | 排队位次表以 sid 为键，同会话并发消息互相覆盖/误清 |
| P3-6 | services/inference/voice_engine.py:527 | ASR 换模型直接覆盖旧 pipeline，切换瞬间显存双份驻留 |
| P3-7 | api/knowledge.py:245 | async 路由内联同步解析 pdf/docx，事件循环停摆（应走 run_blocking） |
| P3-8 | api/dialog.py:158 | async 路由内联 PIL 解码+LANCZOS 缩略，大图卡事件循环 |
| P3-9 | api/system.py:1286-1287 | 备份导出 tarfile 的 fileobj 无显式 close（mode="w:gz" 不负责关 fileobj），异常路径半刷写风险 |

**扫描零发现声明（诚实口径）**：except 吞错抽查均为探测/empty_cache 兜底且有注释；novel 轮询 sleep 均经 run_blocking 下沉；page_guard urlopen 实为 to_thread 包装（初判误报）；前端裸 Promise 调用逐个核对均有内部 try/catch。

## 四、功能/资产级问题（P1×2 + 资产项）

| 级 | 问题 | 证据 |
|---|------|------|
| P1 | **导播台断链**：h3_engine.py:544-549 引用 TheodoreDirector_Project/SelectShot/H3Adapter，全 ComfyUI 树无定义，走该模式必失败 | 全树 grep 无提供方 |
| P1 | **wan21-i2v-1.3b 空账**：manifest wired:true、声明 3.2G，目录 0 字节，选中即失败 | models_manifest.json + du |
| P2 | **43GB 字节级重复权重**（nvfp4 12.5G / klein-9b-fp8 9.4G / ref2va 21G 两边各一份；scripts/comfy_link 挂接未生效） | 两边同大小实测 |
| P2 | 模型账实漂移多条（klein-4b 17.5↔15、qwen-image 22↔29、H3 42.7↔52） | du 对账表 |
| P3 | 旧资产：LTX 24G、ToonCrafter 烂尾 7.2G（残缺 ckpt 3.2G+4.1G）、SD15 5.2G、AnimateLCM 1.7G、data/backups 682M、data 根残留 | du 实测 |

## 五、架构债汇总（随方案 v2 批3 处理）

绘画新旧双栈（旧 diffusers 栈 UI 已被漫画页顶掉但 draw 链悬空）· video_engine 旁路化 + docstring 失实 · vision_tools 前端零调用（TripoSR/YOLO 疑孤儿，depth/segment 被 comic 链在用）· learn/learning 双轨 · usePaintStore(633行)/paintApi 悬空 · 15 张服务层自建表绕开迁移体系 · comic_gen 空 router · @hooks 空指别名 · /v1 代理残留 · config redis/celery 占位节 · sqlalchemy 零 import · 三处过时注释/docstring。

## 六、与历史轮对账

- 09-10 轮 P1×6 已全部修复入库（本轮回访未发现回归）。
- 近 88 笔提交（09-10 以来：SageAttention 上线、对话排队、升级机制批2~4、ASR/comic/novel 修复、体积优化 A4~A6）**未发现 P0/P1 级代码回归**——唯一高危聚集点是 09-12 新落的对话排队收尾时序（P2-1/P2-2），量级可控、修法明确。

## 七、测试实跑回填

**后端 pytest（tools/run_tests.py，722 collected）**
- 第一遍：**713 passed / 1 failed / 8 skipped**（251.53s）。唯一失败 = `unit/test_dialog_queue_position.py::test_position_ordering_fifo_by_arrival`。
- 失败定位：该文件单跑 **2 passed（0.36s）**；第二遍全量**全部通过**。判定：**非稳定复现的全局态污染型偶发**——排队位次表 `_DIALOG_LOCK_WAITERS` 为模块级 dict 且清理无 finally 兜底（即 P2-1/P3-5 同一病灶），其他测试残留状态即可打翻 FIFO 断言。**此偶发即 P2-1 的活体佐证**，不另立缺陷。
- 8 skipped 均为显式门控（vLLM 压力/GPU E2E 需环境变量、GGUF 权重未就位），口径诚实。

**前端 vitest（便携 node 直调补跑；run_tests.py 因 PATH 无 npm 跳过 L2）**
- 正确 cwd 下 **70 passed / 1 failed（71 项）**：唯一失败 = 静态扫描门禁抓到 `UpgradeSection.tsx:64` 静默吞错（已列 **P2-5**）。首次跨目录跑出的另 2 失败为工作目录假阳性，已排除。

**汇总：后端 722 项实质全绿（1 偶发已定性）、前端 71 项 1 红（P2-5）、ruff/tsc/mypy 全绿。**
