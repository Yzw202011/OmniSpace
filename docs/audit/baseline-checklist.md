# OmniSpace AI v2.3.1 合规基线清单（唯一合规基线）

> 本文件是 OmniSpace AI v2.3.1 项目后续全部开发/审计工作的**唯一合规基线**。
> 全部条目均可机器核对或人工逐条打勾；每条标注文档依据，可回溯原文。
> 本文档为研究产物，不修改任何代码；文档与实现的偏差一律在第 10 章登记裁决。

---

## 0. 阅读指南

### 0.1 文档代号

| 代号 | 文件 | 性质 | 行数 |
|------|------|------|------|
| A | `OmniSpace AI v2.3.1（修正版A）.txt` | 开发计划（38 项核心要求、任务清单、自适应方案） | ~929 |
| B | `OmniSpace AI v2.3.1中文版（修正版B）.txt` | **唯一权威全量技术文档（终极版，可信）** | 1492 |
| C | `OmniSpace AI  v2.3.1编程语言规范（修改版C）.txt` | 8 种语言规范与协同方案 | ~706 |
| D | `OmniSpace_AI_v2.3.1_布局视觉与规范（修正版D）.txt` | 布局视觉规范 + CSS 变量 + 修正条款 | 239 |
| E | `OmniSpace_AI_v2.3.1_极致细粒度全量文档（修正版E）.txt` | 极致细粒度全量文档（12 章 + 5 附录） | 4062 |

### 0.2 矛盾裁决优先级（用户指定）

```
B（可信） > E（细粒度补充） > D（布局+修正条款） > A（计划） > C（语言规范，正交领域优先）
```

- 正交领域（如语言编码细则）以 C 为准，因其为唯一专门规范且不与其他文档冲突。
- 每条矛盾记录：各文档怎么写 → 裁决结果 → 理由（见第 10 章）。

### 0.3 引用记法

`文档代号-章节号`，例如：`B-9.1` = 文档 B 第 9.1 节；`E-附录B` = 文档 E 附录 B；`A-1.5` = 文档 A 第 1.5 节。

### 0.4 章节条目统计（已自审核对）

| 章节 | 内容 | 条目数 |
|------|------|--------|
| 1 | 技术栈与版本基线 | 40 |
| 2 | API 契约基线 | 26 |
| 3 | 数据库 Schema 基线 | 22 |
| 4 | 前端布局与视觉基线 | 47 |
| 5 | 功能模块与任务清单基线 | 123 |
| 6 | 引擎与调度规则基线 | 68 |
| 7 | v2.3.1 五项优化验收标准 | 32 |
| 8 | 编程语言规范基线 | 42 |
| 9 | 性能量化指标与落地保障基线 | 36 |
| 10 | 文档矛盾记录与裁决 | 20 |
| **合计** | | **456** |

### 0.5 核对方式

- 「机器可核对」= 可通过文件检索/配置读取/接口调用/SQL 查询直接验证（给出验证方法）。
- 「人工核对」= 需 UI 走查或主观判断（给出判定标准）。
- 凡带 ⚠ 标记的条目表示与当前实现存在已知偏差，裁决见第 10 章。

---

## 1. 技术栈与版本基线

验收总方法：核对 `backend/requirements.txt`（或 pyproject.toml）、`frontend/package.json`、`src-tauri/Cargo.toml` 中的版本约束；运行 `python -V`、`node -v`、`cargo --version`；`nvidia-smi` 查驱动。

### 1.1 后端技术栈（依据 B-8.2.2）

| # | 检查项 | 基线要求 | 验收标准（机器可核对） | 依据 |
|---|--------|----------|------------------------|------|
| 1.1 | Python | **3.12** ⚠ | `python -V` 输出 3.12.x（当前内嵌运行时 3.10.11，偏差见 M-03） | B-8.2.2, C-一 |
| 1.2 | FastAPI | ≥0.115 | requirements 中 `fastapi>=0.115` 且 `pip show fastapi` 满足 | B-8.2.2 |
| 1.3 | Uvicorn | ≥0.30 | `uvicorn>=0.30` | B-8.2.2 |
| 1.4 | Celery | ≥5.4 | `celery>=5.4`，后台任务统一走 Celery | B-8.2.2, A-5.1 |
| 1.5 | Redis | ≥7（嵌入式，Celery broker） | 进程内嵌或本机 Redis 7.x，Celery broker URL 指向之 | B-8.2.2 |
| 1.6 | SQLAlchemy | 2.0（异步） | `sqlalchemy==2.*`，使用 `asyncio` 扩展 + aiosqlite | B-8.2.2, C-2.1 |
| 1.7 | Pydantic | v2 | `pydantic>=2`，全部数据模型经 Pydantic v2 校验 | B-8.2.2, C-2.1 |
| 1.8 | PyTorch | **2.8 stable（cu124 + cu128）** ⚠ | `torch.__version__` 以 2.8 为基线（当前 2.11.0+cu128，偏差见 M-04） | B-1.4, B-8.2.2 |
| 1.9 | transformers | latest（锁定版本） | requirements 锁定具体版本号，禁止不指定版本安装 | B-8.2.2, C-2.1 |
| 1.10 | diffusers | latest（锁定） | 同上 | B-8.2.2 |
| 1.11 | peft | latest（锁定） | 同上，LoRA 微调使用 | B-8.2.2 |
| 1.12 | accelerate | latest（锁定） | 同上，训练加速/DeepSpeed 配置载体 | B-8.2.2, B-5.5 |
| 1.13 | cefpython3 | latest（锁定） | 内置 CEF 浏览器绑定可用 | B-8.2.2 |
| 1.14 | ChromaDB | latest（锁定） | 向量库，HNSW 索引 | B-8.2.2, D-4.3.3 |
| 1.15 | 文本嵌入模型 | BGE-large-zh | RAG 向量化统一使用 BGE-large-zh，向量维度 1024 | B-8.2.2, B-8.3.1, D-4.3.2 |
| 1.16 | XGBoost | latest（锁定） | ML 预加载预测引擎 | B-8.2.2, A-1.3 |
| 1.17 | loguru | latest（锁定） | 后端日志统一 loguru | B-8.2.2 |
| 1.18 | SQLite | WAL 模式 | 连接后 `PRAGMA journal_mode` 返回 `wal` | B-8.2.2, C-2.5 |
| 1.19 | FFmpeg | 7.x | `ffmpeg -version` 主版本 7 | B-8.2.2 |
| 1.20 | SVT-AV1 | 2.x | 软编链路可用 `--preset 6` | B-8.2.2, A-1.5 |

### 1.2 前端技术栈（依据 B-8.2.1）

| # | 检查项 | 基线要求 | 验收标准（机器可核对） | 依据 |
|---|--------|----------|------------------------|------|
| 1.21 | React | 19 | package.json `react: ^19` | B-8.2.1 |
| 1.22 | Vite | 6 | `vite: ^6` | B-8.2.1 |
| 1.23 | TypeScript | 5.x | `typescript: ^5`，`tsc --noEmit` 通过 | B-8.2.1, C-2.2 |
| 1.24 | Tailwind CSS | 4 | `tailwindcss: ^4`，集成 Sakura 主题 Token | B-8.2.1, A-5.1 |
| 1.25 | Three.js | r170 | `three` 版本 0.170.x；3D 导演台 WebGL 浏览器内渲染，无独立进程 | B-8.2.1, A-1.5(TASK-COMIC-006) |
| 1.26 | Zustand | 5 | 全局状态管理仅用 Zustand，禁止引入 Redux | B-8.2.1, C-2.2 |
| 1.27 | React Router | 7 | `react-router: ^7` | B-8.2.1 |
| 1.28 | axios | latest（锁定） | HTTP 客户端统一 axios | B-8.2.1 |
| 1.29 | framer-motion | latest（锁定） | 动效库 | B-8.2.1 |
| 1.30 | react-markdown | latest（锁定） | 对话 Markdown 渲染 | B-8.2.1 |
| 1.31 | react-syntax-highlighter | latest（锁定） | 代码高亮 | B-8.2.1 |
| 1.32 | react-flow | latest（锁定） | 知识图谱渲染（v2.3.1） | B-8.2.1, B-5.5 |

### 1.3 底层/原生技术栈（依据 B-8.2.3, C）

| # | 检查项 | 基线要求 | 验收标准 | 依据 |
|---|--------|----------|----------|------|
| 1.33 | Rust | 1.75+，Tauri 2.x | `cargo --version` ≥1.75；`tauri = "2"` | C-一, B-8.2.3, A-1.5(TASK-SYS-005) |
| 1.34 | C/C++ | CEF 引擎/FFmpeg 编解码/图像算子/SimHash | 代码占比 ≤8%，全部经 pybind11 暴露 | B-8.2.3, C-2.4 |
| 1.35 | CUDA | 12.1+（驱动 ≥535.xx） | `nvidia-smi` 显示 CUDA ≥12.1 | A-1.4 |
| 1.36 | pybind11 | Python↔C++ 唯一桥 | 模块命名 `_omnispace_{module}`，带 .pyi 存根 | C-3.2.3 |

### 1.4 运行环境基线（依据 A-1.1, A-1.4, B-1.1）

| # | 检查项 | 基线要求 | 验收标准 | 依据 |
|---|--------|----------|----------|------|
| 1.37 | 操作系统 | Windows 10 21H2+ / Windows 11 64 位 (x64) | 安装器/启动器校验 OS 版本，不满足则拒绝或警告 | A-1.1, B-1.1 |
| 1.38 | GPU 底线 | NVIDIA RTX 3060 12GB / RTX 4060 8GB；推荐 RTX 4090 24GB / 5090 32GB | 硬件探测输出档位（见 6.7 六档映射） | A-1.4, B-4.2 |
| 1.39 | 系统内存 | ≥32GB（低于此进入受限档） | `psutil.virtual_memory().total` ≥32GB 为全功能门槛 | A-1.4 |
| 1.40 | 其他依赖 | DirectX 12、AV1 Codec 支持 | 启动自检输出检测结果 | A-1.4 |

---

## 2. API 契约基线

