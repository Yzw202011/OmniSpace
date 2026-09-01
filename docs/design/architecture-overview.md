# OmniSpace AI 架构总览

> 版本 v2.3.1 ｜ 生成于 2026-08-20（TASK-P2-02，对应审计 P03）｜ 事实来源：backend/main.py、config.yaml、launcher/launcher.py 及各模块源码
> **2026-08-28 校准**：路由模块 13→14（logs）、HTTP 端点 270→328（含别名）、限流 100→300/min、引擎与 api 行数按当前源码刷新、前端 React 19.0 + 9 路由、launcher 端口口径补充。
> **2026-09-02 复核**：路由模块 14→**15**（license 入列，见 §3 路由表）、路由装饰器 328→**319**（08-29 剔除 3D 导演台路由 + 统计口径差异，与《全量技术文档-2026-09-01》319 口径一致）；middleware/ 七件实况：cors/error_handler/feature_lock/logger/rate_limit/request_context/upload_guard。
>
> 配套文档：[API 端点总表](api-endpoints.md) ｜ [数据库 ER 说明](database-er.md)

## 1. 系统形态

OmniSpace AI 是一台跑在单机 Windows 工作站上的全模态创作工作站：对话、绘画、漫剧（分镜→资产→关键帧→视频）、知识学习、模型管理、视频风格训练六个业务域共用一组本地大模型与一块 16GB 级 GPU。进程形态只有两个——一个 Python 后端进程和一个由它静态托管的前端页面，外加一个负责拉起与守护的启动器。

```
┌─────────────────────────────────────────────────────────────────┐
│ launcher/launcher.py（独立进程）                                  │
│  · 环境自检（Python/磁盘/CUDA/依赖） → 端口冲突三级递进            │
│  · subprocess 启动 uvicorn → 5s 心跳守护 → 崩溃自动重启(≤5次)     │
│  · 就绪后 webbrowser.open(http://127.0.0.1:{实际端口})            │
└──────────────────────────┬──────────────────────────────────────┘
                           │ backend.main:app (cwd=项目根, 127.0.0.1:{端口})
┌──────────────────────────▼──────────────────────────────────────┐
│ FastAPI 后端进程（唯一服务进程）                                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ HTTP API   /api/v1/*（15 路由模块，319 路由装饰器含别名）    │  │
│  │ WebSocket  /ws + /api/v1/hardware/realtime + learn 进度     │  │
│  │ 静态托管   frontend/dist（Hash Router，GET / 即 SPA 入口）   │  │
│  └───────────────────────────────────────────────────────────┘  │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTP(SSE/WS)
                 ┌─────────▼─────────┐
                 │ 浏览器（React SPA）│  Vite+TS+Zustand, Hash Router
                 └───────────────────┘
```

服务边界有三条硬约束写在 config 导入期：

- **回环绑定**：`assert_loopback_host()` 在任何 socket 绑定之前校验 host 必须是 127.0.0.1/localhost，否则 RuntimeError 拒绝启动（豁免需 `OMNISPACE_ALLOW_LAN=1`，P0-05）。
- **端口（双口径，2026-08-28 实测确认）**：直接 uvicorn（读 config.yaml）= **5800**；launcher 默认 `--port 8765`（`launcher.py` argparse 默认值，覆盖 dataclass 缺省 5800），端口占用时先清理 OmniSpace/python 残留进程，再扫描 **5800–5835** 递进。
- **API 前缀**：`/api/v1`（ADR-03，旧 `/v1` 已废弃返回 404）。

## 2. 后端分层

请求自上而下穿过五层，每层职责单一、依赖方向朝下：

