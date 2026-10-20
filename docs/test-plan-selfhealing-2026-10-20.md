# 自愈机制全景重建计划 + 后端/链路监控严酷测试计划 v1（2026-10-20）

> 用户令（2026-10-20）：项目在迭代、功能在加，自愈机制未实时跟上——要求按整个项目**重建完整自愈机制计划**，并制定**极其详细、严格、复杂**的后端+链路监控测试计划（步骤/指标/稳定性/漏洞/功能跑通），200 条以上。
> 本文档 = ①自愈机制全景地图（代码真身盘点）②迭代漂移缺口清单 ③补缺方案（四段制，待拍板）④298 条测试用例全集 ⑤KPI 指标基线 ⑥熔断线。
> 执行进度账见 §九；证据落 `docs/测试记录/自愈-2026-10-20-RN/`。

---

## 〇、结论摘要（大白话）

- 家底比想象厚：盘点出 **11 层 90+ 项自愈机制**，全部找到了代码真身（文件:行号），没有一项是文档吹牛。
- 但**迭代快过自愈更新**的嫌疑缺口抓到 **12 个**（§二），最实的三个：前端断线补发队列满 50 条**静默丢弃无告警**；舱壁健康喂点依赖统一错误信封——**新加的端点不走信封就对舱壁失明**；9 套队列里只有视频队列接了孤儿回收，**其余队列的"重启后变孤儿"覆盖待核**。
- 测试计划 **298 条**（A~Z 十四组），R0 协议只读批已随本计划交付时执行完毕（8 项实测全绿），R1 单测闸批同日执行。
- 本计划不改任何代码；补缺方案全部待拍板。

---

## 一、被测对象地图：11 层自愈体系（代码真身盘点，2026-10-20 实测行号）

### L0 启动链自愈
| 机制 | 真身 | 关键参数 |
|---|---|---|
| 启动页端口递进 | launcher/boot.py:97,744-753 | SPLASH_PORT_RANGE=(5850,5869)，禁 SO_REUSEADDR |
| 后端端口三级递进 | launcher/launcher.py:177-216 | 一级杀残留→二级扫 (5800,5835)→三级 RuntimeError |
| 幂等接管（先探测再 spawn） | boot.py:903-919 | _probe_existing：psutil LISTEN + HTTP /health |
| **启动页死锁根修（死则接管）** | boot.py:715-761 | 孤儿启动页+后端不可达→本实例接管拉后端（10-20 拍板 A） |
| 副本指纹闸 | boot.py:923-1013 | copy_id/build_id/md5 前 8 位；OMNISPACE_ALLOW_MULTI 豁免 |
| 看门狗心跳+三振 | launcher.py:770-799 | 5s 间隔，连续 3 次不健康→crash；startup_grace 180s |
| 补位重启上限 | launcher.py:927-998 | max_restart_attempts=5；稳定窗 600s 计数不清零 |
| 接管实例失联补位 | boot.py:1208-1240 | attached 失联 2 次→转自有 spawn，wait_until_ready(60s) |
| 僵尸循环防线 | launcher.py:941-947 | 子进程死但端口健康→退位退出 |
| 崩溃取证 | launcher.py:51-53,861-891 | crash_forensics/crash-*.json+WER+NTSTATUS 人话 |
| 孤儿引擎清理 | launcher.py:893-925 | 只杀父进程已死的 comfyui/vllm/llama-server |
| py312 守卫 | boot.py:1470-1519 | 非 py312 转投重拉，REEXEC_GUARD 防循环 |
| /api/quit 正规退出链 | boot.py:398-400,1395-1441 | 摘 .heartbeat+代写 backend_stopped 终笔 |
| 页面守卫 60s 倒计时 | src/services/page_guard.py:15,155,209-229 | quit 失败永久停用自身，不裸杀 |
| 升级中断自愈（断电） | boot.py:1250-1316 | phase∈{backing_up,applying,verifying_start}→还原备份 |
| boot.log 轮转 | boot.py:59-76 | 10MB→.1 保留一代 |

### L1 进程存活与 RAM 三件套
| 机制 | 真身 | 关键参数 |
|---|---|---|
| 崩溃心跳 | src/services/heartbeat.py:37-41 | logs/.heartbeat 30s；残留+pid 死→crash_detected |
| 心跳历史轮转 | heartbeat.py | 512KB/1000 行 |
| ①启动前 RAM 提交预检 | launcher.py:331-364 | ullAvailPageFile<8GB 警告不阻断 |
| ②vLLM 装载 RAM 闸 | vllm_service.py:609-640 | 权重+3GB 余量，30 次×2s 等 60s 诚实拒绝 |
| ③运行期 RAM/Commit 守卫 | resource_guard.py:43-120 | SOFT 80%/HARD 85%/CRITICAL 90%/COMMIT_HARD 90% |
| 冷却防振荡 | resource_guard.py | 60/120/30s 三档 |
| WER 取证 | launcher.py:832-859 | RADAR_PRE_LEAK_64 提交耗尽判死 |
| 冻结（stalled）检测 | flow_trace.py:77,510-521 | STALL_THRESHOLD_S=120 无心跳→stalled |

### L2 引擎自愈
| 机制 | 真身 | 关键参数 |
|---|---|---|
| vLLM sleep/wake 热切换 | vllm_service.py:99-101,338-456 | sleep_for_paint settle 90s / wake 120s；sleep_mode 默认 false |
| Windows fallback 停进程让渡 | vllm_service.py:376-415 | /sleep 404→停子进程置 stopped_for_paint→后台重启 |
| 假 ready 防线 | vllm_service.py:259-269; dialog_engine.py:1240-1256 | 收养态以服务态为准；失联→降级 unloaded 重载 |
| 孤儿收养/清扫 | vllm_service.py:1145-1223 | /health+/v1/models 双验一致才收养 |
| 装不下降级链 | dialog_engine.py:72-100,664 | qwen35-9b→qwen3-vl-4b→8b-awq→deepseek-14b；degraded_from |
| 显存/RAM 准入闸 | vllm_service.py:459-509 | 60s 复测，ADMISSION_FACTOR=0.98 |
| stop 取消协议 | vllm_service.py:1051-1075 | _cancel_requested 旗标，启动循环每 2s 自查 |
| ComfyUI Job Object | comfy_proc.py:173-234 | KILL_ON_JOB_CLOSE 父死子随 |
| ComfyUI 空闲自关 | comfy_proc.py:62-64,561-601 | 300s 巡检 15s；/queue 事实判据+锁豁免+异常续命 |
| ComfyUI 孤儿清扫 | comfy_proc.py:321-369 | spawn 前收账 |
| PuLID/EVA 缓存自愈 | comfy_proc.py:81-158 | sha256 校验+备份回灌 |
| mark_busy 护闸 | comfy_proc.py:307-319; comfy_paint_engine.py:1239/1247; h3_face_refine.py:178/260 | 计数式冻结空闲计时 |
| FaceRefine 挂死根修 | h3_face_refine.py:65-66,171-327 | 超时 1500s；丢任务 3 次×10s；重启指纹；自愈重试 1 次；任何失败返回原片零阻断 |
| 超时止损/取消 | comfy_paint_engine.py:1568-1648 | _interrupt_best_effort 5s |
| 对话云端解绑兜底 | dialog_engine.py:1257-1270 | remote 身份不匹配→卸载降本地 |
| 功能锁延迟启动 | vllm_service.py:600-609 | paint/video_gen/training 持锁→start 延后 |

### L3 显存/资源守卫
| 机制 | 真身 | 关键参数 |
|---|---|---|
| 阈值单源 | src/config.yaml:53-78 | 80/90/95%；温度 85/90；util 85%·5s/95%·15s/4s 滞回 |
| 滞回决策 | scheduler/decision.py:50-222 | hysteresis 30s；ALL_TENSE 直通（int4+只留 dialog） |
| 两级空闲回收 | config.yaml:38-41 | shallow 60s / deep 300s / dialog 900s |
| 资源守卫每秒 check | resource_guard.py:288-340 | 驱逐保护活动功能+常驻 dialog+台账外兜底 |
| 阈值常量搬家单源 | src/services/vram_policy.py:70-133 | 22/15.5/3/4GB；0.80/0.87/0.92；TRAINING_MIN_FREE_GB=10 |
| 功能互斥锁 | middleware/feature_lock.py:34-292 | 四功能按资源域互斥；计数式；sleep_guard |
| 忙碌登记簿 | inference/gpu_budget.py:437-504 | 云任务上榜，防空闲误卸（假空闲防线） |
| 让渡协调器 | gpu_budget.py:507-651 | sleep_for_generation/wake_now/schedule_wake_if_idle；只认最新 source |
| release_for_module | api/models.py:1299-1335 | 模块切换 3s 预算 |

### L4 队列自愈
| 机制 | 真身 | 关键参数 |
|---|---|---|
| 通用队列核（worker 永不退出） | task_queue.py:37-57,238-296 | _worker_loop 异常兜底；本地单 worker+云端并发池 |
| 视频孤儿回收 | video_queue.py:126-128,376-408 | pending/generating 超 120s 不在活集→error「后端重启导致任务中断」 |
| 假终态兜底 | video_queue.py:241-248 | runner 逃逸→兜底写 cancelled/error |
| 锁释放等待 | video_queue.py:332-345; image_queue.py:307-318 | 等待 10s→失败升 error |
| kind 路由/镜间接力 | video_queue.py:141-149 | 队首 h3_chain→keep_loaded |
| 图像准入钩子链 | image_queue.py:71-88 | acquire/thermal/vllm_sleep/unload/budget |
| 热暂停兼判视频锁 | image_queue.py:275-296 | _thermal_paused 兼判 _lock_held（RAM 92% 对策） |
| 增量让位 | image_queue.py:236-262 | _yield_paint_deficit：/free+卸进程内对话模型 |

### L5 数据自愈
| 机制 | 真身 | 关键参数 |
|---|---|---|
| 备份 GCM 加密 | api/system.py:276-345 | 设置 .json.enc + DB backup API .db.enc；密钥不可用明文回退不假加密 |
| 恢复链 | api/system.py:350-374,763-824 | 文件名白名单/体积≥4096/.enc 先解密暂存/restore-pending/cancel |
| pre_restore 防呆件 | main.py:185-239 | 恢复前当前库→backups/omnispace_pre_restore_<ts>.db 同款加密（BUG-10 根修） |
| 升级验签 | upgrade_service.py:26-218 | Ed25519+兼容区间+verify_extracted 复核 |
| 升级状态机/回滚 | updater/updater.py:308-392 | phases 全序；.upgrading 原子名；5890 state 伺服 |
| DB WAL+迁移组 | data/database.py:462-660 | user_version；库>码拒绝启动；_migrate_columns 幂等 |
| DB 降级容错 | database.py:860-866 | get_db_safe()→SYSTEM_DB_DEGRADED 内存模式 |
| 存储清理中心 | api/system.py:381-470,514-531 | backups_keep 3 份+_safe_clear_dir 穿越守卫 |

