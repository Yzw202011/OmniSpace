# OmniSpace AI 部署手册

目标：**换机冷启动只靠本文档可完成**。覆盖硬件要求、环境装配、模型资产配置、首次启动、RC 交付五个阶段。

---

## 1. 硬件要求

| 项目 | 最低要求 | 说明 |
| --- | --- | --- |
| GPU | NVIDIA，显存 ≥ 16GB（如 RTX 4080/4090） | 模型路由按显存分级；8GB 显存可跑但对话/绘画/视频会降档到小模型甚至 CPU 路由 |
| 显存 | 16GB | 本项目按 16GB 约束调优：LTX-2 用 int8 量化版、常驻与按需模型间靠 force_unload 切换 |
| 磁盘 | 空余 ≥ 20GB（不含模型库） | Launcher 自检硬门槛（`min_disk_space_gb=20`），模型库 + 生成物写入余量 |
| 磁盘（含模型） | ~250GB | `models/` 全量约 186GB + 系统与运行时 |
| 系统 | Windows 10/11 x64 | 加密密钥依赖 Windows DPAPI；launcher 使用 `CREATE_NEW_PROCESS_GROUP` |
| CUDA | NVIDIA 驱动自带 CUDA Runtime | torch 2.11.0+cu128，无需单独安装 CUDA Toolkit |

无 GPU / 驱动异常时系统可启动：对话路由到 CPU 档（qwen3-vl-2b-int4-cpu / sdxl-cpu / cogvideocx-cpu），显存守门人进入降级记账模式（见故障手册 §4）。

## 2. 环境装配

项目自带全部运行时，**不依赖系统 Python**。

### 2.1 目录就位

```text
e:\OmniSpace\
├── runtime\py310\   内嵌 Python 3.10（python310._pth 挂载双站点）
├── pydeps\          承重依赖站点（fastapi/torch/diffusers/chromadb 等 156 包仅存于此）
├── backend\ frontend\ launcher\ tools\ docs\ ...
└── models\          模型资产（见 §3，可后置下载）
```

三条铁律：

1. **`runtime/` 与 `pydeps/` 勿删勿动**。`python310._pth` 将两者挂载为双站点（pydeps 优先），删除 pydeps 即后端瘫痪。
2. **launcher 必须以项目根目录为 cwd 启动**（后端用 `cwd=PROJECT_ROOT` 拉起 uvicorn，相对路径全部依赖此约定）。
3. **解释器唯一**：任何脚本一律用 `runtime\py310\python.exe` 执行（`.venv` 已废弃，指向已删除的系统 Python）。

### 2.2 依赖校验

依赖锁定文件为 `requirements-lock.txt`（154 包精确版本，唯一真源）。冷启动后自检：

```powershell
runtime\py310\python.exe -c "import fastapi, torch, diffusers; print(torch.__version__, torch.cuda.is_available())"
```

预期输出 `2.11.0+cu128 True`。若需重建环境：

```powershell
runtime\py310\python.exe -m pip install -r requirements-lock.txt --target pydeps --no-deps
```

> 常规交付（RC 目录 / 整机拷贝）无需此步——`runtime/` 与 `pydeps/` 随包携带。

### 2.3 前端依赖（仅开发模式需要）

生产模式前端静态资源由后端托管，无需 Node。开发模式才需要：

```powershell
cd frontend
npm install
```

## 3. 模型资产配置表

`models/` 全量约 186GB。**下载 ≠ 可用**：模型须在引擎候选表/路由表中接线才生效，状态以 `docs/requirements-traceability.md`（M-01~M-16）为准。

### 3.1 模型目录清单

