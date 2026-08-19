# OmniSpace AI 工程整改任务待办清单

> 生成：2026-08-19 ｜ 来源：《项目健康度审计报告》（docs/project-sdlc-audit/）
> 对照发现编号：P01-P28（完整定义见审计报告附录）
>
> **使用规则**：
> 1. 每完成一项勾选 `[x]`，并随代码提交（提交信息引用任务编号，如 `TASK-P0-01`）
> 2. 任务与需求追踪矩阵（requirements-traceability.md）联动：涉及需求状态的，同步改矩阵对应行
> 3. 完成标准是验收门槛，不满足则不算完成
> 4. 工时为单人净工时估算，含自测不含返工

---

## P0 终结不可逆风险（本周内，合计约 2.5 天）

> 特征：每项都可能在任意一天造成不可逆损失。全部可独立完成，无外部依赖。

- [x] **P0-01 合并双真源交付**（对应 P20）
  - 动作：写 `tools/build_rc.py`，从 e:\OmniSpace 生成 E:\RC1002（robocopy 排除 .git / data / logs / __pycache__）；此后 RC 目录只允许由脚本产出，禁止手工改动
  - 完成标准：脚本一键执行成功；RC 目录与源码树 diff 为零（排除清单内项）；矩阵 E-07 状态改 ✅
  - 工时：半天
  - **完成记录（2026-08-19）**：脚本落地并实测——排除清单用绝对路径防误杀 backend/data（.gitignore 同款教训）；全量同步 198.86GB（22,678 文件）2 分 11 秒；robocopy /L 列表模式校验零差异；矩阵 E-07 已改 ✅。收尾增量同步待全部代码任务结束后重跑
  - **勘误（2026-08-19，随 P0-06）**：当时据审计 P23 错误前提把 pydeps 加入排除清单，致 RC1002 缺 fastapi/numpy 等而无法启动（零差异校验未拦截——排除清单对比对两侧同时生效，属校验盲区）。已移出排除清单并重同步（1.36GB），RC 恢复可启动。教训：排除清单每项都必须有独立证据，"审计说它是垃圾"不算

- [x] **P0-02 三套依赖声明归一**（对应 P10）
  - 动作：删除 `backend/requirements.txt`（陈旧 v2.1，声明的 sqlalchemy 实际未使用）；根 `requirements.txt` 顶部声明"精确版本以 requirements-lock.txt 为准"
  - 完成标准：仓库内只剩两份依赖文件且权威关系明确；`runtime/py310/python.exe -c "import backend.main"` 启动自检通过
  - 工时：10 分钟

- [x] **P0-03 上传端点类型校验**（对应 P27）
  - 动作：三个端点补后缀白名单 + 文件头魔数嗅验——`api/knowledge.py:208-225`、`api/learn.py:272-289`、`api/style.py:49-72`；白名单集中定义一处（如 backend/security.py 或各文件常量）
  - 完成标准：上传 .exe 改名 .jsonl 被 400 拒绝；正常 .jsonl/.md/.txt/.pdf 上传不受影响；补 1 条冒烟测试
  - 工时：半天
  - **完成记录（2026-08-19，3eaa837）**：新增 middleware/upload_guard.py（后缀白名单 + PE/ELF/Mach-O 黑名单 + 魔数嗅验 + UTF-16 BOM 检测），三端点接线；拒绝走 ADR-01 统一失败信封（项目无 HTTP 4xx 约定，"400 拒绝"按信封语义执行）；16 单元 + 4 API 测试全过