> 依据：B-9.1（接口规范）、E-9.1/附录B、C-3.2.2、D-4.2.5。四者信封格式完全一致，无矛盾。
> ⚠ 前缀与端口存在文档 vs 实现偏差（M-01/M-02），本节按**文档基线**记录。

### 2.1 基础约定

| # | 检查项 | 基线要求 | 验收标准（机器可核对） | 依据 |
|---|--------|----------|------------------------|------|
| 2.1 | API 基础 URL | `http://localhost:8000/api/v1` ⚠ | 配置文件中 `API_PREFIX="/api/v1"`、`PORT=8000`（当前实现 `/v1` + 5800，见 M-01/M-02） | B-9.1, C-3.2.2, E-9.1 |
| 2.2 | 路由前缀 | 所有 API 路由统一 `/api/v1/` 前缀 | 路由注册表全部命中前缀；OpenAPI 文档路径以此前缀开头 | B-9.1 |
| 2.3 | WebSocket | `ws://localhost:8000/ws/{module}` | WS 端点按模块划分并文档化 | B-9.1 |
| 2.4 | 请求体 | JSON | `Content-Type: application/json`；文件上传例外（multipart） | D-4.2.5, C-3.2.2 |
| 2.5 | 时间格式 | ISO 8601 UTC（如 `2026-08-07T00:45:00Z`） | 所有响应/入库时间字符串可被 ISO8601 解析且带时区 | C-3.2.2, D-4.3.2 |
| 2.6 | 认证 | 本地 Token（Tauri 启动时生成并注入前端） | 未授权请求返回 401 | C-3.2.2, A-5.1(TASK-SYS-011) |

### 2.2 统一响应信封（四文档一致，零矛盾）

| # | 检查项 | 基线要求 | 验收标准 | 依据 |
|---|--------|----------|----------|------|
| 2.7 | 成功响应 | `{success:true, data:{...}, error:null, meta:{request_id,timestamp,duration_ms}}` | 随机抽 10 个端点，响应 JSON 四键齐全；`request_id` 为 UUID | B-9.1.1, E-9.1.1, D-4.2.5, C-3.2.2 |
| 2.8 | 失败响应 | `{success:false, data:null, error:{code,message,detail,suggestion?}, meta:{...}}` | 制造 1 个业务错误，error 四子字段齐全（suggestion 可空） | 同上 |
| 2.9 | 分页响应 | `data:{items, total, page, page_size}` | 任一分页端点返回四字段且数值自洽 | B-9.1.1, D-4.2.5 |
| 2.10 | 分页参数 | 统一 `page` + `page_size` | 全部列表端点接受该两参数 | C-3.2.2 |
| 2.11 | 裸数据禁令 | 禁止直接返回裸数据（必须包信封） | 全部端点响应顶层必含 `success` | E-9.1.1 |
| 2.12 | TS 泛型 | 前端 `ApiResponse<T>` 泛型与信封一一对应 | `shared/api_types.ts`（或等价）存在该 interface | D-4.1.1 |

### 2.3 RESTful 与版本控制

| # | 检查项 | 基线要求 | 验收标准 | 依据 |
|---|--------|----------|----------|------|
| 2.13 | HTTP 动词 | GET/POST/PUT/DELETE/PATCH 语义化使用 | OpenAPI 中动词与动作语义匹配（查询=GET、创建=POST 等） | D-4.2.5 |
| 2.14 | 资源命名 | 复数名词（如 `/api/v1/users`） | 路由表抽查 | D-4.2.5 |
| 2.15 | 过滤/排序/分页 | 使用查询参数 | 列表端点支持查询参数 | D-4.2.5 |
| 2.16 | 版本控制 | URL 路径版本（`/api/v1/`） | 所有路由含版本段 | D-4.2.5 |
| 2.17 | 向后兼容 | 废弃接口添加 `Deprecation` Header | 存在废弃接口时响应含该头 | D-4.2.5 |

### 2.4 错误码体系（字符串语义化，6 类 24 码）

| # | 类别 | 错误码 | 依据 |
|---|------|--------|------|
| 2.18 | 1xxx 模型 | `MODEL_NOT_LOADED` / `MODEL_LOAD_FAILED` / `VRAM_INSUFFICIENT` / `MODEL_INFERENCE_FAILED` | D-错误码, A-9.1, C-3.2.2 |
| 2.19 | 2xxx 知识 | `KNOWLEDGE_PARSE_FAILED` / `KNOWLEDGE_VECTOR_TIMEOUT` / `KNOWLEDGE_TRAINING_EMPTY` / `KNOWLEDGE_RAG_FAILED` | 同上 |
| 2.20 | 3xxx 浏览器 | `BROWSER_CRASHED` / `BROWSER_POOL_EXHAUSTED` / `BROWSER_PAGE_TIMEOUT` / `BROWSER_NAVIGATION_FAILED` | 同上 |
| 2.21 | 4xxx 训练 | `TRAINING_OOM` / `TRAINING_LOSS_NAN` / `TRAINING_CHECKPOINT_FAILED` / `TRAINING_DATA_CORRUPTED` | 同上 |
| 2.22 | 5xxx 系统 | `SYSTEM_PARAM_INVALID` / `SYSTEM_UNAUTHORIZED` / `SYSTEM_RESOURCE_NOT_FOUND` / `SYSTEM_INTERNAL_ERROR` / `SYSTEM_DISK_FULL` | 同上 |
| 2.23 | 6xxx 前端 | `FRONTEND_RENDER_ERROR` / `FRONTEND_NETWORK_ERROR` / `FRONTEND_STATE_INVALID` / `FRONTEND_WEBSOCKET_DISCONNECTED` | 同上 |

验收：代码库全局检索错误码字符串常量；任一 error.code ∈ 上表枚举；禁止数字错误码。

### 2.5 流式/上传/模块路由

| # | 检查项 | 基线要求 | 验收标准 | 依据 |
|---|--------|----------|----------|------|
| 2.24 | 流式输出 | AI 对话用 SSE（Server-Sent Events）逐 token 返回 | `/chat/stream` 响应 `text/event-stream`，前端流式渲染 | B-8.3.1, C-3.2.2 |
| 2.25 | 文件上传 | `multipart/form-data` | 导入文档/模型端点支持 multipart | C-3.2.2, B-7.1.2 |
| 2.26 | WS 消息结构 | 必须 `{type, payload}` | 抽查 WS 推送消息含 type 字段 | C-3.2.2 |

模块路由组（裁决后基线，命名矛盾见 M-05/06/07）：

| 模块 | 基线路由组 | 依据 |
|------|-----------|------|
| AI 对话 | `/api/v1/chat` | B-7.1.1, E-附录B.1 |
| AI 绘画 | `/api/v1/paint`（B 为准；E 作 `/art` 被否决） | B-7.1.1 |
| 漫剧分镜 | `/api/v1/storyboard` | B-7.1.1 |
| 3D 导演台 | `/api/v1/director`（子路由 /scene, /render, /export） | B-7.1.3 |
| 视频生成 | `/api/v1/video`（/generate, /progress, /export） | B-7.1.3 |
| 知识学习 | `/api/v1/learn`（21 个端点，见 E-附录B.4 + B-7.1.2） | B-7.1.2, E-附录B.4 |
| 模型管理 | `/api/v1/models`（B 为准；E 作 `/model` 被否决） | B-7.1.1 |
| 视频风格 | `/api/v1/style` | B-7.1.1, E-附录B.6 |
| 系统设置 | `/api/v1/system` | B-7.1.1, E-附录B.7 |
| 内置浏览器 | `/api/v1/browser`（v2.3 新增） | B-7.1.1 |

E-附录B 全量端点（7 模块 78 端点）为**端点级细粒度基线**：chat 9、art 12、comic 13、learn 21、model 10、style 8、system 9；路由组名与 B 冲突时以 B 为准，组内端点定义以 E 为准（E 为细粒度补充）。

---

## 3. 数据库 Schema 基线

> 依据：E-附录C（8 张核心表 + FTS5）、D-4.3.2/4.3.3（修正条款：SQLite 兼容类型与索引策略）、C-2.5（SQL 铁律）。

### 3.1 SQLite 通用规则

| # | 检查项 | 基线要求 | 验收标准（机器可核对） | 依据 |
|---|--------|----------|------------------------|------|
| 3.1 | 日志模式 | `PRAGMA journal_mode=WAL` | 连接执行该 PRAGMA 返回 `wal` | C-2.5, B-8.2.2 |
| 3.2 | 同步级别 | `PRAGMA synchronous=NORMAL` | PRAGMA 返回 1 | C-2.5 |
| 3.3 | 参数化查询 | 全部 SQL 参数化，禁止字符串拼接 | 静态扫描 `execute(f"` / `%` 拼接为 0 处 | C-2.5 |
| 3.4 | 字符串类型 | VARCHAR(255) 或 TEXT | schema 核对 | D-4.3.2 |
| 3.5 | 整数类型 | INTEGER 或 BIGINT | schema 核对 | D-4.3.2 |
| 3.6 | 布尔类型 | BOOLEAN | schema 核对 | D-4.3.2 |
| 3.7 | 精确数值 | DECIMAL(precision, scale) | schema 核对 | D-4.3.2 |
| 3.8 | 日期时间 | TEXT 存 ISO 8601 字符串 | 抽查数据行可被 ISO8601 解析 | D-4.3.2 |
| 3.9 | JSON 数据 | TEXT 存 JSON 字符串，应用层 Pydantic v2 / Zod 校验 | JSON 字段写入前经 schema 校验 | D-4.3.2, C-2.8 |
| 3.10 | 向量数据 | 由 ChromaDB 独立管理（HNSW，1024 维），**不入 SQLite** | SQLite 中无 BLOB 向量列 | D-4.3.2 |
| 3.11 | 大字段策略 | 内容 >64KB 时存文件系统，SQLite 只存路径+元数据 | 超 64KB 内容行有 file_path 而非内联 | C-2.5 |
| 3.12 | 写并发 | 单写锁规避：写操作经单一连接池串行化；禁止多进程同写一库 | 后端仅一个写连接池 | C-2.5 |
| 3.13 | 文件权限 | 数据库文件权限 600（类 Unix）/ 等效 ACL | 权限检查 | C-2.5 |

