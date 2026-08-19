# OmniSpace AI 故障排查手册

覆盖四大高频故障：**显存 OOM / 端口冲突 / 数据库版本冲突 / 模型探测失败**。每节一页，含症状、根因、诊断、处置、预防。

## 0. 通用排查入口

| 资源 | 位置 |
| --- | --- |
| 后端标准输出 / 错误流 | `logs/backend_stdout.log`、`logs/backend_stderr.log` |
| 日志脱敏与错误信封 | 后端日志（`omnispace.*` 命名空间，按模块分段） |
| 健康检查 | `GET /health` → `{"code":0, "data":{"status":"healthy",...}}` |
| 硬件遥测 | `GET /api/v1/hardware`（探测失败的字段显式为 `null` + `available:false`，**不会**回填假数据） |
| 显存记账 | `GET /api/v1/models`（含 `min_vram_gb` 估算与 degraded_probes 降级记录） |
| 模型清单 | `GET /api/v1/models`；接线状态权威源 `docs/requirements-traceability.md`（M-01~M-16） |

快速分诊：

```
启动就失败          → §2 端口冲突 / §3 数据库版本冲突
启动成功但生成报错   → §1 显存 OOM（错误码 20xxx 段）
模型列表缺模型       → §4 模型探测失败
```

---

## 1. 显存 OOM（CUDA out of memory / 错误码 20013）

### 症状

- 前端弹错：错误码 **20013**「显存不足且无可驱逐模型」或 **20014**「功能互斥，当前无法加载」；
- 后端日志出现 `torch.cuda.OutOfMemoryError: CUDA out of memory`；
- 训练链路报 `TRAINING_OOM`（训练显存溢出）；
- 偶发：生成任务卡在 `generating` 后转 `error`（视频轮询 3 次失败会收敛到 error 并释放互斥锁，不会永久锁死）。

### 根因

1. **功能互斥**：dialog / paint / video_gen / training 四类重量级功能进程内全互斥（`middleware/feature_lock.py`），冲突返回 `FEATURE_MUTEX_LOCKED`（20014）——这是设计行为，不是故障。
2. **显存估算闸门拦截**：目标模型估算显存 > 当前可用（`model_manager.ensure_loaded` 先查 `estimate_vram_gb`，驱逐后仍不够 → 20013）。16GB 卡跑 24GB 档模型（LTX-2 fp32）必然触发。
3. **真实 OOM**：估算偏乐观或显存碎片化，推理中途爆掉。
4. **驱动级显存泄漏**：崩溃后 CUDA 上下文未释放，`torch.cuda` 记账与实际不符。

### 诊断步骤

```powershell
# 1. 看真实显存占用（找占用大户）
nvidia-smi

# 2. 查后端显存记账与降级探测
curl http://127.0.0.1:8765/api/v1/models   # 看 loaded / min_vram_gb / degraded_probes

# 3. 查互斥状态（哪个功能持有锁）
#    后端日志 grep FEATURE_MUTEX_LOCKED / 20014

# 4. 看错误细节
type logs\backend_stderr.log | findstr /i "OutOfMemory 20013"
```

`degraded_probes` 非空（如 `vram_total_mb` / `empty_cache`）说明 CUDA 探测本身失败，显存记账已退化为纯记账值，此时 20013 的判定不可信，先按 §4 修探测。

### 处置步骤

1. **互斥冲突（20014）**：等当前任务完成，或在前端结束另一功能（如正在生成视频时先不发起绘画）。
2. **显存不足（20013）**：
   - 前端「模型管理」对已加载的大模型执行 **force_unload**（常驻与按需模型间的切换机制，16GB 约束下的正解）；
   - 重试，路由器会自动降档（视频 8GB 档 → AnimateLCM 2GB 档；对话 8B → 4B）。