- [x] **P0-04 RTM 台账刷新 + 模型接线决策**（对应 P04、P21）
  - 动作：
    1. 逐目录实测 models/ 尺寸回填矩阵 M 系列（当前台账尺寸已漂移）
    2. 修正三处错误状态：M-01（qwen3-vl-8b 已入高档候选）、M-03（LTX-2 管线已接线）、F-10（备注过时）
    3. 65.9GB 未接线模型逐个给出结论：`接线排期` / `删除`，写进矩阵备注列
  - 完成标准：矩阵 44 条与代码实况零出入；每个 🟦 条目备注列有明确处置决策（含"下季度再定"也算决策）
  - 工时：半天
  - **完成记录（2026-08-19）**：实测刷新规模超出原计划——除原定 3 处外另修正 M-10/M-11（llama-cpp-python 0.3.34 实为可用，原"空壳"结论有误）、M-13/M-14/M-15（AnimateLCM 分支与 vision_tools 四端点早已接线）共 8 处，M-04 转 ⚪（资产已删）；🟦 剩 6 条全部落决策（接线排期 4 + 待用户确认删除 2，合计 7.79GB）；F-10、E-07 一并上调 ✅；另发现 sd15 冗余副本 ~22GB 磁盘清理候选

- [x] **P0-05 非回环绑定硬拒绝**（对应 P25 缓解升级）
  - 动作：`launcher/launcher.py` 启动时校验 host，非 127.0.0.1/localhost 直接拒绝拉起（替代 main.py 现有的仅日志告警）；保留环境变量显式豁免开关（如 OMNISPACE_ALLOW_LAN=1，豁免时打印大字告警）
  - 完成标准：host 配错时 launcher 报错退出且错误信息可读；豁免开关可用；补 1 条单元测试
  - 工时：1 小时
  - **完成记录（2026-08-19）**：双层防线——①闸门前移到 `backend/config.py` 导入期（原 main.py lifespan 告警发生在 socket 绑定后，为时已晚），任何方式启动均无法绕过；②launcher initialize() 提前拦截避免拉起注定失败的后端。注意：launcher 的 backend_host 为硬编码默认值从不读 config.yaml，真实配置错误的拦截靠①（后端进程导入即崩，launcher 报启动失败）。5 条单元测试 + 端到端三场景实测（0.0.0.0 拒绝/豁免放行/127.0.0.1 正常）全过；总测试数 41

- [x] **P0-06 清退 pydeps 历史残留**（对应 P23）
  - 动作：确认 PythonPath 解析不再依赖 pydeps 后整目录删除（lock 文件已记录 37 个空壳包隐患）；删除后跑全量冒烟测试
  - 完成标准：pydeps/ 不存在；`pytest -m smoke` 全过；对话/绘画/视频三条链路手工各验证一次
  - 工时：半天（含验证）
  - **完成记录（2026-08-19，前提证伪 + 定向清退）**：审计 P23"pydeps=37 空壳残留"结论不成立——实测 pydeps 承载 161 个真实包（fastapi/numpy/scipy/diffusers/chromadb/modelscope/pytest 等 156 个仅存于此），与 runtime/site-packages 构成双站点互补架构（._pth 挂载，pydeps 优先）；删除整个目录 = 后端当场瘫痪。原"37 空壳"系 pip 分发名 ≠ import 名的系统性误判（pillow→PIL、scikit-learn→sklearn 等），仅 torch/sympy/tokenizers 3 条属实。已执行定向清退：删 pydeps/torch（40MB 死目录+命名空间陷阱）、sympy、tokenizers、tests（遮蔽根 tests/ 的第三方测试套件）及 4 个幽灵 dist-info；实测 12 关键包 import + backend.main + 41 测试全过；连带修复 P0-01 遗留缺陷——build_rc.py 曾据错误前提排除 pydeps 致 RC1002 后端无法启动，已移出排除清单并重同步（1.36GB，零差异校验过）。requirements-lock.txt 头部勘误同步

---

## P1 建立事故免疫体系（2-4 周内穿插完成，合计约 6 天）

> 特征：测试与门禁每多存在一天，就多拦截一批未来事故。各项可独立穿插进日常开发。