### 3.2 核心表结构（E-附录C，共 8 表 + 1 FTS5 虚拟表）

> 当前实现首启建 13 表（超集），基线要求**不得缺少**下列任何一张表及关键字段（见 M-14）。

| # | 表 | 关键字段/约束（基线最小集） | 索引/特殊 | 依据 |
|---|----|------------------------------|-----------|------|
| 3.14 | users | id(TEXT PK UUID), username(VARCHAR64 UNIQUE NN), email(VARCHAR255 UNIQUE), password_hash(TEXT NN, bcrypt), hardware_tier(VARCHAR16 NN), preferences(TEXT JSON), is_active, created_at, updated_at | idx_users_username, idx_users_email | E-C.1 |
| 3.15 | projects | id(PK), user_id(FK→users), name(NN), project_type(chat/paint/comic/learn), status 默认 active, metadata(JSON) | idx_projects_user_id, idx_projects_type | E-C.2 |
| 3.16 | assets | id(PK), project_id(FK), user_id(FK), asset_type(image/video/audio/model), file_path, file_size(BIGINT), sha256_hash, width/height/duration_ms, tags(JSON) | idx_assets_project_id, idx_assets_user_id | E-C.3 |
| 3.17 | learning_sessions | id(PK), topic_id(FK), user_id(FK), status(running/paused/stopped/error), progress(REAL 0~1), pages_crawled, knowledge_extracted, checkpoint_data(JSON), started_at/paused_at/completed_at | — | E-C.4 |
| 3.18 | knowledge_entries | id(PK), topic_id(FK), session_id(FK), title(VARCHAR500), content, content_hash(SimHash), source_url, source_type(webpage/document/clipboard), importance_score, embedding_id(ChromaDB ID), access_count, is_archived | + FTS5 | E-C.5 |
| 3.19 | learning_topics | id(PK), user_id(FK), name, seed_urls(JSON), learning_depth(1-5 默认3), total_knowledge, total_sessions | — | E-C.6 |
| 3.20 | lora_trainings | id(PK), user_id(FK), name, base_model, rank(INT), alpha(REAL), learning_rate, epochs, batch_size, status(running/completed/failed), loss_history(JSON), weight_path, previous_version | — | E-C.7 |
| 3.21 | model_configs | id(PK), model_name(UNIQUE NN), model_type(llm/diffusion/video/embed), file_path, file_size, sha256_hash(NN), quantization(fp32/fp16/bf16/int8/fp8 默认fp16), vram_required, ram_required, is_loaded, load_priority(P0-P5 默认3) | — | E-C.8 |
| 3.22 | knowledge_fts（虚拟表） | `CREATE VIRTUAL TABLE knowledge_fts USING fts5(title, content, tags, content='knowledge_entries', content_rowid='rowid', tokenize='unicode61')` | FTS5 全文检索（v2.3.1 混合检索关键词通道） | E-C.5, C-2.5, B-5.5 |

验收：首启自动建库后 `.schema` 含全部 9 张表结构；关键字段非空约束生效；FTS5 表可对中文分词检索。

---

## 4. 前端布局与视觉基线

> 依据：D（布局+CSS 变量+修正条款）、B-6.1~6.3（布局与 Sakura 主题）。色值冲突裁决见 M-09/M-12。

### 4.1 五层布局架构（D-1.1.1，与 B-6.1.1 一致）

| # | 检查项 | 基线要求 | 验收标准（机器可核对） | 依据 |
|---|--------|----------|------------------------|------|
| 4.1 | 顶层导航栏 | 固定高 48px，Z-index 最高；左 Logo / 中全局搜索 / 右通知+头像下拉+主题切换 | CSS `--header-height: 48px` 存在且生效 | D-1.1.1 |
| 4.2 | 左侧边栏 | 展开 260px / 折叠 64px；含树形菜单、空间切换器、底部收藏 | `--sidebar-width: 260px`、`--sidebar-collapsed-width: 64px` | D-1.1.1, B-6.1.2 |
| 4.3 | 主内容区 | 剩余自适应（flex:1），多标签页，每 Tab 独立路由/视图 | DOM 结构核对 | D-1.1.1 |
| 4.4 | 右侧面板 | 展开 320px，可折叠；上下文属性/操作历史/AI 对话侧栏 | `--right-panel-width: 320px` | D-1.1.1, B-6.1.3 |
| 4.5 | 底部状态栏 | 固定高 28px 贴底；状态灯、API 连接、版本号 v2.3.1、CPU/内存进度条 | `--statusbar-height: 28px` | D-1.1.1, B-6.1.4 |

### 4.2 响应式断点（D-1.1.2）

| # | 断点 | 基线行为 | 验收标准（人工核对） | 依据 |
|---|------|----------|----------------------|------|
| 4.6 | Desktop ≥1440px | 完整四栏布局 | 缩窗走查 | D-1.1.2 |
| 4.7 | Laptop 1280–1439px | 右面板默认折叠 64px 图标态 | 同上 | D-1.1.2 |
| 4.8 | Tablet 768–1023px | 左侧栏折叠 64px；右面板隐藏（浮动按钮唤出） | 同上 | D-1.1.2 |
| 4.9 | Mobile <768px | 单栏；侧栏全屏 Overlay；状态栏隐藏，底部导航替代 | 同上 | D-1.1.2 |

### 4.3 CSS 变量体系（D-1.1.3，必须 CSS Custom Properties；默认深色，亮色可切换）

| # | 变量组 | 基线内容 | 验收标准 | 依据 |
|---|--------|----------|----------|------|
| 4.10 | 颜色变量 | `--color-primary:#FF6B9D`、`--color-secondary:#4ECDC4`、`--color-accent:#FFB3C6`、`--color-accent-warm:#FFD93D`、bg-primary/secondary/tertiary/card/input/overlay、text-primary~disabled、border(-hover)、success/warning/error/info 全套 | 全局样式表 `:root` 逐一检索存在 | D-1.1.3 |
| 4.11 | 尺寸变量 | sidebar 260/64、header 48、statusbar 28、right-panel 320、radius-sm 4/md 8/lg 12 | 同上 | D-1.1.3 |
| 4.12 | 间距变量 | xs4/s8/m16/l24/xl32 | 同上 | D-1.1.3 |
| 4.13 | 字体变量 | xs12/sm14/md16/lg18/xl24/2xl32/3xl40/4xl48 | 同上 | D-1.1.3 |
| 4.14 | 阴影变量 | sm/md/lg/xl/2xl（深色 rgba(0,0,0,.3~.5)） | 同上 | D-1.1.3 |
| 4.15 | 过渡变量 | fast 150ms / normal 250ms / slow 400ms ease-in-out | 同上 | D-1.1.3 |
| 4.16 | 亮色主题 | `[data-theme="light"]` 覆盖整套变量；默认深色 | 切换主题变量值变化 | D-1.1.3 |

### 4.4 Sakura 主题色值（⚠ B/D 冲突，裁决后基线，见 M-09）

| # | 语义 | 裁决色值（B 为准） | D 原文（被否决） | 依据 |
|---|------|--------------------|-------------------|------|
| 4.17 | 主色（樱花粉） | #FF6B9D | #FF6B9D（一致） | B-6.3.1 |
| 4.18 | 辅助色（薄荷绿） | #4ECDC4 | #4ECDC4（一致） | B-6.3.1 |
| 4.19 | 主背景（深蓝黑） | #1A1A2E | #1A1A2E（一致） | B-6.3.1 |
| 4.20 | 卡片色（深蓝） | #16213E | #16213E（一致） | B-6.3.1 |
| 4.21 | 输入背景 | #0F3460 | #0F3460（一致） | B-6.3.1 |
| 4.22 | 强调色（暖黄） | #FFD93D | #FFD93D（一致） | B-6.3.1 |
| 4.23 | 文字色（主） | **#EAEAEA** | #E8E8F0 | B-6.3.1 > D |
| 4.24 | 成功色 | **#6BCB77** | #5BA87A | B-6.3.1 > D |
| 4.25 | 警告色 | **#FFD93D** ⚠ 与强调色同色（B 内部瑕疵，见 M-19） | #E8A84C | B-6.3.1 |
| 4.26 | 错误色 | **#FF6B6B** | #E05555 | B-6.3.1 > D |
| 4.27 | 圆角 | 卡片 12px / 按钮 8px / 输入框 6px（D 补 sm 4px 用于小组件，见 M-12） | 4/8/12 | B-6.3.1, D-1.1.3 |
| 4.28 | 阴影 | 0 4px 12px rgba(0,0,0,0.3) | 分量级变量 | B-6.3.1 |

### 4.5 组件设计规范（B-6.3.2）

| # | 组件 | 基线样式 | 依据 |
|---|------|----------|------|
| 4.29 | 按钮-主要 | 背景 #FF6B9D、白字、圆角 8px、hover 加深 10% | B-6.3.2 |
| 4.30 | 按钮-次要 | 透明底、边框/文字 #4ECDC4 | B-6.3.2 |
| 4.31 | 按钮-危险 | 背景 #FF6B6B、白字 | B-6.3.2 |
| 4.32 | 输入框 | 背景 #0F3460、边框 #4ECDC4 30%、圆角 6px | B-6.3.2 |
| 4.33 | 卡片 | 背景 #16213E、圆角 12px、内边距 16px | B-6.3.2 |
| 4.34 | 导航项-活跃 | 左侧 3px 粉色条 + 背景高亮 | B-6.3.2 |
| 4.35 | 进度条 | 背景 #0F3460、填充渐变 #FF6B9D→#4ECDC4 | B-6.3.2 |
| 4.36 | 标签页 | 底部 2px 指示条，活跃粉色 | B-6.3.2 |
| 4.37 | 弹窗 | 背景 #16213E、圆角 16px、阴影加深 | B-6.3.2 |
| 4.38 | 状态指示灯 | 正常/警告/错误/未激活 四态 | B-6.3.2 |

### 4.6 动效与字体（B-6.3.3/6.3.1）

