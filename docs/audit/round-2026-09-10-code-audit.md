# OmniSpace 增量代码审计报告（2026-09-10）

> 审计方式：**增量审计**。以上轮全库逐行审计（round-2026-08-28）之后的全部变更为主要扫描面
> （69 commits，backend+frontend+launcher 307 文件，+65,133/-11,529 行），叠加四道既有闸门复跑
> 与上轮 25 条 P0/P1 修复状态逐条复核。三路并行扫描（后端安全模式 / 上轮复核 / 前端禁项），
> 全部 P0/P1 级关键条目由主审亲读源码二次确认。
> 证据规则：每条发现含 `文件:行号` + 原文引用；标注【亲核】= 主审计者二次读取源码确认；
> 其余为扫描代理报告（原文引用逐条可查）。
> 分级：P0 = 显存/数据破坏级；P1 = 真实可触发的高危缺陷；P2 = 值得修但影响有限；P3 = 随主问题消解。
> **本轮无 P0。本报告只审不改——修复须经用户拍板。**

---

## 0. 覆盖率

| 区域 | 规模 | 覆盖方式 |
|---|---|---|
| backend（08-28 后变更） | 193 文件（约 75 个非测试） | 8 类危险模式全量 grep + 候选逐条回读上下文 |
| 上轮 P0/P1 复核 | 25 条目 | 逐条对现行代码核验（按符号重定位，不信旧行号） |
| frontend/src（08-28 后变更） | ~100 文件 | 禁项全量扫描（any/XSS/hex/storage/WS）+ 重点文件精读 |
| 工作区未提交改动 | 9 文件 diff + 3 未跟踪项 | 全 diff 逐行审 |
| 既有闸门 | ruff / dir_audit / mypy / smoke | 全部实跑复验 |
| 未覆盖 | tests/ 根目录活编排脚本（仅审与未提交改动相关者）、ComfyUI 便携版内部、tools/musubi-tuner（第三方训练库）、两份未跟踪方案文档（非代码） | — |

---

## 1. 闸门状态（2026-09-10 实跑）

| 闸门 | 结果 | 根因 |
|---|---|---|
| ruff | ✅ All checks passed | — |
| scripts/dir_audit.py | ✅ 干净（musubi-tuner 已入编译豁免） | — |
| mypy 第 4 闸 | ✗ 基线外新增 1 条 | `tests/unit/eval/uno_smoke.py`（未提交新文件）→ P2-1 |
| pytest -m smoke | ✗ 1 failed / 92 passed | 登记表幽灵条目 → P1-D |

**两条红闸均会拦截下一次 commit**（pre-commit 三闸+1 全串行）。

---

## 2. 本轮新发现

### P1（6 条）

**P1-A【路径穿越写】资产上传的 `pid` 未过安全名校验** 【亲核】
`api/manga/comic_asset.py:1149`（`pid = (project_id or "").strip()` 仅查非空）→ `:1178`（`out_dir = _COMIC_ASSET_DIR / pid / conf["subdir"] / asset_name`）+ `:1183 write_bytes`。
同函数内 `asset_name` 已过 `_check_safe_name`（:1172 注释明写"审计 P1-1 修复"），**`pid` 是漏网的兄弟输入**；`_ensure_project(db, pid)`（:1167）还会用任意 id 直接建 projects 行。`pid="..\\..\\x"` 可把 PNG 写出 `comic_assets/`。
缓解面：回环绑定 + CORS Origin 白名单（跨站 multipart 驱动被 Origin 403 拦截），远程不可达——按上轮对 name 同类问题的定级口径维持 P1。修复 = 补一行 `_check_safe_name(pid)`。

**P1-B【路径穿越写】`AssetTurnaroundRequest` 无任何校验器** 【亲核】
`data/models.py:735-745`：姊妹模型 `AssetGenerateRequest`（:714-727）对 `name`+`project_id` 双挂 `_safe_name` 校验器，本模型一个没有；消费点 `api/manga/comic_gen.py:1177`（`out_dir = _COMIC_ASSET_DIR / req.project_id / "characters" / req.name`）mkdir 并写 5+ 张 PNG（四视图+canvas+master）。属明显的不一致遗漏，修复 = 复制姊妹校验器。

