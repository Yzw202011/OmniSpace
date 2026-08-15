# OmniSpace AI v2.3.1 后端零信任评估报告（Round 0）

- 评估日期：2026-08-07
- 评估对象：`e:\OmniSpace\backend\`（71 个 .py + config.yaml）、`launcher\launcher.py`、`tools\*.py`（4 个）、`tests\*.py`（2 个）、`backend\tests\*`、`requirements.txt`、`backend\requirements.txt`
- 对照文档（5 份，均位于 `e:\OmniSpace\`）：
  - 文档B：《OmniSpace AI v2.3.1中文版（修正版B）》（**权威文档，矛盾时以此为准**）
  - 文档E：《极致细粒度全量文档（修正版E）》（API 端点表 / 数据库 schema / ADR）
  - 文档A：《程序开发计划（修正版A）》
  - 文档C：《编程语言规范（修改版C）》
  - 文档D：《布局视觉与规范（修正版D）》（含 API 响应格式修正）

---

## 1. 评估范围与方法

**零信任原则**：假设所有代码、注释、配置均不可信，逐条与文档交叉验证；代码内"规格 §x"引用一律回查文档原文，不接受注释自述。

**方法**：
1. 全量枚举 backend 71 个 .py 文件 + config.yaml（约 1.5 万行），全部经过全库模式扫描（路由表、错误码、异常吞噬、SQL 拼接、subprocess/pickle/eval、硬编码密钥、threading、mock/占位/虚假关键词、TODO/FIXME 等 12 组模式）；
2. 关键文件精读（40+ 文件）：main.py、config.py/yaml、startup_check.py、全部 13 个 api/ 模块、error_handler/cors/rate_limit/feature_lock/logger 全部中间件、data/ 全部 7 个、engines/ 全部、scheduler 全部 6 个、model_manager 全部 7 个、inference 全部 5 个、lora/style/learning/browser/ws_hub 等 services；
3. launcher.py（666 行）、tools/ 4 个、tests/ 2 个、两份 requirements.txt 精读；
4. 五份文档逐节比对：文档B §4.1/§4.2/§4.3/§8/§9.1/§9.2、文档E §7.1 API 表、文档D 响应格式修正、文档C Python 铁律。

**前端交叉验证**：`frontend/src/services/api.ts:16` 确认前端实际使用 `http://127.0.0.1:5800/v1`，用于裁决前后端契约一致性。

---

## 2. 总体结论

**后端整体实现完成度约 75%**。核心推理/调度/学习链路为真实实现且工程质量较高（诚实降级标注是显著优点），但存在两项 P0 级契约/真实性违约、一批 v1.0 历史残留损坏文件、以及若干出货环境不可用的空壳功能。

| 模块 | 完成度估计 | 说明 |
|---|---|---|
| AI 对话 | ~85% | 真实 Qwen 推理 + SSE/WS 流式 + 会话/评分/收藏；mock 内存双写为残留 |
| AI 绘画 | ~80% | SDXL 真实管线（fp16/offload/VAE tiling）；ControlNet 预览为空壳 |
| 漫剧创作 | ~75% | 分镜/导演台/视频任务真实落库；视频 AI 模型未随包（降级管线顶替）；音色试听为占位音频 |
| 知识学习 | ~80% | 主题/会话/配额/Agent 循环/FTS5+RRF/知识图谱均真实；WS 进度端点缺失；§4.3 策略未全实现 |
| 模型管理 | ~85% | 注册/导入/SHA256/加载/卸载/互斥/预测均接线；XGBoost 缺位走马尔可夫回退（诚实标注） |
| 视频风格 | ~60% | 训练/推理管线真实（diffusers+peft QLoRA），但 LTX-2 基座未随包，出货环境全部端点诚实报错，实际不可用 |
| 调度器 | ~80% | 6 协同模式/滞回/热保护/分级采样真实；**协同调度历史学习引擎缺失**；阈值与文档B数值不一致 |
| 系统/硬件 | ~60% | 硬件采集真实；**/system/diagnose 为全量假数据**；项目导入/导出为空壳 |
| 内置浏览器 | ~85% | Playwright 专属线程架构 + 安全层（黑名单/禁表单/禁下载）+ 进程池预热 + 分级决策，均为真实实现 |