- [x] **P1-01 引擎层纯逻辑测试**（对应 P17）
  - 动作：不碰 GPU，直测可脱离权重的纯函数——对话候选路由表（dialog_engine.py:49-54）、显存估算 `_dir_load_bytes_fp16`、模型路径解析（含 modelscope 嵌套快照结构）、LTX 参数校验（分辨率整除 32、帧数 ≡1 mod 8）；参考 test_knowledge_pipeline.py 的注入手法
  - 完成标准：新增 ≥ 12 个测试函数全部通过；`pytest` 总数从 16 升至 28+
  - 工时：2 天
  - **完成记录（2026-08-19）**：新增 `backend/tests/unit/test_engine_logic.py` 共 43 个测试函数全过（含 7 组参数化不变式），总数 41→84。覆盖：对话候选 tier 路由 4（含 12.0 边界含/11.9 不触发）、硬件档位表 6（含 5070Ti 先于 5070 的表序依赖、未登记型号按显存保守降档）、模型路径解析 4（扁平/modelscope 嵌套/.incomplete 残留/缺失）、后端判定+显存估算+目录发现 7、`_dir_load_bytes_fp16` 7（F32 减半/F16·BF16 原值/混合保守/同名 bin 跳过/损坏头保守/递归子目录）、LTX 校验 6、候选表一致性 2。附带重构：video_engine.py 内联 LTX 约束抽取为纯函数 `_ltx_align_params`（行为不变，生成路径改调它）

- [x] **P1-02 pre-commit 钩子**（对应 P18）
  - 动作：写 `.git/hooks/pre-commit`（或 pre-commit 框架配置），提交前跑 `pytest -m smoke` + `compileall` 语法基线；ruff 装好后追加 check
  - 完成标准：故意注入语法错误的提交被拦截；正常提交 < 60 秒完成
  - 工时：半天
  - **完成记录（2026-08-19，eb54dee）**：钩子置于版本库内 `.githooks/pre-commit` 并以 `git config core.hooksPath .githooks` 激活（钩子本身随仓库版本化，换机只需重跑该 config 一行，用法写在钩子头部注释）。实测：注入语法错误的提交 1 秒被拦截；正常提交（compileall 全量 + 17 冒烟）15 秒通过。ruff 接入后追加 check 的挂点已预留（P1-03 落地后于钩子 [2/3] 步插入）

- [x] **P1-03 lint 工具链激活**（对应 P14、P19）
  - 动作：
    1. 前端：`npm install -D @eslint/js eslint`，package.json 加 `"lint": "eslint src/"`（node 运行时已就绪于 runtime/node20）
    2. 后端：下载 ruff 单文件二进制到 `tools/ruff/ruff.exe`（ruff.toml 注释已引用该路径）
  - 完成标准：`npm run lint` 与 `tools/ruff/ruff.exe check backend/` 均可执行；存量违规允许 --baseline 化但命令退出码可判
  - 工时：1 小时
  - **完成记录（2026-08-19）**：
    - 前端：@eslint/js + typescript-eslint + eslint-plugin-react-hooks 实装（eslint 9 flat config）；typescript-eslint 仅作解析器（TS 语法支持），语义检查仍由 tsc --noEmit 承担；react-hooks 只开 rules-of-hooks/exhaustive-deps 两条经典规则（v7 编译器系规则对存量代码误报多）。`npm run lint` 0 错误 10 警告（全部 no-console，降为可接受底噪）；`--fix` 清掉 12 处冗余 disable 指令与 prefer-const；tsc --noEmit 同步通过
    - 后端：ruff 0.16.3 实装于 tools/ruff/（28MB 二进制不入库，.gitignore 排除，ruff.toml 头注释记录获取命令）。存量 748 项全清零（653 项安全自动修复 + 37 处 B904 批量补 `from exc`（tokenize 定位语句边界的一次性脚本）+ 46 项逐项手工修复）；ruff check . 全绿，compileall + 84 全量测试通过
    - 强化：pre-commit 钩子升级三步门（compileall → ruff check → smoke），静态检查由"可跑"变"强制"，新增违规在提交时拦截
    - 修复中发现 2 个真实缺陷：decision.py `TaskDispatcher` 7 处注解未定义（靠 `from __future__ import annotations` 侥幸未炸，补 TYPE_CHECKING 导入）；startup_check.py lz4 可用性探测改 find_spec（不再为探测而真实 import）

