# OmniSpace AI v2.3.1 — AI开发记忆文件（2026-08-28 实测校准版）

> **此文件为AI编程助手的强制上下文。每次开发会话开始时必须完整读取。**
> **本文件已于 2026-08-28 按代码实测全面校准**（端点/表数/版本/端口均经脚本与运行时实测核对）。
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
- [ ] 我知道 Python 运行时是**嵌入式 3.10.11**（runtime/py310 唯一解释器；vLLM 子进程独用 py313），不是 3.12
- [ ] 我知道后端用 FastAPI 0.141 + Pydantic v2，不是 Django/Flask
- [ ] 我知道**没有 Tauri/Rust 壳层**——ADR-002（2026-08-20）已裁剪，交付形态 = launcher + 系统浏览器
- [ ] 我知道数据库是 SQLite（WAL，**31 张用户表**，user_version=7）+ ChromaDB，不是 PostgreSQL/MongoDB
- [ ] 我知道任务队列是**进程内 PriorityQueue（模拟 Celery 语义）**，没有 Celery/Redis 依赖（Redis 仅缓存可选降级）
- [ ] 我知道所有CSS颜色必须用 var(--color-*)（权威源 frontend/src/styles/tokens.css），禁止硬编码
- [ ] 我知道在进行所有项目操作和决策过程中，必须严格遵循团队协作模式（产品/设计/前端/后端/测试各司其职，决策基于讨论）

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
| 后端语言 | **Python 3.10.11**（runtime/py310 嵌入式；pydeps/ 156 包双站点承载） | Python 3.12 |
| 后端框架 | **FastAPI 0.141.1 + Pydantic v2 + uvicorn** | SQLAlchemy ORM（2.0.51 装而未用，全裸 sqlite3） |
| AI 推理 | **torch 2.11.0+cu128 + diffusers 0.39 + transformers**；GGUF 走 llama.cpp；vLLM 0.26.0+cu128 子进程（py313，AWQ/GPTQ 自动路由） | — |
| 关系数据库 | **SQLite WAL + FTS5**（31 张用户表，PRAGMA user_version=7） | — |
| 向量数据库 | **ChromaDB 1.5.9**（bge-large-zh 1024 维，不可用时降级 TF-IDF 内存检索） | — |
| 任务队列 | **进程内 PriorityQueue**（high/medium/low/background 四级，模拟 Celery 语义）+ WS Hub 广播 | Celery + Redis（F-13 已裁定 ⚪ 豁免） |
| 浏览器 | **Playwright Chromium 进程池**（学习代理用）+ 用户系统浏览器 | CEF 120+ |
| 加密 | **AES-256-GCM 字段级**（crypto.py）+ Windows DPAPI 密钥保护 | SQLCipher（未启用） |
| 包管理 | 后端 **pip + requirements-lock.txt**（154 包钉版）；前端 **pnpm** | uv |
| 静态检查 | **ruff**（ruff.toml） | mypy（未安装） |
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

### 2.3 路由（React Router 7，Hash 模式，实测 9 个一级路由）

```
/chat      → components/dialog/DialogPage.tsx
/paint     → components/paint/PaintPage.tsx
/storyboard→ components/manga/MangaPage.tsx
/learning  → components/learning/LearningPage.tsx
/models    → components/model/ModelManager.tsx
/style     → components/style/StylePage.tsx
/settings  → components/Settings.tsx
/logs      → components/logs/LogsPage.tsx（2026-08-21 新增）
/help      → components/help/HelpPage.tsx
/ 与 * 重定向 /chat；无 /about、无 /comic（旧文档口径作废）
```

### 2.4 3D 导演台

现状是**原生 Three.js 命令式封装**（`src/three/SceneManager.ts` 等六件套 + `components/manga/DirectorStage.tsx`）。
若未来引入 @react-three/fiber 重写，属技术栈变更，须先立 ADR 再动工；日常迭代沿用现有封装。

### 2.5 CSS 变量体系（已落地，权威源 tokens.css）

```css
/* 禁止硬编码，MUST使用变量 —— frontend/src/styles/tokens.css 为唯一权威 */
--color-primary: #FF6B9D;      /* Sakura 樱花粉主色 */
--color-accent: #4ECDC4;       /* 薄荷绿 */
--color-bg: #1A1A2E;  --color-card: #16213E;  --color-input-bg: #0F3460;
--color-success: #6BCB77;  --color-warning: #FFD93D;  --color-error: #FF6B6B;
/* 亮色主题经 [data-theme="light"] 切换；sakura-*/mint-* 色阶见 sakura-theme.css @theme */
```

### 2.6 前端禁止项（保留有效）

| 禁止 | 正确替代 |
|------|---------|
| `any` 类型 | `unknown` + 类型守卫 / Zod schema（services/schema.ts） |
| 硬编码颜色 | `var(--color-*)` |
| `!important` | 提升选择器特异性 |
| useEffect无cleanup | MUST return cleanup函数 |
| 直接操作数据库 | 调后端API |
| WebSocket传AI token | SSE（POST /api/v1/chat/stream，F-011 裁定） |