3. **真实 CUDA OOM**：
   - 确认模型用对版本：LTX 必须 int8 量化版；视频生成分辨率须整除 32、帧数 ≡ 1 (mod 8)；
   - `nvidia-smi` 找残留 python 进程 → `taskkill /PID <pid> /F`；
   - 仍失败则重启后端（launcher 心跳会自动拉起，或手动重跑 launcher）。
4. **训练 OOM（TRAINING_OOM）**：调小 batch / LoRA rank，或换更小底模；16GB 卡训练与推理严格互斥，训练前确保其他引擎已卸载。

### 预防

- 信任显存闸门：不要绕过 `ensure_loaded` 直连引擎；
- 大模型操作前先 unload（前端模型管理页有显存水位提示）；
- 换模型档位看路由表（部署手册 §3.2），16GB 卡不要选 24GB 档模型。

---

## 2. 端口冲突

### 症状

- Launcher 提示「端口 8765 被占用」并自动切换到 5800~5835 区间某端口（浏览器仍会自动打开正确地址）；
- 极端情况：区间全被占用，启动失败；
- 后端起不来但无报错（残留进程假死占用端口）。

### 根因

后端默认监听 `127.0.0.1:8765`。占用来源：① 上次未退干净的 OmniSpace 残留进程；② 本机其他服务（IIS / 另一个 Python web 服务 / 占用工具）。

### 诊断步骤

```powershell
# 谁占用了端口（任选其一）
netstat -ano | findstr :8765
# 拿到 PID 后看进程名
tasklist /FI "PID eq <pid>"
```

### 处置步骤

Launcher 自带三级递进，通常无需人工干预：

1. **第一级（自动）**：占用者是 OmniSpace/python 残留 → launcher 自动 terminate/kill（严格匹配进程名 + 命令行含 omnispace + backend.main，**不会误杀**其他 Python 服务）；
2. **第二级（自动）**：外部占用 → 扫描 5800~5835 取第一个空闲端口，动态写前端 `config.js`，界面地址随之更新；
3. **第三级（人工）**：区间全满 → 手动清理：

```powershell
# 确认是可杀的进程后再执行（勿杀系统服务）
taskkill /PID <pid> /F
# 或者给 launcher 指定别的端口
runtime\py310\python.exe launcher\launcher.py --port 5900
```

### 预防

- 退出程序用托盘菜单正常退出（launcher 会停心跳、杀后端子进程）；
- 避免在 5800~5835 区间部署其他常驻服务。

---

## 3. 数据库版本冲突

### 症状

后端启动即崩，日志/界面出现：

```
RuntimeError: 数据库 schema 版本 v<N> 高于程序 v2，请升级程序后再打开（data\omnispace.db）
```

（`backend/data/database.py` 版本守护；`N > 2`。）

### 根因

**用旧版程序打开了新版程序产生的数据库**（版本回退场景）：迁移机制用 `PRAGMA user_version` 携带版本号（当前 `SCHEMA_VERSION=2`），库版本高于代码版本说明库里已有代码不认识的新列/新表，强行打开会造成静默数据损坏，因此启动时直接 RuntimeError 拒绝——这是守护，不是 bug。

常见触发：① RC 目录回退到旧版本但仍指向新 `data/`；② 手工把新库拷给旧程序；③ 多份代码副本共用同一个 `data/` 目录。

### 诊断步骤

```powershell
# 直接读库版本
runtime\py310\python.exe -c "import sqlite3; print(sqlite3.connect(r'data\omnispace.db').execute('PRAGMA user_version').fetchone())"
```

输出 `2` = 库与代码同级（v2），冲突另有原因；`>2` = 确认版本冲突。

### 处置步骤

1. **正解：升级程序**——把代码（RC 目录）更新到与库匹配的版本（新版本 `SCHEMA_VERSION ≥ 库版本`），库原样打开；
2. **确要回退**：先备份整个 `data/` 目录，再用**新版本**程序导出所需数据，回退后重建；
3. **禁止**：手工 `PRAGMA user_version=2` 强改版本号——新版列还在库里，旧代码读写即损坏；
4. **多副本共用 data/ 的情况**：让每份代码用独立 `data/` 目录，杜绝交叉。