```
api/            路由层 —— 参数校验(Pydantic) / 信封构造 / 降级决策
                dialog(1526) draw(1317) manga/(包,9模块) learn(407)
                learning(798) knowledge(739) models(1656) style(473)
                browser(141) vision_tools(173) voice(127) hardware(355) system(1485) logs(182)
middleware/     横切层 —— 错误信封 / 功能互斥锁 / 限流 / CORS / 上传闸门
                / 请求上下文(contextvars) / 日志脱敏
services/       业务层 —— 领域服务（知识/学习/风格/行为/浏览器…）
                inference/ 推理引擎    model_manager/ 显存协调    scheduler/ 协同调度
engines/        资源层 —— gpu_backend(后端选择) vram_manager(显存记账)
                memory_manager(RAM LZ4 压缩)
data/           存储层 —— database(SQLite/WAL) file_store cache(进程内)
                vector_db(bge-large-zh) fts_store(FTS5) graph_store crypto(AES-GCM)
```
（行数为 2026-08-28 源码实测；`wc` 口径，随开发持续增长。）

依赖方向上，api 只 import services 与 data，不直接触碰 engines；services/inference 的各引擎经 `model_manager` 申请显存。`services/offload.py` 是唯一的同步推理入口（`run_blocking` = asyncio.to_thread 语义收敛，`sync_core` 装饰器把同步核心包成自调度协程）——API 层不允许出现 `asyncio.to_thread` 直调（P1-06 有测试锁定）。

### 2.1 漫剧包（api/manga/）

TASK-P2-01 把 4521 行的单文件拆成 9 模块的包，`__init__.py` 聚合 router 后由 main.py 经 importlib 挂载，对外零变化。
（行数为 2026-08-28 实测；路由数为**权威端点口径**（不含别名），同日装饰器静态扫描共 107 个（含别名），两口径并存：）

| 模块 | 行数 | 路由数 | 职责 |
|------|-----:|-------:|------|
| common.py | 1123 | 0 | 共享层：内存态兜底存储、行/项目辅助、引擎单例、出图规格铁律 |
| storyboard.py | 1360 | 23 | 分镜表 CRUD / 导入导出 / AI 分镜与描述 / 情绪检测 |
| comic_asset.py | 2028 | 19 | 资产库 / 绑定采纳 / 参考图 / 历史与描述 |
| video.py | 1166 | 15 | 视频生成与任务 / 媒体回读 / 叙事生成 / 可用模型清单 |
| director.py | 353 | 17 | 全景图 / 4合1截图 / 机位与角色 / 文本转3D |
| comic.py | 478 | 8 | 漫画项目 CRUD / 剧本 DSL 导入 / 场景物件 / 导出包 |
| voice.py | 280 | 6 | 音色列表 / 绑定 / 情感 / 试听 / 上传克隆 |
| keyframe.py | 1777 | 7 | 关键帧生成 / 批量 / 版本回滚 / 故事生图 / DINOv2 一致性门禁 |
| comic_gen.py | 1312 | 0 | 生成管线层（one-pass 单图四视图/批量/整图与单视图重生成），由 comic_asset 路由调用 |

## 3. 请求生命周期

中间件按注册顺序反向包裹（Starlette `add_middleware` 是 insert(0) 语义，最后注册的最外层）：

```
请求 → TrustedHost（Host 头白名单 127.0.0.1/localhost/[::1]，防 DNS 重绑定）
     → RequestContext（生成 request_id/duration_ms，变更类请求上报学习调度器）
     → RateLimit（300 req/min/端点滑动窗口（2026-08-28 校准，config.yaml rate_limit: 300；由 100 提额以避免多标签页合法轮询被 429）；/health 豁免；manga/media 独立 600/min 桶）
     → CORS（仅 localhost 任意端口，拒绝时 403 信封）
     → 异常处理器（ApiError/校验/HTTP/兜底 四个，全部收敛为 200 信封）
     → 路由（15 模块，319 路由装饰器含别名）
```

响应统一走信封（ADR-01，HTTP 恒 200）：

```json
// 成功
{"success": true, "data": {...}, "error": null,
 "meta": {"request_id": "...", "timestamp": "...", "duration_ms": 12}}
// 失败
{"success": false, "data": null,
 "error": {"code": "FEATURE_MUTEX_LOCKED", "message": "...", "detail": {...}, "suggestion": "..."},
 "meta": {...}}
```

