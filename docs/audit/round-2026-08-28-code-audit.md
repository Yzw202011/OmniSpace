# OmniSpace 全库代码逐行审计报告（2026-08-28）

> 审计方式：**逐行读取**。后端核心（api / services / data / middleware / engines / launcher）全部文件读到 EOF；
> 前端 35,467 行中逐行覆盖安全关键路径（约 6,000 行：App/router/api/ws/schema/errors/Task store/videoPoller/DialogPage/DirectorStage/构建配置），其余按引用方抽查。
> 发现分级：P0 = 显存/数据破坏级；P1 = 真实可触发的高危缺陷；P2 = 值得修但影响有限。
> 证据规则：每条发现含 文件:行号 + 原文引用。标注【已亲核】的条目由主审计者二次读取源码确认；其余为逐行审计代理报告（原文引用逐条可查）。
> 本报告不覆盖：根目录测试脚本（tests/ 46 文件，属活后端编排）、backend/vendor、frontend 其余 UI 组件、ComfyUI 便携版内部。

## 0. 覆盖率

| 区域 | 行数 | 覆盖 |
|---|---|---|
| backend（126 文件） | 60,114 | 逐行全覆盖 |
| launcher/launcher.py | 779 | 逐行全覆盖 |
| middleware + crypto（主审计者亲读） | ~1,500 | 逐行 |
| frontend/src | 35,467 | 安全关键路径逐行 ~6,000，其余抽查 |
| tests(root) | 12,560 | 未审（定位为活后端 E2E 脚本） |

## 1. 分模块功能审计表

| 模块 | 规模(行) | 功能状态 | 审计结论 |
|---|---|---|---|
| middleware（7 文件） | ~700 | ✅ 正常 | 设计良好：XFF 防伪、路径归一化限流、上传魔数闸门、CSWSH/TrustedHost 防护真实有效。P1：功能锁重入无计数 |
| data/crypto.py | 215 | ✅ 正常 | AES-256-GCM + DPAPI + 诚实回退；加解密读写路径全库对称。P2×2：加密失败静默明文、解密失败静默空串 |
| data 层其余（database/models/vector/fts/graph/cache/file_store） | ~2,800 | ✅ 正常 | 迁移幂等、FTS 词法防注入、路径防穿越双校验。P1×2：CRUD 标识符拼接无白名单、vram 记账同 key 覆盖双累加 |
| engines（gpu/vram/memory/vllm） | ~1,470 | ✅ 正常 | vLLM 子进程树管理/孤儿清扫/睡眠协商细致。P2×8（端口误报就绪、句柄泄漏、记账口径不含子进程等） |
| api/dialog.py | 1,649 | ✅ 功能完整 | SSE/被动补全/加密落库与实测一致；first_token 计时真实。P1×2：断连孤儿 producer 继续烧 GPU；start_flow 窗口锁泄漏 |
| api/draw.py | 1,317 | ✅ 功能完整 | 队列/取消/优先级状态机闭环、无锁序死锁。P1×2：inpaint 参数 500、预检失败产生僵尸 pending 任务 |
| api/style.py + style_lora_service | ~1,800 | 🟨 训练可用但闭环无效 | 上传/魔数/诚实门控（80010）达标。**P1×3：version 路径直拼可 rmtree(models/)；训练无显存闸门无 checkpoint；训练条件为哈希假嵌入——训练-预览条件分布不一致，训练闭环实际无效** |
| api/models.py + system.py + hardware/logs/voice/vision_tools/browser | ~4,400 | ✅ 正常 | 删除/导出闸门齐全、流式哈希、诚实降级。P1×3：vision_tools 事件循环内同步加载模型；system_backup 同步热备；switch 异常路径锁泄漏 |
| api/manga/storyboard+comic+common+comic_asset | ~5,500 | ✅ 功能完整 | 200 行上限四入口齐、A/B/C 协议确定性、AI 分镜三层降级。P1×1：资产写入路径 project_id/name 未校验可穿越；P2×21（CSV 公式注入、zip 条目注入、reorder 非事务等） |
| api/manga/keyframe+comic_gen+face_similarity+gen_router | ~4,200 | ✅ 一致性门禁真实 | DINOv2 硬门禁/阈值/重试/落库链完整 |
| api/manga/video.py + video_engine + h3 + encoder | ~5,500 | ✅ 实测出片 | 管线探测→四档显存装载→逐步回调→诚实降级，与 08-28 实测（wan22-ti2v-5b 83.4s）完全吻合。P1×1：重启后 video_tasks 永卡 generating |
| 知识学习域（knowledge/learn/learning/knowledge_service/injection/scheduler/browser_agent/pool/behavior） | ~5,400 | 🟨 功能可用但 3 处静默失效 | SimHash/RRF/质量评分真实计算；浏览器动作白名单/防撞墙完整。**P1×4：流量配额 SQL 少逗号永久失效；learning_sessions 永不落库；seed_urls 写后无人消费；网页内容无隔离进 prompt/训练集** |
| 推理引擎群（dialog_engine/paint_engine/backends/offload/accelerator/comfy/gguf_stream） | ~4,900 | ✅ 主链路可用 | 单文件级失败收敛/诚实门控优秀。**P0×1：img2img/inpaint 绕开推理锁并发互踩；P1×6：生命周期锁与推理锁零交集、streamer 不可中断、引擎间 ABBA 死锁环、GGUF hook 泄漏、直卸绕过盾与台账** |
| 调度/model_manager/switch/训练服务 | ~7,900 | ✅ 正常 | 决策落库/滞回/取消传播/资源守卫保护语义与实测日志全部吻合。P1×3：ensure_loaded 竞态、evaluate 用错验证集、style 训练无 OOM 防护 |
| 前端核心 | ~6,000/35,467 | ✅ 正常 | **XSS 面干净**（无 rehype-raw/innerHTML，react-markdown 默认 URL 白名单）；ws 退避/清理对称；DirectorStage dispose 闭环。P1×1：launcher 心跳无首启宽限可误杀慢启动后端；P2：9 类 WS 消息无前端订阅者 |
| launcher.py | 779 | ✅ 正常 | 白名单环境变量 spawn、三级端口处理、双实例退位、UTF-8 reconfigure 到位 |