| 目录 | 大小 | 用途 | 接线状态 |
| --- | --- | --- | --- |
| `models/paint/sdxl-base-1.0/` | 32.6GB | 文生图主力（SDXL base，bf16 实占约 7GB 显存） | ✅ 已接线 |
| `models/paint/flux2-klein-4b/` | 13.2GB | FLUX 文生图主力 | 🟦 接线排期 P1 |
| `models/qwen3-vl-8b/` | 16.3GB | 对话/视觉理解高档路由（modelscope 嵌套快照布局） | ✅ 已接线 |
| `models/qwen3-vl-4b/` | 8.3GB | 对话中档路由 | ✅ 已接线 |
| `models/qwen2-vl-2b/` | 2.3GB | 对话低档路由 | ✅ 已接线 |
| `models/qwen3-32b/` | 19.3GB | Q4 GGUF 推理增强（llama-cpp 后端，16GB 卡需 CPU offload） | ✅ 已接线 |
| `models/codestral-22b/` | 12.4GB | Q4 GGUF 代码辅助 | ✅ 已接线 |
| `models/video_gen/ltx-video-0.9.5/` | 23.7GB | 视频生成主力（LTXPipeline 动态发现） | ✅ 已接线 |
| `models/video_gen/AnimateLCM/` | 1.7GB | img2video 兜底（运动模块，2GB 显存档） | ✅ 已接线 |
| `models/sd15/` | 32.5GB | AnimateLCM 的 SD1.5 底座（含四份底权重复制，≈22GB 清理候选） | ✅ 已接线 |
| `models/qwen3-tts/` | 4.2GB | TTS 主力候选 | 🟦 排期 P1（transformers 4.51 无 Qwen3TTS 管线类，门控） |
| `models/qwen3-asr/` | 4.4GB | 语音识别 | 🟦 排期 P2 |
| `models/cosyvoice2/` | 5.2GB | TTS 备选（与 Qwen3-TTS 重叠） | 🟦 待确认删除 |
| `models/gpt-sovits/` | 2.6GB | 声音克隆（推理代码包永不随包） | 🟦 待确认删除 |
| `models/embed/bge-large-zh/` | 1.2GB | 向量嵌入（现役，Chroma 向量库基座） | ✅ 已接线 |
| `models/embed/bge-m3/` | 7.9GB | 嵌入升级候选（切换需重建向量库） | 🟦 排期 P1 |
| `models/3d/TripoSR/` | 1.6GB | 单图转 3D（.glb 输出） | ✅ 已接线 |
| `models/3d/hunyuan3d-2.1/` | 13.9GB | 3D 画质增强候选 | 🟦 排期 P2 |
| `models/sam-vit-h/` | 2.4GB | 图像分割（/art/segment） | ✅ 已接线 |
| `models/depth/`（MiDaS） | 0.1GB | 深度估计（/art/depth） | ✅ 已接线 |
| `models/detect/`（YOLOv8-nano） | 0.0GB | 目标检测（/art/detect） | ✅ 已接线 |
| `models/whisper-tiny/` | 0.1GB | ASR 兜底 | ✅ 已接线 |
| `models/lora/`、`models/style_lora/` | 0.5GB | LoRA 训练产物 | 训练后生成 |

最小可跑集（对话 + 绘画 + 视频 + 3D 四条主链路）：`paint/sdxl-base-1.0` + `qwen3-vl-4b` + `video_gen/ltx-video-0.9.5` + `video_gen/AnimateLCM` + `sd15` + `3d/TripoSR` ≈ 100GB。

### 3.2 显存路由表（硬编码于 `backend/data/models.py`）

加载时按当前可用显存自动选档（`model_manager.ensure_loaded`）：

**视频**（`VIDEO_ROUTING_TABLE`）：

| 显存 ≥ | 模型 |
| --- | --- |
| 24GB | LTX-2 |
| 16GB | Wan2.1-14B-FP8 |
| 12GB | Wan2.1-14B-INT4 |
| 8GB | Wan2.1-1.3B / LTX-Video 0.9.5（T5 int8 量化加载后约 9GB） |
| 6GB | CogVideoX-2B |
| 2GB | AnimateLCM（随包兜底） |
| 0 | CogVideoX-2B-CPU |