---

## 3. 问题清单（55 条）

严重度：P0=阻断（对外契约违约或安全误导）｜P1=严重（功能缺失/虚假实现/文档硬冲突）｜P2=一般（残留/偏差/风险）

| 编号 | 严重度 | 类别 | 文件:行号 | 问题描述 | 文档依据 | 修复建议 |
|---|---|---|---|---|---|---|
| BK-001 | P0 | API契约 | middleware/error_handler.py:159-181；data/models.py:110-113 | **统一响应格式未落实**：实现为 `{code:int, message, data, detail}`；权威格式为 `{success, data, error:{code,message,detail,suggestion}, meta:{request_id,timestamp,duration_ms}}`，且 error.code 应为语义化字符串 | 文档B §9.1.1（L1243/1250/1300-1343）；文档D L123-189 | 全局裁决：要么改造后端+前端到 {success,data,error,meta}（对齐文档），要么修订文档B/D 承认现行 {code,message,data} 为既定契约。二选一，禁止长期双轨 |
| BK-002 | P0 | 虚假实现 | api/system.py:39-67,108-123 | **/system/diagnose 返回 26 项硬编码假结果**：其中"数据库加密:pass"（实为明文 SQLite）、"激活状态:pass"、"机器指纹一致性:pass"、"断点续传扫描:pass" 对应的子系统根本不存在，对用户构成安全状态误导 | 文档B §8 系统诊断要求真实检测；config.yaml:42-43 自述明文 SQLite | 改为调用 startup_check.run_all_checks() 的真实 26 项结果；不存在的子系统项删除或如实返回"未实现" |
| BK-003 | P1 | API契约 | config.py:32；main.py:176；frontend/src/services/api.ts:16 | **API 前缀 /v1 vs 文档 /api/v1，端口 5800 vs 文档 8000，WS /ws vs ws://host/ws/{module}**。三份文档一致写 `/api/v1` + `localhost:8000`；代码与已定型前端同用 /v1+5800 | 文档B L713-723/1298-1299；文档E L1612-1624；文档B §9.1.3 | 裁决建议：前端已按 /v1+5800 定型，改代码牵动面大——建议修订文档B/E/D 统一为 /v1+5800，并在文档B §9.1.3 补记 WS 实际端点表（/ws、/v1/dialog/stream、/v1/hardware/realtime） |
| BK-004 | P2 | API契约 | main.py:245 | 健康检查在根路径 /health 而非 /api/v1/health；文档B TASK-SYS-002 仅要求"/health 返回 ok"，与统一前缀策略存在解释冲突 | 文档B L349 vs L1298-1299 | 随 BK-003 一并裁决；如保留根路径需在文档明确豁免 |
| BK-005 | P2 | API契约 | api/learning.py（无该端点） | 文档E §7.1.2 规定 `GET /learn/session/progress` 为 WS 端点，代码未实现；学习进度改经 /ws hub 广播 | 文档E §7.1.2（L1668） | 补 WS 端点或在文档E 标注"进度经 /ws hub module=learn 推送" |
| BK-006 | P2 | API契约 | middleware/error_handler.py:150,181 | 所有错误响应 HTTP 状态恒 200（含 429/403 以外的业务错误），RESTful 语义缺失 | 文档E API 规范（状态码语义） | 裁决：若保留 200+code 风格需写入文档；否则 ApiError.http_status 按码段映射 4xx/5xx |
| BK-007 | P2 | API契约 | middleware/cors.py:51-54；全库无 request_id 生成点 | meta.request_id/duration_ms 全链路缺失；CORS 暴露了 X-Request-ID 响应头但没有任何组件写入该头 | 文档B §9.1.1 meta 定义 | 增加请求上下文中间件：生成 request_id、计时 duration_ms、写入响应头与 meta |
| BK-008 | P2 | API契约 | api/draw.py:22,205 vs middleware/error_handler.py:84-88 | 错误码 50005（绘画模型未就绪）被使用但未登记进 ERROR_CODES 映射，前端查表将得到"未知错误" | 文档B §9.1.2 错误码需统一登记 | ERROR_CODES 补登 50005 |
| BK-009 | P2 | API契约 | api/dialog.py:224-816；api/manga.py:235-957；api/learning.py:498-573 | 双套路由别名膨胀契约面：/dialog/* 与 /chat/* 两套会话 API；/manga/* 与顶层 /storyboard|/director|/video 别名；/learn/settings 与 /learn/settings/get|update；/learn/session/logs 与 /log | 文档B/E 每域仅一套路由 | 收敛：保留文档规定的一套，其余标 deprecated 并给前端迁移期 |
| BK-010 | P1 | 功能缺失 | services/scheduler/decision.py 全文件 | **五大引擎之"协同调度历史学习"未实现**：决策引擎仅 30s 滞回，无历史调度效果记录/学习/策略优化；全库 grep 无任何历史学习实现 | 任务五大引擎清单；文档A 引擎要求 | 新增调度历史表 + 策略效果回归（可复用 predictor 的马尔可夫基础设施），或文档中明确降级为"滞回+阈值" |
| BK-011 | P1 | 功能缺失 | data/models.py:78-103 | **硬件等级自适应映射表未按 GPU 型号实现**：仅按可用显存阈值路由，无法区分"RTX 4070Ti 12GB→Qwen3-VL-4B/Wan2.1-1.3B"与"RTX 3060 12GB→Qwen3-VL-2B/CogVideoX-2B"；且现表 12GB→qwen3-vl-8b、12GB→wan2.1-14b-int4 与文档B §4.2 直接冲突 | 文档B §4.2（L317-324） | 引入 GPU 型号识别（pynvml name 匹配）+ 文档B §4.2 映射表；或修订文档为纯显存路由并校正冲突行 |
| BK-012 | P1 | 功能缺失 | api/draw.py:531 | ControlNet 预览为空壳：恒返回 50003"尚未启用" | 文档B §4.3 绘画能力（ControlNet） | 接入 ControlNet 模型管线，或在文档/前端标注为后续版本能力并下架端点 |
| BK-013 | P1 | 功能缺失 | services/inference/voice_engine.py:248-270 | 语音合成在出货环境为静音占位 WAV（CosyVoice/ChatTTS 未随包），漫剧配音功能实际不可用 | 文档B §4.4 音色/情感配音 | 模型随包或提供下载器；API 响应增加 degraded 标记 |
| BK-014 | P1 | 功能缺失 | services/inference/video_engine.py:194-247,396-397 | LTX-2/Wan2.1/CogVideoX 真实视频模型均未随包，视频生成实际走 Ken Burns 图片推拉+FFmpeg 降级管线（产出真实文件但非 AI 生成视频） | 文档B §4.4/§5.2 视频模型路由 | 模型下载器/随包；任务结果与 WS 事件须携带 degraded:true 告知前端 |
| BK-015 | P2 | 功能缺失 | services/style_lora_service.py:27-33（诚实标注） | 视频风格 LoRA 出货环境不可用（80010/80013 诚实报错）；管线真实但基座缺位 | 文档B §7.1.4 /style 模块 | 模型下载引导；保持诚实报错（做得对） |
| BK-016 | P2 | 功能缺失 | services/knowledge_service.py:51-53；services/lora_training_service.py | 文档B §4.3 学习策略调整未全实现：①知识库近 10 万条时去重阈值 0.85→0.8 动态调整（现为静态 0.9/0.7）；②LoRA 连续 3 次质量下降暂停自动训练（未见实现）；③连续 5 页无有效知识切换搜索策略（未验证到实现） | 文档B §4.3（L325-331） | 三项策略补齐或文档降级 |
| BK-017 | P2 | 功能缺失 | services/model_manager/predictor.py:6-15（诚实标注） | XGBoost 未装、无训练产物 data/ml/feature_model.json，ML 预加载预测恒走马尔可夫+时间回退 | 文档B §3.4 ML 预测 | 依赖+离线训练产物随包；当前诚实标注正确 |
| BK-018 | P2 | 功能缺失 | 全库无 activation/license 实现 | 激活/授权/机器指纹体系不存在，但诊断 API 报告其"pass"（见 BK-002）；tools/init_db.py 仍引用 activation 表 | 文档B L537（未激活状态灯）暗示存在激活态 | 裁决：v2.3.1 是否需要激活体系；不需要则清除全部相关假状态 |
| BK-019 | P2 | 功能缺失 | api/system.py:84-105 | /system/backup 仅导出设置 JSON，不含项目/知识库/数据库，非文档语义的"备份" | 文档B §4.7 系统 API | 实现 sqlite 备份 + data/ 打包（zip），或改名 settings_export |
| BK-020 | P2 | 功能缺失 | config.yaml:47-63（诚实标注）；backend/requirements.txt:12-13 | Redis/Celery 目标架构未部署，现为进程内缓存/PriorityQueue 模拟 | 文档E §7.1 架构（Redis+Celery） | 维持现状则修订文档E 架构图；或落地 Redis/Celery 部署方案 |
| BK-021 | P2 | 规范违背 | data/database.py 全文件；backend/requirements.txt:5 | 文档B §9.1.4 规定"Python↔SQLite：SQLAlchemy ORM，不写裸 SQL"；实现为 sqlite3 裸 SQL（手写 DDL/DML），sqlalchemy 依赖声明后从未 import | 文档B §9.1.4（L1380） | 裁决：接受裸 SQL（修订文档）或迁移 SQLAlchemy；至少删除未用依赖 |
| BK-022 | P2 | 功能缺失 | main.py:193-195 | OpenAPI 文档端点全部关闭（docs_url/redoc_url/openapi_url=None），与文档B §9.1.4"Pydantic 校验，自动 OpenAPI 文档"的契约消费方式不符 | 文档B §9.1.4（L1373） | 开发模式开启 /docs（仅 127.0.0.1），或文档注明出货关闭 |
| BK-023 | P1 | 调度机制 | config.yaml:17-33 vs 文档B §4.1 | 自适应阈值数值不一致：文档B "显存>90% 动作、GPU 利用率>95% 持续 10s 降质、CPU>90% 暂停学习"；实现为显存 70/85/95、利用率 80/90、CPU 70/85 | 文档B §4.1（L310-316） | 裁决阈值表：以实测安全值为原则更新文档B，或按文档改 config |
| BK-024 | P2 | 调度机制 | services/learning_scheduler.py:35-42 | 学习标签数配额按用户状态（空闲5/创作2/低内存1）而非文档B §4.2 的硬件等级（5090→5、4070Ti→3、3060→2、RX6600→1） | 文档B §4.2 | 配额矩阵增加硬件等级维度（与 BK-011 的 GPU 型号识别共用基础设施） |
| BK-025 | P2 | 调度机制 | middleware/feature_lock.py:26-31 vs services/model_manager/__init__.py MUTUAL_EXCLUSION_MATRIX | 互斥矩阵双份定义：feature_lock 4 功能；model_manager 7 类别（多 ltx2_training/browser_learning/behavior_learning）。单一事实源缺失，长期必漂移 | 文档B §6.1 互斥状态机（单一矩阵语义） | 收敛为一份定义（建议 model_manager 引用 feature_lock 的矩阵并扩展后台类） |
| BK-026 | P2 | 调度机制 | services/priority.py；services/scheduler/dispatcher.py | P0-P5 优先级枚举已定义且被 lora/style/learning 引用，但 scheduler/dispatcher 执行层无优先级概念，调度动作不分优先级 | 文档B §8.4.2（priority.py 注释引用） | dispatcher 动作携带 Priority；或文档注明 P0-P5 仅约束后台任务队列 |
| BK-027 | P2 | 调度机制 | services/scheduler/__init__.py:153 | get_state() 读取 `gpu.get("util", gpu.get("usage_percent", 0.0))`，但 monitor.get_gpu() 的键是 `util_percent`——**gpu_usage 恒为 0.0**，/hardware/synergy 聚合态失真 | —（实现内部 bug） | 改为 `gpu.get("util_percent", 0.0)` |
| BK-028 | P1 | 安全隐患 | api/system.py:152-166 | /system/project/import 接受任意 file_path 并读取内容——本机任意文件可读（仅 127.0.0.1 绑定缓解） | 文档C 安全铁律（输入校验/路径校验） | file_path 限制在 DATA_DIR/exports 白名单内，拒绝绝对路径与 `..` |
| BK-029 | P2 | 安全隐患 | middleware/rate_limit.py:138-143 | 限流客户端标识优先取 X-Forwarded-For 首跳，请求方可伪造该头无限换新桶绕过限流 | 文档C 安全铁律 | 本地直连场景直接使用 request.client.host，忽略 XFF（无反向代理） |
| BK-030 | P2 | 安全隐患 | data/cache.py:75,182；services/model_manager/cache.py:266 | pickle.loads 反序列化缓存数据；若缓存落盘文件被篡改可致任意代码执行 | 文档C 安全铁律（反序列化） | 改 JSON/msgpack；或 pickle 前加 HMAC 校验 |
| BK-031 | P2 | 安全隐患 | data/database.py:321-352 | 通用 insert/update/delete 以 f-string 拼接表名/列名/WHERE；当前调用方均为常量暂无注入点，但 API 形态本身是注入温床 | 文档C（参数化查询） | 表名/列名走标识符白名单校验；WHERE 一律参数化 |
| BK-032 | P2 | 安全隐患 | middleware/cors.py:23-25,88-95 | CORS 正则把 0.0.0.0 当作合法 Origin（语义错误，0.0.0.0 不是可访问源）；allow_credentials=True 对本地白名单可接受但无必要 | 文档B §14 约束2 | 移除 0.0.0.0；评估关闭 credentials |
| BK-033 | P2 | 安全隐患 | services/model_manager/importer.py:49-77 | /models/import 接受任意本地路径（功能所需），无确认交互/路径提示 | 文档C 输入校验 | 响应中回显 resolve 后绝对路径；前端加确认框 |
| BK-034 | P2 | 安全隐患 | main.py 全局 | 无任何 API/WS 鉴权：本机任意进程（含恶意软件）可调用全部端点（含文件读、模型卸载、训练触发） | 文档B §6.1 安全架构；§14 约束2 | launcher 启动时生成随机 localhost token，经环境变量注入前端 config.js，后端中间件校验 |
| BK-035 | P2 | 规范违背 | api/dialog.py:224-225 等 | dialog_send/chat_send 等端点用裸 `dict = Body(default_factory=dict)` 接收请求，绕过 Pydantic v2 模型校验 | 文档C：所有入口 Pydantic 校验 | 定义 DialogSendRequest 等请求模型替换裸 dict |
| BK-036 | P2 | 规范违背 | services/lora_training_service.py:510；inference/dialog_engine.py:558；api/draw.py:244；api/manga.py:857 | threading.Thread 承载训练/生成等 CPU+GPU 密集工作；文档C 禁止 threading 做 CPU 密集（训练数据预处理为 CPU 密集段） | 文档C Python 铁律 | 训练迁移至独立进程（multiprocessing）；或文档C 增补"GPU 推理工作线程豁免"条款并逐处标注 |
| BK-037 | P2 | 规范违背 | 全库 40+ 文件 docstring | 系统性版本标注漂移：大量模块自称"v2.1 规格 §x"，交付文档为 v2.3.1，"规格"出处不可追溯、无法回查 | 文档C（注释可追溯） | 全库替换为"文档B §x"可回查引用 |
| BK-038 | P2 | 规范违背 | requirements.txt:1 vs runtime/py310 | 根 requirements 标注 Python 3.12、文档B §9.2.1 亦写 Python 3.12；出货 runtime 为 Python 3.10.11 embed | 文档B §9.2.1 | 统一：升级 runtime 至 3.12 或文档改 3.10 |
| BK-039 | P2 | 规范违背 | backend/tests/（仅 1 个单测文件） | 测试覆盖严重不足：仅知识管线 8 个用例有自动化单测；文档定义的大量 TC-U/TC-S/TC-I 用例无对应自动化；tests/ 下两个脚本为手动集成测试 | 文档A/E 测试矩阵 | 按文档测试矩阵补 pytest 用例（至少 TC-S 安全组与调度组） |
| BK-040 | P2 | 规范违背 | api/hardware.py:71-170；startup_check.py:133-250；engines/gpu_backend.py:130-160；engines/vram_manager.py:56-211 等 80+ 处 | 大量 `except Exception: pass` 静默吞异常（部分无日志无注释），违反文档C 错误处理铁律；另有 ~30 处带注释的刻意降级（可接受） | 文档C 错误处理 | 静默处统一补 `log.debug`；无法避免处注明降级理由 |
| BK-041 | P1 | 虚假实现 | api/system.py:126-149 | /system/project/export 仅写 3 字段 JSON 占位文件冒充 .omnispace 归档，不含任何项目数据 | 文档B §4.7 项目导出 | 实现真实归档（项目+分镜+资产引用打包 zip），或端点下架 |
| BK-042 | P1 | 虚假实现 | api/system.py:152-173 | /system/project/import 不恢复任何数据；解析失败也返回 `imported: true` 成功 | 文档B §4.7 | 实现真实恢复流程；失败必须返回错误码 |
| BK-043 | P1 | 虚假实现 | api/hardware.py:95-145 | 硬件画像/遥测采集失败时返回 "Mock CPU/8核16线程" 等模拟数据，**响应中无 degraded 标记**，前端无法区分真实与模拟 | 文档B §4.6 硬件 API（真实性要求） | 模拟回退必须带 `degraded: true` 字段 |
| BK-044 | P2 | 虚假实现 | api/models.py:481 | /models/{id}/verify 在无本地文件时返回"确定性模拟指纹"冒充 SHA256 校验结果 | 文档B §5.4 SHA256 校验 | 无文件应返回 30001/校验跳过，不得伪造指纹 |
| BK-045 | P2 | 虚假实现 | api/manga.py:103-104,1093-1112 | /manga/voices/preview 返回空 WAV 头占位音频（docstring 已注明"模拟"，诚实但功能为空） | 文档B §4.4 试听 | 接入 voice_engine 真实合成；响应带 degraded 标记 |
| BK-046 | P1 | 死代码 | tools/init_db.py:5,19-30 | 引用已删除的 `backend.core.db`（v1.0 模块，backend/core 不存在），工具直接无法运行；docstring 仍宣传"SQLCipher 全库加密"（与现行明文 SQLite 矛盾）；CORE_TABLES 为 v1.0 表清单 | —（v1.0 残留） | 重写为现行 13 表 + knowledge_meta/kg_*/behavior_logs 等自建表校验，或删除 |
| BK-047 | P1 | 死代码 | tools/diagnostics.py:5,23 | 引用不存在的 `backend.routers.system`（现行为 backend.api.system，且无 build_diagnostics），工具无法运行 | —（v1.0 残留） | 改接 startup_check 或删除 |
| BK-048 | P1 | 死代码 | tools/benchmark.py:5,19 | 引用不存在的 `backend.core.gpu_manager`，工具无法运行 | —（v1.0 残留） | 改接 engines/scheduler monitor 或删除 |
| BK-049 | P2 | 死代码 | launcher/launcher.py:237-244 | blake3 完整性校验清单指向不存在文件：backend/core/security.py、frontend/src/app.jsx、frontend/src/api.js（前端为 TS）——校验恒报 missing，完整性功能实效 | — | 更新为现行关键文件清单（backend/main.py、frontend/dist/index.html 等） |
| BK-050 | P2 | 死代码 | api/dialog.py:52-53 及 20+ 引用点 | _mock_sessions/_mock_messages 内存存储与 SQLite 双写双读，数据源二义（同一会话可能一半在 DB 一半在内存） | — | 收敛单一数据源：DB 优先，内存仅在 DB 不可用时整体接管 |
| BK-051 | P2 | 死代码 | backend/requirements.txt:5 | sqlalchemy==2.0.36 声明但全库零 import（仅 logger.py 降噪字符串提及） | — | 删除依赖或迁移 ORM（随 BK-021 裁决） |
| BK-052 | P2 | 死代码 | middleware/error_handler.py:33-40 | legacy 错误码 10001/10002/10003/99999 保留；99999 不在文档任何码段 | 文档B §9.1.2 | 前端确认无引用后删除 |
| BK-053 | P1 | 文档矛盾 | middleware/error_handler.py:1-3 | docstring 声称"文档 §9.1 统一响应格式 {code, message, data}"——**文档B §9.1.1 原文为 {success,data,error,meta}，该引用系伪造文档依据**（零信任原则下最恶劣的一类问题：注释为错误实现背书） | 文档B §9.1.1 | 修正 docstring；随 BK-001 裁决统一 |
| BK-054 | P2 | 文档矛盾 | services/behavior_service.py:40；lora_training_service.py:6 | 文档自身冲突：文档B §4.3"行为数据<50 条不触发微调" vs §3.3/TASK-035"≥100 条才允许训练"；代码取 100 | 文档B §4.3 vs §3.3 | 文档B 内部裁决统一阈值 |
| BK-055 | P2 | 文档矛盾 | data/database.py:33-203 vs 文档E schema | 数据库 schema 不符：文档E 定义 users/assets/knowledge_entries/lora_trainings 等表；实现为 13 张自建表 + knowledge_meta/kg_entities/kg_edges/behavior_logs/learning_* 等运行时自建表，无 users/assets | 文档E 数据库 schema 章节 | 裁决以哪份 schema 为准；补迁移脚本或修订文档E |