**P1-C【路径穿越写+递归复制】`AssetAdoptRequest` 无校验 + 任意建行 + copytree** 【亲核】
`data/models.py:762-765`（无校验器）→ `api/manga/comic_asset.py:421`（`_ensure_project` 任意 id 建项目行）→ `:443`（`new_dir = _COMIC_ASSET_DIR / req.project_id / conf["subdir"] / name`，`name` 取自源资产行、受 P2-2 二阶污染影响）→ `:448-449 shutil.copytree(old_dir, new_dir, dirs_exist_ok=True)`。链尾：经此建出的恶意 id 项目走删除流程可触达 `rmtree(_COMIC_ASSET_DIR / project_id)`（:551）。

> A/B/C 共性：上轮 P1-6 立下的"路径名必须消毒"内部不变量，在三个新端点上执行不一致——非系统性缺失，是同一条规矩没抄全。

**P1-D【提交闸红】模型登记表幽灵条目，smoke 闸当前必拦 commit**
`pytest -m smoke` 实跑 1 failed：`tests/unit/unit/test_model_registry.py:70`——`deepseek-r1-14b-w4a16` 登记在册但磁盘无权重（"幽灵条目（登记但磁盘无权重，禁止回潮）"断言失败）。权重疑在历次体积清理/隔离区批处理中移走而登记表未同步。**当前任何人 commit 都会被 pre-commit 拦截，或被迫 --no-verify 破坏闸门纪律。** 修复 = 登记表对账（删条目或回补权重标记）。

**P1-E【验收工具链断】未提交的 `project_type` 必传收口打断了 flow E2E 套件**
工作区改动（backend 必传收口）本身正确、backend 单测已全量同步（test_comic_script/test_comic_export 均带参）✅；但根目录活编排层漏改：
- `tests/flow/cases_comic.py:80/95/108/110/122`：TC-FLOW-COMIC-001/002/003/004 创建项目均不带 `project_type` → 必传后集体 422，整个漫画 flow 套件 P0 用例起跑即挂；
- `tests/ui_batch_comic_a.py:68-70` 同病；`tests/archive/smoke_batch1.py:64`（归档，低危）。
影响 = 下次漫剧/漫画颗粒验收时工具链先红，易误判为功能回归。修复 = 四处 payload 补 `"project_type"`。

**P1-F【前端】漫剧库「创建并进入」并不进入工作台** 【亲核】
`frontend/src/components/manga/MangaLibrary.tsx:167-188`：`handleCreate` 的 `.then()` 只弹 toast/收表单，**丢弃 store 返回的 `project_id`、未调 `openProject`**；`MangaPage.tsx:17` 以 `currentProject` 决定进不进工作台 → 创建后停留在库页。按钮文案（:655「创建并进入」）与文件头注释（"创建后直入编辑器"）双重矛盾。对照组：`ComicLibrary.tsx:87` 创建成功即 `onOpen` 进入。无数据风险，绕行 = 手点项目卡。修复 = `.then((projectId) => …openProject…)` 或改文案。

### P2（12 条）

**后端**
- **P2-1【闸门】uno_smoke.py 死代码 + 类型错误**【亲核】：`tests/unit/eval/uno_smoke.py:38-41` `_load_image` 全文件零调用且返回类型不符（mypy 报 `Incompatible return value type`）——一提交即被第 4 闸拦。修复 = 删函数（工作流图内已内联 LoadImage 引用）。
- **P2-2【二阶路径污染】资产 name 的两个写入口未消毒**：改名端点 `comic_asset.py:622-644`（`AssetUpdateRequest.name` 仅限长 100）与 infer-entities 的 LLM 输出 `nm`（:1803-1807）直接落库；事后被 `_asset_dir_for`（:775-783）、图替换（:1090）、P1-C copytree 拼进磁盘路径。LLM 实体名含 `/`、`..` 即二阶穿越写。
- **P2-3【F-008 边缘】async 端点内同步图像解码/落盘**：`comic_asset.py:822/826/1114/1187`（最大 10MB 图 PIL 解码）、`keyframe.py:2107/2128`、`system.py:203`。与"秒级以上一律走 services/offload.py"约定有张力（数十毫秒级，不阻塞主流程）。
- **P2-4【反序列化】缓存层 pickle.loads**：`data/cache.py:75,182`、`services/model_manager/cache.py:279`。数据源为本机缓存，攻击者需已有本机写权限；与全库 JSON 风格不一致，建议换 JSON 或注明信任边界。
- **P2-5【配置漂移】comfy_model_map.json 两条挂接指向不存在的源**【亲核】：`uno/flux1-dev-Q4_K_S.gguf`、`uno/t5-v1_1-xxl-encoder-Q5_K_M.gguf` 在 `models/uno/` 无对应文件（该目录 09-10 时点无任何 .gguf）。容错已确认无害（comfy_mount.py:184 计 `missing_src` 不阻断），疑为并发会话下载中。
- **P2-6【前端潜伏】projectSlice.createProject 硬编码 `listProjects('manga')`**（projectSlice.ts:30）：签名已允许传 `'comic'`，一旦未来有人传，创建成功但刷新错域、新项目"消失"。现调用方不可触发（MangaLibrary 传 manga、ComicLibrary 绕过 store 直调 API）。修复一行 = 改 `listProjects(projectType)`。