- [x] **P1-04 前端 vitest 落地**（对应 P12）
  - 动作：建 vitest.config.ts；先写三个最痛的测试——useMangaStore 状态流转、mangaApi 的 Zod 响应解析、VIDEO_STATUS_LABELS 与后端枚举一致性
  - 完成标准：`npm run test` 可执行且 3 个测试文件全过；package.json test 脚本从声明变为真实
  - 工时：2 天
  - **完成记录（2026-08-19）**：
    - vitest 2.1.8 实配落地：vitest.config.ts（别名与 vite/tsconfig 同步，node 环境，fileParallelism=false 防 store 交叉污染）；package.json test 脚本 `vitest` → `vitest run`（CI 单次退出），另留 test:watch
    - 三个测试文件 34 用例全绿：useMangaStore.test.ts（13，项目/分镜/视频全链路状态流转：openProject/autoSplit 乐观追加/reorderRows 乐观重排+失败回滚/generateVideo 全流程 generating→done 收敛/功能互斥拒绝/取消/轮询失败收敛，fake timers 驱动真实 2s 轮询节拍）；mangaApi.test.ts（14，mock 请求层直测 Zod 解析：合法/passthrough 宽容/缺字段/类型错/枚举漂移/progress 越界全维度）；videoStatus.test.ts（7，**直读 backend/api/manga.py 源码文本正则提取 status 字面量，与前端 VIDEO_TASK_STATUSES/标签/Zod schema 三方双向核对**，后端加状态前端没跟上时立即红）
    - 顺带完成 P2-07 首步：VIDEO_STATUS_LABELS 从 MangaWorkspace.tsx 抽到 src/constants/videoStatus.ts 单一真源（store 类型 VideoTaskStatus 同源引用）
    - **测试逼出 1 个真实缺陷并修复**：视频轮询连续失败 3 次放弃后，任务永久停留 generating → video_gen 功能锁与 videoGenerating 永不释放（挂死，只能刷新页面）。修复：放弃轮询时任务收敛 error 终态、行状态同步 error、failTask + 释放功能锁，回归用例锁死该行为
    - 验证：npm run test 34/34 过；tsc --noEmit 过（补装 @types/node，测试文件用 node:fs 读后端源码）；npm run lint 0 错误；npm run build 生产构建过（常量抽取不影响打包）

- [x] **P1-05 吞错治理第一批**（对应 P07、P11）
  - 动作：
    1. `engines/vram_manager.py:56-226` 四处 `except: pass` 改为显式降级（log.warning + 状态标记）
    2. `api/hardware.py:171-192` 假数据回填改为 unknown 标记，前端硬件面板对 unknown 禁用渲染
  - 完成标准：显存守门人无静默失败路径；前端不再渲染编造的 35.0%/8192MB 读数
  - 工时：1 天
  - 完成记录（2026-08-19）：
    - vram_manager 四处 except pass 全部治理：`_degraded_probes` 字典记录探测失败（vram_total_mb / empty_cache / memory_allocated / mem_get_info 四键），log.warning 带后果说明（如"显存门禁将退化为不可用""物理显存可能未真正回收"），get_usage() 新增 `degraded_probes` 字段向 API 层暴露——上层可判断读数是真实探测还是纯记账退化值
    - hardware.py 假数据清零：实时遥测失败路径 35.0%/8192MB/24576MB/52°C/"Mock CPU"/32GB/512GB/80.5% 等编造读数全部改为 `None + available=False`；静态画像兜底改"未知（硬件探测失败）"+ power="unknown"；synergy 端点对 None 读数按 0 收敛防 TypeError
    - 前端诚实门控：HardwareRealtime 类型 5 字段改 `| null`；normalizeRealtime 保留 null 不强转；消费方核对——BottomStatusBar/RightPanel pctText 原生支持 null→'--'，StylePage formatVRAM/formatPercent null 安全，**ModelManager VRAM 进度条修掉探测失败显示 0% 的编造下界**（改 null→'--' 且不触发 95% 强制线告警）
    - 新增 `tests/unit/test_telemetry_honesty.py` 8 用例锁定契约：psutil 不可用/GPU 探测失败/整链失败三路径必须 available=False+None（断言假读数绝迹）；vram 总显存/用量/空闲探测失败必须进 degraded_probes；无 torch 纯记账模式不算降级
    - 验证：后端 92/92 过（84+8 新增）；tsc --noEmit 过；npm run test 34/34 过；npm run lint 0 错误；ruff check 改动文件全过