### L6 前端自愈
| 机制 | 真身 | 关键参数 |
|---|---|---|
| WS 指数退避重连 | services/ws.ts:54-57,171-186 | 1s 起×2，封顶 30s，无限重试 |
| 断线补发队列 | ws.ts:131-138,193-200 | MAX_PENDING=50，onopen 按序补发 |
| 非法帧跳过 | ws.ts:140-155 | Zod safeParse 丢弃不断链 |
| 遥测 HTTP 兜底 | useHardwareStore.ts:155-172 | wsStatus!=open 时 5s 拉取 |
| lanToken 重连重拼 | ws.ts:119-124 | ?token= 重读 |
| 对话补发轮询 | useDialogStore.ts:649-681,1011-1046 | 3s×120 次=6min；占位文案 |
| 绘画自动续跑 | InspectorPanel.tsx:240-268 | 错误正则→warmup paint→waitPaintReady 150s→runOnce(false) 仅一次 |
| 三态常驻条 | DialogWarmupBar.tsx:39-203 | 2s 轮询；渐近进度 90% 封顶不谎报 |
| 幽灵任务收敛 | useTaskStore.ts:137-248 | 15min 无进度强制 done+60s 巡检；上限 200 |
| 视频轮询恢复 | videoSlice.ts:298-357; videoPoller.ts:26-37 | 前台 2s/隐藏 10s+tickOnVisible |
| 对话断流 8s 宽限 | useDialogStore.ts:771-806 | 超时终止+abort 释放连接 |
| 根级 ErrorBoundary | App.tsx:129-174,635-639 | 全路由 |
| chunk 失败自愈刷新 | router.tsx:63; RouteErrorScreen.tsx:17-41 | 会话首次自动 reload 防循环 |
| 全局异常上报 | main.tsx:40-63 | onerror/unhandledrejection→/logs/frontend-event，10s 节流 |
| 安全模式 | App.tsx:363-378 | ?safe=1 短路全部自动预热（负对照） |
| 自愈失败横幅 | RecoveryGuideCard.tsx:26-97 | WS self_heal failed→四步指南+一键重试 |

### L7 监控观测
| 机制 | 真身 | 关键参数 |
|---|---|---|
| 事件账 | services/event_log.py:52-149 | logs/events 按日 jsonl；30 天保留 |
| 流程聚合/统计 | event_log.py:211-395 | query_flows 启发式聚合+7 天 stats |
| flow 节点埋点 | services/flow_trace.py:75-77,350-460 | 独立库 flow_trace.db；node.progress 心跳；stalled 120s |
| flow 崩溃恢复 | flow_trace.py:702-749 | recover_orphans：running→orphan，节点→skipped |
| 诊断包 | api/logs.py:324-426 | zip=events+失败流程+raw+hardware |
| 错误聚合 | api/logs.py:245 | /logs/errors/summary |
| 就绪总检 | api/models.py:771-781; model_manager/readiness.py:84-145 | manifest 逐条对账 |
| WS 广播 | ws_hub.py:204 | broadcast 线程安全 |

### L8 体检修复 + 统一自愈链 + 人话化
| 机制 | 真身 | 关键参数 |
|---|---|---|
| L1 运行时体检 8 项 | services/health_check.py:274-283 | vram/ram/disk/orphan/manifest/queues/logs/license |
| 修复白名单 3 动作 | health_check.py:286-368 | clean_logs/rebuild_dirs/kill_orphans；白名单外 SYSTEM_PARAM_INVALID |
| 孤儿判定守卫 | health_check.py:41-88 | _ORPHAN_SIGNS+父链深度>8 判有主；绝不碰 python 进程 |
| 环境级 26 项自检 | startup_check.py:548-575 | POST /system/diagnose |
| 舱壁健康账本 | services/module_health.py:22-108 | 四模块；3 连败→degraded；成功清零；degraded 自动触发 self_heal |
| 统一自愈链 | services/self_heal.py:23-173 | 冷却 90s/上限 2 次/验证 150s poll 3s；只护 paint/dialog；video/training 诚实转指南 |
| 人话化 171 码 | middleware/error_handler.py:47-247,348-533 | 171 语义码+171 条默认出路；裸报错灭绝 |
| 错误→舱壁联动 | error_handler.py:628-632 | record_failure(sem) |

### L9 安全防线（自愈视角=防误伤防线）
| 机制 | 真身 |
|---|---|
| 单实例命名互斥体 | src/single_instance.py:26-64（fail-open） |
| 激活门禁双闸 | 前端 LicenseGate.tsx:28-175 + 启动页 #actPage + 后端 license_gate_middleware |
| 回环绑定闸门/TrustedHost/限流 300req/min | main.py 中间件栈 |
| 上传闸门 PE/ELF 黑名单 | api 上传链 |
| SSRF 护栏 | h3_face_refine.py（回环字面量+urlsplit 白名单） |

### L10 升级/发行自愈
| 机制 | 真身 |
|---|---|
| 断电自愈（boot 最早钩子） | boot.py:1250-1316 + updater/restore.py |
| torch 契约守卫 | launcher.py:251-299（失配阻断，ALLOW_TORCH_DRIFT 豁免） |
| 模型挂接自愈 | boot.py:767-874（拖入/收件箱/跨盘 extra_model_paths+幂等回填） |
| 停止链孤儿清扫 B0 | launcher/stop.py:252-264（无启动链仍扫孤儿） |
| golden_master 鉴别 | tests（基线陈旧≠加壳回归） |

---

## 二、迭代漂移缺口清单（盘点结论：自愈没跟上的 12 处）

> 编号 SH-GAP-01~12。1~4 为**实锤/高度嫌疑**，5~12 为设计取舍或待核验。全部**先测出证据、修复待拍板**。

| # | 缺口 | 证据/嫌疑 | 风险 | 靶区用例 |
|---|---|---|---|---|
| SH-GAP-01 | 前端断线补发队列满 50 条**静默丢弃**，无告警无日志 | ws.ts:193-200（MAX_PENDING=50，超限直接丢） | 断线期间发的消息悄悄消失，用户无感知 | I-04 |
| SH-GAP-02 | 舱壁健康喂点**只挂统一错误信封**（error_handler.record_failure）；迭代新增端点若抛裸异常/不走信封 → 舱壁失明、self_heal 不触发 | error_handler.py:628-632 为唯一喂点；无「喂点覆盖率」哨兵 | 新功能故障不可见，自愈链空转 | J-19, K-10 |
| SH-GAP-03 | **9 套队列孤儿回收覆盖不均（已实锤，F-14 审计收口 2026-10-20）**：全项目唯一启动期回收点=recovery.py:29-63 sweep_stale_tasks，词表窄——①char_lora_tasks（人物 LoRA，CLAUDE 清单漏计的第十套）**完全不在扫描**：ghost 'training' 连手动取消都救不回（cancel 只置内存旗标不写库 character_lora_service.py:277-280）、永久阻断版本删除；②train_tasks/style_tasks 的 **queued 行永挂**且有毒：_has_active_training 计 queued→自动微调永锁（behavior_service.py:438-439）、ghost queued 永久阻断版本删除（api/learn.py:478）；③learning_sessions 崩溃时 running 行无清扫→僵尸会话，restore_checkpoint 是零调用死代码；④style_lora resume_task 无 worker 存活校验→重启后 resume 造假 running。对话位次表/行为/浏览器①/绘画页任务=纯内存天然自愈 ✅；video=双保险标杆 ✅ | recovery.py / character_lora_service.py:277 / behavior_service.py:438 / api/learn.py:478 | 重启后任务静默悬挂 + 幽灵锁死业务（P1×4） | F-14 ✅已审，修复=批4 |
| SH-GAP-13 | **（F-14 审计新立）队列孤儿统一回收应落 recovery.py 而非逐宿主复制 video 模式**：QueueSpec 无 orphan 字段，video 的 submit 期 reconcile 与 P20 功能重叠；更优落点=启动期单点 sweep_stale_tasks 一次扩齐（train/style 加 queued、char_lora_tasks 全量、learning_sessions running→interrupted）+ style resume 加 worker 存活校验 | recovery.py:29-63 | 修复路线选择 | 批4 方案 |
| SH-GAP-04 | self_heal 只护 dialog/paint；**video（主力链）/training 无自动恢复入口**，只诚实转恢复指南 | self_heal.py:151-173 设计如此 | 视频生成失败仍要人点；是否补「自动重新入队」待拍板 | K-19 |
| SH-GAP-05 | 前端 WS 重连成功**无提示**（只有底部灯变绿） | BottomStatusBar.tsx:177-181 | 体验缺口，用户不知已恢复 | I-25 |
| SH-GAP-06 | 对话补发为**单条模块级变量**：断线期间连发多条，新发送取消旧补发 | useDialogStore.ts:113-118 cancelPendingResend | 断线窗口的多条消息可能只补最后一条 | I-09 |
| SH-GAP-07 | WS 无限重试无上限（退避封顶 30s 但永不放弃） | ws.ts:171-186 | 设计取舍：后端真死时前端永续后台请求 | I-01 |
| SH-GAP-08 | updater 硬编码 py310 → 发行包升级链死 | 09-20 审计实锤；现行口径=.upg 停用只发整包 | 升级链暂缓（口径下挂起） | H 组全组注记 |
| SH-GAP-09 | health_check 8 项**不含 ChromaDB/云端连接/知识库**健康（迭代新增子系统未纳入） | health_check.py:274-283 清单 | 一键体检查不出向量库哑火 | K-01, SH-GAP 补缺批 1 |
| SH-GAP-10 | 错误聚合噪声大户：learn 1678 次/style 786 次 api_failed（多为参数校验类）堆在 summary 里 | 2026-10-20 16:50 实测 /logs/errors/summary | 告警噪声淹没真故障 | J-11 |
| SH-GAP-11 | **flow 节点埋点覆盖不均（已实锤，J-05 审计收口 2026-10-20）**：A 类完全盲区（stalled 检测零可见）=三个训练队列（learn/style/char_lora，全仓最长任务）+关键帧单行/批量分格+小说大纲+小说角色卡（连 DB 行都没有，卡住即无痕）+知识库导入；B 类半盲（有 start/end 无 node→超 120s 必出**假 stalled**）=小说章节（10-19 修复遗留：novel_service.py:1029-1031 注释明示不加 node，批量时每章一条假记录）+云端视频（轮询数分钟）+漫剧资产 5 链（四视图最易超时）；小尾巴=对话 SSE 断连路径 end 无 finally 兜底（dialog.py:1296-1301） | 全仓仅 6 个文件 import flow_trace；`flow_node()` 便捷接口零调用（哑节点机制闲置） | 假 stalled 淹没真卡住（与 SH-GAP-10 噪声问题共振，P1） | J-05 ✅已审，修复=批5 |
| SH-GAP-12 | 独立脚本跑 SR/试验**必须记得 mark_busy**，否则空闲看门狗可能误杀（10-17 实锤过一次）；缺「外部占用探测」兜底哨兵 | comfy_proc.py:538 _generation_active 只认锁+登记簿 | 试验脚本被误杀→盲等 | D-05, D-20 |