**前端**
- **P2-7【架构裁定项】tech-theme.css 构成第二令牌源**：114 处 hex 重定义 `--color-*`（tech-theme.css:23-32 起），与 CLAUDE.md"tokens.css 唯一权威源"规则冲突。它是 `data-family="tech"` 主题族的合法机制还是违规，**需架构裁定**：要么修订 CLAUDE.md 承认主题族定义点，要么改为派生。
- **P2-8【令牌违规存量】硬编码 hex 共 321 处**（tokens.css/sakura-theme.css 豁免外）：近期文件重点 = LicenseGate.tsx（13 处内联）、CloudApiSettings.tsx、app.css（61 处）、sakura.css（hex 回退 3 处 + `#fff` 6 处）、noir.css（18 处主色字面量）。
- **P2-9【健壮性】两 Library 的 useEffect 无卸载防护**（ComicLibrary.tsx:61-73、MangaLibrary.tsx:79-91）+ MangaLibrary 作品卡 onKeyDown 缺 Space 键（:396-401，对照 ComicLibrary 两者都处理）+ mangaApi.test.ts 缺 projectType 必传回归用例。

**上轮 P2 抽查确认仍在**
- **P2-10 style preview `image_path` 任意读（潜伏）**：`style_lora_service.py:944` `Image.open(image_path)` 零校验 + `api/style.py:315` 透传——本机任意图片可被外带 base64。
- **P2-11 encoder `audio_path` 无来源校验**：`encoder_service.py:434-440` 任意本机/UNC 路径直喂 ffmpeg。
- **P2-12 漫剧 CSV 公式注入**：`storyboard.py:1165-1174` `csv.writer` 直写用户文本，无 `= + - @` 前缀中和。

### P3（2 条）
- `_asset_move_to_global` 目录校验可被 `project_id=".."` 穿透（comic_asset.py:487-489）——P1-A/C 修复后自然消解。
- MangaLibrary.tsx:477/654 `handleCreate()` 返回 Promise 未 `void`（风格级）。

---

## 3. 上轮（08-28）P0/P1 修复状态复核

25 条目逐条核验：**完全修复 7 / 部分修复 4 / 原样未动 14**（上轮报告标称"P1 共 27 条"，实际清单为 P0-1 + P1-1..24 = 25 条目，本表按实列核对）。

**✅ 完全修复（7）**
| 编号 | 摘要 | 证据 |
|---|---|---|
| P0-1 | img2img/inpaint 绕开推理锁 | paint_engine.py:443-445 `_infer_lock` 改 RLock；:1362/:1546 全部入锁 |
| P1-1 | 功能锁重入无计数 | feature_lock.py:73 `_hold_counts`、:156-157/:188-199 计数语义 |
| P1-8 | 流量配额 SQL 少逗号 | browser_agent_service.py:549 `(key,)` |
| P1-9 | learning_sessions 永不落库 | learning.py:437-438 `json.dumps` |
| P1-11 | 重启后 video_tasks 永卡 generating | main.py:238-247 启动兜底置 error |
| P1-17 | models_switch/load 异常锁泄漏 | models.py:1252-1300 严格持锁+让位协议+兜底 release |
| P1-22 | launcher 心跳无首启宽限 | launcher.py:72-75 `startup_grace_s=180` |

**🔶 部分修复（4）**
| 编号 | 已修面 | 残留面 |
|---|---|---|
| P1-5 | style rmtree 主闸（42c65a3：版本白名单+is_relative_to 双闸） | lora_training_service.py:1492-1500 rollback 无白名单（污染 current 指针，无 rmtree 危害）；export/evaluate/merge 服务层未校验（只读，危害低） |
| P1-6 | name 维度消毒（_check_safe_name 体系+AssetGenerateRequest） | **即本轮 P1-A/B/C：project_id 维度在三个新端点回潮** |
| P1-14 | `_dataset` 为 None 情形 | lora_training_service.py:1319 优先级未倒置，外部 JSONL 评估仍可读旧集 |
| P1-23 | 热切换路径已补 release_stale | 空闲看门狗卸载（dialog_engine.py:2004）不调 release_stale，非热切换路径不一致仍在 |