| # | 检查项 | 基线要求 | 依据 |
|---|--------|----------|------|
| 4.39 | 页面切换 | 淡入淡出 200ms | B-6.3.3 |
| 4.40 | 卡片悬停 | 上浮 2px + 阴影加深 150ms | B-6.3.3 |
| 4.41 | 按钮点击 | 缩放 0.95，100ms | B-6.3.3 |
| 4.42 | 进度条/弹窗/通知 | 300ms ease / 0.9→1+淡入 250ms / 右侧滑入 300ms | B-6.3.3 |
| 4.43 | 动画性能 | 仅 transform/opacity（禁 width/height/top/left/margin 动画） | C-2.6 |
| 4.44 | 字体 | PingFang SC, Microsoft YaHei, WenQuanYi Zen Hei, sans-serif | B-6.3.1 |
| 4.45 | 字号 | 标题 24 / 正文 14 / 辅助 12 | B-6.3.1 |

### 4.7 全局约束

| # | 检查项 | 基线要求 | 验收标准 | 依据 |
|---|--------|----------|----------|------|
| 4.46 | 界面语言 | 全中文 | UI 走查无英文残留（TASK-SYS-005） | B-5.1 |
| 4.47 | 列表性能 | >100 条列表必须虚拟列表（@tanstack/virtual 或等价）；知识图谱节点 >200 启用 LOD | 代码检索 + 卡顿走查 | C-2.6 |

---

## 5. 功能模块与任务清单基线

### 5.1 六大核心模块（B-1.2, A-1.2）

| # | 模块 | 技术选型基线 | 核心功能 | 依据 |
|---|------|--------------|----------|------|
| 5.1 | AI 对话 OmniChat | Qwen3-VL-8B/4B/2B | 多模态对话、Agent 工具调用、RAG 增强、上下文记忆 | B-1.2 |
| 5.2 | AI 绘画 OmniDraw | FLUX.1-dev / Kolors 2.1 / SDXL + ControlNet + IP-Adapter | 文生图、图生图、姿态控制、风格迁移、局部重绘 | B-1.2 |
| 5.3 | 漫剧创作 OmniComic | 分镜表 + 3D 导演台（Three.js r170）+ LTX-2/Wan2.1/CogVideoX | 分镜解析、3D 编排、视频生成、多镜一致性 | B-1.2 |
| 5.4 | 知识学习 OmniLearn（v2.3 新增） | CEF 浏览器 + AI Agent + LoRA 进化 | 网页解析、知识结构化、自动摘要、LoRA 增量训练 | B-1.2 |
| 5.5 | 模型管理 ModelManager | ~392GB 模型库 + 三方协同调度 + SQLite 元数据 | 索引、Hash 校验、热加载/冷卸载 | B-1.2, A-1.2.5 |
| 5.6 | 视频风格 VideoStyle | LTX-2 LoRA 微调引擎 | 视频风格训练/推理、帧间一致性 | B-1.2 |

### 5.2 五大智能引擎（B-1.2, A-1.3）

| # | 引擎 | 技术原理 | 触发条件 | 依据 |
|---|------|----------|----------|------|
| 5.7 | LTX-2 LoRA 微调引擎 | Diffusion Transformer 微调，QLoRA 4bit 量化增量训练，显存分块 | 用户提交训练任务 | B-1.2, A-1.3 |
| 5.8 | 模型预加载 ML 预测引擎 | XGBoost 时序预测，特征窗口 50 步 | 操作完成或空闲 >500ms | A-1.3, A-1.5 |
| 5.9 | 协同调度历史学习引擎 | 强化学习 PPO 优化资源分配 | 资源冲突/任务反馈 | A-1.3 |
| 5.10 | AV1 硬件编码引擎 | NVENC 硬编 + SVT-AV1 软编降级 | 视频生成/导出 | A-1.3 |
| 5.11 | 自主学习引擎 | RAG + Agent 决策树 + 增量学习闭环 | 用户授权/新知识源 | A-1.3 |

### 5.3 三十八项核心要求（A-1.5，逐条可验收）

| # | 编号 | 功能 | 验收标准 | # | 编号 | 功能 | 验收标准 |
|---|------|------|----------|---|------|------|----------|
| 5.12 | CHAT-001 | Qwen3-VL 多模态对话 | 首字延迟门槛 <500ms（B 裁决，见 M-10），显存 <12GB | 5.31 | COMIC-005 | CogVideoX 长视频 | 分块+重叠融合，接缝不可见 |
| 5.13 | CHAT-002 | Agent 工具调用 | Function Calling 成功率 >98% | 5.32 | COMIC-006 | 3D 场景渲染 | WebGL 浏览器内渲染，无独立进程，显存减半 |
| 5.14 | DRAW-001 | FLUX.1-dev 文生图 | 1024×1024 <30s（B 裁决，见 M-11；4090 档 <15s） | 5.33 | COMIC-007 | 帧间一致性 | 光流+Latent 约束，闪烁率 <1% |
| 5.15 | DRAW-002 | Kolors 2.1 中文优化 | 中文语义理解准确率 >95% | 5.34 | LEARN-001 | 内置浏览器 CEF | Chromium 120+ 沙箱隔离，内存泄漏 <50MB/h |
| 5.16 | DRAW-003 | SDXL+ControlNet 联动 | 姿态对齐误差 <2px | 5.35 | LEARN-002 | Agent 知识抽取 | 关键信息召回率 >90% |
| 5.17 | DRAW-004 | IP-Adapter 风格迁移 | 多图权重混合，SSIM >0.85 | 5.36 | LEARN-003 | LoRA 进化训练 | Loss 收敛且无灾难性遗忘 |
| 5.18 | COMIC-001 | 分镜表结构化解析 | 自定义 DSL，解析成功率 100% | 5.37 | LEARN-004 | 知识库 RAG 检索 | ChromaDB 检索延迟 <100ms |
| 5.19 | COMIC-002 | 3D 资产加载 | USDZ/glTF，10 万面 <3s | 5.38 | LEARN-005 | 浏览器安全策略 | CSP+进程隔离，无 XSS/CSRF |
| 5.20 | COMIC-003 | LTX-2 视频生成 | 720p/30fps，5s 视频 <60s | 5.39 | MODEL-001 | 模型库索引 | 392GB 索引构建 <30s |
| 5.21 | COMIC-004 | Wan2.1 视频生成 | 动态帧率，FVD <200 | 5.40 | MODEL-002 | 三方协同调度 | 显存/内存/IO 联合调度，冲突率 <1% |
| 5.22 | STYLE-001 | 视频风格 LoRA | 风格一致性 >0.9 | 5.41 | MODEL-003 | 基础包模型导入 | 拖拽/路径映射，自动识别注册 |
| 5.23 | ENG-001 | LTX-2 微调引擎 | 梯度检查点+显存分块，24GB 可训 720p LoRA | 5.42 | MODEL-004 | 多模型热切换 | 显存复用异步加载，UI 不卡顿 |
| 5.24 | ENG-002 | ML 预加载预测 | XGBoost 50 步窗口，准确率 >85% | 5.43 | SYS-001 | 离线包完整性 | SHA256 校验，失败自动修复或提示 |
| 5.25 | ENG-003 | 历史学习调度 | PPO，调度效率提升 >20% | 5.44 | SYS-002 | 显存监控保护 | OOM 拦截，自动卸载非关键模型 |
| 5.26 | ENG-004 | AV1 硬编 RTX50 | NVENC AV1 >120fps | 5.45 | SYS-003 | 用户数据隐私 | 本地加密存储，敏感数据不可读 |
| 5.27 | ENG-005 | AV1 硬编 RTX30/40 | NVENC AV1 >80fps ⚠（B 改为 H.264 硬编，见 M-20） | 5.46 | SYS-004 | 错误日志结构化 | JSON 含堆栈，可自动分析 |
| 5.28 | ENG-006 | AV1 软编 SVT-AV1 | 预设 6，CPU <80% 画质达标 | 5.47 | SYS-005 | 桌面壳层打包 | Tauri 2.x，安装包 <500MB |
| 5.29 | ENG-007 | 自主学习引擎 | 后台低优先级，创作延迟增加 <5% | 5.48 | ENG-008 | LoRA 权重合并 | 线性/非线性，合并后可用性验证 |
| 5.30 | ENG-009 | Checkpoint 管理 | 自动清理保留 Top-K，磁盘可控 | 5.49 | ENG-010 | 编码码率自适应 | CRF/VBR，文件大小符合预期 |

### 5.4 落地任务清单（B-5.1~5.4；A 有对应细化版本，冲突处 B 优先）

**P0 必做（19 项，B-5.1）**

| # | 编号 | 验收标准 | # | 编号 | 验收标准 |
|---|------|----------|---|------|----------|
| 5.50 | SYS-001 项目目录结构 | 目录创建完成可 git 追踪 | 5.60 | LEARN-002 Agent 决策循环 | 搜索→点击→阅读→提取完整流程，单页决策 <5s |
| 5.51 | SYS-002 后端基础框架 | uvicorn 可启动，/health 返回 ok | 5.61 | LEARN-003 知识提取管线 | 页面→知识→ChromaDB，>5 知识点/页 |
| 5.52 | SYS-003 前端基础框架 | npm run dev 可启动，Sakura 主题正确 | 5.62 | LEARN-004 RAG 检索注入 | 检索 <100ms，命中率 >75% |
| 5.53 | CHAT-001 对话后端 | SSE 流式，首 token <500ms，RAG <100ms | 5.63 | LEARN-005 行为学习记录器 | <10ms 延迟，不影响操作 |
| 5.54 | CHAT-002 对话前端 | 流式显示、Markdown、代码高亮 | 5.64 | MODEL-001 模型管理界面 | 模型列表/加载/卸载 |
| 5.55 | DRAW-001 绘画后端 | 文生图 1024×1024 <30s，ControlNet 可用 | 5.65 | SYS-004 三方协同调度 | GPU 显存分配正确 |
| 5.56 | DRAW-002 绘画前端 | 参数面板、进度实时更新、历史画廊 | 5.66 | SYS-005 Sakura 主题 UI | 全中文，视觉完整 |
| 5.57 | COMIC-001 分镜后端 | 分镜 CRUD+拖拽排序+AI 描述生成 | 5.67 | SYS-006 安装包打包 | 392GB 完整包可安装 |
| 5.58 | COMIC-002 3D 导演台 | Three.js >30fps，对象拖拽/旋转，截图导出 | 5.59 | COMIC-003 视频生成后端 | 5s 片段 <120s，LoRA 风格正确，AV1 编码 |
| 5.68 | LEARN-001 CEF 集成 | CEF 启动 <3s，点击/输入/截图正常 | | | |