- [x] **P1-06 异步边界规范固化**（对应 P08）
  - 动作：消灭 dialog.py:313 的"调用方须放线程池"注释契约——被动重推理函数内部自建 `asyncio.to_thread` 或统一走 draw.py 模式的任务调度；同步推理入口收敛为一个装饰器/工具函数
  - 完成标准：grep 确认无"须放线程池"类注释契约残留；所有同步推理调用点结构上不可能误用
  - 工时：1 天
  - 完成记录（2026-08-19）：
    - 新建 `services/offload.py`：唯一同步推理入口 `run_blocking()`（语义= asyncio.to_thread，收敛为可 grep 单点）+ `sync_core` 装饰器（同步核心→自调度 async 函数，漏 await 只得到 coroutine 不会阻塞循环，误用面结构性消失）
    - dialog.py 两处注释契约（`_quick_search_supplement`/`_passive_reinfer`）改 `@sync_core` 自调度，docstring 契约删除；voice_engine.py:809 COM 契约改为结构说明
    - 收敛 41 处调用点到 run_blocking：dialog 5 / voice 2 / vision_tools 4 / models 4 / draw 2 / manga 20 / system 5（manga/system 含 sqlite 批写与归档秒级 IO）——API 层 asyncio.to_thread 直调清零
    - 保留合法例外（非单次阻塞调用）：dialog.py 流式生产者 run_in_executor（生产者-消费者模式）、scheduler 周期 tick 循环、ws_hub 毫秒级遥测读取（offload.py 策略明示）
    - 新增 `tests/unit/test_async_boundary.py` 8 用例锁定：义务性契约短语全后端源码扫描必须为 0；API 层 to_thread 直调必须为 0；run_blocking 真实卸载（线程号验证）+ 异常如实传播；sync_core 协程函数性 + 工作线程执行；dialog 被动补全两助手必须为协程函数
    - 验证：后端 100/100 过（92+8）；ruff 改动 10 文件全过；全部 TestClient 冒烟通过（应用启动无循环导入）

---

## P2 规模与形态升级（1-2 个月按业务节奏排，合计约 10 天）

> 特征：纯改善项，晚做没有代价。禁止跳过 P0/P1 直接开工。