---

## 三、补缺方案（四段制摘要，**全部待拍板，拍板前不改码**）

### 批 1（零风险观测层，建议先行）
1. **改动清单**：①ws.ts 补发队列溢出时 console.warn+reportBgError（1 行级）②新增哨兵单测：grep 全部 api/ 路由 error 响应必经 error_handler 信封（防舱壁失明，架构哨兵测试一件）③health_check 增补第 9 项 chroma（只读探测 ChromaDB 可用性，friendly+出路）④health_check 增补第 10 项 cloud（只读探测已配置云端连接可达性，超时 2s，降级不算 fail）。
2. **影响面**：①前端仅日志行为②纯测试③④GET /system/health-check 响应 items 从 8→10，前端 HealthCheckCard 为数据驱动渲染自适应；doc_claims 如有「8 项」断言需同步。
3. **验证方法**：单测哨兵绿；实弹 GET health-check 见 10 项；断 ChromaDB 数据目录（改名）→ 第 9 项红→修复→绿。
4. **风险与回退**：极低；回退=还原两文件。风险点：cloud 探测超时拖慢体检——已限 2s+只读。

### 批 2（中风险，须逐端点审幂等）
- video 域 self_heal 入口：`_recover_video`=把当前 failed 任务**原参数重新入队**（上限 1 次，幂等键=task_id+attempt）；四段：改动 self_heal.py+video_queue 暴露 resubmit 内部口；影响面=失败自动重跑可能重复消耗时长/GPU（本地无计费风险，云端须排除——cloud_video 一律不自动重试）；验证=杀 comfy 制造失败→观察 1 次自动重入队→第 2 次失败转指南；回退=config 门控 `self_heal.video_enabled` 默认 false。

### 批 3（结构性，随 v4 B5 统一队列治理合流）
- 队列孤儿回收统一化：把 _reconcile_orphan_tasks 上提到通用队列核（QueueSpec 增 orphan_timeout_s 可选项），9 套队列逐个声明是否启用。**建议不单独立即做**，等统一队列批次一起，先靠 F-14 盘点用例摸清现状。

### 挂起不改（记录口径）
- SH-GAP-05（重连成功提示）、06（多消息补发）、07（无限重试）：产品层取舍，凑下一轮前端拍板。
- SH-GAP-08：.upg 停用、只发整包口径不变，升级链测试全部标「窗口依赖」。

### 批 4（F-14 审计实锤后的修复候选，**待拍板**，2026-10-20 新立）
1. **改动清单**：recovery.py sweep_stale_tasks 词表扩齐（train_tasks/style_tasks 加 `'queued'`、新增 char_lora_tasks 全状态、learning_sessions `running→interrupted`）+ character_lora cancel 对 ghost 'training' 改为落库 + style_lora resume_task 加 worker 存活校验（进程内无该任务线程→拒绝/纠偏 DB）。
2. **影响面**：启动期一次性，零热路径成本；副作用=重启后这些 ghost 全部转 error/interrupted 终态（用户可见「已中断」而非永挂）；自动微调锁与版本删除闸解除误锁。
3. **验证方法**：单测=各表造 ghost 行→sweep 后终态断言；实弹=重启窗口后查四张表零 ghost+自动微调可发+版本可删。
4. **风险与回退**：低（只动启动清扫词表+两处校验）；回退=还原 recovery.py 与两个 service。风险点=词表误伤合法 queued（重启瞬间刚入队的任务）——沿用 video 的 updated_at 新鲜度豁免口径。

### 批 5（J-05 审计实锤后的埋点补齐候选，**待拍板**，2026-10-20 新立；纯观测层零逻辑改动）
1. **改动清单**（按审计优先级）：P0=三个训练队列 worker 补 start_flow+node+三出口 end（进度回调现成：lora_training_service.py:761 / style_lora_service.py:603 / character_lora_service.py:349，转发 `node.progress()` 即心跳，模式照抄 80906a0c）+小说章节 run_chapter_job 把 LLM 推理段包进 node（diff 最小）；P1=关键帧单行/批量分格（image_queue submit 处 start、runner 内 node，`_map_step`:1857/`_wait_cb`:3116 现成可转心跳）+小说大纲/角色卡补 start+三出口 end（对齐 GAP-16 修法）；P2=云端视频补 node+心跳（`_prog`:1205）+漫剧资产 5 链补 node；P3=对话 SSE 断连 end 补 finally 兜底。
2. **影响面**：纯观测层（flow_trace.db 多写行+心跳），生成逻辑零改动；flow_trace 独立库不连坐主库；30 天清理守护已有。
3. **验证方法**：单测不适用（埋点），实弹=每链跑一次短任务→查 flow_trace 有 start+node+end 且 nodes≥1；批量章节跑 2 章验证零假 stalled。
4. **风险与回退**：极低；回退=还原各文件。风险点=node 包装改动长任务代码缩进——逐文件小步走+每步实弹。

---

## 四、测试纪律与环境（全计划硬约束）

1. **活动门**：每次开测前 `GET /api/v1/draw/queue` 空 + 无 >1GB comfy 进程 + 后端近 5 分钟无新任务提交；**发现并发在飞立即停手，绝不重启后端**。
2. **绝不直杀后端进程**；需要「杀后端」的用例一律走隔离脚本窗口（boot 守护链自我恢复），或用 mock/单测替代；`/api/quit` 是唯一正规退出通道。
3. **单实例红线**：全机只跑一套；A 组启动链用例需**停机窗口**（用户安静期），标 ⛔窗；GPU 类用例标 ⛔GPU。
4. 测试数据隔离：造的脏数据（任务/事件/备份文件）全部带 `SELFHEALTEST` 前缀或走 temp 目录，测完清；不碰用户项目。
5. 缺陷分级：P0=数据丢失/后端静默死亡/自愈机制误伤用户任务/僵尸进程；P1=自愈该触发没触发/终态悬挂/误报；P2=指标漂移/文案/覆盖缺口；P3=观测噪声/体验。
6. 每条用例判据必须**可取证**（命令输出/日志行/DB 行/WS 帧/DOM），不接受目测。
7. pytest 一律 `runtime\py312\python.exe -m pytest`（裸 python=py313 红线）；前端 vitest 用 runtime 便携 node。

---

## 五、KPI 指标基线表（阈值真源=代码常量，实测锚点随执行回填）

| 指标 | 基线/目标 | 真源 |
|---|---|---|
| 后端冷启动（splash→就绪） | 暖机 ~22s；上限 240s | CLAUDE.md §8 / boot.py:106 |
| 看门狗健康检查间隔/三振 | 5s / 3 次（宽限 180s） | launcher.py:117,123 |
| 补位重启上限/稳定窗 | 5 次 / 600s 不清零 | launcher.py:118-119 |
| WS 前端重连退避 | 1s×2 递增，封顶 30s | ws.ts:54-57 |
| 补发队列上限 | 50 条 | ws.ts |
| 对话补发轮询 | 3s×120（6min） | useDialogStore.ts |
| 绘画续跑等待 | waitPaintReady 150s，仅续 1 次 | InspectorPanel.tsx |
| self_heal 冷却/上限/验证 | 90s / 2 次 / 150s（poll 3s） | self_heal.py:23-25 ✅实测 |
| 舱壁降级阈值 | 连败 3 次→degraded | module_health.py:25 ✅实测 |
| ComfyUI 空闲自关/巡检 | 300s / 15s | comfy_proc.py:62-64 |
| FaceRefine 超时/丢任务/重试 | 1500s / 3 次×10s / 1 次（就绪等 120s） | h3_face_refine.py:65-66 |
| RAM 三件套 | 预检 8GB；装载闸 60s；守卫 80/85/90%；COMMIT 90%；冷却 60/120/30s | launcher.py:331 / vllm_service.py:609 / resource_guard.py:43 |
| VRAM 三档 | 80/90/95%（warning/critical/force_unload） | config.yaml:53-78 |
| 温度闸 | 85 warning / 90°C 拒新任务 | config.yaml |
| 空闲回收 | 60s 浅 / 300s 深 / dialog 900s | config.yaml:38-41 |
| release_for_module 预算 | 3s | api/models.py:1299 |
| 视频孤儿回收窗口 | 120s | video_queue.py:126 ✅实测 |
| flow stalled 阈值 | 120s 无心跳 | flow_trace.py:77 |
| 心跳刷新/历史轮转 | 30s / 512KB·1000 行 | heartbeat.py |
| 事件/日志保留 | 30 天；boot.log 10MB→.1 | event_log.py / boot.py:59 |
| 人话化覆盖率 | 171 语义码=171 条 suggestion（1:1） | error_handler.py ✅实测口径 |
| 错误聚合基线 | learn=1678/style=786（10-20 快照，治理后应显著下降） | /logs/errors/summary 实测 |
| 对话首字/就绪口径 | <500ms（暖态 SSE） | CLAUDE.md §8 |

---

## 六、轮次推进总览（先便宜后昂贵，不带病进下一轮）

| 轮 | 内容 | 组 | 依赖 |
|---|---|---|---|
| R0 | 协议只读批（端点存在性/体检/舱壁/常量抽查） | 抽 K/J/A | 无（已随本计划执行✅） |
| R1 | 单测闸批（vram_policy/gpu_budget/队列守卫/信封哨兵） | 抽 E/F/C | 无（同日执行） |
| R2 | 启动链批 | A | ⛔停机窗口 |
| R3 | 进程存活/RAM 批 | B | ⛔窗口（部分可 mock） |
| R4 | 引擎自愈批 | C+D | ⛔GPU 窗口 |
| R5 | 资源守卫批 | E | ⛔GPU 窗口（部分单测） |
| R6 | 队列批 | F | 后端重启窗口 |
| R7 | 数据自愈批 | G+H | 备份目录写入（随做随清） |
| R8 | 前端自愈批 | I | 浏览器（Browser Use/IAB） |
| R9 | 监控+体检修复批 | J+K | 可造轻故障（随造随修） |
| R10 | 漏洞/负向批 | V | 隔离环境/temp 目录 |
| R11 | 稳定性浸泡批 | S | 24h 专窗（邻线 L-08 口径对齐） |
| R12 | 端到端贯通批 | Z | 全部前轮绿后收官 |