---

## 4. 虚假实现 / 死代码专项清单

**虚假实现（返回假数据/假成功，按恶劣度排序）**：
1. **BK-002（P0）** `/system/diagnose`——26 项全假，含 4 项不存在子系统的假"pass"（数据库加密/激活/机器指纹/断点续传）
2. **BK-042（P1）** `/system/project/import`——不恢复数据且失败也报成功
3. **BK-041（P1）** `/system/project/export`——占位 JSON 冒充 .omnispace 归档
4. **BK-043（P1）** `/hardware/info|realtime`——Mock CPU 模拟数据无 degraded 标注
5. **BK-044（P2）** `/models/{id}/verify`——模拟指纹冒充 SHA256
6. **BK-045（P2）** `/manga/voices/preview`——空 WAV 占位（有诚实注释）
7. **BK-013（P1）** `/draw/controlnet/preview`——空壳端点（恒 50003）
8. **BK-014（P1）** 视频生成——降级管线产出真实文件但非 AI 视频（需 degraded 标记）
9. **BK-050（P2）** dialog mock 内存双写——半真半假数据源

**死代码 / 历史残留**：
1. **BK-046（P1）** tools/init_db.py → 引用已删除的 backend.core.db（无法运行）
2. **BK-047（P1）** tools/diagnostics.py → 引用不存在的 backend.routers.system（无法运行）
3. **BK-048（P1）** tools/benchmark.py → 引用不存在的 backend.core.gpu_manager（无法运行）
4. **BK-049（P2）** launcher 完整性校验清单指向 3 个不存在文件（功能实效）
5. **BK-051（P2）** sqlalchemy 未使用依赖
6. **BK-052（P2）** legacy 错误码 10001-10003/99999
7. engines/__init__.py 自述 cpu_scheduler/model_splitter 死代码已移除（正面案例：清理方向正确）
8. 全库 80+ 处 `except: pass`（BK-040，部分为刻意降级）

