
<div align="center">

# 🚀 OmniSpace AI

**本地优先的全栈 AI 漫剧创作工作站**

<p align="center">
  <a href="#-核心特性">核心特性</a> •
  <a href="#-技术栈">技术栈</a> •
  <a href="#-快速开始">快速开始</a> •
  <a href="#-如何贡献">如何贡献</a> •
  <a href="#-路线图">路线图</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10.11-blue?logo=python" alt="Python">
  <img src="https://img.shields.io/badge/React-19.0-61dafb?logo=react" alt="React">
  <img src="https://img.shields.io/badge/FastAPI-0.141-009688?logo=fastapi" alt="FastAPI">
  <img src="https://img.shields.io/badge/PyTorch-2.x-ee4c2c?logo=pytorch" alt="PyTorch">
  <img src="https://img.shields.io/badge/vLLM-latest-7c3aed" alt="vLLM">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
</p>

> 💡 **对话 × 绘画 × 分镜 × 视频 × 语音 × 知识学习 × 3D 资产**
> 全部推理在 **本地 GPU** 完成，数据 **永不离开你的机器**

</div>

---

## 🌟 为什么 OmniSpace 值得你 Star & 贡献

这不是又一个 AI 玩具 —— 这是一个**完整的、工程化的、可落地的本地 AI 创作平台**。

| 亮点 | 说明 |
|------|------|
| 🎯 **真·全模态** | 文本 / 图像 / 视频 / 语音 / 3D / 知识图谱，一站式创作管线 |
| 🏠 **100% 本地优先** | 所有模型推理跑在你自己的 GPU 上，没有 API 调用，没有数据上传 |
| ⚡ **显存调度器** | 自研显存协调器，16GB 显存也能跑全流程（自动加载/卸载/路由） |
| 🏗️ **工程质量高** | 328 HTTP 路由（含别名，2026-08-28 静态实测）+ 4 WebSocket，五层架构；pytest **115 通过** / vitest / 活后端 E2E 三层测试体系 |
| 🧩 **插件式推理引擎** | 支持 vLLM / Transformers / GGUF 多后端，可热切换 |
| 📚 **丰富文档** | 架构图、API 文档、部署手册、故障排查，新人友好 |

---

## ✨ 核心特性

### 🎨 AI 绘画（Paint）
- SDXL / Flux / LCM 多模型支持
- LoRA 风格训练与切换
- ControlNet（线稿/深度/姿态）
- 高清修复、局部重绘、扩图

### 🎬 AI 漫剧（Manga）
- **分镜编辑器**：可视化分镜编排，所见即所得
- **角色一致性**：同一角色跨页保持形象统一
- **自动镜头语言**：根据剧情自动生成运镜
- **资产库管理**：角色、场景、道具系统化管理

### 🎥 AI 视频（Video）
- 图生视频、文生视频
- 镜头级视频生成 + 自动拼接
- LTX-Video / AnimateLCM 双后端

### 💬 AI 对话（Dialog）
- 多模型对话（Qwen / Llama 等）
- 流式输出，打字机效果
- 上下文记忆 + 知识库 RAG

### 🧠 知识学习（Learn）
- 文档自动向量化入库
- RAG 检索增强生成
- 知识图谱可视化
- 浏览器代理，自主上网学习

### 🔊 AI 语音（Voice）
- TTS 文字转语音
- 声音克隆（少量样本即可）
- 多说话人管理

### 🎮 3D 资产（3D）
- 图生 3D（TripoSR）
- 3D 模型预览与导出
- 材质编辑

---

## 🛠 技术栈

| 层级 | 技术 |
|------|------|
| **前端** | React 19.0 + TypeScript + Vite 6 + Tailwind CSS v4 + Zustand 5 + React Router 7（Hash 路由，Sakura 主题） |
| **后端** | FastAPI 0.141 + Pydantic v2 + uvicorn（Python 3.10.11 嵌入式 runtime） |
| **AI 推理** | PyTorch 2.11 (cu128) + Diffusers + Transformers；vLLM 为可选子进程后端（py313，AWQ/GPTQ/GGUF 模型自动路由） |
| **数据库** | SQLite (WAL, 31 张用户表) + ChromaDB + FTS5 全文检索 |
| **存储加密** | AES-256-GCM 字段级加密（对话正文/行为日志）+ DPAPI 密钥保护 |
| **交付形态** | launcher 守护进程 + 系统浏览器（Tauri 桌面壳已于 2026-08-20 经 [ADR-002](docs/ADR-002-tauri-shell-decision.md) 裁剪） |
| **测试** | pytest（115 用例）+ vitest + 活后端 E2E 流程编排 |
| **代码质量** | ruff + pre-commit hooks |

---

## 📁 项目结构

```
OmniSpace/
├── backend/               # FastAPI 后端
│   ├── api/               # 路由层（dialog / paint / manga / learn / models …）
│   ├── services/          # 业务层 + 推理引擎 + 模型管理
│   ├── engines/           # 资源层（GPU / VRAM / 内存管理）
│   ├── data/              # 存储层（DB / 向量 / 全文 / 图谱 / 加密）
│   └── middleware/        # 横切层（错误处理 / 限流 / 互斥锁 / CORS）
├── frontend/              # React SPA
├── launcher/              # 启动守护进程（自检/端口/心跳/重启）
├── docs/                  # 完整文档
├── tools/                 # 构建与诊断脚本
└── tests/                 # E2E 流程编排
```

---

## 🚀 快速开始

### 前置要求