---

## 七、用例全集（298 条）

> 列：编号 | 测试点 | 步骤 | 通过判据与指标 | 级别。⛔=依赖窗口。

### A 组：启动链自愈（28 条）

| # | 测试点 | 步骤 | 判据 | 级 |
|---|---|---|---|---|
| A-01 | py312 守卫 | ⛔窗：用 py313 解释器拉 launcher/boot.py | boot.log 出现守卫转投记录；REEXEC_GUARD=1 防循环；最终后端进程为 py312 | P0 |
| A-02 | 后端端口三级递进 | ⛔窗：占住 5800（假监听 socket）再拉启动链 | launcher 日志一级尝试清残留→二级顺延 5801 就绪；三级（占满区间）报 RuntimeError | P1 |
| A-03 | 启动页端口递进 | ⛔窗：占住 5850 再拉 boot.py | 启动页落 5851+；allow_reuse_address=False 双绑互劫不复现 | P1 |
| A-04 | 启动页幂等双击 | 后端运行中，二次运行 boot.py | 探测到在跑→让位 exit 0；无新守护进程累积 | P0 |
| A-05 | **死锁根修回归** | ⛔窗：造「启动页活+后端死」（隔离实例杀后端保 splash）→再拉 boot.py | 本实例体检后端不可达→接管拉起后端；不再是旧版「让位退出=没人拉后端」 | P0 |
| A-06 | 接管实例失联补位 | ⛔窗：attached 模式下隔离杀后端 | 5s×2 次失联→端口占用再等 8s→自动 spawn 补位，wait_until_ready 60s 内就绪 | P0 |
| A-07 | 看门狗三振 | ⛔窗：mock 后端健康端点连续失败 | 5s 间隔连续 3 次→判 crash 走补位；startup_grace 180s 内不误判冷启动 | P0 |
| A-08 | 补位重启计数不清零 | ⛔窗：600s 窗口内连续制造 3 次崩溃 | 重启成功不清零；计数累计 | P1 |
| A-09 | 超限永久崩溃+取证 | ⛔窗：人为第 6 次崩溃 | crash_permanent；logs/crash_forensics/crash-*.json 落盘含退出码+WER+RAM/GPU 快照 | P1 |
| A-10 | 僵尸循环防线 | ⛔窗：子进程已死但端口由他者健康监听 | 看门狗退位退出，不无限拉短命进程 | P1 |
| A-11 | /api/quit 正规链 | POST 启动页 /api/quit | 优雅收尾；logs/.heartbeat 被摘除；事件账有 backend_stopped 终笔 | P0 |
| A-12 | 崩溃取证可读性 | 读最近 crash-*.json | NTSTATUS 0xC0000005 等映射人话 hint；取证生成异常不影响主流程（fail-open） | P2 |
| A-13 | 孤儿引擎清理边界 | ⛔窗：造父进程已死的 comfy 残留+父存活的引擎各一 | 只杀父死的；父存活的毫发无损 | P0 |
| A-14 | 副本指纹闸 | ⛔窗：改 OMNISPACE_COPY_ID 拉第二副本指向同一后端 | build_id/copy_id 不一致→不收编；同副本幂等接管 | P1 |
| A-15 | 多实例豁免 | OMNISPACE_ALLOW_MULTI=1 拉测试实例 | 豁免生效可双跑；默认路径仍单实例 | P2 |
| A-16 | 单实例互斥体 | ⛔窗：直接双拉 src.main | 第二个秒失败并给指引（Local\OmniSpace.backend.singleinstance）；pytest 态豁免 | P1 |
| A-17 | 断电升级自愈 | ⛔窗：伪造 updates/state.json phase=applying（新鲜）→拉 boot | _recover_interrupted_upgrade 从最新备份还原旧版→照常启动；事件账记录 | P0 |
| A-18 | 就绪超时上限 | ⛔窗：让后端启动必然失败（坏契约） | 240s 上限触发；启动页显示失败而非永转 | P1 |
| A-19 | 启动进度回调 | 冷启动观察启动页 | 每 15s 有进度推进（BACKEND_PROGRESS_EVERY_S），无假死白屏 | P2 |
| A-20 | torch 契约守卫 | ⛔窗：篡改 src/torch_contract.json 版本号 | 阻断启动+指因；OMNISPACE_ALLOW_TORCH_DRIFT=1 可豁免 | P1 |
| A-21 | 模型挂接自愈 | models 收件箱放一个模型目录→启动 | 幂等回填 manifest；跨盘走 extra_model_paths 标记；失败不阻断启动 | P2 |
| A-22 | splash build_id 回显 | GET 启动页 /api/status | 带 build_id/copy_id，供接管闸判定 | P3 |
| A-23 | 停止链兜底清扫 | ⛔窗：无启动链状态下跑 stop.py | 仍扫并清 OmniSpace-LLM.exe/壳残留（12.8GB 孤儿案例回归） | P1 |
| A-24 | 停止链不越界 | :8765 有独立实例时跑 stop.py | 只提示不动手 | P1 |
| A-25 | boot.log 轮转 | 造 >10MB boot.log→启动 | 轮转为 .1 只留一代；新 log 从头写 | P3 |
| A-26 | 设置只读探测容错 | ⛔窗：临时改坏 settings KV 存储→启动 | SQLite 只读探测失败按默认值，不阻断启动 | P2 |
| A-27 | 启动页 DOM 实测 | Playwright 量启动页 DOM | HUD 进度/文案与 /api/status 一致（VLM 幻觉教训：以 DOM 为准） | P3 |
| A-28 | 冷启动计时锚点 | ⛔窗：完整冷启动一次 | splash→就绪 ≤240s 且暖机口径 ~22s 不劣化 >20% | P1 |

### B 组：进程存活 / RAM 三件套（20 条）

| # | 测试点 | 步骤 | 判据 | 级 |
|---|---|---|---|---|
| B-01 | 心跳文件刷新 | 后端运行中每 30s 查 logs/.heartbeat | mtime 持续刷新（30s±5s） | P2 |
| B-02 | crash_detected | ⛔窗：隔离强杀后端（脚本窗口） | 残留心跳+pid 死→事件账 crash_detected；历史 jsonl 可推死亡窗口 | P1 |
| B-03 | 计划内停机不误报 | POST /api/quit 正常退出 | 无 crash_detected（心跳被代摘，boot.py:1403-1407） | P0 |
| B-04 | RAM 提交预检 | ⛔窗：mock 提交内存 <8GB 环境启动 | 警告出现但不阻断启动 | P2 |
| B-05 | vLLM 装载 RAM 闸 | ⛔GPU：压低可用提交内存再 ensure_loaded | 等 60s（30×2s）后诚实拒绝（不 OOM 假死）；4B 降级链接管 | P1 |
| B-06 | RAM_SOFT 80% | 单测/受控压力：占用至 80% | 触发 gc collect；10s 节流不风暴 | P2 |
| B-07 | RAM_HARD 85% | 同上至 85% | 卸空闲模型+EmptyWorkingSet；活动功能受保护 | P1 |
| B-08 | RAM_CRITICAL 90% | 同上至 90% | 收缩可再生内存；事件账记录 | P1 |
| B-09 | COMMIT_HARD 90% | mock 提交 90% | 提交收缩动作触发（RADAR_PRE_LEAK 判死前干预） | P1 |
| B-10 | 冷却防振荡 | 连续触发同档守卫 | 60/120/30s 冷却内不重复动作（无卸载-重载抖动） | P1 |
| B-11 | 驱逐保护名单 | RAM_HARD 时有活动生成在跑 | _evict_candidates 跳过活动功能+常驻 dialog+台账外兜底判断 | P0 |
| B-12 | WER 取证链 | 读最近崩溃取证 | RADAR_PRE_LEAK/WER 事件被抓取并入库 crash-*.json | P2 |
| B-13 | 退出码人话 | 构造退出码 0xC0000005 重启 | _interpret_exit_code 给出「访问违例」类人话 | P3 |
| B-14 | faulthandler 落盘 | ⛔窗：注入 SIGBREAK 场景 | faulthandler dump 文件生成且被取证引用 | P2 |
| B-15 | RAM 92% 互斥增强 | 单测：_thermal_paused 且视频锁持有 | 图像新单被挡（H3 与分镜图不叠跑回归） | P1 |
| B-16 | stalled 标注 | 造无心跳长流程节点 | 120s 后查询侧标 stalled+卡住秒数+最后活跃节点 | P1 |
| B-17 | 心跳历史轮转 | 造 >512KB heartbeat_history | 轮转至 1000 行内 | P3 |
| B-18 | 取证 fail-open | mock 取证函数抛异常 | 崩溃流程照常退出，不被取证拖死 | P2 |
| B-19 | 稳定窗语义 | 单测：600s 内 5 次重启 vs 稳定 600s 后崩溃 | 前者触顶；后者计数重置 | P1 |
| B-20 | 守卫不裸杀 | RAM_CRITICAL 时核对动作清单 | 无任何 kill 进程动作；只收缩内存/卸模型 | P0 |

### C 组：vLLM 引擎自愈（20 条）