---

## 5. 已验证合规的部分（抽验通过）

- **调度器主链真实**：monitor（分级采样 2s/5s/10s TTL）→ analyzer（6 协同模式）→ decision（30s 滞回 + ALL_TENSE 紧急直通）→ dispatcher（preload/force_unload/compress 真实接线 ModelManager），且 tick 经 run_in_executor 避免阻塞事件循环
- **温度保护链完整**：thermal_guard 状态机（85°C 降频/90°C 暂停/5°C 回差/连续 3 次锁 80% 利用率）+ feature_lock 入口统一拦截 20004 + WS 广播
- **功能互斥**：dialog/paint/video_gen/training 四功能互斥 + 同功能可重入，浏览器/行为学习正确排除在重量级锁外
- **混合检索真实**：FTS5 trigram（2 字 CJK 走 LIKE 兜底）+ 向量双路召回 + RRF(k=60) 融合（injection_service）
- **LoRA 训练真实**：peft QLoRA 4bit + 版本管理（10 版/回滚/增量续训）+ TASK-053 六项加速（flash-attn 探测/paged_adamw_8bit/cosine 重启/自适应批量/DeepSpeed 可选）+ 显存三件套（del+gc.collect+empty_cache）到位
- **浏览器安全层完备**：登录墙/验证码识别即撤离、禁填表单（password 检测）、禁下载、域名黑名单、≥1s 节流、Cookie 隔离
- **知识管线真实**：过滤→分段→提取→质量评估→SimHash 去重（>0.9 skip / 0.7-0.9 merge）→生命周期四阶段
- **绘画引擎真实**：SDXL fp16 + cpu_offload + VAE slicing/tiling + 低显存 sequential 降级 + secrets 真随机种子
- **无硬编码密钥**；subprocess 全部 list 参数无 shell=True；上传校验（JSONL 限定、50MB 上限）；日志脱敏过滤器（api_key/password/token 等）
- **诚实降级文化**：style_lora/predictor/config.yaml 对缺位能力均如实标注（全库注释可信度的正面样本）