错误码体系：语义串（`MODEL_*` / `KNOWLEDGE_*` / `BROWSER_*` / `TRAINING_*` / `STYLE_*` / `SYSTEM_*` / `HARDWARE_*` / `FEATURE_*` 约 100 个），历史数字码（40xxx-80xxx）经 `_LEGACY_CODE_MAP` 自动映射，前端只见语义串。

## 4. 推理引擎与显存协调

全部引擎是模块级懒加载单例（双重检查锁），加载策略围绕 16GB 显存的现实设计：

| 引擎 | 模型 | 加载策略 |
|------|------|---------|
| dialog_engine (1251行) | Qwen3-VL 系列（vl/text/gguf 三后端动态发现；AWQ/GPTQ 自动路由 vLLM 子进程） | bf16 + device_map，预检显存不足先腾挪绘画引擎；预填充上限 3072 token（16GB 安全线） |
| paint_engine (1439行) | SDXL base 1.0 / FLUX.2 Klein 4B·9B / qwen-image-2512 多级候选 + Real-ESRGAN | cpu_offload + VAE slicing/tiling；每 step 轮询质量总督可中断 |
| video_engine (2712行) | Wan2.2-TI2V-5B / LTX-Video-0.9.5 / Wan2.1 / CogVideoX / AnimateLCM+SD1.5 多级探测链 | 显存不足最终降级 PIL Ken Burns + FFmpeg 真实 MP4（降级原因如实下发） |
| voice_engine (807行) | TTS 链 cosyvoice→chattts→bark→SAPI5→静音占位 | 自动装载失败只试一次，逐级诚实降级；Whisper ASR |
| depth/detect/segment/triposr | MiDaS-small ONNX / YOLOv8n / SAM ViT-H / TripoSR | 随包小模型，按需加载（文档E 附录B 接线 F-01~F-04） |

（行数为 2026-08-28 源码实测。）

显存协调集中在 `services/model_manager/`：

- `ensure_loaded(category, model_id)` —— 加载前置检查，`_vram_lock` 全程持锁防并发超发；不足时循环 `evict_lowest_priority()` 驱逐低优先级模型。
- 互斥矩阵 `MUTUAL_EXCLUSION_MATRIX` —— dialog / paint / video_gen / training 四类重量级功能全互斥（`middleware/feature_lock.py` 进程内异步锁落地，跨功能 acquire 抛 FEATURE_MUTEX_LOCKED，热保护 ≥90°C 拒绝新任务）。
- 驱逐优先级 `_EVICTION_PRIORITY` —— auxiliary(0) 最先驱逐 … dialog(5) 最后。
- `get_gpu_status()`（pynvml）+ `vram_manager` 记账（CUDA 不可用退化为纯记账，探测失败进 `_degraded_probes` 如实上报，P1-05）。

## 5. 协同调度器

`services/scheduler/` 是常驻 asyncio 后台任务（lifespan 启动，tick 1s，经 run_in_executor 投线程池防阻塞事件循环）：

```
每 tick：采集(GPU 2s/CPU 5s/磁盘 10s 分级缓存) → 热保护检查
   → 学习调度 tick(30s 节流) → 瓶颈分析(6 种 SynergyMode) → 质量总督旗标
   → 空闲回收(功能锁空闲>300s 卸载全部非 embedding 模型)
   → 决策(30s 滞回) → 策略执行(migrate/degrade/preload/compress_cache/force_unload)
   → 模式切换落库 schedule_history
```

其余后台守护线程：behavior-flush（行为日志批量落库，入队 <10ms）、lora-training 与 style-lora-training（PriorityQueue 四级优先队列模拟 Celery，串行消费）、browser_pool 预热线程、ws_hub 遥测广播（2s 周期推 system_status）。

## 6. 启动时序

lifespan 内按 T+N 锚点推进，慢操作全部后置或异步化：