**对话**：≥12GB → qwen3-vl-8b；≥8GB → qwen3-vl-4b；≥4GB → qwen3-vl-2b；否则 CPU 档。
**绘画**：≥24GB → flux.1-dev-fp8；≥16GB → flux.1-schnell-fp8；≥12GB → kolors-2.1；≥8GB → sdxl-base-1.0 / sdxl-lcm；否则 CPU 档。

### 3.3 特殊布局注意事项

- **modelscope 嵌套快照**：`qwen3-vl-8b` 实际权重在 `models/qwen3-vl-8b/models/Qwen--Qwen3-VL-8B-Instruct/snapshots/master/` 下。`model_manager._MODEL_PATH_HINTS` 已登记此路径；新模型若采用同款布局，须在此表补条目，否则磁盘扫描不命中（详见故障手册 §4）。
- **模型放置后须验证**：启动后访问 `GET /api/v1/models`（带 `min_vram_gb` 估算）确认被发现；模型加载由显存估算闸门把关，防止 16GB 卡 OOM。

## 4. 首次启动

```powershell
cd e:\OmniSpace
runtime\py310\python.exe launcher\launcher.py
```

Launcher 启动序列：

1. **环境自检**：OS / CUDA / 磁盘余量（≥20GB，不足直接拒绝）；
2. **端口冲突三级处理**（详见故障手册 §2）；
3. **模型完整性校验**：缺失模型走断点续传下载；
4. **拉起后端**：`python -m uvicorn backend.main:app --host 127.0.0.1 --port <N> --log-level info`（cwd = 项目根）；
5. **心跳监控**：每 5 秒探测 `GET /health`，崩溃自动重启（最多 5 次，冷却 30 秒）；
6. **动态写前端 config.js** + 打开浏览器 `http://127.0.0.1:<port>`。

启动验证清单（换机部署逐项打勾）：

- [ ] `curl http://127.0.0.1:8765/health` 返回 `{"code": 0, "data": {"status": "healthy", ...}}`
- [ ] `GET /api/v1/models` 列出模型且带显存估算
- [ ] `GET /api/v1/hardware` 硬件遥测中 GPU/显存字段非 `null`（探测失败会显式标 `available: false`，不回填假数据）
- [ ] 前端页面可打开（生产模式由后端根路径 `/` 托管，非 `/static`）
- [ ] 冒烟测试全绿：`runtime\py310\python.exe -m pytest -m smoke`

## 5. RC 交付（发布目录构建）

`E:\RC1002` **只能由脚本产出，禁止手工改动**（单一真源原则）：

```powershell
runtime\py310\python.exe tools\build_rc.py            # 增量同步至 E:\RC1002
runtime\py310\python.exe tools\build_rc.py --verify   # 仅校验差异，零漂移
```

排除规则：顶层 `.git`、`data`、`logs` 等 + 全树 `__pycache__`/`node_modules` + `tmp_*`/`_tmp_*`/`*.log` 等文件模式。**`pydeps/`、`runtime/`、`models/` 必须随 RC 交付**——排除任一项都会导致 RC 后端无法启动（pydeps 缺失 = 156 个包消失）。

## 6. 配置项速查

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| 后端端口 | 8765（`--port` 覆盖） | 冲突时自动扫描 5800~5835 |
| 绑定地址 | 127.0.0.1 | 非回环拒绝启动；`OMNISPACE_ALLOW_LAN=1` 显式豁免（API 无认证，自担风险） |
| 磁盘水位 | 20GB | launcher 自检门槛 |
| 心跳间隔 | 5s / 重启上限 5 次 / 冷却 30s | 后端崩溃自动拉起 |
| 数据库 | `data/` 下 SQLite（WAL，30 表） | 版本化迁移 `PRAGMA user_version`，当前 v2 |
| 加密密钥 | `keys/`（Windows DPAPI 保护） | 缺失时按机器重新生成，跨机拷库需连 keys/ 一起迁移 |

---

*关联文档：故障排查手册（`docs/troubleshooting.md`）· 架构总览（`docs/design/architecture-overview.md`）· 模型接线状态权威源（`docs/requirements-traceability.md` M-01~M-16）*
