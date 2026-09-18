# OmniSpace AI v2.5.0 — AI开发记忆文件（2026-08-28 实测校准版）

> **此文件为AI编程助手的强制上下文。每次开发会话开始时必须完整读取。**
> **本文件已于 2026-08-28 按代码实测全面校准**（端点/表数/版本/端口均经脚本与运行时实测核对）。
> **2026-09-08 新增第 16 节「开发流程铁律」**（方案四段制/验收前置/汇报格式/禁止清单/并发会话——用户拍板，约束「瞎写代码 + 走一步看三步」）。同日勘误：§1/§3.1 补记 mypy 第4闸已上岗（旧文误记「未安装」）。
> **冲突裁决优先级：代码 > docs/INDEX.md 登记的现行文档 > 本文件 > 根目录历史 txt（A/B/C/D/E/裁定书/开发清单）。**
> 与代码现状的历次差异对照，另见 docs/omnispace-tech-doc/ §12.4。

---

## 0. 会话启动检查清单

每次开始编码前，AI必须确认以下全部为YES：

- [ ] 我已读取此文件全部内容
- [ ] 我知道前端用 **React 19.0**（package.json 实测 19.0.0），不是 Vue3/Svelte/SolidJS
- [ ] 我知道状态管理用 **Zustand 5**（5.0.0），不是 Pinia/Redux
- [ ] 我知道包管理用 **pnpm**（packageManager 字段锁定），不是 npm/yarn
- [ ] 我知道 **3D 导演台已整链路剔除**（2026-08-29 用户裁定：后端 director 路由/前端 DirectorStage/src/three/ 六件套/DB 表不再使用），前端无 3D 视口
- [ ] 我知道 Python 运行时是**嵌入式 3.12.10 主链（B1 2026-09-13 拍板项 0=A；py310 并存保留为回退锚点与打包基线）**；vLLM 子进程独用 py313，ComfyUI 便携包自带 py313——不是 3.10
- [ ] 我知道后端代码已**扁平化至 src/**（2026-09-15 重构：backend/ 目录已删除，四层架构 = HTTP 层 src/api / 业务层 src/services / 资源层 src/engines+data / 内核层 src/core+cutemamen；config.yaml 位于 **src/config.yaml**；仓库外冻结进程的 backend.main:app 旧入口由 backend/ 兼容 shim 转发）
- [ ] 我知道后端用 FastAPI 0.141 + Pydantic v2，不是 Django/Flask
- [ ] 我知道**没有 Tauri/Rust 壳层**——ADR-002（2026-08-20）已裁剪，交付形态 = launcher + 系统浏览器
- [ ] 我知道数据库是 SQLite（WAL，**32 张用户表＝主库 30 + flow 独立库 logs/flow_trace.db 2**，user_version=7，2026-09-01 实测校准）+ ChromaDB，不是 PostgreSQL/MongoDB
- [ ] 我知道任务队列是**进程内 PriorityQueue（模拟 Celery 语义）**，没有 Celery/Redis 依赖（Redis 仅缓存可选降级）
- [ ] 我知道所有CSS颜色必须用 var(--color-*)（权威源 frontend/src/styles/tokens.css），禁止硬编码
- [ ] 我知道在进行所有项目操作和决策过程中，必须严格遵循团队协作模式（产品/设计/前端/后端/测试各司其职，决策基于讨论）
- [ ] 我知道**大活必走方案四段制**（§16.1）：改动清单/影响面/验证方法/风险回退，四段齐才动手，用户拍板前不准改任何代码
- [ ] 我知道**单测全绿 ≠ 验收**（§16.2）：功能交付必须实弹取证；论断必须标 ✅/🔶/⚠️ 验证状态 + 行号证据
- [ ] 我知道**禁止清单红线**（§16.4）：改动不超方案圈定范围、不擅自加依赖、H3 主力链路与打包台/激活台冻结不动、未经拍板不 commit

**如有任何一项为NO，立即停止编码并重新读取此文件。**

---

## 1. 技术栈现状表（2026-08-28 实测校准）

| 层级 | 现行技术（实测版本） | 不使用（历史文档曾宣称） |
|------|----------------------|--------------------------|
| 前端框架 | **React 19.0.0**（Function Component + Hooks） | React 19.2（CLAUDE.md 旧版笔误）；Vue3/Svelte |
| 状态管理 | **Zustand 5.0.0** | Pinia/Redux |
| 路由 | **react-router-dom 7.0.0**（createHashRouter，9 个一级路由） | — |
| 组件库 | **自研 Omni 体系**（components/common/） | shadcn/ui（未使用） |
| 3D | **已剔除**（2026-08-29 裁定：src/three/ 六件套与 DirectorStage 已删除） | @react-three/fiber、drei |
| 构建工具 | **Vite 6 + TypeScript 5.6**（strict）+ vitest 2.1.8 | — |
| CSS | **Tailwind CSS 4**（@tailwindcss/vite） | — |
| 后端语言 | **Python 3.12.10（runtime/py312 嵌入式主链，B1 升级；py310 并存=回退锚点/打包基线）** | Python 3.10（作为主链的历史口径） |
| 后端框架 | **FastAPI 0.141.1 + Pydantic v2 + uvicorn** | SQLAlchemy ORM（2.0.51 装而未用，全裸 sqlite3） |
| AI 内核 | **DistributedFormer 移植（2026-09-15）**：CubeGPT 脉冲主模型（src/core/）+ CuteMamen 插件内核（src/cutemamen/，双路导入：内核相对导入优先 / 宿主 omnispace.plugin 注入回退）+ OSP v1 插件运行时（src/services/plugin_runtime/，base/bridge/loader/registry 四件套，三道运行闸） | — |
| AI 推理 | **torch 2.13.0+cu130（契约真源=src/torch_contract.json，boot 预检闸守）+ diffusers 0.39 + transformers**；GGUF 走 llama.cpp；vLLM **0.27.1** 子进程（py313；生成期让渡=杀子进程制，config vllm.sleep_mode 门控真 sleep 待上游） | — |
| 关系数据库 | **SQLite WAL + FTS5**（32 张用户表=主库 30+flow 独立库 2，PRAGMA user_version=7） | — |
| 向量数据库 | **ChromaDB 1.5.9**（bge-large-zh 1024 维，不可用时降级 TF-IDF 内存检索） | — |
| 任务队列 | **进程内多套队列并存（实测 9 套）**：对话位次表/图像/视频/小说/训练×2/行为/浏览器×2，模拟 Celery 语义；统一任务子系统=治理项（v4 方案 B5）+ WS Hub 广播 | Celery + Redis（F-13 已裁定 ⚪ 豁免） |
| 浏览器 | **Playwright Chromium 进程池**（学习代理用）+ 用户系统浏览器 | CEF 120+ |
| 加密 | **AES-256-GCM 字段级**（crypto.py）+ Windows DPAPI 密钥保护 | SQLCipher（未启用） |
| 包管理 | 后端 **pip + requirements-lock.txt**（154 包钉版）；前端 **pnpm** | uv |
| 静态检查 | **ruff（ruff.toml）+ mypy 第4闸**（2026-09-05 批4 上岗：存量 234 条基线冻结 tools/mypy_baseline.txt 只增不减；tools/run_mypy.py --staged / --rebaseline） | — |
| 桌面壳层 | **launcher.bat/launcher.py + 系统浏览器**（ADR-002 裁剪） | Tauri 2.x / Electron |
| 视频编码 | **FFmpeg（runtime/ffmpeg）**，encoder_service.py | 独立 SVT-AV1 构建 |

---

## 2. 前端编码铁律

### 2.1 组件规范

```
组件文件扩展名：.tsx（禁止.vue）
组件命名：PascalCase（OmniButton.tsx）
页面组件：XxxPage.tsx，存放于 components/<域>/（pages/ 目录为空占位，路由挂载在 router.tsx）
全局组件前缀：Omni（OmniButton, OmniModal, OmniDrawer）
组件结构：Function Component + Hooks（禁止Class Component）
useEffect 必须返回 cleanup
```

### 2.2 状态管理（Zustand 5，实测 11 个 store）

```
src/stores/：
  useAppStore / useDialogStore / usePaintStore / useMangaStore(+manga/ 六切片)
  / useLearningStore / useLearnStore / useModelStore / useStyleStore
  / useTaskStore / useWarmupStore / useHardwareStore
（旧文档写的 useChatStore/useStoryboardStore/useSettingsStore/useModalStore/useWebSocketStore 不存在）
```

### 2.3 路由（React Router 7，Hash 模式，实测 11 个一级路由；2026-09-18 监督审计勘误：/training 入列后 10→11）

```
/chat      → components/dialog/DialogPage.tsx
/paint     → components/comic/ComicPage.tsx（AI 漫画页；URL 名沿用历史，旧绘画页已清退）
/storyboard→ components/manga/MangaPage.tsx（漫剧创作）
/novel     → components/novel/NovelPage.tsx
/learning  → components/learning/LearningPage.tsx
/models    → components/model/ModelManager.tsx
/style     → 重定向 /training（P1 训练中心 2026-09-17：知识+风格+人物训练归一；StylePage 以 embedded 形态内嵌）
/training  → components/training/TrainingPage.tsx（2026-09-17 新增）
/settings  → components/Settings.tsx
/logs      → components/logs/LogsPage.tsx（2026-08-21 新增）
/help      → components/help/HelpPage.tsx
/ 与 * 重定向 /chat；无 /about、无 /comic（旧文档口径作废）
```

### 2.4 3D 导演台

**已整链路移除（2026-08-29 用户裁定）**：`src/three/` 六件套、`components/manga/DirectorStage.tsx`、3D 路由与后端 director 路由均不存在；前端零 three import（package.json/vite.config 的 three 残留已于 2026-09-03 清除）。未来若重启 3D 能力，属技术栈变更，须先立 ADR 再动工。

### 2.5 CSS 变量体系（已落地；2026-09-10 修订：承认主题族定义点）

```css
/* 禁止硬编码，MUST使用变量 */
/* 令牌「名字」与缺省族（Sakura）「值」的权威源 = frontend/src/styles/tokens.css */
--color-primary: #FF6B9D;      /* Sakura 樱花粉主色 */
--color-accent: #4ECDC4;       /* 薄荷绿 */
--color-bg: #1A1A2E;  --color-card: #16213E;  --color-input-bg: #0F3460;
--color-success: #6BCB77;  --color-warning: #FFD93D;  --color-error: #FF6B6B;
/* 亮色主题经 [data-theme="light"] 切换；sakura-*/mint-* 色阶见 sakura-theme.css @theme */
```

**主题族定义点豁免（2026-09-10 用户拍板，P2-7 裁定）**：产品支持多主题族
（`<html data-family="tech|sakura">`，useAppStore 写入）——各主题族文件
（tech-theme.css / sakura.css / noir.css / dazzle.css / tech-fx.css 等）
**允许在自己的 `data-family` 作用域选择器下重定义 `--color-*` 的值**
（同名令牌换值=CSS 主题标准机制）；令牌**命名**仍以 tokens.css 为准，
新增令牌必须登记在 tokens.css。豁免仅限上述主题族定义文件——其余任何
文件（组件 tsx 内联、app.css、工具样式）硬编码 hex/rgb 仍然违规
（P2-8 存量清理中）。

### 2.6 前端禁止项（保留有效）

| 禁止 | 正确替代 |
|------|---------|
| `any` 类型 | `unknown` + 类型守卫 / Zod schema（services/schema.ts） |
| 硬编码颜色 | `var(--color-*)` |
| `!important` | 提升选择器特异性 |
| useEffect无cleanup | MUST return cleanup函数 |
| 直接操作数据库 | 调后端API |
| ~~WebSocket传AI token~~（F-011 已于 2026-09-18 拍板 B 翻转：WS /dialog/stream 即对话流式终态主路径；SSE 端点在库未接线仅作能力备份） | 现行=WS 终态 |

---

## 3. 后端编码铁律（Python 3.10）

### 3.1 绝对规则

| 规则 | 说明 |
|------|------|
| 全部函数MUST写type hints | `def foo(x: int) -> str:` |
| API 数据模型MUST用Pydantic v2 | 集中在 src/data/models.py |
| asyncio循环中禁止同步阻塞 | 同步推理唯一入口 `services/offload.py`（run_blocking/sync_core，P1-06 有测试锁定） |
| 大模型卸载MUST三件套 | `del model; torch.cuda.empty_cache(); gc.collect()` |
| 禁止循环中重复加载模型 | 引擎为模块级懒加载单例（双重检查锁） |
| SQLite 写入串行化 | threading.local 连接 + 全局 _write_lock；批量写走 execute_in_transaction |
| 静态检查 | ruff check + mypy 第4闸通过（提交前，.githooks 三闸+1；mypy 基线冻结只增不减，见 §12.1） |
| 降级必须诚实 | 不可用功能返回 degraded:true + degrade_reason，禁止伪造结果 |

### 3.1.1 目录布局（2026-09-15 src 扁平化重构后）

```
src/
├── api/          HTTP 层：18 个路由模块（plugins 为 2026-09-16 新增）
├── services/     业务层：推理/调度/模型管理/插件运行时(plugin_runtime)等
├── engines/      资源层：vllm/gpu/vram 等引擎适配
├── data/         资源层：数据库/模型 schema
├── core/         内核层：distributedformer.py（CubeGPT 脉冲主模型）
├── cutemamen/    内核层：插件内核（plugin/pkg/rust_coding 等；video_making 已于 2026-09-17 移除）
├── middleware/   HTTP 中间件（error_handler/feature_lock/...）
├── config.py     配置（SRC_DIR 基准；config.yaml 同目录）
└── main.py       FastAPI 入口（uvicorn src.main:app）