| # | 测试点 | 步骤 | 判据 | 级 |
|---|---|---|---|---|
| C-01 | 健康收养 | ⛔GPU：端口预置同模型健康 vLLM→ensure_loaded | _adopt_if_healthy 双验（/health+/v1/models 模型一致）→直接 ready | P1 |
| C-02 | 收养拒绝 | 预置不同模型的服务 | 不收养；走自有启动 | P1 |
| C-03 | 假 ready 防线 | 收养后静默杀服务→is_running | 以服务态为准返回 False（不信内存标记） | P0 |
| C-04 | 失联重载不刷屏 | 收养态失联→ensure_loaded | 降级 unloaded 重载一次成功；无「收养→降级→再收养」循环日志 | P1 |
| C-05 | 降级链顺序 | ⛔GPU：显存压至装不下 9B | 按 DIALOG_MODEL_CANDIDATES 顺位落 4B；degraded_from 记录 | P0 |
| C-06 | 降级可见性 | C-05 后取对话 meta | meta.degraded_from 有值；WS/前端 toast「装不下改用…」 | P1 |
| C-07 | sleep_for_paint | ⛔GPU：绘画生成期观察 | /sleep level=1 或 fallback 停进程；settle 90s 内完成让渡 | P1 |
| C-08 | wake_from_paint | 生成收尾 | wake_up+settle 120s 内恢复对话能力；失败不阻断生成收尾 | P1 |
| C-09 | Windows fallback | mock /sleep 404 | 停子进程置 stopped_for_paint；后台 _restart_after_paint_bg 恢复 | P1 |
| C-10 | 显存准入复测 | ⛔GPU：临界显存发起启动 | 60s（30×2s）复测窗口；不足诚实拒绝不硬闯 OOM | P1 |
| C-11 | 功能锁延迟启动 | paint 持锁时 ensure_loaded | start 延后不抢显存；日志可证 | P0 |
| C-12 | stop 取消协议 | 启动中途（157s 持锁内）调 release_for_module | _cancel_requested 2s 内自杀响应；3s 预算不被击穿 | P1 |
| C-13 | 云端解绑兜底 | 解绑云端连接后继续用对话 | remote 身份不匹配→卸载降回本地；无幽灵云端服役 | P1 |
| C-14 | vLLM 孤儿清扫 | ⛔窗：预置孤儿 vllm 进程→启动 | _reap_orphans 清掉且不误杀在役进程 | P1 |
| C-15 | mock 远程协议回归 | runtime\py312\python.exe -m pytest tests/unit/test_remote_engine.py tests/unit/mock_remote_vllm.py | 全绿（远程后端协议不回归） | P1 |
| C-16 | sleep_mode 门控 | config vllm.sleep_mode=false（默认） | 不真 sleep；走 fallback 口径 | P2 |
| C-17 | RAM headroom 闸 | 单测 test_vram_policy | VLLM_RAM_HEADROOM_GB=3 等常量逐值锁定绿 | P2 |
| C-18 | tier 路由 | 高档位请求路由 | 按候选 tier 正确路由（dialog_engine.py:382） | P2 |
| C-19 | 对话排队 | paint 持锁时发对话 | 进位次表排队而非秒拒（FEATURE_MUTEX 演进语义） | P1 |
| C-20 | 远程后端失联 | mock remote 断连 | remote_backend 降级/重试路径给终态不悬挂 | P1 |

### D 组：ComfyUI 生命周期自愈（24 条）

| # | 测试点 | 步骤 | 判据 | 级 |
|---|---|---|---|---|
| D-01 | Job Object 绑定 | ⛔GPU：杀后端父进程 | comfy 子进程随死（KILL_ON_JOB_CLOSE），无孤儿占显存 | P0 |
| D-02 | 空闲 300s 自关 | ⛔GPU：生成完静置 | ~300s 后 comfy 退出；显存回落 | P1 |
| D-03 | 巡检节拍 | 观察巡检日志 | 15s 间隔，无误杀窗口 | P3 |
| D-04 | /queue 事实判据 | 自关计时期间投新任务 | _queue_has_work=True→不关；生成完整跑完 | P0 |
| D-05 | 锁/登记簿豁免 | mark_busy 期间静置 400s | _generation_active 豁免冻结计时，comfy 不被误杀（R7-AI2 回归） | P0 |
| D-06 | 巡检异常续命 | mock 巡检 /queue 抛异常 | 续命不关（宁可晚关不误杀） | P1 |
| D-07 | spawn 前孤儿清扫 | ⛔窗：预置孤儿 comfy→触发生成 | _reap_orphans 收账后正常 spawn | P1 |
| D-08 | PuLID/EVA 缓存回灌 | 删缓存文件→生成 | sha256 校验失败→从备份回灌→生成不阻断 | P2 |
| D-09 | 重启指纹验尸 | ⛔GPU：FaceRefine 轮询中杀 comfy | pid 变更+poll 验尸→抛 _JobLost（不再盲等 1500s） | P0 |
| D-10 | 丢任务探测 | ⛔GPU：清 history 但任务不在 /queue | 连续 3 次×10s 查无→判丢 | P1 |
| D-11 | 一次自愈重试 | D-09 触发后观察 | attempt 2 前 cm.spawn 重拉+_wait_comfy_ready 120s→重跑成功 | P0 |
| D-12 | 失败零阻断 | 连续制造 2 次失败 | 第 2 次后返回原片继续链（不卡死整个 H3 链） | P0 |
| D-13 | 1500s 止损 | mock 任务永不完成 | 超时 _TIMEOUT_S 后止损走失败路径 | P1 |
| D-14 | 中断止损 | 触发取消 | _interrupt_best_effort POST /interrupt 5s 内生效 | P2 |
| D-15 | 轮询取消检查点 | 取消后轮询循环 | _poll_history 检查点识别取消并退出 | P2 |
| D-16 | 冷启动接线 | comfy 未跑时直调超分/SR | 自动 spawn+就绪等待再执行（坑回归：不报「连接拒绝」） | P1 |
| D-17 | mark_busy 计数式 | 嵌套两次 mark_busy/一次 mark_idle | 计数归零前保持忙碌态（嵌套安全） | P1 |
| D-18 | 护闸全链覆盖 | grep 生成入口 | comfy_paint_engine:1239/1247、h3_face_refine:178/260 配对出现 | P2 |
| D-19 | 8189 端口哨兵 | 跑架构哨兵单测 | 无新增硬编码 8189（port_8189 规则绿） | P3 |
| D-20 | SR 脚本护闸 | 独立脚本跑 SR 带 mark_busy | 空闲看门狗不杀（10-17 事故回归）；无护闸的裸脚本应被发现（关联 SH-GAP-12） | P1 |
| D-21 | comfy 崩溃前端感知 | ⛔GPU：生成中杀 comfy | WS 错误帧+任务 error 终态+前端提示（非永转） | P0 |
| D-22 | /free 让位 | 增量让位触发 | /free 调用+进程内对话模型卸载日志（_yield_paint_deficit） | P2 |
| D-23 | spawn 失败细分码 | mock spawn 失败 | COMFY_* 9 细分错误码之一+出路文案 | P2 |
| D-24 | 挂接兜底重挂 | 冷启前 models_external 标记存在 | _mount_models_before_spawn 幂等重挂不重复 | P3 |

### E 组：显存/资源守卫（22 条）

| # | 测试点 | 步骤 | 判据 | 级 |
|---|---|---|---|---|
| E-01 | 80% warning | 单测/受控注入 used=81% | 警告事件+调度器介入记录，不动模型 | P2 |
| E-02 | 90% critical | used=91% | 降精度/卸非活跃模型动作 | P1 |
| E-03 | 95% force_unload | used=96% | 强制卸载链执行；dialog 驱逐优先级最后 | P1 |
| E-04 | 温度闸 | mock 温度 90°C | 新任务被拒（feature_lock 层）+文案带出路 | P1 |
| E-05 | util 降参 | mock util 86% 5s / 96% 15s | steps×0.67 / ×0.4；4s 滞回防抖 | P2 |
| E-06 | 滞回+ALL_TENSE | 单测 decision.py | hysteresis 30s 生效；ALL_TENSE 跳滞回直通 | P1 |
| E-07 | 两级空闲回收 | 静置 70s/310s/920s 分别观察 | 60s 卸浅层小模型；300s 卸非驻留大模型；900s 卸对话 | P1 |
| E-08 | 模块切换释放 | 前端切模块触发 release-for-module | 3s 内释放完成；带功能锁保护 | P1 |
| E-09 | 让渡协调器收编 | 四编排点各触发一次 | 全部走 VramYieldCoordinator（无各自为政的 sleep 调用残留） | P1 |
| E-10 | 只认最新 source | 连续两次不同 source 让渡 | 旧 source 的 wake 被去抖丢弃 | P2 |
| E-11 | 空闲唤醒排程 | schedule_wake_if_idle 后静置 | 按需唤醒；无任务不空转唤醒 | P2 |
| E-12 | 云任务假空闲防线 | 云端任务在跑时触达空闲回收 | BusyRegistry 上榜→不误卸（回收看见云任务） | P0 |
| E-13 | ALL_TENSE 组合 | mock 全紧张 | int4 降级+只留 dialog 策略生效 | P2 |
| E-14 | 空闲预加载摘除 | 静置观察 | 无自动预载大模型动作（boot 预热默认关回归） | P2 |
| E-15 | 阈值单源 | grep 双写 | 阈值只读 config.yaml scheduler.thresholds；无第二硬编码源 | P2 |
| E-16 | vram_policy 单测闸 | pytest tests/unit/test_vram_policy.py | 全绿逐值锁定 | P1 |
| E-17 | 绘画 RAM 地板 | 单测 | PAINT_QWEN_RAM_FLOOR_GB=22 拦截口径正确 | P2 |
| E-18 | 训练准入 | mock 空闲 <10GB 发训练 | TRAINING_MIN_FREE_GB=10 拒绝+出路 | P2 |
| E-19 | comic_asset 需求档 | 单测 image_queue | _PAINT_NEED_GB_BY_KIND comic_asset=13.0 | P3 |
| E-20 | 增量让位 | 额度不足场景 | /free+卸对话增量让位后放行（而非直接拒单） | P1 |
| E-21 | 生成期两态 | 80% 档+功能锁持有 | 只记事件不迁移模型（不打断在跑任务） | P0 |
| E-22 | 拒单文案 | 触发热拒绝 | 人话+出路（错误信封 suggestion） | P3 |

### F 组：队列自愈（26 条）