---

## 3. 后端编码铁律（Python 3.10）

### 3.1 绝对规则

| 规则 | 说明 |
|------|------|
| 全部函数MUST写type hints | `def foo(x: int) -> str:` |
| API 数据模型MUST用Pydantic v2 | 集中在 backend/data/models.py |
| asyncio循环中禁止同步阻塞 | 同步推理唯一入口 `services/offload.py`（run_blocking/sync_core，P1-06 有测试锁定） |
| 大模型卸载MUST三件套 | `del model; torch.cuda.empty_cache(); gc.collect()` |
| 禁止循环中重复加载模型 | 引擎为模块级懒加载单例（双重检查锁） |
| SQLite 写入串行化 | threading.local 连接 + 全局 _write_lock；批量写走 execute_in_transaction |
| 静态检查 | ruff check 通过（提交前） |
| 降级必须诚实 | 不可用功能返回 degraded:true + degrade_reason，禁止伪造结果 |

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

## 4. API路由现状（实测 2026-08-28）

```
Base URL: http://127.0.0.1:5800/api/v1   （直接 uvicorn / config.yaml）
          launcher 默认 --port 8765（冲突扫描 5800-5835）—— 两套端口并存，联调前先确认

14 个路由模块（main.py _API_MODULES）：
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
└── /api/v1/logs                    日志可视化

WebSocket 4 个：/ws（消息中枢）、/api/v1/dialog/stream/{session_id}（已废弃，兼容保留）、
               /api/v1/hardware/realtime、/api/v1/learn/session/progress
静态托管：frontend/dist（Hash Router，_ApiAwareMount 保护 API 命名空间）
```

> 旧版 CLAUDE.md 的 /chat /paint /storyboard /director /video 五前缀路由表来自文档E附录B，与实现不符，作废。

---

## 5. 数据库规则（实测 2026-08-28）

| 数据库 | 用途 | 规则 |
|--------|------|------|
| SQLite (WAL) | `data/omnispace.db`，**31 张用户表**（database.py `_SCHEMA` 18 张 + 服务层自建 13 张） | PRAGMA user_version=**7**（迁移组机制，历史组禁改只许追加）；FTS5 知识全文检索 |
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

---

## 7. 安全与资源策略（阈值真源 = backend/config.yaml scheduler.thresholds）

| 显存阈值（config.yaml 实值） | 动作 |
|------|------|
| ≥ 80%（gpu_vram_warning） | 警告，调度器介入 |
| ≥ 90%（gpu_vram_critical） | 降精度/卸载非活跃模型 |
| ≥ 95%（gpu_vram_force_unload） | 强制卸载 |

GPU 利用率：>85% 持续 5s 轻度降参（steps×0.67）；>95% 持续 15s 深度降档（×0.4）；4s 滞回（S4，2026-08-28）。
温度：热保护 ≥90°C 拒绝新任务（feature_lock 层）；空闲回收：功能锁空闲 60s 释放小模型 / 300s 卸载非驻留大模型。

其他安全（保留有效）：零信任输入校验；上传闸门（扩展名白名单 + 文件头魔数，PE/ELF 黑名单）；
回环绑定闸门（非 127.0.0.1 拒绝启动，豁免 OMNISPACE_ALLOW_LAN=1）；TrustedHost 防 DNS 重绑定；限流 300 req/min/端点。

---

## 8. 性能指标（性质：验收目标值；非实测现状）

> 2026-08-28 实测锚点（RTX 5070 Ti 16GB）：对话 qwen3-vl-4b 首字 **461ms**；
> SDXL 768² 20步 **13.6s**；wan22-ti2v-5b I2V 2s/720p **83.4s**；后端冷启动 **T+10s 就绪**。
> 旧版本节的"当前值"列（TTFT 240ms / 启动 4.2s 等）无实测来源，作废。

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

**项目硬件基线：RTX 3060 12GB + 32GB RAM（入门档位）**——模型选型硬约束（HARDWARE_TIER_TABLE 按 min_vram 分档路由）。
开发实测机：RTX 5070 Ti 16GB。所有功能 MUST 在 12GB 档可用。

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
| 全量技术文档（协作者上手版） | docs/omnispace-tech-doc/（HTML，含 12.4 差异对照） | 新人上手 |
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
9. **3D开发**：沿用 src/three/ 自研封装，勿引入原生 new THREE.Scene 散落在组件里
10. **状态管理**：用 Zustand create() 创建 store，不要用 Context+useReducer

---

> **此文件是AI开发的行为宪法。违反此文件 = 引入Bug = 项目风险。**
> **但记住：本文件描述的是 2026-08-28 的实测现状——代码再次演化后，请先更新本文件再编码。**