- Windows 10/11
- NVIDIA GPU（≥ 16GB 显存推荐，8GB 可精简运行）
- CUDA 12.x
- ≥ 50GB 磁盘空间（模型文件）

### 启动

```powershell
# 克隆仓库
git clone https://github.com/Yzw202011/OmniSpace.git
cd OmniSpace

# 启动（需先配置模型文件，详见部署手册）
runtime\py310\python.exe launcher\launcher.py
```

浏览器会自动打开 `http://127.0.0.1:8765`

> 端口说明：launcher 默认 `--port 8765`（冲突时先清理残留进程，再扫描 5800–5835 递进）；
> 开发模式直接跑 uvicorn 时用 `config.yaml` 里的 5800。

### 开发模式

```powershell
# 后端（端口 5800）
runtime\py310\python.exe -m uvicorn backend.main:app --reload

# 前端（端口 5173，自动代理 API）
cd frontend
pnpm install
pnpm dev
```

---

## 🤝 如何贡献

我们欢迎 **所有级别的贡献者**！无论你是刚入门还是资深开发者，都能找到适合的位置。

### 💡 贡献什么？

| 方向 | 适合人群 | 入门难度 |
|------|---------|---------|
| 🐛 **Bug 修复** | 所有人 | ⭐ |
| 📝 **文档完善** | 技术写作爱好者 | ⭐ |
| 🎨 **UI/UX 优化** | 前端/设计师 | ⭐⭐ |
| 🔧 **新功能开发** | 全栈开发者 | ⭐⭐⭐ |
| 🧠 **模型集成** | AI 算法工程师 | ⭐⭐⭐⭐ |
| ⚡ **性能优化** | 资深工程师 | ⭐⭐⭐⭐ |

### 🚀 快速上手贡献

1. **Fork 本仓库**（右上角点 Fork 按钮）
2. **Clone 到本地**
   ```bash
   git clone https://github.com/你的用户名/OmniSpace.git
   ```
3. **创建分支**
   ```bash
   git checkout -b feature/你的功能名
   ```
4. **提交更改**
   ```bash
   git commit -m "feat: 添加了什么功能"
   ```
5. **推送并提 PR**
   ```bash
   git push origin feature/你的功能名
   ```
   然后在 GitHub 上创建 Pull Request

### 📋 Good First Issues

想找容易上手的任务？关注以下方向：
- 前端 UI 细节优化（动画、交互、响应式）
- 后端 API 补充单元测试
- 文档翻译与润色
- 添加更多模型支持

### 📐 代码规范

- 后端：遵循 `ruff.toml` 配置，提交前跑 `ruff check`
- 前端：TypeScript strict 模式，遵循项目已有代码风格
- 提交信息：`feat: / fix: / docs: / refactor: / perf:` 前缀

---

## 🗺 路线图（Roadmap）

### ✅ 已完成
- [x] 基础架构搭建（FastAPI + React）
- [x] AI 对话模块（Qwen3-VL 多模态 + RAG 混合检索）
- [x] AI 绘画模块（SDXL / FLUX.2 Klein / qwen-image + LoRA）
- [x] 漫剧分镜编辑器（分镜→资产→关键帧→视频全链路）
- [x] 视频生成模块（Wan2.2 / LTX-Video / AnimateLCM 真实推理，Ken Burns 诚实降级）
- [x] 知识学习与 RAG（ChromaDB + FTS5 + RRF 融合）
- [x] 显存协调器与功能互斥锁
- [x] 图生 3D 基础管线（TripoSR）

### 🔄 进行中
- [ ] 语音克隆与 TTS 优化
- [ ] 角色一致性算法提升（DINOv2 人脸/场景门禁已上线，持续标定中）
- [ ] 移动端适配

### 🔮 规划中
- [ ] 插件系统（第三方模型/功能扩展）
- [ ] 多人协作创作
- [ ] 社区素材共享平台
- [ ] Linux / macOS 支持
- [ ] 更多模型生态接入

> 💡 **有想法？提 Issue 讨论！** 我们非常欢迎新的创意和建议。

---

## 📚 文档索引

| 文档 | 内容 | 何时看 |
|------|------|--------|
| [部署手册](docs/deployment-manual.md) | 硬件要求、环境装配、模型配置 | 第一次部署 |
| [架构总览](docs/design/architecture-overview.md) | 分层结构、中间件链、数据流 | 理解系统设计 |
| [API 端点总表](docs/design/api-endpoints.md) | 328 HTTP（含别名）+ 4 WS 端点文档 | 开发对接 |
| [数据库 ER](docs/design/database-er.md) | 31 张用户表结构 + 迁移说明 | 数据库相关 |
| [故障排查](docs/troubleshooting.md) | OOM / 端口冲突 / 模型加载失败 | 出问题时 |
| [需求追踪矩阵](docs/requirements-traceability.md) | 功能/模块/架构/工程四类需求追踪 | 查需求状态 |

---

## 💬 社区与交流

- **GitHub Issues**：Bug 报告、功能建议 → [提 Issue](https://github.com/Yzw202011/OmniSpace/issues)
- **Discussions**：技术讨论、想法交流 → [去讨论](https://github.com/Yzw202011/OmniSpace/discussions)

---

## ⭐ Star 历史

如果这个项目对你有帮助，点个 Star 支持一下吧！你的支持是我们持续迭代的最大动力 💪

---

## 📄 License

MIT License（仓库暂未附带 LICENSE 文件，待补充）

---

<div align="center">

**用 AI 释放创作力 · OmniSpace**

Made with ❤️ by the OmniSpace Community

</div>