**P1 重要（23 项，B-5.2）**

| # | 编号 | 验收标准 | # | 编号 | 验收标准 |
|---|------|----------|---|------|----------|
| 5.69 | LEARN-006 混合检索 | 命中率 +15%，延迟 +<20ms（详见第 7 章） | 5.81 | STYLE-002 视频风格迁移 | LoRA 应用+预览 |
| 5.70 | LEARN-007 Agent 分级决策 | 简单页面 <500ms（详见第 7 章） | 5.82 | SYS-010 项目管理 | 项目 CRUD+素材管理 |
| 5.71 | LEARN-008 LoRA 训练加速 | 单 epoch -30%，PPL 不降（详见第 7 章） | 5.83 | SYS-011 导出功能 | 视频/图片/分镜导出 |
| 5.72 | LEARN-009 浏览器进程池 | 就绪 <1s，无数据泄露（详见第 7 章） | 5.84 | LEARN-011 学习主题前端 | 创建/编辑/删除/进度 |
| 5.73 | LEARN-010 知识图谱可视化 | 拖拽/缩放/详情（详见第 7 章） | 5.85 | LEARN-012 浏览器实时查看 | 用户可见 AI 浏览内容 |
| 5.74 | STYLE-001 LTX-2 LoRA 引擎 | 用户训练自定义风格 | 5.86 | LEARN-013 LoRA 增量微调 | v1 基础上训练 v2 |
| 5.75 | SYS-007 ML 预测预加载 | 预测准确率 >85% | 5.87 | LEARN-014 知识质量评估 | 密度/相关性/准确性评分 |
| 5.76 | SYS-008 协同调度历史学习 | 调度策略自优化 | 5.88 | LEARN-015 会话检查点 | 断电恢复 <5s |
| 5.77 | SYS-009 AV1 硬件编码 | RTX 50 系硬编正常 | 5.89 | LEARN-016 被动补全学习 | 不确定→搜索→回答 |
| 5.78 | CHAT-003 语音合成 | CosyVoice3/GPT-SoVITS | 5.90 | LEARN-017 项目驱动学习 | 建项目→自动学习相关知识 |
| 5.79 | LEARN-018 学习设置面板 | 全部设置项可配置 | 5.91 | LEARN-019 学习日志报告 | 完整操作记录+完成报告 |
| 5.80 | LEARN-020 知识去重 | SimHash >95% 准确率 | | | |

**P2 优化（12 项，B-5.3）**

| # | 编号 | 验收标准 | # | 编号 | 验收标准 |
|---|------|----------|---|------|----------|
| 5.92 | SYS-012 快捷键系统 | 常用操作快捷键 | 5.98 | SYS-015 模板系统 | 预设模板一键使用 |
| 5.93 | SYS-013 拖拽交互 | 素材拖拽流畅 | 5.99 | SYS-016 多语言预留 | i18n 框架搭建 |
| 5.94 | SYS-014 历史记录管理 | 生成历史浏览/删除 | 5.100 | SYS-017 自动更新 | 增量更新包 |
| 5.95 | DRAW-003 批量处理 | 批量图片/视频生成 | 5.101 | LEARN-021 好奇心探索 | AI 自主发现新领域 |
| 5.96 | LEARN-022 多源交叉验证 | 矛盾知识标记 | 5.102 | LEARN-023 学习数据可视化 | 知识图谱/统计图表 |
| 5.97 | LEARN-024 知识库导出导入 | JSON/CSV | 5.103 | LEARN-025 学习回放 | 回放 AI 浏览过程 |

**P3 长期（5 项，B-5.4）**：5.104 SYS-018 多用户协作；5.105 SYS-019 云端同步（可选）；5.106 SYS-020 插件系统；5.107 ENG-001 更多视频模型；5.108 ENG-002 3D 场景生成。

### 5.5 模型清单（A-1.7, B；完整包 ~392GB ⚠ 见 M-15）

| # | 模型 | 大小 | 加载策略 | # | 模型 | 大小 | 加载策略 |
|---|------|------|----------|---|------|------|----------|
| 5.109 | Qwen3-VL-8B-Instruct | 16GB | 常驻显存/内存 | 5.117 | LTX-2-Style-LoRA | 500MB | 训练后热加载 |
| 5.110 | Qwen3-VL-4B-Instruct | 8GB | 按需加载 | 5.118 | OmniLearn-RAG-Index | 动态 | 内存映射 |
| 5.111 | Qwen3-VL-2B-Instruct | 4GB | 预加载 | 5.119 | MLPredict-XGBoost | 50MB | 常驻内存 |
| 5.112 | FLUX.1-dev | 24GB | 按需，FP8 | 5.120 | ControlNet-OpenPose | 1.5GB | 随 SDXL |
| 5.113 | Kolors-2.1 | 12GB | 按需 | 5.121 | IP-Adapter-Plus | 1GB | 随 SDXL |
| 5.114 | SDXL-Base-1.0 | 7GB | 常驻内存 | 5.122 | LTX-2-Video | 18GB | 按需 |
| 5.115 | Wan2.1-14B | 28GB | 按需，显存分块 | 5.123 | CogVideoX-5B | 10GB | 按需 |

其余 ~300GB 为各类 LoRA/VAE/ControlNet 变体，冷存储按需加载（A-1.7）。

---

## 6. 引擎与调度规则基线

> 依据：B-2.1~2.5、B-4.1~4.4、B-8.3/8.4、A-2.1~2.3、A-4.1~4.4、E-2.x/4.x（与 B 一致处不重复标注）。

### 6.1 大模型互斥矩阵（B-2.1 / A-2.1，6×6）

| # | 模块对 | 关系 | 依据 |
|---|--------|------|------|
| 6.1 | OmniChat × OmniDraw | 兼容（共享显存池） | B-2.1 |
| 6.2 | OmniChat × OmniComic | 兼容（Agent 驱动） | B-2.1 |
| 6.3 | OmniChat × OmniLearn | **互斥**（CPU 竞争） | B-2.1 |
| 6.4 | OmniChat × VideoStyle | **互斥**（GPU 计算冲突） | B-2.1 |
| 6.5 | OmniDraw × OmniComic | **强依赖**（关键帧） | B-2.1 |
| 6.6 | OmniDraw × OmniLearn | **互斥**（显存竞争） | B-2.1 |
| 6.7 | OmniDraw × VideoStyle | **互斥**（GPU 计算冲突） | B-2.1 |
| 6.8 | OmniComic × OmniLearn | **互斥**（显存+CPU） | B-2.1 |
| 6.9 | OmniComic × VideoStyle | **互斥**（GPU 计算冲突） | B-2.1 |
| 6.10 | OmniLearn × VideoStyle | **互斥**（GPU 计算冲突） | B-2.1 |
| 6.11 | ModelManager × 全部 | 兼容（被调用方/写入方） | B-2.1 |

验收：调度器配置中存在该矩阵的机器可读表达；互斥功能并发触发时 UI 置灰/排队，强依赖联动加载。「互斥=排队或抢占；兼容=可并行受总资源限制；强依赖=必须同时存在」（B-2.1 注）。

### 6.2 后台服务优先级（B-2.5 / B-8.4.2）

| # | 优先级 | 服务 | 规则 |
|---|--------|------|------|
| 6.12 | P0（最高） | 用户当前操作 | 不可被抢占 |
| 6.13 | P1 | 模型预加载（ML 预测） | 用户即将使用 |
| 6.14 | P2 | LoRA 训练 | 后台队列 |
| 6.15 | P3 | 浏览器学习 | 空闲时运行 |
| 6.16 | P4 | 行为学习记录 | 始终后台，单次 <10ms |
| 6.17 | P5（最低） | 知识库清理/归档 | 深夜/空闲 |

验收：调度器优先级队列实现 P0>P1>P2>P3>P4>P5；资源冲突时低优先级让路。

### 6.3 模型加载互斥规则（A-2.3 / B-2.3）

| # | 优先级 | 模型/模块 | 抢占策略 | 卸载策略 |
|---|--------|-----------|----------|----------|
| 6.18 | P0 | Qwen3-VL-8B（Agent 模式） | 不可抢占 | 仅用户手动卸载 |
| 6.19 | P1 | FLUX.1-dev（生成中） | 可被 P0 抢占 | 保存中间状态后卸载 |
| 6.20 | P2 | LTX-2（视频生成中） | 可被 P0/P1 抢占 | 保存 Checkpoint 后卸载 |
| 6.21 | P3 | OmniLearn（训练中） | 可被 P0-P2 抢占 | 暂停训练保存状态 |
| 6.22 | P4 | ControlNet/IP-Adapter | 随基座卸载 | 立即卸载 |
| 6.23 | P5 | MLPredict-XGBoost | 不可抢占 | 仅系统关闭时卸载 |

### 6.4 GPU 显存自适应阈值（B-4.1.1 / A-4.1.1；⚠ B-8.4.2 摘要表述不同，裁决以本详表为准，见 M-13）

| # | 显存占用 | 状态 | 精度策略 | 卸载策略 |
|---|----------|------|----------|----------|
| 6.24 | <70% | 安全区 | 维持 fp32/bf16 | 全量驻留显存 |
| 6.25 | 70–85% | 警戒区 | 降级 fp16 | 全量驻留 + 显存碎片整理 |
| 6.26 | 85–95% | 危险区 | 降级 bf16/int8 | 卸载非活跃 LoRA 至内存 |
| 6.27 | >95% | 溢出区 | 强制 int8 量化推理 | 卸载基础模型至内存，启用 CPU Offload |