**❌ 原样未动（14）**——按域分组：
- **锁语义跨切面（5，上轮"总体评价"点名族）**：P1-2 卸载不等待在途推理（paint_engine.py:985-987 只取 `_lock`【亲核】）；P1-3 引擎互卸 ABBA 环（09-06 的修复只解了同锁重入自死锁）；P1-18 dialog SSE 断连孤儿 producer 继续烧 GPU；P1-19 ensure_loaded check-then-act 竞态；P1-20 vram track_alloc 覆盖+双累加（vram_manager.py:109-115【亲核】——**与 F-4 修复不冲突**：F-4 改的是 get_usage 读数口径，账本自身虚增缺陷仍在）。
- **训练闭环（3）**：P1-12 风格训练假嵌入（style_lora_service.py:858-871）；P1-13 无 OOM 防护无 checkpoint；P1-14 残留见上。
- **事件循环（2）**：P1-15 vision_tools 同步加载（vision_tools.py:71-79）；P1-16 system_backup 同步热备（system.py:215-223）。
- **静默失效（2）**：P1-7 CRUD 标识符拼接无白名单（database.py:765-800，现调用点安全）；P1-10 seed_urls 仍是假实现（输入侧已补校验，消费端 LearningBudget 仍不解析）。
- **其他（2）**：P1-4 streamer 线程不可中断（transformers_backend.py:340-361）；P1-24 style 版本分配竞态（style_lora_service.py:1291-1300）。

**P2 抽查**：CSV 公式注入/image_path 任意读/audio_path 无校验三条确认仍在（并入本轮 P2-10/11/12）；TextTo3D 写路径一条随 3D 端点移除而消亡。

---

## 4. 安全面正面确认（本轮扫描为干净的模式）

- **SQL**：全库值一律 `?` 参数化；f-string 插值均为内部常量（列集/占位符/硬编码表名）——无真实注入面。
- **危险执行**：`eval(` 命中均为 `model.eval()`；无 exec/marshal；yaml 仅 safe_load。
- **子进程**：零 shell=True/os.system/os.popen；全列表参数 + CREATE_NO_WINDOW（有契约测试）。
- **密钥**：零硬编码；keys/ 未入库；字段加密 DPAPI 绑机；升级包有签名校验。
- **边界**：回环绑定双闸 + CORS localhost 白名单 + WS Origin 守卫（CSWSH）有效。
- **except-pass**：重点新模块（vram/llama/comic 域/cloud/comfy_paint_engine）裸吞计数为 0。
- **前端**：`any` = 0；XSS 面（dangerouslySetInnerHTML/innerHTML/eval）= 0；localStorage/sessionStorage 零敏感值；WS URL/载荷零凭据。
- **asyncio**：主链路全部经 run_blocking（仅 P2-3 三处边缘）。

---

## 5. 修复优先级建议（须拍板后动手）

1. **立即解红提交链**：P1-D（登记表对账 deepseek-r1-14b-w4a16）+ P2-1（删 uno_smoke.py 死函数）。两处清掉后四闸复绿。
2. **P1 对齐补丁（十几行级）**：P1-A（`_check_safe_name(pid)`）+ P1-B/C（复制姊妹校验器）+ P2-2 同族消毒；P1-E（flow 四用例补 project_type）；P1-F（openProject 接线）。
3. **择机**：P2-3~P2-9、P2-10~12。
4. **建议另立专项**：上轮遗留的锁语义五连（P1-2/3/18/19/20）是同族跨切面问题，零敲碎打不如与显存调度账本工作合流一次批处理；其中 P1-20 与 F-4 的关系已在 §3 注明。

---

## 6. 边界与声明

- 并发会话警示：`models/uno/` 今晨新增三文件（flux-2-klein-4b / qwen_3_4b / flux2_ae，07:07-08:04）疑为并发会话在飞产物，本次审计未触碰、未计入发现。
- 本轮未覆盖：tests/ 根目录其余活编排脚本、ComfyUI 便携版内部、tools/musubi-tuner（第三方）、两份未跟踪方案文档。
- 工作区未提交改动（project_type 收口 + uno map）本体审计通过；其引发的两个工具链断裂见 P1-D/E。
- 本报告只审不改；所有修复动作（含 commit）须经用户拍板。
