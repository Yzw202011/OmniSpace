# OmniSpace AI

本地优先的 AI 漫剧创作工作站：对话、绘画、分镜、视频生成、语音、知识学习、3D 资产，全部推理在本地 GPU 完成，数据不出机器。

- **后端**：FastAPI + uvicorn（`backend/`），270 个 HTTP 端点 + 4 个 WebSocket，统一前缀 `/api/v1`
- **前端**：React 19 + TypeScript + Vite + Tailwind v4 + Zustand（`frontend/`），Hash Router SPA
- **存储**：SQLite（WAL，30 张表，敏感字段 AES-256-GCM 加密）+ ChromaDB 向量库 + FTS5 全文检索
- **推理**：SDXL / LTX-Video / AnimateLCM / Qwen3-VL / TripoSR / SAM / MiDaS 等本地模型，显存协调器统一调度（16GB 显存约束下自动路由与驱逐）
- **运行时**：内嵌 Python 3.10（`runtime/py310/`），无需系统 Python

---

## 快速启动（用户视角）

```powershell
# 在项目根目录 e:\OmniSpace 下执行（cwd 必须是项目根目录）
runtime\py310\python.exe launcher\launcher.py
```

Launcher 会依次完成：环境自检（OS / CUDA / 磁盘 ≥ 20GB）→ 端口冲突处理 → 模型完整性校验（缺失模型断点续传）→ 拉起后端（`uvicorn backend.main:app`）→ 心跳监控 + 崩溃自动重启 → 自动打开浏览器。

- 默认地址：`http://127.0.0.1:8765`（可用 `--port` 覆盖；被占用时自动在 5800~5835 区间扫描）
- 健康检查：`GET /health` 返回 `{"code": 0, "data": {"status": "healthy", ...}}`
- 不自动开浏览器：加 `--no-browser`
- 生产模式下前端静态资源由后端直接托管（根路径 `/`），无需单独部署前端

> 安全约束：后端只允许绑定回环地址（127.0.0.1 / localhost）。API 无认证体系，绑定非回环地址会在启动时拒绝（`OMNISPACE_ALLOW_LAN=1` 可显式豁免，自担风险）。

## 开发工作流

```powershell
# 后端冒烟测试（提交前必须通过，pytest.ini 已注册 smoke/schema 标记）
runtime\py310\python.exe -m pytest -m smoke

# 后端全量测试
runtime\py310\python.exe -m pytest

# 后端静态检查（ruff，配置 ruff.toml）
tools\ruff\ruff.exe check backend/ tools/ launcher/

# 前端开发服务器（端口 5173，代理 /api 到本地后端）
cd frontend; npm run dev

# 前端单测（vitest：store 状态流转 / Zod 解析 / 前后端枚举一致性）
cd frontend; npm run test

# 前端静态检查 + 类型检查 + 生产构建
cd frontend; npm run lint
cd frontend; npm run build
```

依赖锁定：`requirements-lock.txt`（154 包，精确版本，环境可复现的唯一真源）。前端依赖见 `frontend/package.json`。

版本控制：项目使用 Git；`models/`、`pydeps/`、`runtime/`、`keys/` 不入库。提交钩子（`.githooks/`）会自动跑冒烟测试 + 语法检查。

## 目录结构

```
e:\OmniSpace\
├── backend/               # FastAPI 后端
│   ├── api/               # 路由层（dialog / paint / manga/ / learn / models / hardware …）
│   ├── services/          # 业务层 + inference/ 推理引擎 + model_manager/ 显存协调
│   ├── engines/           # 资源层（gpu_backend / vram_manager / memory_manager）
│   ├── data/              # 存储层（database / vector_db / fts_store / graph_store / crypto）
│   ├── middleware/        # 横切层（错误信封 / 功能互斥锁 / 限流 / CORS / 上传闸门）
│   └── tests/             # pytest 测试（smoke / schema / unit 标记）
├── frontend/              # React SPA（src/pages + src/stores + src/components）
├── launcher/              # 启动器守护进程（自检 / 端口管理 / 心跳重启 / 托盘）
├── models/                # 模型资产（~186GB，不入库；配置表见部署手册）
├── runtime/py310/         # 内嵌 Python 3.10 运行时（唯一解释器，不入库）
├── pydeps/                # 承重依赖站点（156 个包，与 site-packages 双站点互补，勿删）
├── data/                  # 运行时数据（SQLite / 生成物 / 向量库），不入库
├── keys/                  # 加密密钥（DPAPI 保护），不入库
├── docs/                  # 文档（设计文档 / 部署手册 / 故障排查 / 需求追踪矩阵）
├── tools/                 # 构建与诊断脚本（build_rc.py / init_db.py / diagnostics.py）
├── tests/                 # 流程编排脚本（与 backend/tests 关系见 P2-09）
├── requirements-lock.txt  # 依赖锁定（唯一真源）
├── pytest.ini             # 测试配置（testpaths=backend/tests）
└── ruff.toml              # 后端静态检查配置
```

## 文档索引

| 文档 | 内容 | 何时看 |
| --- | --- | --- |
| `docs/deployment-manual.md` | 部署手册：硬件要求、环境装配、模型资产配置表、RC 交付 | 换机部署 / 配置模型 |
| `docs/troubleshooting.md` | 故障排查手册：OOM / 端口冲突 / 数据库版本冲突 / 模型探测失败 | 出故障时 |
| `docs/design/architecture-overview.md` | 架构总览：分层结构、中间件链、数据流 | 理解系统怎么运转 |
| `docs/design/api-endpoints.md` | API 端点总表（270 HTTP + 4 WS） | 找端点定义 |
| `docs/design/database-er.md` | 数据库 ER 说明（30 张表 + 迁移 + 加密） | 找表结构 |
| `docs/requirements-traceability.md` | 需求追踪矩阵（F/M/A/E 四类） | 查需求状态与决策 |
| `docs/audit-task-checklist.md` | 工程治理任务清单（P0/P1/P2） | 查治理进度 |

## 常用运维命令

```powershell
# RC 交付目录构建（E:\RC1002 只能由此脚本产出，禁止手工改动）
runtime\py310\python.exe tools\build_rc.py            # 同步构建
runtime\py310\python.exe tools\build_rc.py --verify   # 仅校验差异

# 数据库初始化 / 诊断
runtime\py310\python.exe tools\init_db.py
runtime\py310\python.exe tools\diagnostics.py
```

遇到启动失败、生成报错、模型加载异常，先查 `docs/troubleshooting.md`，再查 `logs/`（`backend_stdout.log` / `backend_stderr.log`）。
