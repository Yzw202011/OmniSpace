# OmniSpace AI 部署手册

> **2026-09-02 诚实化校准**：§1/§3/§6 原停留于 2026-08 中旬状态（约半数模型目录已隔离、路由表已裁剪重写、DB 版本落后 5 代），本轮已按代码与磁盘实测重写；勘误证据见 `docs/audit/round-2026-09-02-doc-honestification.md`。模型状态唯一真源 = `models/models_manifest.json` + `GET /api/v1/models` 磁盘扫描。

目标：**换机冷启动只靠本文档可完成**。覆盖硬件要求、环境装配、模型资产配置、首次启动、RC 交付五个阶段。

---

## 1. 硬件要求

| 项目 | 最低要求 | 说明 |
| --- | --- | --- |
| GPU | NVIDIA，显存 ≥ 12GB（基线 RTX 3060 12GB） | 三张路由表最低档 6~12GB；无 NVIDIA 显存时 AI 生成功能不可用（如实降级，无 CPU 假跑档） |
| 显存 | 12GB 基线 / 16GB 推荐 | 常驻与按需模型间靠 force_unload 切换；H3 视频 5s 段实测峰值 ~10.6GB |
| 磁盘 | 空余 ≥ 20GB（不含模型库） | Launcher 自检硬门槛（`min_disk_space_gb=20`），模型库 + 生成物写入余量 |
| 磁盘（含模型） | ~60GB（最小集）~ 260GB（全家桶） | `models/` 实占 244G（2026-09-02 实测，两批清理后；manifest 登记 210.3G/20 项） |
| 系统 | Windows 10/11 x64 | 加密密钥依赖 Windows DPAPI；launcher 使用 `CREATE_NEW_PROCESS_GROUP` |
| CUDA | NVIDIA 驱动自带 CUDA Runtime | torch 2.11.0+cu128，无需单独安装 CUDA Toolkit |

无 GPU / 驱动异常时系统可启动、`/health` 正常，但三张路由表最低档均需 NVIDIA 显存 6~12GB，AI 生成功能不可用；显存守门人进入降级记账模式（见故障手册 §4），不回填假数据。

## 2. 环境装配

项目自带全部运行时，**不依赖系统 Python**。

### 2.1 目录就位

```text
e:\OmniSpace\
├── runtime\py310\   内嵌 Python 3.10（python310._pth 挂载双站点）
├── pydeps\          承重依赖站点（fastapi/torch/diffusers/chromadb 等 170+ 包仅存于此，dist-info 实测 181 个）
├── backend\ frontend\ launcher\ tools\ docs\ ...
└── models\          模型资产（见 §3，可后置下载）
```

三条铁律：

1. **`runtime/` 与 `pydeps/` 勿删勿动**。`python310._pth` 将两者挂载为双站点（pydeps 优先），删除 pydeps 即后端瘫痪。
2. **launcher 必须以项目根目录为 cwd 启动**（后端用 `cwd=PROJECT_ROOT` 拉起 uvicorn，相对路径全部依赖此约定）。
3. **解释器唯一**：任何脚本一律用 `runtime\py310\python.exe` 执行（`.venv` 已废弃，指向已删除的系统 Python）。

### 2.2 依赖校验

依赖锁定文件为 `requirements-lock.txt`（155 条精确版本，唯一真源）。冷启动后自检：

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

`models/` 实占 244G（2026-09-02 实测）。**下载 ≠ 可用**：模型须在引擎候选表/路由表中接线才生效，状态真源 = `models/models_manifest.json`（v3，20 项）+ 启动后 `GET /api/v1/models` 磁盘扫描。

### 3.1 模型清单（manifest v3 实测生成，2026-09-02）

**在盘登记（20 项合计 210.3G；盘实占 244G 含 GGUF/多格式副本）**：