## 2. 发现清单

### P0（1 条）

**P0-1【显存安全】img2img/inpaint 全程绕开推理锁** —— paint_engine.py:1209-1340（`generate` 在 `with self._infer_lock`（:1103）内采样，img2img/inpaint 不取锁）+ :902-918 `_get_img2img_pipe` 无锁 check-then-act。并发图生图会在共享组件上并发扩散（accelerate hook / scheduler.step 非线程安全），输出损坏 + 显存颠簸——与本类自述"引擎层串行化推理"(:337-339) 直接矛盾。修法：img2img/inpaint 整体纳入 `_infer_lock`，管线构建入锁。

### P1（27 条，按域分组）

**显存/锁体系（4）**
1. 【已亲核】功能锁重入无计数：middleware/feature_lock.py:96-114——同功能二次 acquire 成功后，先完成者 release 将锁清空，另一任务仍在跑，跨功能互斥失效（视频域与对话 SSE 均可触发）。
2. 生命周期锁与推理锁零交集：dialog_engine.py:1240-1242 / paint_engine.py:851——卸载不等待在途推理，显存实际未释放却上报 unloaded；paint 底座切换时新旧管线并发加载。
3. 引擎互为腾挪 ABBA 死锁环：dialog_engine.py:751-759 与 paint_engine.py:428-436 持自身 `_lock` 互卸对方。
4. transformers streamer 线程不可中断：backends/transformers_backend.py:332-353——中断后 `join(5s)` 超时即弃，后台线程把 max_new_tokens 跑完，持续占 GPU。

**破坏性/路径（3）**
5. 【已亲核】`DELETE /style/{version}` 无格式校验 + `shutil.rmtree(STYLE_LORA_DIR / version)`（style.py:366 → style_lora_service.py:1040-1050）：Windows 单段可含 `..`/反斜杠，`DELETE /style/..` 可删整个 `models/`。同类拼接遍布 rename/rollback/export/evaluate（:1014/:1025/:1123/:878）；learn.py:340 的 LoRA rollback 同病。
6. 【已亲核】资产写入路径穿越：comic_asset.py:1046 / common.py:1224 以未校验 `project_id`/`name`（models.py:690 仅限长度 100）直拼 `_COMIC_ASSET_DIR`，mkdir+write 可逃逸 DATA_DIR（multipart 属 CORS 简单请求，可被恶意网页跨站触发）。
7. 【已亲核】CRUD 标识符拼接无白名单：database.py:645/664/670 `f"INSERT INTO {table} ({col_str})...WHERE {where}"`——当前调用点全部安全（已全库核验），但一处疏忽即注入。