```
T+0s   数据库初始化（quick_check 自检，损坏则隔离重建）
T+3s   文件存储 / 进程内缓存
T+4s   嵌入模型预加载（bge-large-zh 常驻 CUDA 低位显存段）
T+5s   调度引擎启动（可选降级，失败不阻断）
T+5.5s 浏览器进程池后台预热（不阻塞就绪）
T+6s   WS 中枢 bind_loop + 遥测启动 + 向 draw/lora/agent 注入广播器
T+10s  就绪（launcher 侧轮询 GET /health 校验 data.db=="ok"，60s 超时）
```

关闭逆序：browser_pool.shutdown → ws_hub.stop_telemetry → scheduler.stop。

前端挂载细节：`_ApiAwareMount` 子类让静态路由对 `/api/v1`、`/health`、`/ws` 返回 Match.NONE（避免静态 catch-all 吞掉 API 路径）；优先 `frontend/dist`，缺省回落 `frontend/`；全部响应 `Cache-Control: no-cache`（本地迭代场景避免旧资源缓存）。

## 7. 前端结构

React 19.0 + TypeScript + Vite，`createHashRouter`（react-router-dom 7，COM-001：file:// 与静态托管均可离线可用）。九个一级路由：`/chat`、`/paint`、`/storyboard`、`/learning`、`/models`、`/style`、`/settings`、`/logs`（2026-08-21 新增）、`/help`，全部 lazy 懒加载。

```
frontend/src/
├── stores/       Zustand（useAppStore/useDialogStore/useMangaStore/… + manga/ 六切片）
├── services/     api.ts 统一封装 + 各域 Api 模块 + schema.ts(Zod 响应校验) + ws.ts
├── components/   dialog/ paint/ manga/ learning/ learn/ style/ model/ help/ layout/ common/
├── constants/    videoStatus.ts（状态文案单一真源，与后端枚举一致性测试守护）
├── three/        3D 导演台（Three.js）
└── styles/       sakura 主题加载链（CSS 分层）
```

漫剧状态按子域切片（TASK-P2-01）：`stores/manga/` 下 project / rows / asset / keyframe / voice / video 六个 slice + types（切片契约）+ helpers + videoPoller（轮询定时器单例，可见性降频），由 `stores/useMangaStore.ts` 组装为单一 store，22 个消费方零感知。

## 8. 数据流：漫剧主链路

以最重的业务链路为例，横向贯通全部五层：

```
用户在分镜表编辑行
  → rowsSlice.updateRow → PUT /api/v1/manga/storyboard/{pid}/rows/{rid}
  → storyboard.py 参数校验 → database.py（SQLite WAL, threading.local 连接）
AI 自动分镜
  → rowsSlice.autoSplit → POST .../auto-split
  → storyboard.py → dialog_engine（经 run_blocking 卸载线程）→ 追加分镜行
关键帧生成
  → keyframeSlice → POST /api/v1/manga/keyframe/generate
  → keyframe.py → paint_engine（1280×720 生成 + LANCZOS 2x → 2560×1440 出图铁律）
视频生成
  → videoSlice.generateVideo（先抢 video_gen 功能锁）→ POST .../video/generate
  → video.py 后台线程真实产出 → 前端 2s 轮询 status → done 后 GET result 下载
```

三条铁律贯穿：出图统一 2560×1440（1280×720 + LANCZOS 2x 上采样，SDXL 不直出大图）；进度只来自真实轮询，前端绝不伪造；降级必须带原因下发（degraded/degrade_reason 字段），前端如实展示。

## 9. 关键设计决策索引

代码内的审计编号体系是决策溯源线索，此处只列高频引用：

- ADR-01 统一 200 信封 ｜ ADR-03 API 前缀 /api/v1
- COM-001 Hash Router 离线可用 ｜ COM-009 Sakura 主题
- P1-06 run_blocking 唯一同步推理入口 ｜ P1-05 探测失败不回填假数据
- P2-01 manga.py 拆包 / store 切片 ｜ P0-05 非回环绑定硬拒绝
- 16GB 显存约束下的常驻/按需模型切换（force_unload 机制）与 LTX-2 int8 选型

完整决策记录见 `docs/audit-task-checklist.md` 与各源文件头部注释。