| 模型 | 体积 | min_vram | 角色 |
| --- | --- | --- | --- |
| qwen3-vl-4b | 8.4G | 9.0 | 对话主力（8G 档） |
| qwen3-vl-8b-awq | 7.0G | 7.5 | 对话 12G+ 档（09-02 起兼承 16G 档） |
| deepseek-r1-14b-w4a16 | 9.3G | 11.5 | 分镜描述精修（ai-describe 链 12 文件在用） |
| flux2-klein-9b | 32.3G | 8.5 | 绘画主力（推理实走 GGUF Q6_K；diffusers 分片被就绪检查硬性要求） |
| flux2-klein-9b-fp8 | 12.0G | 10.5 | ComfyUI 侧硬链在用 |
| flux2-klein-4b | 17.5G | 12.0 | 已弃 one-pass 路线，去留待拍板（拍板清单 A5） |
| qwen-image-2512 | 22.0G | 6.0 | 绘画高精度档（中文原生理解，GGUF 流式，RAM 28G+） |
| sdxl-base-1.0 | 6.9G | 7.0 | 学习页 LoRA 训练底座 |
| sd15 | 4.2G | 4.0 | AnimateLCM 视频兜底底座 |
| wan22-ti2v-5b | 12.0G | 13.0 | 视频 I2V |
| wan21-i2v-1.3b / wan21-vace-1.3b | 3.2G + 3.2G | 8.0 | 弱活备选（引擎管线在） |
| ltx-video-0.9.5 | 23.6G | 12.0 | 弱活备选（下拉可选） |
| AnimateLCM | 1.7G | 2.0 | 视频兜底（2G 档） |
| minimax-h3 | 42.7G | 13.0 | 视频主力（ComfyUI 链式） |
| whisper-tiny / bge-large-zh / sam-vit-h / dinov2-small / insightface | 合计 ~4.4G | ≤2.5 | ASR / 向量检索 / 分割（漫剧在用）/ 一致性 / 人脸 |

**已隔离（盘上不存在，2026-09-02 两批移入 `E:\_trash_staging\`，语音路线拍板后从中恢复一家）**：qwen3-vl-8b 完整版（原为 4KB 空壳）、qwen2-vl-2b、qwen3-32b、codestral-22b、qwen3-tts、qwen3-asr、cosyvoice2、gpt-sovits、bge-m3、TripoSR、MiDaS、YOLOv8（`3d/`、`depth/`、`detect/` 整目录）。

最小可跑集（对话 + 绘画 + 视频）：qwen3-vl-4b + flux2-klein-9b + AnimateLCM + sd15 + 小模型组 ≈ **55G**。

### 3.2 显存路由表（硬编码于 `backend/data/models.py`，2026-09-02 实读）

加载时按当前可用显存自动选档（`model_manager.ensure_loaded`）：

- **视频**（`VIDEO_ROUTING_TABLE`）：≥12GB → minimax-h3（唯一档；H3 NVFP4，5s 段实测采样峰值 ~10.6GB）
- **对话**（`DIALOG_ROUTING_TABLE`）：≥12GB → qwen3-vl-8b-awq；≥8GB → qwen3-vl-4b
- **绘画**（`PAINT_ROUTING_TABLE`）：≥10GB → flux2-klein-9b；≥6GB → qwen-image-2512

> 旧版"视频七档 / CPU 兜底档"已于 2026-08-29 裁剪——显存不足最低档时功能**如实降级**（degraded + degrade_reason），不路由 CPU 假跑。GPU 型号级另有 `HARDWARE_TIER_TABLE` 六档自适应（含 rtx5070ti 档）。

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
6. **打开浏览器** `http://127.0.0.1:<port>`（2026-09-02 核实：无"动态写 config.js"机制，launcher 不写任何前端配置文件）。

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

排除规则：顶层 `.git`、`data`、`logs` 等 + 全树 `__pycache__`/`node_modules` + `tmp_*`/`_tmp_*`/`*.log` 等文件模式。**`pydeps/`、`runtime/`、`models/` 必须随 RC 交付**——排除任一项都会导致 RC 后端无法启动（pydeps 缺失 = 170+ 个包消失）。

## 6. 配置项速查

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| 后端端口 | 8765（`--port` 覆盖） | 冲突时自动扫描 5800~5835 |
| 绑定地址 | 127.0.0.1 | 非回环拒绝启动；`OMNISPACE_ALLOW_LAN=1` 显式豁免（API 无认证，自担风险） |
| 磁盘水位 | 20GB | launcher 自检门槛 |
| 心跳间隔 | 5s / 重启上限 5 次 / 冷却 30s | 后端崩溃自动拉起 |
| 数据库 | `data/` 下 SQLite（WAL；主库 30 表 + `logs/flow_trace.db` 独立库 2 表） | 版本化迁移 `PRAGMA user_version`，当前 **v7**（2026-09-02 实测） |
| 加密密钥 | `keys/`（Windows DPAPI 保护） | 缺失时按机器重新生成，跨机拷库需连 keys/ 一起迁移 |

---

*关联文档：故障排查手册（`docs/troubleshooting.md`）· 架构总览（`docs/design/architecture-overview.md`）· 模型接线状态权威源（`docs/requirements-traceability.md` M-01~M-16）*