**静默失效（4）**
8. 【已亲核】每日学习流量配额永久失效：browser_agent_service.py:545 `(key)` 少逗号——str 被当 17 个绑定参数 → ProgrammingError 被 :557 吞掉，50MB 配额永不触发（:569 同函数正确写法对照）。
9. 【已亲核】learning_sessions 永不落库：learning.py:435 `session.budget.to_dict()`（dict）直接当 SQL 参数 → InterfaceError 被 :439 吞掉 → 效率分析恒 0、会话记录重启即失。
10. seed_urls 写后无人消费：learning.py:505 注入 budget，`LearningBudget.from_dict`（browser_agent_service.py:300-304）不解析——"会话起始页面"功能整体未实现但 API 返回成功。
11. 重启后 video_tasks 永卡 generating：main.py:166 仅恢复 flow_trace；video.py:722-744 status 直读 DB 行——崩溃重启后任务无限转圈。【已亲核 main.py 零 video_tasks 引用】

**训练闭环（3）**
12. 风格训练条件为假嵌入：style_lora_service.py:855-868 `encoder_hidden_states` 来自 prompt 哈希播种的 `torch.randn`，预览走真实 T5——训练目标与推理条件分布不一致，adapter 训练闭环无效（docstring 自认"确定性哈希嵌入"，模块头注却称"真实实现"）。
13. style 训练无 OOM 防护无 checkpoint：style_lora_service.py:729-836 无空闲显存门槛（对照 lora_training_service.py:890-897 的 10GB 闸门），仅训练完一次性保存；STYLE-018"断点续训"实为重跑。
14. 训练质量门用错验证集：lora_training_service.py:1317 `evaluate()` 优先读旧 `self._dataset`，实际训练集在 `_last_train_data`（:867）——外部 JSONL 训练时质量结论失真。

**事件循环阻塞（2）**
15. vision_tools.py:67-75 四个 async 端点在事件循环内同步 `engine.load_model()`（SAM/MiDaS/YOLO/TripoSR 秒级 IO），与自身 docstring"推理经 run_blocking"矛盾（推理走了，加载没走）。
16. system.py:179-187 `async def system_backup` 内同步 SQLite 热备（同文件 project/export 均已 run_blocking，唯独 backup 漏）。

**锁泄漏/竞态（4）**
17. models.py:1154-1166 `models_switch`：`_acquire_switch_lock` 后 `engine.submit` 只捕 SwitchBusyError/ValueError，其他异常直接上抛 → 功能锁以 `switch:{model_id}` 永久持有。models_load:1007-1021 同病。
18. dialog SSE 断连孤儿 producer：dialog.py:825-848——客户端断开只置停止旗标+放锁，线程池 `_produce` 把 2048 tokens 跑完，与新请求并发争用 GPU；`queue.get()`(:800) 无超时。
19. model_manager ensure_loaded 竞态：model_manager/__init__.py:749-751 已加载短路 check-then-act 不原子——并发同模型加载双份预留+双次加载（与 P1-4 vram 双累加互为因果）。
20. 【已亲核】vram 记账同 key 覆盖双累加：vram_manager.py:82-86 track_alloc 覆盖旧记录但 `_total_allocated_mb` 只加不减，差额永久虚高（触发源即 P1-19 的竞态）。

**注入链（1）**
21. 网页内容无隔离进入 prompt/对话/训练集：browser_agent_service.py:834-836/1085-1107 页面文本直拼决策与理解 prompt、决策 URL 直接导航；injection_service.py:169-177 网页知识直入对话 prompt——恶意页面可操纵 Agent 与长期污染 LoRA 训练集。配套：黑名单可被 userinfo/punycode/尾点绕过（browser_service.py:258/270，P2-1）、无内网 SSRF 防护（P2-2）。

**launcher（1）**
22. 心跳无首启宽限：launcher.py:444-447/515-518——uvicorn lifespan（bge 1.3GB、vLLM 冷启动 ~157s）期间 `/health` 不可达，心跳 15s 即判崩溃 kill 存活进程，陷入重启循环直至 crash_permanent。

**数据一致性（2）**
23. 引擎直卸不同步台账：dialog_engine.py:751-759/paint_engine.py:428-436 直卸不回调 `mgr.unload_model` → ensure_loaded 短路返回 True 与真实 unloaded 不一致（dialog_engine.py:1058-1059）。
24. style 版本分配竞态：style_lora_service.py:1256-1265 扫描-分配 `v{max+1}` 无锁，worker 保存与 API merge 并发共用同一版本目录互相覆盖。