验收：毫秒级显存监控存在；构造压测使占用跨越各阈值，观测精度/卸载行为符合表。显存安全阈值默认 85%，用户可调 70%–95%（A-4.4）。

### 6.5 CPU/内存/温度自适应（A-4.1.2/4.1.3，B-4.1 同）

| # | 检查项 | 基线要求 | 验收标准 |
|---|--------|----------|----------|
| 6.28 | 内存保护 | 系统内存 >85% 触发保护：Celery 并发 4→1，暂停非紧急 LoRA 训练 | 压测内存触发后观测并发数 |
| 6.29 | 进程优先级 | `os.nice()` 降低非关键 Worker 优先级，保前端 60FPS | 代码检索 + 行为观测 |
| 6.30 | GPU 温度 | >85°C 降频（batch size 减半）；>90°C 强制暂停生成任务并 UI 警告 | pynvml 轮询逻辑存在；模拟温度事件 |
| 6.31 | 功耗墙 | 触及 TDP 且升温 → 推理 batch size 减半 | 同上 |
| 6.32 | 降频锁定 | 连续 3 次高温降频 → 当前会话锁定 GPU 利用率上限 80% | 日志出现锁定事件 |

### 6.6 自适应决策树（A-4.1.4 / B-4.1.4）

| # | 检查项 | 基线要求 |
|---|--------|----------|
| 6.33 | 决策树 | `VRAM>95%→int8+卸载LoRA→仍OOM则CPU Offload`；`温度>85°C→batch减半+记录thermal_throttle`；`RAM>85%→Celery=1+暂停LoRA`；否则维持最优性能档 |

### 6.7 硬件等级映射（A-4.2 / B-4.2，六档）

| # | 档位 | 配置 | 对话模型 | 绘画模型 | 视频模型 | 限制 |
|---|------|------|----------|----------|----------|------|
| 6.34 | 顶配 | RTX 5090 32GB+64GB | Qwen3-VL-8B fp16 | FLUX.1-dev fp16 | LTX-Video-2B | 无限制，支持 8K |
| 6.35 | 高端 | RTX 4090 24GB+32GB | 8B int8 | FLUX.1-dev fp16 | Wan2.1-T2V-1.3B | 视频限 720p |
| 6.36 | 中端 | RTX 4070 12GB+24GB | 4B int8 | SDXL-Turbo fp16 | CogVideoX-2B | 视频限 480p |
| 6.37 | 入门 | RTX 3060 12GB+32GB | 2B int8 | SDXL-Turbo int8 | CogVideoX-2B | 禁用高分辨率修复 |
| 6.38 | AMD | RX 6600 8GB+16GB | 2B int8 | SDXL-Turbo int8 | 禁用 | 视频不可用，AV1 软编 |
| 6.39 | 纯CPU | 无独显+16GB | 2B fp32 | SD1.5 fp32 | 禁用 | 仅基础对话+低清绘画 |

学习速率映射：顶配/高端 LR=1e-4，中端 5e-5，入门/AMD 1e-5，纯CPU 禁用（A-4.2）。

### 6.8 运行时模型选择逻辑（B-8.3.1/8.3.2/8.3.4）

| # | 检查项 | 基线要求 |
|---|--------|----------|
| 6.40 | 对话模型选择 | 显存 ≥24GB→8B fp16；≥16GB→8B int8；≥10GB→4B int8；<10GB→2B int8 |
| 6.41 | 绘画模型选择 | ≥16GB→FLUX.1-dev；≥12GB→Kolors 2.1；<12GB→SDXL |
| 6.42 | 视频模型选择 | 高配 LTX-2 / 中配 Wan2.1-1.3B / 基线 CogVideoX-2B |
| 6.43 | 编码调度 | RTX50→NVENC AV1；RTX30/40→NVENC **H.264**（B 裁决，见 M-20）；AMD/纯CPU→SVT-AV1 软编；AV1 失败自动降级 H.264 |
| 6.44 | 编码参数 | CRF 23、预设 medium、AAC 256kbps |
| 6.45 | RAG 注入 | BGE-large-zh→ChromaDB Top-5→过滤 score<0.5→注入 Prompt；上下文默认 8192 token |

### 6.9 学习策略自适应（A-4.3 / B-4.3）

| # | 检查项 | 基线要求 |
|---|--------|----------|
| 6.46 | 知识库容量 | <10k 全量加载；10k–100k 启用 HNSW；>100k 切 ChromaDB+FTS5 混合检索 |
| 6.47 | 学习速率 | 知识增量 >1k/天 → 自动降低 LoRA Epoch 防灾难性遗忘 |
| 6.48 | 数据保留 | LRU，30 天未命中归档冷存储 |
| 6.49 | LoRA 质量 | Validation Loss 连续 5 Step 无下降→超参微调；震荡→LR×0.8；停滞→增 Rank；回升 >5%→早停存最优 |
| 6.50 | 网络自适应 | 带宽 <10Mbps→下载并发 1+断点续传；断网→本地缓存+禁在线 RAG+UI 离线标识 |
| 6.51 | 用户行为 | 高频 Prompt/参数构建用户画像；领域频繁微调→推荐 LoRA；空闲 RL 探索惊喜推荐 |
| 6.52 | 训练触发 | 训练数据 ≥100 条自动触发 LoRA 微调；新 LoRA 注册后全功能可用（B-8.4.3） |

### 6.10 用户可调整项（A-4.4 系统级 6 项 + B-4.4 学习级 10 项，互补全收）

| # | 设置项 | 默认值 | 范围 |
|---|--------|--------|------|
| 6.53 | 显存安全阈值 | 85% | 70–95% |
| 6.54 | 后台任务并发数 | 4 | 1–8 |
| 6.55 | LoRA Rank | 16 | 4–64（显存平方级增长） |
| 6.56 | 视频生成分辨率 | 720p | 480/720/1080p |
| 6.57 | 知识库自动清理 | 30 天 | 7/30/90 天/禁用 |
| 6.58 | 硬件加速模式 | Auto | Auto/CUDA/ROCm/CPU |
| 6.59 | 学习时段 | 仅空闲 | 始终/仅空闲/手动 |
| 6.60 | 单次学习时长上限 | 30 分钟 | 5–120 |
| 6.61 | 单次学习页数上限 | 20 页 | 5–100 |
| 6.62 | 每日流量上限 | 50MB | 10–500MB |
| 6.63 | 自动微调频率 | 每周 | 每日/每周/每月/手动 |
| 6.64 | 搜索引擎 | Bing | Bing/百度/DuckDuckGo |
| 6.65 | 广告过滤 | 开启 | 开/关 |
| 6.66 | 敏感内容处理 | 标记待审核 | 跳过/标记/停止 |
| 6.67 | 显示浏览器 | 开启 | 开/关 |
| 6.68 | 域名白名单 | 不限 | 自定义列表 |

验收：设置面板 16 项全部可配置（TASK-LEARN-018）；资源配额按用户状态（空闲/创作/推理中/断网/电池）动态调整（B-8.4.2）。

---

## 7. v2.3.1 五项优化验收标准（B-5.5，逐条可验收）

### 7.1 TASK-LEARN-006 混合检索系统（Hybrid Search）

| # | 类别 | 基线要求 |
|---|------|----------|
| 7.1 | 目标 | 提升 RAG 检索精度，解决专有名词/代码精确匹配问题 |
| 7.2 | 实现 | 知识入库同步写 FTS5（KnowledgeProcessingService） |
| 7.3 | 实现 | 重构 retrieve：向量（ChromaDB）+ 关键词（FTS5）双路召回并行调用 |
| 7.4 | 实现 | RRF 融合：`score = 1/(k+rank_vector) + 1/(k+rank_keyword)`，**k=60** |
| 7.5 | 实现（高级） | 可选 Cross-Encoder（如 bge-reranker）对合并候选前 50 精排 |
| 7.6 | **验收** | 含专有名词测试集上命中率（Hit Rate）提升 **>15%** |
| 7.7 | **验收** | 检索延迟增加 **<20ms** |

### 7.2 TASK-LEARN-007 Agent 分级决策机制

| # | 类别 | 基线要求 |
|---|------|----------|
| 7.8 | 实现 | BrowserAgentService 新增 `classify_page` 方法 |
| 7.9 | 实现 | 分类特征：DOM 结构（form/input/button 数量）、内容密度（文本/HTML 比）、样式特征（onclick/复杂类名） |
| 7.10 | 实现 | 简单页面（博客/新闻/文档）：跳过截图与 Qwen3-VL 视觉分析，仅 DOM 文本快速决策，可用 Qwen3-4B 加速；复杂页面维持「DOM+视觉」双模 |
| 7.11 | **验收** | 简单页面单次决策 ~3s → **<500ms** |
| 7.12 | **验收** | 复杂页面决策准确率无明显下降（对比优化前评测集） |

### 7.3 TASK-LEARN-008 LoRA 训练加速

| # | 类别 | 基线要求 |
|---|------|----------|
| 7.13 | 目标 | 微调时间 30min–2h → 15min–1h |
| 7.14 | 实现 | 模型加载 `attn_implementation="flash_attention_2"` |
| 7.15 | 实现 | 优化器换 bitsandbytes AdamW 8-bit / PagedAdamW |
| 7.16 | 实现 | DataLoader `num_workers>0` 且 `pin_memory=True` |
| 7.17 | 实现 | 学习率调度 cosine_with_restarts 或 polynomial |
| 7.18 | 实现 | 显存允许时增大 gradient_accumulation_steps |
| 7.19 | 实现 | accelerate 启用 DeepSpeed ZeRO-2 或 ZeRO-3 |
| 7.20 | **验收** | 同数据集同硬件单 epoch 训练时间减少 **>30%** |
| 7.21 | **验收** | 最终模型困惑度（PPL）与生成质量无下降 |

### 7.4 TASK-LEARN-009 浏览器进程池