- [x] **P2-01 manga.py 拆分**（对应 P06、P15）
  - 动作：按路由域拆为 storyboard / director / video / voice 四个模块（APIRouter 各自挂载）；同步把 useMangaStore（738 行）按子域切片
  - 完成标准：单文件 < 1000 行；`tsc --noEmit && vite build` 通过；漫剧主流程（建项目→分镜→出图→导出）手工回归通过
  - 工时：3-4 天
  - **完成记录（2026-08-20）**：
    - 后端 manga.py（4521 行 / 95 路由）拆为 `api/manga/` 包 9 模块：common（共享层：内存态兜底/行辅助/引擎单例/出图规格铁律，无路由）/ storyboard（23 路由）/ keyframe（7）/ director（17）/ video（15）/ voice（6）/ comic（8）/ comic_asset（19）/ comic_gen（0 路由，管线层由 comic_asset 调用）；`__init__.py` 聚合 9 个子 router，main.py 经 importlib 挂载零改动。拆分用 AST 一次性脚本（tools/_tmp_split_manga.py，已删）：顶层块解析 → 按路由路径钉死域归属（行号边界对路由组起点不精确）→ 共享名定点迭代提升 common → 跨域导入环检测（环上最小非路由名提升解环）
    - 路由对等性机器验证：拆分前后各 95 路由（path+methods）集合完全相等（FastAPI 0.141 惰性挂载，经 `_IncludedRouter.effective_candidates()` 递归物化枚举）；create_app() 13 模块全注册、全 app 295 路由
    - 前端 useMangaStore（744 行）拆为 `stores/manga/` 六切片：project/rows/asset/keyframe/voice/video + types.ts（切片契约与后端能力边界注释）+ helpers.ts + videoPoller.ts（轮询定时器模块级单例，可见性降频）；useMangaStore.ts 仅作组装点（32 行），对外接口与切片前完全一致，22 个消费方零改动
    - videoStatus.test.ts 同步更新：一致性测试改扫描 `backend/api/manga/` 整包（拆包后状态字面量分散于 common/keyframe/video/comic_asset 四模块，合并提取集合仍为 pending/generating/done/error/cancelled 五值，与拆分前一致）
    - 验证：后端 100/100 过 + 17 冒烟过；ruff check 新包全绿；前端 34/34 过、tsc --noEmit 过、npm run build 生产构建过（3.4s）；各模块行数 max 718（comic_asset）全部 < 1000；临时拆分脚本与路由对等快照已清理