### P2（137 条，按域分组摘要）

- **视频链路（12）**：project_id 文生 3D 写路径穿越（director.py:343）；KB 网段文件名含用户 row_id（video.py:363）；audio_path 无来源校验可读任意文件/UNC 外带（encoder_service.py:432）；screenshot_4in1/text 无长度上限；KB 取消信号两级吞掉后仍翻 done；H3 取消未实现 POST /interrupt；_run_ffmpeg 无行流时超时失效可致功能锁永久泄漏；video_generate except 引用未定义 flow；真实管线产物目录与白名单不一致；导出全败时 PNG 兜底冒充 mp4；`model_used` 前缀口径不一。
- **数据/资源层（22）**：execute_in_transaction 只捕 Exception；database.py 头注"Database 层自动加解密"不实（实际在服务层）；版本高于支持时静默降级内存致"数据消失"观感；ChromaDB 首错永久降级且集合"消失"；bge/哈希向量可混集；内存向量库无界；cleanup_temp 死代码；file_store 白名单只告警不拦截；Redis 运行期死亡 stats 仍报 redis + flushdb 全库清；memory_manager access() 对压缩块返回原始 lz4 字节 + zstd 静默按 lz4；vram get_usage 不含子进程/WDDM、degraded_probes 永不清；manager 单例无锁双检；vLLM 端口被占可谎报就绪、日志句柄泄漏、start_async 忽略所请模型、孤儿清扫可能误杀第二实例、n_ctx 8192 与预算口径、PAINT_ROUTING_TABLE klein-4b 死条目；fts add 非原子+8000 字截断；graph 内存键无分隔符碰撞。
- **知识学习域（16）**：黑名单 userinfo/punycode 绕过；浏览器 SSRF 无私网防护；docx/pdf 解压炸弹；解析失败降级乱码入库；learning PUT 数值转换 500；会话启动/等待队列 check-then-act；四存储无跨库事务；三类日志/checkpoint 无界；behavior 内存回退无界；stats 每次全量拉 1 万条解密×2；关键词路 score 硬编码阈值；SimHash 只比对最近 5000 条；网络检测同步阻塞 6s；CSV 导出公式注入；GET /learn/quota 调用带副作用 evaluate()。
- **models/system 域（16）**：models_import 任意绝对路径登记；export 可打包项目根；models_health 恒 healthy:true；verify 只算哈希不比对；_write_full_export 临时目录泄漏；restart execv 失败熔死重启；logs 全量读内存；diagnose 内联 26 探测；disk 端点裸 import psutil；备份线程 check-then-act；voice 落盘/读盘同步 IO；vision_tools 参数转换 500；browser 截图缓存无锁；hardware monitor 懒初始化无锁；ZIP 成员整读无上限。
- **dialog/draw 域（14）**：base64 解码无上限；空 body POST 可被跨站清空全部对话（CSRF，建议 Content-Type 校验或自定义头）；style preview image_path 任意读（潜伏）；任务 LRU 可淘汰 pending/running；first_token_ms 被被动补全重推理覆盖；调度层 ApiError 归一为 50001 失真；pending 队列无上限+done 任务常驻 b64 数百 MB；会话搜索 N+1 解密；async 端点内 PIL 解码/加解密；style 上传 200MB 全量入内存+失败不清理；WS 路径缺 _stop_flags.discard；LIKE 通配未转义；cancel 响应 status 为目标态；dialog 锁获取与 try 之间有裸窗口。
- **推理引擎（19）**：backend.load False 路径缺 empty_cache；last_output_tokens 实为片段数；热切换失败无回滚丢当前模型；knowledge_text 无预算截断；模型发现同名碰撞+status 全盘扫描；gguf n_gpu_layers 文档失实/unload 不持锁/坏 chunk 静默；paint qwen-image 非 GGUF 回退无物理硬闸；GGUF 档位无质量排序；img2img SDXL 无分桶；`or` 默认吞合法 0 值；Real-ESRGAN 恒失败路径；ensure_loaded 驱逐面过宽（vision 类别误伤）；comfy _log_fp 句柄泄漏/空目录不清理/HTTPError 裸抛/unload 无锁；accelerator FA/SDPA 口径不实；VL 回退重试前不清理显存；translate_batch 丢 system_prompt。
- **漫剧域（21）**：CSV 公式注入；auto-split 剧本无定界符；资产导出 zip 条目可注入；批量生成参数异常整批 500；_reconcile_shot_lines 潜在 IndexError；时间码无位数上限（日期生成假镜头行）；reorder 非事务；绑定/采纳/commit 读-改-写竞态；storyboard_save 信任客户端 id 绕过枚举校验；情绪回退标签超出预置集；"21世纪1020年代"错别字进提示词；"50行上限"文案过期；导入/分镜落库非事务；DSL errors="ignore" 吞 GBK；image_task 全量内存分页+meta.history 无界；_remove_background 纯 Python 逐像素；PDF/占位图中文渲染损坏（Helvetica 无 CJK）；ER 文档缺 scope 列；导出包视频路径口径；网格标记 1×1/2×1 可过；图片替换无解码校验。
- **调度/训练服务（17）**：model_manager 卸载回调不传 model_id、paint 直载不入台账；allocate_memory 锁内执行 gc/empty_cache；_get_engine 无锁；驱逐不考虑功能锁；switch cancel TOCTOU 可杀新加载的 vLLM；训练锁降级直捅私有字段+release 不等 result；ModelCache 为死组件（无 put 调用方）；usage JSONL 声明 30 天保留实为永不清理；dataset_*.jsonl 无界；版本分配竞态（升入 P1-24）；暂停期 0.5s 刷库；ws_hub status 类型误译 system 事件+9 类型无发射方；遥测串行 send 无超时；thermal util_cap 无消费方却日志称"已锁定"；quality_governor docstring 漂移；dataset_path 白名单拒绝后静默回退自动构建；杂项 5 条。
- **前端/launcher（14）**：重启熔断为 30s 滚动窗口语义（循环崩溃永不熔断）；`'omnispace' in cmdline` 误杀面；blake3 校验清单指向不存在文件（死代码）；环境变量白名单缺 COMSPEC/PATHEXT；Windows stop 无优雅退出；input() EOFError；videoSlice 取消竞态可把任务翻回 generating；videoPoller 重复注册覆盖旧句柄；abortStream 不释放连接池条目；WS 9 类消息前端无订阅者（告警 UI 永不显示）；isBenignError 与 api.ts abort 码不匹配；ws 离线队列满静默丢弃；launcher/vite 双默认端口错位；noopener/WebGL 上下文小项。