| # | 类别 | 基线要求 |
|---|------|----------|
| 7.22 | 实现 | 创建 BrowserPool 服务，应用启动事件中预初始化空闲 CEF 进程 |
| 7.23 | 实现 | AutonomousLearningService 从池获取/归还实例 |
| 7.24 | 实现 | 会话结束清理 Cookie/LocalStorage/缓存（状态隔离） |
| 7.25 | 实现 | 「干净」快照保存与恢复，后续启动直接恢复快照 |
| 7.26 | **验收** | 学习会话启动浏览器就绪时间 <3s → **<1s** |
| 7.27 | **验收** | 不同学习会话间无 Cookie/缓存数据泄露（跨会话检测） |

### 7.5 TASK-LEARN-010 知识图谱可视化

| # | 类别 | 基线要求 |
|---|------|----------|
| 7.28 | 实现 | extract_knowledge 增加实体关系三元组（实体1, 关系, 实体2）抽取 |
| 7.29 | 实现 | 新增图数据存储（networkx 或轻量图数据库） |
| 7.30 | 实现 | 新增 `GET /api/v1/learn/knowledge/graph`，按知识 ID 返回节点和边 |
| 7.31 | 实现 | 前端 KnowledgeBrowser 增加「图谱视图」切换，react-flow 或 cytoscape.js 渲染 |
| 7.32 | **验收** | 能成功抽取实体关系；前端正确渲染图谱，支持节点拖拽、缩放、点击查看详情 |

---

## 8. 编程语言规范基线（依据 C 全文；B-9.2 引用 C；D 已将语言规范移交 C）

### 8.1 八语言职责边界与占比（C-一）

| # | 语言 | 职责域 | 占比 | 红线 |
|---|------|--------|------|------|
| 8.1 | Python 3.12 | 后端/AI 推理/训练 | ~45% | 不做 UI 渲染、系统级文件操作、像素级图像处理 |
| 8.2 | TypeScript 5.x | 前端 UI/类型安全 | ~25% | 不做 AI 推理、文件 IO、数据库操作 |
| 8.3 | Rust 1.75+ | Tauri 原生层/系统性能 | ~12% | 不做 AI 推理、业务逻辑、UI、数据库 CRUD |
| 8.4 | C/C++ | CUDA/CEF 底层/编解码 | ~8% | 不做业务逻辑、API、UI、数据库 |
| 8.5 | SQL(SQLite) | 持久化/全文检索 | ~3% | 向量检索交 ChromaDB |
| 8.6 | HTML5+CSS3 | 视图渲染 | ~4% | 禁全局 CSS 污染 |
| 8.7 | Shell/PS | 构建/部署/自动化 | ~2% | 超 50 行改 Python |
| 8.8 | JSON/YAML | 配置/数据交换 | ~1% | 必须有校验 schema |

### 8.2 Python 铁律（C-2.1）

| # | 规则 | 验收标准（机器可核对） |
|---|------|------------------------|
| 8.9 | 全部函数写 type hints，`mypy --strict` 通过 | CI 跑 mypy --strict 零 error |
| 8.10 | 数据模型 Pydantic v2 校验 | 模型层全部继承 BaseModel |
| 8.11 | CPU 密集用 multiprocessing/ProcessPoolExecutor，禁 threading 做 CPU 密集 | 代码检索 |
| 8.12 | 循环 >10 万次必须 NumPy/Pandas 向量化 | 代码审查 |
| 8.13 | 模型卸载三件套 `del model; torch.cuda.empty_cache(); gc.collect()`；禁循环内重复加载 | 代码检索 |
| 8.14 | FastAPI 路由禁同步阻塞；IO 全 async/await；数据库统一 aiosqlite | 代码检索路由中的同步调用 |
| 8.15 | 依赖锁定 `pip freeze > requirements.txt`；生产用 uv；禁不指定版本安装 | requirements 全部带版本号 |

### 8.3 TypeScript 铁律（C-2.2）

| # | 规则 | 验收标准 |
|---|------|----------|
| 8.16 | ESLint `@typescript-eslint/no-explicit-any: error`；用 unknown+类型守卫 | ESLint 配置检索；lint 零 error |
| 8.17 | 外部数据（API/IPC）必经 Zod schema 校验 | services 层检索 Zod parse |
| 8.18 | 全局状态 Zustand；禁 Redux | package.json 无 redux |
| 8.19 | useEffect 必写 cleanup；WS/EventSource 卸载关闭；定时器 clearInterval | 代码审查 |
| 8.20 | 路由级代码分割 React.lazy；禁全量引入 UI 库 | 构建产物 chunk 分析 |
| 8.21 | 回调嵌套 ≤2 层，统一 async/await；ErrorBoundary 捕获 | 代码审查 |

### 8.4 Rust 铁律（C-2.3）

| # | 规则 | 验收标准 |
|---|------|----------|
| 8.22 | thiserror 定义错误枚举；`?` 传播；业务代码禁 `unwrap()` | `rg "unwrap\(\)" src-tauri/src` 零命中（测试除外） |
| 8.23 | 统一 tokio 运行时，禁混用 async-std | Cargo.toml 无 async-std |
| 8.24 | Rust 限 Tauri 层与性能热路径；业务逻辑放 Python/TS | 代码占比 ~12% |

### 8.5 C/C++ 铁律（C-2.4）

| # | 规则 | 验收标准 |
|---|------|----------|
| 8.25 | 禁手动 malloc/free；智能指针/RAII | 代码检索 |
| 8.26 | 单元测试 + AddressSanitizer；编译 `-g -fsanitize=address,undefined` | CI 存在 ASan 步骤 |
| 8.27 | CMake 统一管理；pybind11 暴露；GPU 内存分配/释放同函数内完成（RAII） | 构建脚本核对 |
| 8.28 | pybind11 模块命名 `_omnispace_{module}` 且带 .pyi 存根 | 文件检索 |

### 8.6 SQL / HTML-CSS / Shell / 配置 铁律（C-2.5~2.8）

| # | 规则 | 验收标准 |
|---|------|----------|
| 8.29 | SQL 参数化；FTS5 `tokenize='unicode61'` | 见第 3 章 3.3/3.22 |
| 8.30 | 禁全局 CSS 选择器；Tailwind 原子类或 CSS Modules | 样式表检索 |
| 8.31 | 动画仅 transform/opacity | 见 4.43 |
| 8.32 | Bash 开头 `set -euo pipefail`；变量加双引号 | 脚本检索 |
| 8.33 | 脚本禁硬编码密码/Token，用环境变量/密钥文件 | 密钥扫描 |
| 8.34 | JSON/YAML 必须有校验 schema（Pydantic/Zod/serde）；>1MB 配置改 SQLite；JSON 仅 <100KB | 配置加载处检索 |

### 8.7 跨语言通信协议（C-3.2，B-8.4.4）

| # | 通道 | 基线要求 |
|---|------|----------|
| 8.35 | TS↔Rust（IPC） | invoke/event 双通道；JSON；字段 camelCase 两端一致；TS 端 Zod / Rust 端 serde |
| 8.36 | IPC 命名 | `{domain}_{action}_{resource}`（如 `browser_acquire_instance`） |
| 8.37 | IPC 约束 | 大数据 >1MB 走文件路径；异步 IPC 超时默认 30s；错误结构化返回 |
| 8.38 | IPC 性能 | 单次调用 <5ms，事件推送延迟 <50ms（B-8.4.4） |
| 8.39 | TS↔Python | HTTP REST + WebSocket；信封格式；SSE 流式；multipart 上传（见第 2 章） |
| 8.40 | Python↔C++ | pybind11；numpy 数组进出；C++ 层禁抛 Python 异常，用返回值/错误码 |
| 8.41 | Rust↔Python | Rust spawn Python 进程；每 5s GET /health；崩溃 3s 内检测并重启；进程间禁共享内存 |
| 8.42 | 进程生命周期 | Python 进程由 Rust 管理（启动/监控/重启/优雅关闭）；模型加载 Python 自管 |

---

## 9. 性能量化指标与落地保障基线

### 9.1 十七项量化技术指标（E-8.5；目标值为验收线）

| # | 指标 | 目标值 | # | 指标 | 目标值 |
|---|------|--------|---|------|--------|
| 9.1 | 首字延迟 TTFT（P95） | <200ms（优化目标；P0 门槛 <500ms 见 M-10） | 9.10 | LoRA 训练 Loss（100 steps） | <0.5 |
| 9.2 | 对话吞吐量 | >80 tokens/s | 9.11 | 浏览器标签切换 | <100ms |
| 9.3 | 知识提取速度 | >30 pages/min | 9.12 | 风格迁移质量 FID | <15 |
| 9.4 | 视频编码 FPS（1080p） | >60 | 9.13 | 自主学习覆盖率 | >90% 图谱节点 |
| 9.5 | 显存峰值 | <20GB | 9.14 | 错误率 5xx/Total | <0.1% |
| 9.6 | 内存泄漏（24h） | 0 | 9.15 | 磁盘 I/O | <100 MB/s |
| 9.7 | 冷启动时间 | <3s | 9.16 | 网络带宽占用 | <50 Mbps |
| 9.8 | API P99 延迟 | <50ms | 9.17 | 用户满意度（点赞率） | >90% |
| 9.9 | 知识检索 Recall@5 | >85% | | | |

### 9.2 核心约束（A-1.4 / B-1.3.3）

| # | 检查项 | 基线要求 |
|---|--------|----------|
| 9.18 | 离线能力 | 核心功能 100% 离线可用；仅 OmniLearn 网页抓取与可选遥测需网络；已学知识离线可用 |
| 9.19 | 网络要求 | 联网为增强非依赖；断网自动切本地缓存并显示离线标识 |
| 9.20 | 安全边界 | 本地沙箱隔离浏览器进程；LoRA 训练数据仅存本地；禁止未经授权模型外传 |
| 9.21 | 分发 | 完整离线包 ~392GB / 基础包 ~5GB + 手动导入模型（B-1.1） |

### 9.3 浏览器安全策略（B-8.3.x 安全小节 / A-1.5 TASK-LEARN-005）