plugin/           CuteMamen 插件包（*.CuteMamen tar.gz；2026-09-17 起仅
                  RustCoding——VideoMaking 随快速预览链移除）
tests/unit/       单测（含 distributed/ 内核测试族）
docs/             全部文档（AGENTS.md/CLAUDE.md 例外，居根目录作门卫）
backend/          兼容 shim（仅 re-export src.main:app，救仓库外冻结进程）
```

### 3.2 API响应格式（铁律，实测一致）

```json
// 成功（HTTP 恒 200，ADR-01）
{"success": true, "data": {...},
 "meta": {"request_id": "uuid", "timestamp": "ISO8601", "duration_ms": 12}}
// 失败
{"success": false, "data": null,
 "error": {"code": "语义串如 MODEL_NOT_LOADED", "message": "...", "detail": {...}, "suggestion": "..."}}
```

错误码为语义串（约 100 个：MODEL_*/KNOWLEDGE_*/BROWSER_*/TRAINING_*/SYSTEM_*/FEATURE_* 等），
历史数字码（40xxx-80xxx）经 `_LEGACY_CODE_MAP` 自动映射。

---

## 4. API路由现状（实测 2026-08-28；2026-09-02 复核：319 装饰器、模块 15 个）

```
Base URL: http://127.0.0.1:5800/api/v1   （直接 uvicorn / config.yaml）
          launcher 默认 --port 8765（冲突扫描 5800-5835）—— 两套端口并存，联调前先确认