- [x] **P2-02 设计文档补齐**（对应 P03）
  - 动作：三份文档——架构总览（一页分层图+数据流）、API 端点总表（从 api/*.py 头注释抽取）、13 表 ER 说明（从 database.py 提取）
  - 完成标准：三份文档入库 docs/design/；新成员按文档能定位任意端点与表结构，无需通读源码
  - 工时：2 天
  - **完成记录（2026-08-20）**：
    - 三份文档入库 `docs/design/`：architecture-overview.md（156 行：系统拓扑/五层分层/中间件链/引擎与显存协调/调度器/启动时序/漫剧数据流/决策索引）、api-endpoints.md（338 行：270 HTTP + 4 WS 端点全量，按 16 业务域分组）、database-er.md（204 行：30 张表全景 ER 图 + 逐表字段说明 + 迁移与加密机制）
    - API 表数据源升级：不从头注释手工誊抄，而是 create_app() 路由表机器枚举（经 `_IncludedRouter.effective_candidates()` 递归物化，FastAPI 0.141 惰性挂载），270 端点零遗漏零笔误；每行含处理函数名，按名可直接定位源码
    - ER 覆盖修正：规格"13 张业务表"实为 30 张——database.py _SCHEMA 17 张 + 8 个服务层自建表（behavior_logs/paint_history/knowledge_meta/style_tasks/model_benchmarks/api_keys/learning 四表）+ FTS5 + 图谱两表，全部纳入文档；发现 paint_history 竟建在 paint_engine.py、api_keys 建在 api/system.py 等分散事实
    - 表格中发现并如实标注的路由细节：`/director/text-to-3d` 只注册顶层路径（无 /manga 前缀对）；23 个顶层旧别名仅在约定节说明不逐行列出

- [x] **P2-03 README + 部署/故障排查手册**（对应 P22）
  - 动作：根目录 README.md（是什么/怎么启动/目录结构）；docs/ 下新增部署手册（含模型资产配置表）与故障排查手册（OOM/端口冲突/数据库版本冲突/模型探测失败四大高频故障）
  - 完成标准：换机冷启动只靠文档可完成；四大故障各有一页处置步骤
  - 工时：1 天
  - **完成记录（2026-08-20）**：
    - 根 README.md（项目简介/快速启动/开发工作流/目录结构/文档索引/运维命令）；docs/deployment-manual.md（硬件要求/环境装配/24 目录模型资产配置表/显存路由表/首次启动验证清单/RC 交付）；docs/troubleshooting.md（通用排查入口 + 四大故障各一页：症状/根因/诊断/处置/预防 + 20xxx 错误码速查）
    - 全部事实从源码实测核对：launcher 端口默认 8765（argparse 覆盖 config 的 5800）、磁盘门槛 20GB、DB 版本守护 RuntimeError 原文、20013/20014 错误码、_MODEL_PATH_HINTS 嵌套快照、模型接线状态对齐 RTM 矩阵 M-01~M-16

- [x] **P2-04 需求基线入库**（对应 P01、P02）
  - 动作：把《文档E》《文档B》中仍有效的需求抽取为仓库内规格章节（挂 RTM 附录）；docs/ 加 INDEX.md 声明各文档时效性，归档被覆盖的历史版本（三份模型推荐文档收敛为一份）
  - 完成标准：仓库内可独立校验需求-矩阵对应关系；INDEX.md 能回答"哪份文档代表现状"
  - 工时：1 天
  - **完成记录（2026-08-20）**：
    - `docs/requirements-baseline.md` 入库（RTM 附录 A）：六大模块功能清单 + 五大引擎 + 协调规则（互斥/加载优先级/后台优先级/知识反哺）+ 38 项核心要求全量 ↔ RTM 映射表（✅16 / 🟨10 / 🟦1 / ❌10）；每条含 RTM ID 反向指针，未入矩阵项显式标「—」
    - `docs/INDEX.md` 建立：全部 docs/ 条目四态分类（现行/记录/参考/归档）+ 仓库外源文档时效定位 + 按场景找文档速查表
    - 模型推荐三文档收敛：model-deployment-plan.md 确立唯一权威版；based-on-doc / final 两份 git mv 至 docs/archive/ 并加归档横幅（保留历史可追溯）
    - RTM 升 v1.2：头部声明基线链接，维护规则与附录 A 挂接；38 项映射发现未追踪缺口 1 处（#29 Agent 工具调用 ❌ 无 Function Calling 实现）

- [x] **P2-05 crypto.py 接入 DB 层**（对应 P26）
  - 动作：dialog_messages.content 落地 AES-256-GCM 字段级加密（crypto.py 已实现，接 database.py 读写路径）；存量数据一次性迁移
  - 完成标准：库文件中文本字段不可明文直读；矩阵 A-02 状态改 ✅；迁移前后对话记录内容零丢失
  - 工时：2 天
  - **完成记录（2026-08-20）**：
    - schema v3（纯数据迁移组 `_DATA_MIGRATIONS`）：存量明文加密（dialog_messages.content + behavior_logs.content/context/before/after 四字段），前缀检测式幂等（enc:v1: 跳过），加密后解密校验失败则保留明文并告警（零丢失优先）
    - 读写路径接线：database.py 插入路径加密（line 403）、行读取/历史读取解密（line 89/435）；behavior_service.py 写入侧同步接线
    - 生产库迁移实测：211 条对话 + 815 条行为日志全量加密，SQL 层明文残留 0/0，解密回读 5/5 通过
    - 字节级泄漏排查：初次终验发现孤立明文页（迁移先于 VACUUM 代码执行）——迁移逻辑内联 VACUUM 重建库文件后补跑，perf-test 孤立片段 4/4 清除、freelist 归零、WAL checkpoint 清零；残留片段全部可归因于范围外合法明文列（dialog_sessions.title 等，A-02 范围=消息正文与行为日志）
    - 迁移前明文备份 bak-p205 于三重验证通过后删除（防明文副本抵消加密成果）
    - 测试：test_v3_migration_encrypts_legacy_plaintext（加密+可逆+幂等）+ test_v3_migration_idempotent_rerun 入 smoke/schema 套件；全量 102 项测试通过

- [x] **P2-06 Tauri 壳正式决策**（对应 P05）
  - 动作：二选一并落档——排期落地（建 src-tauri 骨架）或矩阵标记"裁剪，理由：浏览器+launcher 形态已满足单机交付"
  - 完成标准：矩阵 A-01 不再是悬置状态；决策记录进 ADR
  - 工时：决策 1 小时 / 落地另计
  - **完成记录（2026-08-20）**：
    - `docs/ADR-002-tauri-shell-decision.md` 入库：裁决 = 裁剪，浏览器+launcher 升格为正式交付形态（非降级）；四条理由（单机闭环/壳层零增益/Rust 工具链违反零系统依赖铁律/#38 安装包前提失效）+ 放弃收益诚实记录 + 四条重启条件（可逆门）
    - RTM A-01：❌ → ⚪ 合理化豁免（v1.4）；基线 #38 同步豁免，统计 ✅17/🟨10/🟦1/⚪1/❌8
    - docs/INDEX.md 登记为新现行 ADR

- [ ] **P2-07 状态文案集中化**（对应 P13）
  - 动作：VIDEO_STATUS_LABELS / SAVE_STATUS_LABELS 等枚举标签抽取到常量模块；文案 key 与后端枚举值类型绑定
  - 完成标准：组件内不再散落状态标签字面量；一致性纳入 P1-04 的 vitest 用例
  - 工时：1 天

- [ ] **P2-08 前端错误呈现统一**（对应 P16）
  - 动作：约定组件层错误处置策略（toast/console/静默三分法），把 MangaWorkspace.tsx:153 的 `catch(() => undefined)` 类静默吞错改为可见反馈
  - 完成标准：保存失败类操作用户必有感知；错误处置策略写入前端 README 或 ADR
  - 工时：1 天

- [ ] **P2-09 双测试入口整合**（对应 P24）
  - 动作：根目录 tests/（流程编排脚本）与 backend/tests/ 关系明确化——迁移或注明定位，pytest.ini testpaths 收口
  - 完成标准：一处命令跑全量测试；入口唯一
  - 工时：半天

---

## 覆盖核对表（28 项发现 → 任务映射）

| 发现 | 任务 | 发现 | 任务 | 发现 | 任务 |
|------|------|------|------|------|------|
| P01 需求基线外置 | P2-04 | P11 假数据回填 | P1-05 | P21 模型资产未接线 | P0-04 |
| P02 文档冗余 | P2-04 | P12 前端零测试 | P1-04 | P22 用户文档缺失 | P2-03 |
| P03 设计文档缺失 | P2-02 | P13 中文硬编码 | P2-07 | P23 双包目录 | P0-06 |
| P04 RTM 漂移 | P0-04 | P14 ESLint 死配置 | P1-03 | P24 双测试入口 | P2-09 |
| P05 Tauri 悬置 | P2-06 | P15 巨型文件/store | P2-01 | P25 API 无认证 | P0-05 + P2-06 决策 |
| P06 manga.py 上帝文件 | P2-01 | P16 错误双轨制 | P2-08 | P26 SQLite 明文 | P2-05 |
| P07 裸异常吞错 | P1-05 | P17 测试覆盖失衡 | P1-01 | P27 上传无校验 | P0-03 |
| P08 事件循环阻塞 | P1-06 | P18 无 CI/钩子 | P1-02 | P28 无监控面板 | 观察项，不立任务 |
| P09 全局单例耦合 | 随 P2-01 顺带治理 | P19 ruff 缺失 | P1-03 | | |
| P10 三套依赖声明 | P0-02 | P20 双真源交付 | P0-01 | | |

> P09（125 处单例）不单独立任务：大规模改造收益低于风险，随 manga.py 拆分时对新模块实施构造注入即可；P28 在单机定位下属合理裁剪，维持观察。

---

## 进度统计

| 阶段 | 任务数 | 总工时 | 已完成 |
|------|--------|--------|--------|
| P0 终结不可逆风险 | 6 | ~2.5 天 | 0 |
| P1 建立事故免疫体系 | 6 | ~6 天 | 0 |
| P2 规模与形态升级 | 9 | ~10 天 | 0 |
| **合计** | **21** | **~18.5 天** | **0** |