---

## 6. 重构阶段优先级建议

**第 0 批（契约裁决，先决策后动手）**：
1. BK-001/BK-053 响应格式与错误码体系裁决（文档B §9.1 vs 现行实现，牵动前后端全链路）
2. BK-003/BK-004 API 前缀/端口/WS 路径裁决（建议修文档保代码）
3. BK-055 数据库 schema 裁决；BK-054 文档内部阈值裁决

**第 1 批（P0/P1 真实性与安全）**：
4. BK-002 diagnose 接 startup_check 真实结果
5. BK-041/BK-042 项目导入导出真实化或下架
6. BK-028 任意文件读取收口；BK-034 localhost token 鉴权
7. BK-046/047/048/049 四个损坏工具与 launcher 校验清单修复
8. BK-043/BK-044/BK-014/BK-013 全量模拟回退加 degraded 标记

**第 2 批（P1 功能补齐）**：
9. BK-010 协同调度历史学习引擎；BK-011/BK-024 GPU 型号级硬件映射
10. BK-012 ControlNet；BK-013 语音模型随包/下载器
11. BK-027 调度状态 gpu_usage 键名 bug（一行修复，先做）

**第 3 批（P2 清淤）**：
12. BK-050 双数据源收敛；BK-009 路由别名收敛；BK-025 互斥矩阵单一化
13. BK-021/BK-051 ORM 裁决；BK-029/030/031/032 安全项；BK-035/036/037/038/039/040 规范项