18 个路由模块 ≈**377 端点**（B1 2026-09-13 实测；API 面水分约 40%——38 死端点+112 仅测试/脚本消费，清退清单见 docs/全量技术评估总汇总与修复总方案-v4 §八）：
├── /api/v1/dialog  (≈/chat 别名)   对话（send/history/sessions/status，SSE: /chat/stream）
├── /api/v1/draw    (≈/paint 别名)  绘画（generate 异步任务 /img2img /inpaint /upscale /queue /history）
├── /api/v1/manga/*                 漫剧包：storyboard / comic / comic_asset / keyframe / video
│                                   / director / voice（+ 23 个顶层旧别名）
├── /api/v1/learn                   学习训练任务/数据集
├── /api/v1/learning                学习会话/主题/调度（含进度 WS）
├── /api/v1/knowledge               知识库（含 /knowledge/graph 图谱 + FTS/向量检索）
├── /api/v1/browser                 浏览器Agent
├── /api/v1/models                  模型管理（load/unload/switch/benchmark/vram…）
├── /api/v1/style                   视频风格 LoRA
├── /api/v1/art(vision_tools)       TripoSR/SAM/MiDaS/YOLOv8 随包小模型
├── /api/v1/voice                   Whisper ASR + TTS
├── /api/v1/hardware                硬件（含 realtime WS）
├── /api/v1/system                  系统（设置/日志/事件）
├── /api/v1/logs                    日志可视化
└── /api/v1/license                 激活门禁（status/activate，boot 启动页直调；发行公钥出包注入后生效）

WebSocket 4 个：/ws（消息中枢）、/api/v1/dialog/stream/{session_id}（已废弃，兼容保留）、
               /api/v1/hardware/realtime、/api/v1/learn/session/progress
静态托管：frontend/dist（Hash Router，_ApiAwareMount 保护 API 命名空间）
```

> 旧版 CLAUDE.md 的 /chat /paint /storyboard /director /video 五前缀路由表来自文档E附录B，与实现不符，作废。

---

## 5. 数据库规则（实测 2026-08-28）

| 数据库 | 用途 | 规则 |
|--------|------|------|
| SQLite (WAL) | `data/omnispace.db` 主库 **23 张 schema 表 + 12 张服务层散建表**（另有 FTS5 影子 5 张＝实体 41；B1 2026-09-13 实测）+ 独立库 `logs/flow_trace.db` 2 张 + 第三库 `data/model_usage.db` 1 张 | PRAGMA user_version=**13**（迁移组机制，历史组禁改只许追加；散建表迁入体系=v4 B10 待办）；FTS5 知识全文检索 |
| ChromaDB | 向量/知识库（data/chroma/） | bge-large-zh 1024 维；不可用降级 TF-IDF |
| localStorage / IndexedDB | 前端偏好/缓存 | 前端直接读写 |

**字段级加密**（crypto.py，AES-256-GCM + DPAPI）：dialog_messages.content 与 behavior_logs 四列
（content/context/before/after）；知识库正文不加密（FTS5/向量检索依赖明文）；整库为普通 SQLite 明文文件。

完整表清单见 `docs/design/database-er.md`（真源）。旧版"8 张表 chat_sessions/paint_history…"口径作废
（真实表名是 dialog_sessions / dialog_messages / storyboard_rows / keyframes / comic_assets / paint_history 等）。

---

## 6. 桌面壳层

**Tauri 已裁剪**（ADR-002，2026-08-20）：交付形态 = launcher 守护进程（自检/端口冲突三级处理/心跳/崩溃重启≤5次/托盘）+ 系统浏览器。
Rust/Tauri 编码规则整节作废；重启壳决策须新立 ADR（重启条件见 ADR-002 §5）。

启动/停止入口（2026-08-31 落地并 E2E 实测）：
- **有窗（日常调试）**：根目录 `启动OmniSpace.bat` → `launcher/boot.py`（启动页/自检/心跳/ComfyUI 清理；Ctrl+C 或关窗退出）
- **免黑窗（桌面交付）**：`launcher/make_shortcut.py` 生成桌面 `OmniSpace.lnk`（pythonw 直拉 boot.py，端口 5800-5835，重复运行幂等重建；boot.py 启动各阶段含冷启动等待期均响应 /api/quit；重复双击触发单实例守卫——检测到在跑启动页即让位退出 exit 0，不再累积守护进程）
- **停止（免黑窗唯一主动退出通道）**：根目录 `停止OmniSpace.bat` → `launcher/stop.py`（优先 POST 启动页 /api/quit 优雅收尾；兜底只清本启动链——boot.py 进程 / 5800-5835 的 src.main / 8189 ComfyUI；项目内独立实例如 :8765 只提示不动手）
- **单实例限制**（2026-08-31，`src/single_instance.py`）：src.main lifespan 首步经 Windows 命名互斥体全机唯一，重复启动秒失败并给出指引；豁免 = pytest 运行态或 `OMNISPACE_ALLOW_MULTI=1`。动机：双栈叠载显存（当日关键帧采样 20+ 分钟超时；08-29 蓝屏同源）。注意：仅对运行新代码的实例生效，存量老进程需重启后才持有互斥体

---

## 7. 安全与资源策略（阈值真源 = src/config.yaml scheduler.thresholds）

| 显存阈值（config.yaml 实值） | 动作 |
|------|------|
| ≥ 80%（gpu_vram_warning） | 警告，调度器介入 |
| ≥ 90%（gpu_vram_critical） | 降精度/卸载非活跃模型 |
| ≥ 95%（gpu_vram_force_unload） | 强制卸载 |

GPU 利用率：>85% 持续 5s 轻度降参（steps×0.67）；>95% 持续 15s 深度降档（×0.4）；4s 滞回（S4，2026-08-28）。
温度：热保护 ≥90°C 拒绝新任务（feature_lock 层）；空闲回收：功能锁空闲 60s 释放小模型 / 300s 卸载非驻留大模型。
漫剧生成期显存协商（2026-08-31 补齐视频链路，助手在 `api/manga/common.py`）：关键帧/视频生成持锁后自动 vLLM 睡眠让渡（~7-12GB）+ 本地绘画管线卸载，收尾释放锁后自动唤醒恢复；前端模块切换/进漫剧项目时 `/models/release-for-module` 兜底。绘画模块冷启动弹窗（PaintWarmupModal）+ `/models/warmup feature=paint` 预热点火（对齐对话模块模式）。
漫剧「模型配置」接线（2026-08-31）：画幅/时长经确认弹窗随 `/manga/video/generate` 下发（H3 分支按时长定 720p/480p、按宽高比定竖屏，`run_h3_chain_task(aspect=)` 翻转宽高）；推理/绘画/视频选型保存时同步 PUT module-config 的 manga-* 槽 default，`gen_router.resolve_route` 对槽内 default 提权到偏好序首位。视频生成门槛改为「描述词 + 带图绑定资产」必填、分镜图可选（H3 以绑定资产多图参考为内容源）。H3 链式六段式提示词按 A/B/C 结构化注入并按时长窗口截取 C 段镜头（此前 summary 仅 120 字且 detailed 塞整行全量时间轴，单镜生成被稀释）。

其他安全（保留有效）：零信任输入校验；上传闸门（扩展名白名单 + 文件头魔数，PE/ELF 黑名单）；
回环绑定闸门（非 127.0.0.1 拒绝启动，豁免 OMNISPACE_ALLOW_LAN=1）；TrustedHost 防 DNS 重绑定；限流 300 req/min/端点。

---

## 8. 性能指标（性质：验收目标值；非实测现状）

> 2026-09-13 实测锚点（RTX 5070 Ti 16GB，B1 勘误）：对话暖态 SSE 首字 **0.29s**（<500ms 达标）；
> 后端冷启动 splash→就绪 **~22s**（暖机口径；冷机 25s~2min=boot.py 口径），就绪日志已改实测 T+Xs。
> 旧锚点（首字 461ms / SDXL 13.6s / wan 83.4s / T+10s）为 2026-08-28 口径，仅存档。

| 指标 | 目标 |
|------|------|
| 首字延迟（≤4K上下文） | <500ms（实测达标） |
| FLUX.2 Klein 2560×1440 四视图 | 可复现出图（RTM M-02 已验收） |
| RAG检索延迟 | <100ms |
| Agent调用成功率 | >98% |
| 10k条列表滚动 | 流畅（60fps 量级） |

---

## 9. 功能互斥（middleware/feature_lock.py 实测落地）

dialog / paint / video_gen / training 四类重量级功能**进程内异步锁全互斥**，
冲突返回 `FEATURE_MUTEX_LOCKED`；GPU ≥90°C 拒绝新任务。
`services/model_manager/` 驱逐优先级：auxiliary(0) 最先 … dialog(5) 最后。
（旧版 6×6 互斥矩阵是文档E的规划口径，实际实现以 feature_lock.py 为准。）

---

## 10. 硬件基线

**项目硬件档位（B1 2026-09-13 诚实化）：推荐体验档 = RTX 5070 Ti 16GB**（开发实测机；空转基线 ~10.9GB 显存、桌面+浏览器常态下空闲可用 ~13.4GB）；
**12GB 入门档 = 降级体验档**（HARDWARE_TIER_TABLE 按 min_vram 分档路由——8 个档位要求 ≥14GB，12GB 机自动落低档/走降级链）。历史口径「所有功能 MUST 12GB 可用」不再作为承诺；对外发行宣传按 16GB 推荐/12GB 降级两档表述。

---

## 11. 开发阶段（历史记录）

P0/P1/P2 整改计划已收尾（TASK-收尾 21/21，见 git 历史与 docs/audit-task-checklist.md）。
当前活跃方向：模型接线（RTM 🟦 项）、一致性标定（DINOv2 v46+）、性能优化（performance-optimization-plan/）。

---

## 12. 跨语言禁止事项总表（F-001~F-015 保留有效）

| 编号 | 禁止 | 原因 |
|------|------|------|
| F-001 | Python threading做CPU计算 | GIL |
| F-002 | TS使用any | 消灭类型安全 |
| F-005 | SQL字符串拼接 | SQL注入 |
| F-006 | 前端直接操作SQLite | 绕过权限 |
| F-008 | asyncio中调同步函数 | 事件循环阻塞 |
| F-011 | WebSocket传AI token | 用SSE |
| F-015 | 多进程同写SQLite | 写锁冲突 |

（F-003/Rust、F-004/C++、F-010/CUDA context 条目随 Tauri/C++ 层裁剪暂不适用，保留备查。）

### 12.1 编程语言规范执行细则（2026-09-04 合规计划落地）

- 规范真源：`docs/历史资料/编程语言规范A.txt` 的**适用部分**（Rust/Tauri/C++/CEF 等作废章节不复活）；
  计划与批次进度：`docs/编程语言规范合规计划-2026-09-04.md`；已批例外：`docs/编程语言规范例外清单.md`。
- **范围冻结（2026-09-04 用户令）**：`packaging_console/` 与 `license_console/` 代码不动（除其自身 bug 修复），合规改造与统计均不含。
- Python：新函数 100% type hints，存量目标 ≥95%（豁免需注释）；async 内禁 time.sleep/requests（阻塞一律走 services/offload.py）；SQL 只参数化。
- TS：禁 any（ESLint error 已落）；新接 API/WS 消息入口一律 Zod `.parse()`（模板=services/schema.ts，schema 照后端 Pydantic 抄，宁松勿严）；路由级组件走 React.lazy 代码分割。
- AI token 流一律 SSE；WS 只做状态广播/遥测（存量 /dialog/stream 为例外 E2，不新增同类）。
- CSS 动画只用 transform/opacity；进度条用 scaleX/scaleY（布局类 width 过渡见例外 E6）。

---

## 13. Git提交规范

```
格式：type(scope): description
类型：feat / fix / docs / style / refactor / test / chore
示例：feat(chat): 实现Qwen3-VL多模态对话流式输出
```

---

## 14. 文档引用表（真源层级）

| 文档 | 位置 | 用途 |
|------|------|------|
| 文档索引与时效声明 | **docs/INDEX.md** | 哪份文档代表现状（真源层级唯一入口） |
| 架构总览 | docs/design/architecture-overview.md | 分层/中间件/引擎/时序 |
| API 端点总表 | docs/design/api-endpoints.md | 端点清单（08-20 快照，增量以代码为准） |
| 数据库 ER | docs/design/database-er.md | 31 张表结构 + 迁移 |
| 需求追踪矩阵 | docs/requirements-traceability.md | 需求状态唯一真源 |
| Tauri 裁剪裁决 | docs/ADR-002-tauri-shell-decision.md | 交付形态依据 |
| 全量技术文档（协作者上手版） | docs/omnispace-tech-doc/（HTML，含 12.4 差异对照；08-26 时点） | 新人上手 |
| 全量技术快照（2026-09-01） | **docs/全量技术文档-2026-09-01.md**（16 章 + 4 附录：319 端点/32 表字段/模型占用/前端 131 文件实测清单） | 全景首选，上手先看这份 |
| 部署 / 故障排查 | docs/deployment-manual.md / docs/troubleshooting.md | 部署与排障 |
| 测试入口 | tools/run_tests.py（唯一入口）+ tests/README.md | 三层测试体系 |

根目录 txt（编程语言规范A / 程序开发计划B / 全量项目技术文档C / 布局视觉D / 极致细粒度E / 标准裁定书 / 开发清单）
为**仓库外源历史文档**，仅作需求追溯素材：已过时声明见各文件头部时效横幅；
其"当前值"指标、技术选型（Tauri/Celery/SQLAlchemy/React 19.2 等）**禁止**作为编码依据。

---

## 15. AI行为规则

1. **每次会话开始**：完整读取此文件，执行第0节检查清单
2. **编码前**：确认技术栈与第1节"现行技术"列一致
3. **编码中**：每写一个组件/函数，对照第2/3节铁律检查
4. **不确定时**：查 docs/INDEX.md 真源层级，不要猜测；代码与任何文档冲突时以代码为准
5. **偏离检测**：发现使用了"不使用"列的技术（或文档撒谎），立即停止并核对现状
6. **新文件命名**：.tsx（前端）/ .py（后端）
7. **绝不自创规范**：所有规范以代码 + docs/ 现行文档 + 本文件为准
8. **组件开发**：沿用自研 Omni 体系（components/common/），不从零另起炉灶
9. **3D**：已整链路移除（2026-08-29 裁定）；勿凭旧文档恢复 src/three 引用，重启 3D 须先立 ADR
10. **状态管理**：用 Zustand create() 创建 store，不要用 Context+useReducer
11. **大活流程**：方案四段制 + 验收前置 + 汇报格式 + 禁止清单，按第 16 节执行

---

## 16. 开发流程铁律（2026-09-08 用户拍板：约束瞎写代码 + 走一步看三步）

> 第 1~12 节管「写什么、用什么技术」；本节管**怎么干活**（方案、验收、汇报、红线、并发）。
> 本节与第 0 节检查清单联动，违反本节 = 打回重做，与违反技术铁律同罪。

### 16.1 方案四段制（大活必走，用户拍板前不准改码）

- **大活判定**（满足其一即是）：跨文件改动 / 动核心链路（模型加载、队列、调度、DB 迁移、发行打包）/ 新增文件或依赖 / 自评改动量 >50 行
- 方案书必须四段齐全，**缺一段 = 打回**：
  1. **改动清单**：要动哪些文件、每处改什么
  2. **影响面**：每处改动会波及什么功能 / 调用方
  3. **验证方法**：什么证据算「做对了」（具体到测试命令 / API 请求 / 浏览器操作）
  4. **风险与回退**：最可能坏在哪、坏了怎么恢复
- 小改豁免：typo、单行修复、纯注释——可直接动手，但汇报仍须带验证证据

### 16.2 验收标准前置

- 动手前先写清「怎么算做完」；写不出验收标准 = 需求没吃透，先问清再动手
- **单测全绿 ≠ 验收**：功能级交付必须实弹——API 真请求 / 真浏览器操作 / WS 帧取证（按功能选）
- 论断必须标验证状态：**✅ 已实弹验证 / 🔶 部分验证 / ⚠️ 未验证推断**；关键论断附 `文件:行号` 证据
- 受阻项要么攻关绕行、要么给出卡点证据，**不许静默跳过**；错误方法公开作废重做；估时 ≠ 承诺

### 16.3 汇报格式

- **结论先行**：第一句说结果（成了 / 没成 / 卡在哪），过程细节其后
- 改了什么、验证证据、遗留什么——三件事缺一不可
- git / 工程操作先大白话讲清后果；**未经用户明确拍板不 commit**（仓库无远程，commit 仅本地记录，同样要拍板）

### 16.4 禁止清单（红线）

| 禁止 | 依据 |
|------|------|
| 超出方案书圈定范围的顺手重构 / 改名 / 格式化 | 改动范围 = 方案圈定范围；发现的存量问题单独上报，不夹带私改 |
| 擅自新增第三方依赖 | 需方案书单列理由 + 用户拍板 |
| 改 VIDEO_ROUTING_TABLE / manga-video 槽 / H3 条目 | 漫剧视频主力锁定（2026-09-06 用户令） |
| 改 packaging_console/ 与 license_console/ | §12.1 范围冻结令（2026-09-04），除其自身 bug 修复 |
| 直接杀 / 重启后端进程 | 绝不走 kill 直杀（boot 看门狗 ≤5 次补位重启）；退出走 splash `/api/quit` 正规链 |
| 拿并发会话的产出当自己的 | 验收必须两跳核对数据归属（如 storyboard_id→project） |

### 16.5 先读后写 + 小步走

- 动手前必须能引用要改处的 `文件:行号`；引用不出来 = 没读 = 先读再写
- 热门文件（多会话常碰的）编辑前必须**重读最新版**，不吃会话开头缓存的旧内容
- 一步一报：每完成一小步带证据停下，用户点头再继续；任务拆小，禁止一口气铺开几十个文件

### 16.6 并发会话规矩（共享战场）

- GPU / ComfyUI 测试前必须核查后端实时任务（近 5 分钟提交记录 + >1GB comfy 进程），撞车会让用户任务降参 / 丢失
- 发现并发会话在飞（日志 / 队列 / 端口被占 / 磁盘文件中途变化）：立即停手协调，**绝不重启后端**；UI 类结论须单会话复验后才可下

---

> **此文件是AI开发的行为宪法。违反此文件 = 引入Bug = 项目风险。**
> **但记住：本文件描述的是 2026-08-28 的实测现状——代码再次演化后，请先更新本文件再编码。**