### 预防

- 迁移铁律已内置：库版本高于代码 → 拒绝启动；历史迁移组只许追加不许修改；
- RC 构建默认排除 `data/`（`tools/build_rc.py`），保持交付目录与运行数据分离；
- 数据备份连 `keys/` 一起拷（加密库的密钥按机器 DPAPI 保护，跨机恢复需同机密钥）。

---

## 4. 模型探测失败

### 症状

- 前端「模型管理」列表缺某模型，但磁盘上目录明明存在；
- 模型状态显示未下载/不可用，或 `GET /api/v1/models` 看不到它；
- 硬件遥测 GPU 字段全 `null` / `available: false`；
- 日志出现「CUDA 总显存探测失败」「探测降级」字样。

### 根因

1. **modelscope 嵌套快照布局**：权重藏在 `模型目录/models/Qwen--*/snapshots/master/` 深层，两层通用磁盘扫描不命中（qwen3-vl-8b 即此布局）；
2. **模型未接线**：目录在、权重对，但不在引擎候选表/路由表里——**下载 ≠ 可用**；
3. **探测链路本身降级**：pynvml 不可用 / 驱动异常 → `vram_manager` 记入 `_degraded_probes`，显存记账退化为不可信；
4. **权重不完整**：目录存在但 safetensors 残缺（下载中断），完整性校验失败。

### 诊断步骤

```powershell
# 1. 目录真实结构与大小（空目录/几百MB 的"目录存在"都是假象）
Get-ChildItem models\<模型目录> -Recurse -File | Measure-Object Length -Sum

# 2. 嵌套快照布局检查（qwen 系 model 下载常见）
Get-ChildItem models\qwen3-vl-8b -Recurse -Directory -Depth 4 | Select-Object -ExpandProperty FullName

# 3. 后端视角：模型清单 + 显存降级记录
curl http://127.0.0.1:8765/api/v1/models

# 4. 驱动与 CUDA 可用性
nvidia-smi
runtime\py310\python.exe -c "import torch; print(torch.cuda.is_available(), torch.cuda.total_memory())"
```

### 处置步骤

1. **嵌套快照布局**：在 `backend/services/model_manager/__init__.py` 的 `_MODEL_PATH_HINTS` 补一条 `"模型id": "相对路径"`（qwen3-vl-8b 已登记，可作范本），重启后端；
2. **未接线模型**：接线需要改引擎候选表（`PAINT_MODEL_CANDIDATES` / video_engine `_VIDEO_PIPELINE_CLASSES` 等）并加显存估算条目——查 `docs/requirements-traceability.md` 该模型（M-xx）的决策（接线排期 or 删除），不要自行下载未接线的模型；
3. **探测降级（degraded_probes 非空）**：先修驱动（重装 NVIDIA 驱动 → `nvidia-smi` 正常），重启后端，确认 `degraded_probes` 清空；
4. **权重不完整**：launcher 的模型完整性校验支持断点续传——重跑 launcher 让它补齐，或删除残缺目录重新放置。

### 预防

- 放置新模型后**必查两件事**：`GET /api/v1/models` 能看到 + 该模型有接线记录（RTM 矩阵）；
- qwen 系从 modelscope 下载的模型统一走 `_MODEL_PATH_HINTS` 登记；
- 保持驱动为 Studio/Game Ready 稳定版，pynvml 依赖驱动接口。

---

## 附录：错误码速查（20xxx 模型加载段）

| 码 | 含义 | 指向 |
| --- | --- | --- |
| 20011 | 模型未下载 | §4（放置/补下载） |
| 20013 | 显存不足且无可驱逐模型 | §1 |
| 20014 | 功能互斥，当前无法加载 | §1（等待或主动结束另一功能） |

*关联文档：部署手册（`docs/deployment-manual.md`）· 架构总览（`docs/design/architecture-overview.md`）· 模型决策权威源（`docs/requirements-traceability.md`）*