| # | 测试点 | 步骤 | 判据 | 级 |
|---|---|---|---|---|
| F-01 | worker 永生 | 单测：注入任务处理抛异常 | _worker_loop 兜底不退出；后续任务照常处理 | P0 |
| F-02 | FIFO 语义 | video 道连投 3 单 | 严格先进先出（priority_sort=False） | P2 |
| F-03 | 双道并行 | 本地单+云端单同时 | 本地道单 worker、云端道并发池互不阻塞 | P2 |
| F-04 | 视频孤儿回收 | ⛔窗：造 pending 超 120s 不在活集→触发 submit | 回收为 error「后端重启导致任务中断」；前端见终态 | P0 |
| F-05 | reconcile 幂等 | 二次 submit | 只在首次触发（_reconciled 旗标） | P2 |
| F-06 | 假终态兜底 | 单测：runner 抛异常逃逸 | 兜底写 cancelled/error 终态，前端轮询不永挂 | P0 |
| F-07 | 锁释放等待 | 单测：release 10s 超时路径 | 失败升 error 日志；无锁残留导致对话白等 300s | P1 |
| F-08 | 镜间接力 | ⛔GPU：h3_chain 连投两段 | 队首 h3_chain keep_loaded 不重载权重 | P1 |
| F-09 | 云任务排首 | 云单+本地单混投 | 云单先跑；跑后非 h3 正常卸载 | P2 |
| F-10 | 准入钩子链序 | 单测 image_queue | acquire/thermal/vllm_sleep/unload/budget 顺序执行不缺环 | P1 |
| F-11 | 排空持锁 | 生成期查 feature_lock | paint 锁被队列持有，外部 submit 排队 | P1 |
| F-12 | 排队可视化 | 连投多单看前端 | 「排队中·前 N」展示与队列事实一致 | P3 |
| F-13 | submit_and_wait 超时 | 单测超时路径 | 超时语义明确（抛错/返回终态），调用方可感知 | P2 |
| F-14 | **9 套队列孤儿审计** | 盘点+逐队列造重启孤儿 | 出审计表：每套队列「重启后 pending/running 去向」；无接guards的列缺口（SH-GAP-03 收口） | P0 |
| F-15 | 训练队列孤儿 | ⛔窗：训练 running 时杀后端→重启 | 任务不永挂：回收或恢复续跑（二选一有据） | P1 |
| F-16 | 行为队列孤儿 | 同法造孤儿 | 有终态去向 | P2 |
| F-17 | 浏览器队列孤儿 | 同法 | 有终态去向 | P2 |
| F-18 | 小说队列孤儿 | ⛔窗：章节生成中重启 | 有终态去向（前章断链案例不复发） | P1 |
| F-19 | 对话位次表 | 排队中重启 | 位次表重建/清空有据，不残影 | P2 |
| F-20 | 队列体检联动 | 造一单长跑后查 health-check | queues 项 detail 反映 queued/current/lock_held 事实 | P2 |
| F-21 | 双账一致性 | 重启后对 DB 状态 vs 内存活集 | 无 DB=running 但活集无主的永久僵尸 | P1 |
| F-22 | 取消链 | 取消 running 任务 | 中断传播→终态 cancelled→锁释放 | P1 |
| F-23 | 重试入口 | 失败任务手动重试 | 原参数重入队；attempt 计数+1 | P2 |
| F-24 | 路由红线回归 | grep VIDEO_ROUTING_TABLE | 零改动（9-06 用户令红线） | P1 |
| F-25 | H3 埋点回归 | ⛔GPU：跑一段 H3 链 | flow nodes≥1（80906a0c 回归：不再 0 节点） | P1 |
| F-26 | 长任务心跳转发 | ⛔GPU：20min+ 长链 | cb 转发节点心跳→无误标 stalled | P1 |

### G 组：数据自愈（24 条）

| # | 测试点 | 步骤 | 判据 | 级 |
|---|---|---|---|---|
| G-01 | 设置备份加密 | POST /system/backup | 落 .json.enc（GCM）；盘上无明文 .json 新增 | P0 |
| G-02 | DB 热备加密 | 同上 | sqlite backup API 热备→整文件 .db.enc；备份期间业务不中断 | P0 |
| G-03 | 明文回退诚实 | mock 密钥不可用→备份 | 明文回退且文件名/响应如实标注（绝不假加密） | P1 |
| G-04 | 恢复清单白名单 | GET /system/restore/list + 越界文件名尝试 | 只列白名单目录；穿越名 400 | P1 |
| G-05 | 体积防损坏 | 造 <4096 字节假备份→restore | 拒绝+指因 | P1 |
| G-06 | .enc 解密暂存 | restore .enc 备份 | 先解密暂存再校验；坏密文拒绝 | P1 |
| G-07 | restore-pending 生效链 | restore→重启 | 重启后生效；事件账双笔（发起+应用） | P0 |
| G-08 | 恢复撤销 | restore→cancel→重启 | 无恢复发生；标记清除 | P1 |
| G-09 | pre_restore 防呆件 | 执行一次真恢复 | backups/omnispace_pre_restore_<ts>.db 存在且为 .enc（BUG-10 回归，71c55e0） | P0 |
| G-10 | 在线恢复不落锁 | 恢复期间查业务接口 | sqlite backup API 在线恢复，无长时间全库锁死 | P2 |
| G-11 | 恢复失败重试 | mock 恢复中途失败 | 保留标记下轮重启重试；不半途清库 | P0 |
| G-12 | 双跳回滚 | 连续两次恢复（16→14 档案例） | 第二跳成功；防呆件带当日数据可再回滚（H-07~13 回归） | P0 |
| G-13 | WAL 生效 | PRAGMA 查询 | journal_mode=wal+busy_timeout 生效 | P2 |
| G-14 | 版本闸 | 造 user_version=99 的库→启动 | 拒绝启动+指因（库>码） | P0 |
| G-15 | 迁移幂等 | 连续启动两次 | _migrate_columns 二次无操作；无重复迁移副作用 | P1 |
| G-16 | 加密迁移校验 | v3 存量字段迁移日志 | 逐字段解密回读校验才 UPDATE；失败不动数据 | P1 |
| G-17 | DB 降级容错 | ⛔窗：锁死 DB 文件→启动 | get_db_safe()→SYSTEM_DB_DEGRADED 内存模式，接口可用+降级标注 | P1 |
| G-18 | flow 独立库 | 查 logs/flow_trace.db | WAL 模式；主库故障不连坐 | P2 |
| G-19 | flow 孤儿恢复 | ⛔窗：running 中杀后端→重启 | recover_orphans：流程→orphan(PROCESS_EXIT)、遗留节点→skipped | P1 |
| G-20 | 30 天清理 | 造 >30 天旧事件/流程文件 | 启动期清理执行；保留期内不动 | P3 |
| G-21 | 清理中心守卫 | clear 请求带 ../ 穿越 | _safe_clear_dir 拒绝；backups_keep 3 份保留 | P1 |
| G-22 | 调度备份真身加密 | 触发调度器 _perform_backup | 产物 .enc（非 boot 页手工备份的旁路明文） | P0 |
| G-23 | manifest 同步 | 删除模型目录→刷新 | models_manifest 同步回收（一天两犯坑回归） | P1 |
| G-24 | 损坏 DB 启动行为 | ⛔窗：换入损坏 .db→启动 | 隔离坏文件+明确报错/重建指引，绝不静默清库 | P0 |

### H 组：升级/断电自愈（10 条）——注记：.upg 停用、只发整包口径；本组以演练包+单测为主，live 升级复测仍挂起（自举限制）

| # | 测试点 | 步骤 | 判据 | 级 |
|---|---|---|---|---|
| H-01 | 验签拒改包 | 篡改演练包 1 字节 | Ed25519 验签失败拒收 | P0 |
| H-02 | 兼容区间 | from_min/max 外版本包 | check_compatibility 拒绝+指因 | P1 |
| H-03 | 解压复核 | extract 后核对 | verify_extracted 复核失败即弃 | P1 |
| H-04 | 状态机全序 | 演练包走全程 | phases：handover_wait→backing_up→applying→verifying_start→success 依序；state.json 每步落盘 | P1 |
| H-05 | 静默窗 | 业务在跑时发起升级 | wait_chain_quiet 等待排空才动手 | P1 |
| H-06 | 原子替换 | applying 期断电模拟 | .upgrading 临时名→无半成品文件生效 | P0 |
| H-07 | verify_start 语义 | mock 首次健康检查失败 | 首错即败回滚（不误杀慢启动——P1-C 修复回归） | P1 |
| H-08 | 回滚链 | mock verify 失败 | restore_backup 还原→rolled_back 终态 | P0 |
| H-09 | 断电自愈新鲜度 | 造 >60s 陈旧 state vs 新鲜 state | 陈旧不误还（updater 可能在跑）；新鲜才还原 | P1 |
| H-10 | 5890 伺服 | 升级中查 5890 | state 伺服可读进度 | P3 |

### I 组：前端自愈（28 条）

| # | 测试点 | 步骤 | 判据 | 级 |
|---|---|---|---|---|
| I-01 | 退避节拍 | 断后端 WS 观察重连时间序列 | 1s→2s→4s→…→30s 封顶；抖动<±20% | P2 |
| I-02 | 归零复位 | 重连成功后再断 | retryCount 归零，退避从 1s 重新开始 | P2 |
| I-03 | 补发队列 | 断线期间 send 3 条→恢复 | 3 条按序补发到达；MAX_PENDING=50 内全量 | P1 |
| I-04 | 补发溢出（缺口） | 断线发 51+ 条 | 现状实锤：第 51 条起静默丢弃且**无任何告警**（SH-GAP-01 证据化） | P1 |
| I-05 | 非法帧容错 | 向 /ws 注入非法 JSON 帧 | Zod 丢弃+reportBgError，连接不断、UI 不崩 | P1 |
| I-06 | 遥测兜底 | WS 断开期间开硬件页 | 5s HTTP 轮询保活；重连后停止轮询 | P2 |
| I-07 | 鉴权重连 | 设 lanToken 后断连重连 | 重连 URL 自动重拼 ?token=；不 401 | P1 |
| I-08 | 对话自动补发 | 模型冷启动时发消息 | 占位文案→3s 轮询→就绪自动补发；6min 上限 | P0 |
| I-09 | 补发单条变量（缺口） | 断线恢复前连发两条 | 实锤行为：新发送取消旧补发（SH-GAP-06 证据化） | P2 |
| I-10 | 三态常驻条 | 冷启动进对话页 | notloaded/loading/ready 三态轮询 2s；文案与状态一致 | P2 |
| I-11 | 进度不谎报 | 冷启动观察进度条 | 渐近曲线 90% 封顶，不闪 100% | P3 |
| I-12 | 绘画自动续跑 | ⛔GPU：制造 MODEL_LOAD_FAILED 后生图 | 正则命中→warmup paint→waitPaintReady 150s→runOnce(false) 恰一次 | P0 |
| I-13 | 真失败出路 | 续跑仍失败 | toast 带出路（模型管理手动加载），非裸报错 | P1 |
| I-14 | prewarm 静默 | mock /chat/prewarm 500 | 静默不弹错（fire-and-forget 设计） | P3 |
| I-15 | 安全模式负对照 | ?safe=1 进入 | 全部自动预热/加载短路；无 warmup 行为 | P1 |
| I-16 | 体检卡片渲染 | 开设置页体检卡 | 8 项三色渲染与后端 items 一致 | P2 |
| I-17 | 修复闭环 | 造可修项→点修复 | friendly toast→自动 refresh 复验转绿 | P1 |
| I-18 | 白名单外动作 | 直接 POST 非法 action | SYSTEM_PARAM_INVALID 400；前端不崩 | P2 |
| I-19 | 深度体检 | 点深度体检 | 26/27 项环境表格渲染完整 | P2 |
| I-20 | 降级只提醒一次 | 同一下降组合连发 3 条 | _lastDegradedKey 去重，toast 恰 1 次 | P2 |
| I-21 | 气泡警示色 | 降级后历史气泡 | modelMismatch 警示色+友好模型名 | P3 |
| I-22 | 幽灵任务收敛 | 造 running 超 15min 无进度 | 60s 巡检强制标 done；前端不永挂 | P1 |
| I-23 | 任务上限裁剪 | 灌 250 个任务 | 上限 200；只裁终态，在跑保全 | P2 |
| I-24 | 视频轮询恢复 | 切后台再回前台 | 隐藏 10s 降频→回前台 tickOnVisible 立即补一拍 | P2 |
| I-25 | WS 灯（缺口注记） | 断连/恢复观察底部栏 | 灯 off/on+title 变化；实锤无重连成功 toast（SH-GAP-05） | P3 |
| I-26 | 断流诚实终止 | 流式中杀后端 | 8s 宽限后终止+中断提示；abort 释放连接无僵尸 | P1 |
| I-27 | chunk 自愈刷新 | 模拟发版后旧 chunk 404 | 会话首次自动 reload；二次失败手动刷新出路 | P2 |
| I-28 | 全局异常上报 | 触发前端未捕获异常 | /logs/frontend-event 收到；同消息 10s 节流 | P2 |