| # | 检查项 | 基线要求 |
|---|--------|----------|
| 9.22 | 沙箱隔离 | CEF 进程沙箱隔离；CSP + 进程隔离，无 XSS/CSRF 风险 |
| 9.23 | Cookie 隔离 | 独立存储不与系统共享；会话结束清除 Cookie/LocalStorage/缓存 |
| 9.24 | 域名控制 | 白名单/黑名单可配置 |
| 9.25 | 流量控制 | 每日流量上限可配置（默认 50MB） |
| 9.26 | 操作频率 | 浏览器自动操作间隔 ≥1 秒 |

### 9.4 数据安全与日志

| # | 检查项 | 基线要求 | 依据 |
|---|--------|----------|------|
| 9.27 | 隐私 | 用户数据本地加密存储，敏感数据不可读 | A-1.5 SYS-003 |
| 9.28 | 日志 | JSON 结构化含堆栈，可自动分析错误原因 | A-1.5 SYS-004 |
| 9.29 | 日志组件 | 后端统一 loguru | B-8.2.2 |
| 9.30 | 完整性 | 离线包 SHA256 校验，失败自动修复或提示 | A-1.5 SYS-001 |

### 9.5 打包与部署

| # | 检查项 | 基线要求 | 依据 |
|---|--------|----------|------|
| 9.31 | 桌面壳 | Tauri 2.x (Rust)，Windows 安装包正常生成，壳体积 <500MB | A-1.5 SYS-005 |
| 9.32 | 目录结构 | frontend/（components/hooks/services/utils/styles/types）、backend/app/（routers/services/models/db/core/tasks）、shared/、config/、scripts/ | B-8.1 |
| 9.33 | 共享类型 | `shared/api_types.ts` + `shared/constants.py` 前后端共享 | B-8.1 |
| 9.34 | 硬件配置 | `config/hardware_profiles.json` 存六档映射 | B-8.1, A-4.2 |
| 9.35 | 健康检查 | 根路径 `GET /health` 返回 ok（活性探针，B-5.1 SYS-002）；API 内 `GET /api/v1/system/health`（E-附录B.7）——两者并存，见 M-08 |
| 9.36 | 启动自检 | 硬件探测（pynvml/psutil）输出档位与显存；自适应调度器读 4.2 映射加载配置 | A-5.1 SYS-013/014 |

---

## 10. 文档矛盾记录与裁决

> 裁决规则：**B > E > D > A > C**（正交领域 C 优先）。类型：〔文档间〕〔文档内〕〔文档vs实现〕。

### 10.1 矛盾总表

| # | 类型 | 主题 | 各文档/实现表述 | 裁决 | 理由 |
|---|------|------|------------------|------|------|
| M-01 | 文档vs实现 | API 前缀 | B-9.1/C-3.2.2/D-4.2.5/E-9.1 全部 `/api/v1`；实现 `backend/config.py: API_PREFIX="/v1"` | **以 `/api/v1` 为基线**；实现须迁移或增加 `/api/v1` 兼容挂载 | 四文档一致且 B 可信；实现为单点偏差 |
| M-02 | 文档vs实现 | 服务端口 | B-9.1/C/E `localhost:8000`；实现 `config.yaml: 5800` | **以 8000 为基线**；实现须改默认端口或登记偏差 | B 可信且三文档一致 |
| M-03 | 文档vs实现 | Python 版本 | B-8.2.2/C-一 要求 Python 3.12；项目内嵌运行时 3.10.11 | **基线 3.12**；3.10.11 登记为过渡偏差，升级前每次审计标记 | B/C 一致；运行时事实不可盲信为合规 |
| M-04 | 文档vs实现 | PyTorch 版本 | B-1.4/8.2.2 要求 2.8 stable（cu124+cu128）；实际安装 2.11.0+cu128 | **基线 2.8 stable**；现版本登记偏差（功能验证通过可豁免，但须记录） | B 可信 |
| M-05 | 文档间 | 绘画路由命名 | B-7.1.1 `/api/v1/paint`；E-附录B.2 `/api/v1/art` | **`/api/v1/paint`** | B > E |
| M-06 | 文档间 | 模型管理路由 | B-7.1.1 `/api/v1/models`；E-附录B.5 `/api/v1/model` | **`/api/v1/models`** | B > E |
| M-07 | 文档间 | 漫剧路由拆分 | B-7.1.1/7.1.3 拆 `/storyboard`+`/director`+`/video`；E-附录B.3 合并 `/comic` | **按 B 拆三组**；E 组内端点定义作细粒度补充 | B > E |
| M-08 | 文档vs实现 | 健康检查端点 | B-5.1(SYS-002) 根 `/health` 返回 ok；E-附录B.7 `/api/v1/system/health`；实现仅有根 `/health` | **两者并存**：根 `/health`（活性）+ API 内 `system/health`（B/E 体系）；实现缺后者须补 | B 自身两处表述可调和，非真冲突 |
| M-09 | 文档间 | Sakura 色值 | B-6.3.1：文字#EAEAEA/成功#6BCB77/警告#FFD93D/错误#FF6B6B；D-1.1.3：#E8E8F0/#5BA87A/#E8A84C/#E05555 | **色值以 B 为准**；D 的 CSS 变量命名体系保留作实现载体 | B > D；D 提供变量名属正交补充 |
| M-10 | 文档间 | 首字延迟 TTFT | A-1.5 <800ms；B-5.1(CHAT-001) <500ms；E-8.5 <200ms(P95) | **P0 验收门槛 <500ms（B）**；E 的 <200ms 作为 v2.3.1 优化目标（非门槛） | B > E > A；E 目标更严但标注为优化方向 |
| M-11 | 文档间 | 文生图耗时 | A-1.5 1024² <15s（限 4090）；B-5.1 <30s（未限硬件） | **通用门槛 <30s（B）**；4090 档附加 <15s（A 作档位细化保留） | B > A；A 限定硬件可并存 |
| M-12 | 文档间 | 圆角档位 | B-6.3.1 卡片12/按钮8/输入框6；D-1.1.3 sm4/md8/lg12 | **B 三档为主**；D 的 4px 补充小组件 | B > D，部分重叠部分互补 |
| M-13 | 文档内（B） | 显存阈值表述 | B-4.1.1 详表 70/85/95 四区；B-8.4.2 摘要「>90%→降精度/卸载」 | **以 B-4.1.1 详表为准** | 同一文档详表优先于摘要 |
| M-14 | 文档vs实现 | 数据库表数 | E-附录C 详列 8 表+FTS5；实现首启建 13 表 | **E 为最小基线（不得缺项）**；实现超集允许 | E 自称「详细设计」非「完整清单」；超集不破坏合规 |
| M-15 | 声称核实 | 完整包 392GB | B-1.1/A-1.1 均称 ~392GB；A-1.7 明细仅列 ~152GB，余 ~300GB 以「各类 LoRA/VAE/变体」概括 | **非文档间矛盾**：A/B 一致；属规模声称。验收按 SYS-006「392GB 完整包可安装 + SHA256 校验」执行；打包审计时复核明细 | 两文档一致，无裁决对象；登记为待核实声称 |
| M-16 | 文档vs实现 | 前端路由形态 | B-6.1.2 导航路径 /chat /paint 等（未约定 Hash/History）；实现 HashRouter | **不构成冲突**：文档未约束路由模式；路径语义以 B 为准 | 实现自由度内 |
| M-17 | 文档间 | 知识检索指标 | A/B：延迟 <100ms、命中率 >75%；E-8.5：Recall@5 >85% | **互补双指标全收**（延迟<100ms ∧ 命中率>75% ∧ Recall@5>85%） | 指标维度不同，非矛盾 |
| M-18 | 文档间 | 浏览器内核版本 | A-1.5 Chromium 120+；B-8.2.2 cefpython3 latest | **Chromium ≥120**（A 具体约束 + B 组件选型，互补） | 维度不同，非矛盾 |
| M-19 | 文档内（B） | 警告色=强调色 | B-6.3.1 强调色 #FFD93D 与警告色 #FFD93D 同色 | **暂行 B 原值**，UI 审计时人工确认是否区分语义（建议参考 D #E8A84C 作警告） | 同文档瑕疵，无高优先级文档可裁决 |
| M-20 | 文档间 | RTX30/40 编码器 | A-1.5(ENG-005) NVENC **AV1** >80fps；B-8.3.4 RTX30/40→NVENC **H.264** 硬编 | **以 B 为准：RTX30/40 用 H.264 硬编**（RTX30 系无 AV1 编码硬件，B 在技术上成立） | B > A；且 B 与硬件事实一致 |

### 10.2 统计

- 矛盾/不一致/偏差总数：**20 条**
  - 文档间矛盾：9 条（M-05、M-06、M-07、M-09、M-10、M-11、M-12、M-17*、M-20）＊M-17/M-18 裁决为互补，实质冲突 7 条
  - 文档内部不一致：2 条（M-13、M-19）
  - 文档 vs 实现偏差：7 条（M-01、M-02、M-03、M-04、M-08、M-14、M-16）
  - 声称待核实：1 条（M-15）
  - 互补确认（非矛盾，登记备查）：1 条（M-18）

### 10.3 最关键裁决摘要（Top 5）

1. **M-01 API 前缀**：四文档统一 `/api/v1`，实现 `/v1` 为偏差 → 基线 `/api/v1`，实现须迁移或兼容。
2. **M-03 Python 3.12**：文档要求 3.12，内嵌运行时 3.10.11 为过渡偏差 → 基线 3.12，每次审计标记直至升级。
3. **M-05/06/07 路由命名**：`/paint`、`/models`、漫剧拆三组（storyboard/director/video）→ 一律以 B 为准，E 仅作组内端点细粒度补充。
4. **M-09 主题色值**：文字 #EAEAEA / 成功 #6BCB77 / 错误 #FF6B6B 等以 B 为准；D 的 CSS 变量命名体系保留作实现载体。
5. **M-10 TTFT**：P0 验收门槛取 B 的 <500ms；E 的 <200ms(P95) 与 A 的 <800ms 分别降级为优化目标与被替代值。

---

> 自审声明：本清单 10 章 456 条均已给出文档依据与可核对验收标准，无空洞条目；全部矛盾均按 B>E>D>A>C 裁决并记录理由。