### 本人亲核清单
feature_lock 重入（P1-1）、style 删除穿越（P1-5）、资产 name 校验缺失（P1-6）、database.py CRUD 拼接（P1-7）、流量配额 `(key)`（P1-8）、learning_sessions dict 参数（P1-9）、video_tasks 无启动恢复（P1-11）、vram track_alloc 双累加（P1-20）、crypto/middleware 全部 7 文件、main.py 无 video_tasks 恢复引用。

## 3. 修复优先级建议（Top 10）

1. **P0-1** img2img/inpaint 纳入 `_infer_lock`（~5 行，收益最大）。
2. **P1-5** style/learn 所有 version/dataset 入口加 `re.fullmatch(r"v\d+")` + rmtree 前 `is_relative_to` 校验。
3. **P1-8/P1-9** 两个"少符号"静默失效：`(key,)` 与 `json.dumps(budget)`——各一行，配单测。
4. **P1-1** feature_lock 改计数式重入或 release 校验 task_id。
5. **P1-11** lifespan 启动时 `UPDATE video_tasks SET status='error' WHERE status='generating'`。
6. **P1-2+P1-19+P1-20** 统一生命周期/推理锁方案 + ensure_loaded per-key 占位 + track_alloc 先减后加（同一套修改）。
7. **P1-6/P1-7** project_id/name 白名单 + database CRUD 标识符断言。
8. **P1-15/P1-16** vision_tools `_ensure_loaded` 与 system_backup 改 run_blocking。
9. **P1-12/P1-13** style 训练：text_encoder 齐备才允许训练 + 显存闸门 + epoch 级 checkpoint。
10. **P1-22** launcher 心跳首启宽限。

## 4. 总体评价

工程完整度和"诚实降级"纪律显著高于平均水平：错误信封、degraded 标注、魔数上传闸门、路径删除白名单、显存腾挪契约、实测可复现的文档校准都是真实存在的。系统性风险集中在**跨切面锁语义**（生命周期锁 vs 推理锁 vs 功能锁三套互不感知）、**静默 except 吞掉的两个功能性 bug**、以及**训练闭环的诚实性**（style LoRA 条件假嵌入）。P0/P1 均给出可定位的行号与修法，Top 10 中 6 条为 ≤10 行改动。