### J 组：监控观测 / flow / 日志（22 条）

| # | 测试点 | 步骤 | 判据 | 级 |
|---|---|---|---|---|
| J-01 | 事件账落盘 | 触发任一事件→查 logs/events | 当日 events-*.jsonl 追加一行合法 JSON | P2 |
| J-02 | 保留清理 | J-20/G-20 联动 | 30 天外清理，期内保留 | P3 |
| J-03 | 历史流程聚合 | 查无 trace_id 旧事件 | query_flows 启发式聚合成流程视图 | P3 |
| J-04 | 7 天统计 | GET event_stats | 分级计数与 jsonl 对账一致 | P3 |
| J-05 | **长链埋点审计** | 盘点小说/训练/学习链 | 出覆盖表：各长链 nodes 数；0 节点链列缺口（SH-GAP-11 收口，BUG-04 同族） | P1 |
| J-06 | Flow.end 事件账 | 跑章节任务 | end 自带终笔事件，无需另打 log_event（10-19 结论回归） | P2 |
| J-07 | 心跳续命 | 长节点周期 node.progress | 不被 stalled 误标 | P2 |
| J-08 | stalled 动态标注 | 造 120s+ 无心跳节点 | 查询侧 stalled+卡住秒数+最后活跃节点 | P1 |
| J-09 | flow 孤儿恢复 | 见 G-19 | 同判据 | P1 |
| J-10 | 诊断包四件 | GET /logs/export | zip 含 events+失败流程+raw+hardware；可解压可读 | P1 |
| J-11 | 错误聚合治理基线 | 读 errors/summary | 记录 learn=1678/style=786 基线；提出参数校验类是否出聚合的拍板项（SH-GAP-10） | P2 |
| J-12 | raw 白名单 | 尝试 tail 非白名单路径 | 拒绝；白名单内正常 tail | P2 |
| J-13 | 前端事件入库 | I-28 联动 | 事件账可见 frontend 来源 | P3 |
| J-14 | 心跳取证 | B-02 联动 | crash_detected 事件入账 | P1 |
| J-15 | 就绪总检对账 | GET /models/readiness | manifest 逐条核对；missing_count 与盘上事实一致（10-20 实测 dialog/paint ready） | P1 |
| J-16 | 8 项体检实弹 | GET /system/health-check | 8/8 ok；queues detail 与队列事实一致（10-20 16:50 实测✅） | P1 |
| J-17 | 环境级 26 项 | POST /system/diagnose | 26 项全出且与机器事实一致（GPU/CUDA/RAM/disk） | P2 |
| J-18 | WS 广播线程安全 | 压测并发 broadcast | 无交错/丢帧（单测+实测） | P2 |
| J-19 | **舱壁喂点覆盖哨兵**（缺口） | 新增架构哨兵：扫描 api/ 错误路径 | 全部错误经 error_handler 信封（record_failure 可达）；裸 raise 漏网列清单（SH-GAP-02 收口） | P0 |
| J-20 | self_heal WS 通道 | K 组触发成功/失败 | WS self_heal kind=success/failed 帧到达前端；failed→RecoveryGuideCard | P1 |
| J-21 | 资源告警节流 | 连续资源告警 | 前端 60s 节流（throttled），无 toast 风暴 | P3 |
| J-22 | 舱壁双通道 | WS 断开时查底部栏 | HTTP GET /system/modules/health 兜底拉取（10-20 实测 4 模块 ok✅） | P2 |

### K 组：体检修复 + 统一自愈链 + 人话化（22 条）

| # | 测试点 | 步骤 | 判据 | 级 |
|---|---|---|---|---|
| K-01 | 8 项逐项触发 | 逐项造轻故障（大文件占盘/假孤儿进程/manifest 抽一本模型） | 对应项 level=warn/fail+friendly 人话+fixable 标注；修复后复验转绿（chroma/cloud 缺口见 SH-GAP-09） | P1 |
| K-02 | 显存档位文案 | used=91%/96% 造数 | 90%/95% 档文案区分正确 | P3 |
| K-03 | 磁盘 20GB 档 | 造剩余 <20GB 报数 | disk warn+清理出路 | P2 |
| K-04 | RAM 90% 档 | 造数 | warn 文案正确 | P3 |
| K-05 | 孤儿判定守卫 | 造 _ORPHAN_SIGNS 进程（父活/父死两种） | 父链深度>8 判有主不误报；父死才报 | P1 |
| K-06 | kill 不碰 python | 触发 kill_orphans | python 进程绝不在击杀名单（316 行守卫回归） | P0 |
| K-07 | rebuild_dirs | 删一个系统目录→rebuild_dirs | 按固定清单重建；清单外不建 | P2 |
| K-08 | clean_logs | 造 >30 天日志→clean_logs | 只删超期；保留期内不动 | P2 |
| K-09 | 白名单闸 | POST action=rm_rf 之类 | SYSTEM_PARAM_INVALID；无任何副作用 | P0 |
| K-10 | 舱壁降级 | 对某模块连造 3 次信封错误 | consecutive_failures=3→state=degraded；degraded 列表出现 | P1 |
| K-11 | 成功清零 | 降级后一次成功 | 计数清零回 ok | P1 |
| K-12 | 降级触发自愈 | K-10 后观察 | 自动调用 self_heal（module_health.py:86-90） | P1 |
| K-13 | 单飞闸 | self_heal running 中再触发 | 不重入（running 列表可见） | P1 |
| K-14 | 冷却闸 | 90s 内连触发 | 第二次被冷却拒绝 | P1 |
| K-15 | 上限转指南 | 连败至 2 次上限 | 停止自动尝试→WS failed→RecoveryGuideCard 四步指南 | P0 |
| K-16 | paint 恢复动作 | 降级 paint→触发 | Comfy ensure_running/legacy ensure_loaded 执行且验证通过 | P1 |
| K-17 | dialog 恢复动作 | 降级 dialog→触发 | ensure_loaded 执行且 150s 内验证 | P1 |
| K-18 | 验证窗口 | mock 恢复动作永不就绪 | 150s 超时（poll 3s）→判失败不悬挂 | P1 |
| K-19 | video/training 诚实转 | 降级 video→触发 | 无盲试；转恢复指南（SH-GAP-04 现状固化，待拍板批 2） | P2 |
| K-20 | 171=171 对应闸 | 哨兵单测 | SEMANTIC_CODES 与 SUGGESTION_DEFAULTS 键集一致无缺漏 | P1 |
| K-21 | legacy 映射 | 发历史数字码 | _LEGACY_CODE_MAP 映射语义码+人话 | P3 |
| K-22 | suggestion 兜底 | 构造未传 suggestion 的 error() | 自动按码补默认出路；裸报错灭绝 | P1 |

### V 组：漏洞 / 负向 / 破坏性（24 条）

| # | 测试点 | 步骤 | 判据 | 级 |
|---|---|---|---|---|
| V-01 | 恢复路径穿越 | restore 传 `..\..\secret.db` | 白名单拒绝；无文件读取 | P0 |
| V-02 | 假 .enc | 截断/翻转字节的 .enc → restore | 解密失败拒绝，不留半恢复态 | P1 |
| V-03 | 假签名升级包 | 换自签钥包 | 验签拒收（对应 H-01 攻击面复核） | P0 |
| V-04 | manifest 投毒 | 改 manifest 指向系统文件 | readiness missing/不采纳；不读该路径 | P1 |
| V-05 | 上传魔数 | 传 PE 头改名 .png | 黑名单拒绝 | P1 |
| V-06 | 回环闸 | 配置 0.0.0.0 启动 | 拒绝启动（ALLOW_LAN 豁免路径单独验） | P1 |
| V-07 | DNS 重绑定 | 伪造 Host 头请求 | TrustedHost 拒绝 | P2 |
| V-08 | 限流 | 同端点 301 req/min | 第 301 起 429/限流响应，恢复后可用 | P2 |
| V-09 | 击杀名单绕过 | 造名为 python.exe 的 _ORPHAN_SIGNS 进程 | 仍不杀（K-06 双保险） | P0 |
| V-10 | 清理穿越 | clear dir 传绝对路径/.. | _safe_clear_dir 拒绝 | P0 |
| V-11 | WS 帧洪泛 | 1s 内 1000 条垃圾帧 | 服务稳定；合法连接不受影响 | P1 |
| V-12 | localStorage 投毒 | 写非法 uiPrefs→启动 | 回放失败 2s 兜底按本地值启动，不白屏 | P2 |
| V-13 | 断网 30s | 拔网（或断回环路由模拟）→恢复 | 全链自愈：WS 重连+补发+轮询恢复，端到端 ≤60s | P1 |
| V-14 | 磁盘写满 | ⛔窗：小盘符模拟写满→触发备份/日志 | 备份失败诚实报错不静默；日志降级不崩主业务 | P1 |
| V-15 | DB 只读 | ⛔窗：db 文件只读属性→运行 | 写操作诚实失败（降级模式）；读正常；恢复后自愈 | P1 |
| V-16 | 目录权限拒绝 | backups 目录只读→备份 | 报错带出路；不假成功 | P1 |
| V-17 | 备份恢复竞态 | 并发 backup+restore | 串行化/互斥有据，无交叉写入 | P1 |
| V-18 | 并发 spawn | 两线程同时 ensure comfy | 单飞锁：仅 1 进程；无双开 8189 | P0 |
| V-19 | 豁免边界 | pytest 态 vs 生产态 | 豁免仅在 PYTEST_VERSION/ALLOW_MULTI；生产路径恒互斥 | P1 |
| V-20 | 激活预览钩子 | OMNISPACE_FORCE_LICENSE_PAGE=1 启动 | #actPage 全屏接管；正常启动不受钩子影响 | P2 |
| V-21 | 占位指纹拒收 | 发占位指纹（恒定值）激活 | 400 拒收（发码台三连回归） | P1 |
| V-22 | SSRF 护栏 | face_refine 目标改外网地址 | 回环字面量+urlsplit 白名单拒绝 | P1 |
| V-23 | 恶意任务载荷 | 注入超长/非法字段任务 | 校验拒绝；队列不崩 | P1 |
| V-24 | chunk 404 攻击面 | 请求不存在 chunk | 走 RouteErrorScreen 自愈刷新（I-27 安全向复核） | P3 |

### S 组：稳定性 / 浸泡 / 压力（14 条）——⛔24h 专窗

| # | 测试点 | 步骤 | 判据 | 级 |
|---|---|---|---|---|
| S-01 | 24h 浸泡 | 后端连续 24h 常规负载 | RSS/提交内存曲线无单调上涨（RADAR_PRE_LEAK 判据）；无静默死亡 | P0 |
| S-02 | 100 轮对话 | 脚本连发 100 轮 | 响应成功率 >98%；内存回落基线 | P1 |
| S-03 | 50 张图 | ⛔GPU：连出 50 图 | 显存回归怠速基线（±1GB）；无累积泄漏 | P1 |
| S-04 | 10 段视频链 | ⛔GPU：连跑 10 段 | 全部终态；无僵尸 comfy；flow 全程 nodes≥1 | P1 |
| S-05 | 重启税 | ⛔窗：连续 5 次重启计时 | 每次冷启动时长不劣化 >20% | P2 |
| S-06 | 三轮转长期 | 浸泡期观察 | boot.log 10MB / events 30 天 / heartbeat 512KB 轮转全部按约执行 | P2 |
| S-07 | WS 重连风暴 | 脚本断/连 100 次 | 前端连接池无僵尸（releaseWsConnection）；后端连接数回落 | P1 |
| S-08 | 高频轮询 1h | 2s poller 跑 1h | 前端内存平稳；无内存泄漏型卡顿 | P2 |
| S-09 | 自愈风暴 | 连造 10 次模块失败 | 冷却+单飞+上限生效：恰 2 次自动尝试+指南；无雪崩 | P0 |
| S-10 | 冻结-死亡回归 | 复放 R15 考古 4 例模式 | 新版本不复现（stalled 标注+守卫介入） | P1 |
| S-11 | 重启频率对账 | 浸泡后统计重启次数 | 显著低于 R15 口径（960 次/24 天历史基线） | P2 |
| S-12 | 双开轰炸 | 脚本连拉 100 次 boot | 全部让位 exit 0；守护进程数恒定 | P1 |
| S-13 | 温度循环 | ⛔GPU：制造高温升降 | 90°C 拒→降温恢复的闸门开合正常，无粘死 | P2 |
| S-14 | 断电模拟 | ⛔窗：硬切断电→上电 | 下次启动全链自愈（升级中断还原+孤儿清理+心跳重建） | P0 |

### Z 组：端到端功能贯通（14 条）——全链「坏了能自己爬起来」

| # | 测试点 | 步骤 | 判据 | 级 |
|---|---|---|---|---|
| Z-01 | 冷启动贯通 | ⛔窗：全冷启动→进前端 | ≤240s 可交互；四模块舱壁全 ok | P0 |
| Z-02 | 对话贯通 | 未加载直接发消息 | 自动加载→回答→（若降级）可见性提示全链 | P0 |
| Z-03 | 绘画自愈贯通 | ⛔GPU：生成中杀 comfy | 自愈重试→成功出图；失败则有终点+出路 | P0 |
| Z-04 | 视频贯通 | ⛔GPU：H3 链一段 | 成片+nodes≥1+进度无倒退 | P1 |
| Z-05 | 杀后端贯通 | ⛔窗：看门狗补位 | 前端 WS 自愈+任务状态诚实；用户刷新即恢复 | P0 |
| Z-06 | FaceRefine 贯通 | ⛔GPU：脸修复中杀 comfy | 自愈重试拉起→成片（593s HEALED 案例回归） | P0 |
| Z-07 | 断网贯通 | V-13 全链版 | 恢复后补发+轮询+灯恢复 | P1 |
| Z-08 | 备份恢复贯通 | 备份→改配置→恢复→重启 | 配置回滚到位；pre_restore 防呆件在 | P0 |
| Z-09 | 升级演练贯通 | 演练包全程 | 验签→应用→健康→（失败则）回滚，state 可追溯 | P1 |
| Z-10 | 体检修复贯通 | 造 3 类故障→体检→修复 | 红→修复→绿闭环，全程人话 | P1 |
| Z-11 | RAM 压力无感 | ⛔GPU：压力下生成 | 守卫降级介入，用户任务完成或诚实失败，无 OOM 崩溃 | P0 |
| Z-12 | 模块切换轰炸 | 快速切模块 20 次 | 无锁残留、无显存爬升、release 3s 预算恒成立 | P1 |
| Z-13 | 长任务跨重启 | ⛔窗：生成中重启 | 孤儿回收终态+人话文案；不悬挂不计费假象 | P0 |
| Z-14 | 诊断包专业可用 | 收尾导出 | 四件齐+crash 取证（如有）能定位当次问题 | P2 |

---

## 八、熔断线（执行中触发即停）

1. 发现并发会话在飞（队列/端口/磁盘文件中途变化）→ 立即停手协调，绝不重启后端。
2. 显存可用 <13.4GB 或温度 >85°C → ⛔GPU 用例顺延。
3. 同一 P0 用例连续 3 次复现失败 → 该组停，出中期报告等拍板。
4. 任何用例需「直杀后端主进程」且无隔离窗口 → 不做，改 mock/单测替代或挂起。
5. 浸泡期间（S 组）后端不可用于其他测试。

---

## 九、执行进度账

| 轮 | 状态 | 结果摘要 |
|---|---|---|
| R0 协议只读批 | ✅ 2026-10-20 已执行 | /health healthy（uptime 3170s, db ok）；L1 体检 8/8 ok（vram 14.5G 余/ram 43%/disk 202G/队列空/license 已激活）；舱壁 4 模块 ok（threshold=3，self_heal 90s/2 次/150s 实照）；readiness manifest 对账 dialog+paint ready；errors/summary 实锤噪声大户 learn=1678/style=786（SH-GAP-10 证据）；draw/queue 空活动门绿；常量抽查 self_heal.py:23-25、module_health.py:25、video_queue.py:376 全与档案一致 |
| R1 单测闸批 | ✅ 2026-10-20 已执行（两批） | **86+45=131 条全绿零红**（vram_policy/gpu_budget/queue_b0/image+video_unified/error_code_registry/health_check/module_health/heartbeat_history/h3_workflow_guard 86 条；开工批后 health_check 扩展+喂点哨兵 20 条 + remote_engine/image_queue/video_queue 25 条） |
| 批1 补缺码 | ✅ 2026-10-20 已落地（未 commit） | 四件全过闸：①ws.ts 补发溢出告警②喂点哨兵 test_module_health_feed.py+基线 160 锁定③④health_check 8→10 项（knowledge/cloud）。闸=ruff 绿+mypy 无新增+tsc+vite build 成品+单测 20 条绿+活体实弹（知识库组件就绪 store= data/vector_store；5 个云端连接全部可达）。**活体生效需后端重启窗口（现役进程仍是老码）** |
| 活体探针批 | ✅ 2026-10-20 已执行（11 项 10✅1🔶） | ✅A-22 启动页 status（build_id 字段在，开发态空值=打包态才有，🔶）｜✅J-17 diagnose 27 项 shape（status=pass 枚举，GPU 检测过）｜✅G-04 restore/list（pre_restore_*.db.enc encrypted:true=BUG-10 活体实锤）｜✅V-01 穿越拒「非法备份文件名」+出路｜✅K-09 白名单外 400+allowed 清单｜✅K-09b kill_orphans 无孤儿安全空跑｜✅V-07 Host 伪造 400｜✅J-12 raw 白名单六选一拒外｜✅J-13 前端事件 recorded:true+落盘 jsonl 可查｜✅V-08 限流 310 连发=300×200+10×429 分毫不差 |
| F-14 九套队列孤儿审计 | ✅ 2026-10-20 已执行 | **实锤 P1×4 + 新立 SH-GAP-13**（详见 §二 SH-GAP-03 行与批 4）：char_lora_tasks 全盲+cancel 不落库、train/style queued 永挂且锁死自动微调与版本删除、learning_sessions 僵尸 running+restore_checkpoint 死代码、style resume 造假 running；对话/行为/浏览器①/绘画页=内存态天然自愈 ✅；video=双保险标杆 |
| J-05 长链埋点审计 | ✅ 2026-10-20 已执行 | **实锤 A 类盲区 7 链 + B 类假 stalled 8 链**（详见 §二 SH-GAP-11 行与批 5）：三训练队列/关键帧/批量分格/大纲/角色卡全盲；小说章节（10-19 遗留）/云端视频/资产 5 链缺 node 必假 stalled；对话 SSE 断连 end 无 finally；全仓仅 6 文件 import flow_trace、flow_node() 便捷接口零调用 |
| R2~R12 | ⬜ 待窗口 | 按 §六轮次推进；A/B/S 组需停机/浸泡专窗，C/D/E 需 GPU 窗口；批1 活体复核（10 项体检）搭任意一次重启窗口 |

---

## 十、汇报口径（验收前置）

- 每轮收工汇报四件：改了什么（本轮无码改动则为零）、验证证据（命令+输出摘要）、遗留什么（挂起+卡点）、下一轮要什么窗口。
- 论断一律标 ✅/🔶/⚠️；单测全绿 ≠ 验收，L2 起全部实弹。
- commit 需用户拍板；本计划执行期默认零码改动（补缺批 1~3 待拍板）。
