# OmniSpace AI 需求追踪矩阵（RTM）

> 版本：v1.5 ｜ 生成：2026-08-15 ｜ 最近刷新：2026-08-29（代码取证校准：F-02/F-03/F-04 由 ❌ 上调 🟨「已接线待 E2E 验收」，F-12 由 ❌ 上调 ✅，E-03 计数更新 156，E-05 由 🟨 上调 ✅——ruff/node 二进制实测在位）
> **2026-08-28 实测校准**（端到端 GPU 审计）：F-01 / F-05 经代码核验由 ❌ 上调 ✅；M-17 按"已接线未实测"上调 🟨；E-03 证据更新为 115 用例实测；E-04 schema 版本口径 2→7。矩阵行数实为 **44 条**（2026-08-20 起历轮文档写"45 条"系计数错误，INDEX.md 已同步更正）。
> 对照基线：《极致细粒度全量文档E》（修正版）+《程序开发计划文档B》→ **已抽取入库为 `docs/requirements-baseline.md`（RTM 附录 A）**，本表「来源§」自此指向基线章节，仓内即可独立校验需求-矩阵对应关系（TASK-P2-04）
>
> **用途**：终结历轮审计口径漂移——每条需求的状态以本表为唯一真源，状态变更必须伴随 git 提交。
>
> **状态定义**：
> - ✅ 已实现：代码存在、已接线、可验证
> - 🟦 已下载未接线：模型/资产已在本地（models/），但代码未接入（2026-08-14 批量下载后新增状态）
> - 🟨 降级实现：功能可用但使用降级路径，响应带 degraded 标记（属诚实降级）
> - ❌ 缺失：需求明确要求，代码无对应实现
> - ⚪ 合理化豁免：与离线单机定位冲突，经裁定不实现
>
> **v1.1 刷新方法**：磁盘逐目录实测尺寸 + 引擎代码 grep 接线证据 + 运行
> `discover_dialog_models()` 实测发现结果（非文档口径）。修正项：M-01/M-03/M-10/M-11/M-13/M-14/M-15 由 🟦/❌ 上调 ✅，F-10 上调 ✅，M-04 转豁免。
>
> **⚠️ 2026-09-02 复核（诚实化审计，全节适用）**：第三节 M 域多行「本地路径」已因 09-02 两批磁盘清理失效——qwen3-vl-8b（4KB 空壳）/ qwen3-32b / codestral-22b / qwen3-tts / cosyvoice2 / qwen3-asr / gpt-sovits / bge-m3 / hunyuan3d-2.1 / TripoSR **均已移入隔离区 `E:\_trash_staging\`，盘上不存在**。口径修正：M-01 高档路由现由 **qwen3-vl-8b-awq** 承接（DIALOG_ROUTING_TABLE 12GB 档实测）；M-02 绘画主力已易主 **flux2-klein-9b**（PAINT_ROUTING_TABLE 10GB 档），klein-4b one-pass 路线待拍板（拍板清单 A5）；sd15 实占已降 5.2G（四份冗余副本已清）。**模型现状真源 = `models/models_manifest.json` v3 + `GET /api/v1/models`**，本节 M 域降级为"选型决策史 + 接线史"（历史状态判定保留原时点含义，勿再据其找盘上路径）。
>
> **⚠️ 2026-09-13 二次复核（B2 产品信誉批）**：下列行的 ✅ 为**历史时点状态**，盘上现状以此为准——**在盘**：qwen3-vl-8b-awq(7.05G)、qwen35-9b-w4a16(10.7G)、flux2-klein-9b(38.0G)、flux2-klein-4b(14.9G)、qwen-image-2512(28.0G)、minimax-h3(51.3G)、sam-vit-h(2.4G)；**不在盘**：qwen3-vl-8b、ltx-video-0.9.5、qwen3-32b、codestral-22b、AnimateLCM、sd15、models/depth/、models/detect/（09-13 起 LTX/ToonCrafter/SD15/AnimateLCM 入 `data/backups/quarantine-20260913/` 观察 7 天）。连带现状：`/art` 六端点仅 segment 在役（depth/detect/3d 无权重必报错）、`/style/train|preview` 因无 LTX-2 基座恒 80010/80013（前端已置灰）。M-14 同前口径。

---

## 一、功能需求（P1 核心主线）

| ID | 需求（来源§） | 状态 | 证据/位置 | 备注 |
|----|--------------|------|-----------|------|
| F-01 | 对话被动补全：检测知识缺口→联网搜索→重推理（§1.2.1/§1.2.4） | ✅ | dialog.py `_maybe_passive_completion()`（不确定标记→fire dialog_gap→搜索→重推理，失败回退原回复）；实测 /dialog/send 响应含 passive_completion 字段 | 2026-08-28 校准上调（原 ❌ 为 08-20 口径，落地时间在其后） |
| F-02 | 学习触发器框架：定时/空闲>5min/项目驱动/对话缺口（§1.3.5） | 🟨 | learning_scheduler.py L103-271 触发器注册（R2-B02：TRIGGER_IDLE/TRIGGER_SCHEDULED/TRIGGER_DIALOG_GAP 默认回调）+ L474-503 调度 tick 周期评估接线 | 2026-08-29 校准上调（原 ❌ 为 08-28 前口径）；触发器→学习会话全链路 E2E 验收待补 |
| F-03 | LoRA 自动微调：数据≥100条/到频自动触发 | 🟨 | learning_scheduler.py L784-873 `_maybe_auto_finetune`（R2-B03）：周期判定+知识点阈值（config finetune_min_knowledge）+GPU 空闲（功能锁+显存双查）+防重入+§4.3 质量下降暂停 | 2026-08-29 校准上调；自动触发→训练→挂载 E2E 验收待补 |
| F-04 | 知识 LoRA 接入推理（训练成果被对话加载） | 🟨 | backends/transformers_backend.py L187-233 `_attach_knowledge_lora`（R2-B04）：peft.PeftModel 挂载 current 版本+基座匹配校验+异常诚实回退基座；dialog_engine.refresh_knowledge_lora L1004 支持热刷新 | 2026-08-29 校准上调；真实 LoRA 产物端到端生成验收待补 |
| F-05 | 学习进度 WebSocket 推送 /learn/session/progress | ✅ | backend/api/learning.py WS 端点（2026-08-28 装饰器扫描命中；已收录 api-endpoints.md WebSocket 4 清单） | 2026-08-28 校准上调 |
| F-06 | 分镜四端点：list / reorder / ai-describe / preview | ✅ | backend/api/manga/storyboard.py（TASK-P2-01 拆包后路径）；reorder 真实持久化 sort_index（DB update per row） | 2026-08-20 核验上调：四端点齐备且 reorder 落库，前端 reorderRows 有测试守护 |
| F-07 | 导演台 /export 导出端点 | ❌ | 仅 panorama / screenshot-4in1 | |
| F-08 | G1 解说漫剧（work_mode=narrative） | ✅ | projects.work_mode v2 迁移（2026-08-14 修复事故后验证通过） | 本表首个由测试守护的需求 |

## 二、功能需求（P2 次要）

| ID | 需求（来源§） | 状态 | 证据/位置 | 备注 |
|----|--------------|------|-----------|------|
| F-09 | /system/info、/system/update 端点 | ❌ | version 部分等价 info；update 与 RC 免安装定位冲突 | update 建议转⚪ |
| F-10 | 对话模型按硬件等级路由（§4.2 六档） | ✅ | dialog_engine.py:240 `_effective_candidates()`：tier≥12GB 追加 8B 首选（R2-B10）；实测本机（5070 Ti 档 min_vram=14）候选序列 8B→4B→2B | 2026-08-19 实测确认；RTX 5070 Ti 已入 HARDWARE_TIER_TABLE |
| F-11 | 附录B 细粒度端点（chat/complete、art/inpaint、ip-adapter、style apply/merge/stop/metrics、model/download） | ❌ | 均未实现 | |
| F-12 | 学习策略自适应 3 项（去重阈值/连续质量下降暂停/冷门降优先级，§4.3） | ✅ | knowledge_service.py L342/L753 去重阈值随知识总量收紧；learning_scheduler.py L799-819 auto_train_paused+check_quality_decline_pause（连续 3 次质量下降暂停自动训练）；model_manager/predictor.py L63-373 冷门功能判定（COLD_FEATURE_MAX_USES=3）+ learning_scheduler.py L684-735 冷门主题降权 | 2026-08-29 校准上调（原"均未实现"与代码不符） |
| F-13 | Celery+Redis 四队列（§3.1） | ⚪ | 进程内等价替代（功能锁+P0-P5 优先级） | 离线单机裁定豁免 |
| F-14 | 附录C users / assets 表 | ⚪ | 本地单用户；资产走文件系统+引用 | 裁定豁免 |
| F-15 | PPO 协同调度引擎（状态/动作空间/奖励函数/策略网络，§1.3.3） | ❌ | scheduler/history.py 仅决策落库+回归分析，无策略网络与 PPO 更新 | 文档B描述较轻，修正版E按未实现计 |
| F-16 | GPU>95% 持续10s → 降在途生成参数 | 🟨 | 仅精度降级阶梯（下次加载生效），不调整在途任务 | |

## 三、模型资产需求（§1.7 清单）

| ID | 要求 | 状态 | 本地路径 | 备注（2026-08-19 实测刷新 + 处置决策） |
|----|------|------|---------|------|
| M-01 | Qwen3-VL-8B 对话/视觉理解（8K 上下文高档路由） | ✅ | models/qwen3-vl-8b/（16.34GB） | dialog_engine.py:54 `_HIGH_TIER_DIALOG_CANDIDATE`（R2-B10）；实测本机候选 8B 首选；嵌套 modelscope 快照经 _resolve_candidate_dir 解析。原记录"代码固定 4B"过时 |
| M-02 | FLUX 系文生图主力 | ✅ | models/paint/flux2-klein-4b/（13.22GB）；paint_engine.py Flux2KleinPipeline 加载分支 + generate/img2img FLUX.2 参数映射（2026-08-20）；四视图 one-pass 主路径实测通过（131s 含加载，2560×1440 seed 可复现，2026-08-20 画幅对齐 参考图.png）；PAINT_ROUTING_TABLE/HARDWARE_TIER_TABLE 已登记 12GB 闸门 | 角色/场景/道具常规生成仍走 SDXL 缺省；FLUX.2 仅四视图路径显式点名（comparator 路线对齐裁定） |
| M-03 | LTX-2 系视频主力（720p） | ✅ | models/video_gen/ltx-video-0.9.5/（23.65GB） | model_index.json `_class_name=LTXPipeline` 命中 video_engine `_VIDEO_PIPELINE_CLASSES` 动态发现；Ken Burns 仅作无模型兜底。原记录"ltx-2.3 95.34GB 含 3 版本"数据过时 |
| M-04 | HunyuanVideo 1.5 画质备选 | ⚪ | 目录已删除（磁盘危机清理） | **决策：裁剪**——fp32 权重 98GB 超 16GB 显存，已在项目排除名单（2026-08-15） |
| M-05 | Qwen3-TTS-1.7B（文档评分⭐9.4） | 🟦 | models/qwen3-tts/（4.21GB） | **决策：接线排期 P1**——选定 TTS 主力；voice_engine 已识别为 tts_qwen 但门控（transformers 4.51 无 Qwen3TTS 管线类，需官方推理包或升级 transformers） |
| M-06 | CosyVoice 2 TTS 备选 | 🟦 | models/cosyvoice2/（5.23GB） | **决策：删除（待用户确认后执行）**——与 Qwen3-TTS 功能重叠，cosyvoice 代码包依赖链重且未随包 |
| M-07 | Qwen3-ASR-1.7B | 🟦 | models/qwen3-asr/（4.38GB） | **决策：接线排期 P2**——ASR 属规格语音输入功能；voice_engine 识别为 asr_qwen 但门控（官方推理包未随包） |
| M-08 | BGE-M3 Embedding | 🟦 | models/embed/bge-m3/（7.91GB） | **决策：接线排期 P1**——部署计划 P0 常驻嵌入；切换需重建 Chroma 向量库（现役 bge-large-zh 1.21GB，models/embed/） |
| M-09 | Hunyuan3D 2.1（Mesh+PBR） | 🟦 | models/3d/hunyuan3d-2.1/（13.89GB） | **决策：接线排期 P2**——TripoSR 已覆盖单图 3D 主链路（/art/image-to-3d），此为画质增强选项，官方依赖较重 |
| M-10 | Qwen3-32B Q4 推理增强 | ✅ | models/qwen3-32b/（19.26GB，128k-Q4_K_M GGUF） | discover_dialog_models() 实测命中（gguf 后端）；llama-cpp-python 0.3.34 实为可用（pydeps 提供，ctypes 后端 + DLL 完整）——原"空壳包"结论有误。16GB 卡需 CPU offload，端到端首用实测待补 |
| M-11 | Codestral 22B Q4 代码辅助 | ✅ | models/codestral-22b/（12.42GB GGUF） | 同 M-10，discover 实测命中 |
| M-12 | GPT-SoVITS（tts/voice_clone） | 🟦 | models/gpt-sovits/（2.56GB） | **决策：删除（待用户确认后执行）**——voice_engine.py 头注释明确裁定"跳过 sovits 并如实上报"，推理代码包永不随包 |
| M-13 | AnimateLCM（img2video 兜底） | ✅ | models/video_gen/AnimateLCM/（1.69GB）+ models/sd15/ 底座 | video_engine.py F-07 分支已接线（AnimateLCM_sd15_t2v.ckpt + SD1.5 组 AnimateDiffPipeline，LCM 少步采样）。另：sd15 目录 32.5GB 含四份底权重复制（ckpt+safetensors × pruned/emaonly ≈22GB 冗余），列磁盘清理候选 |
| M-14 | TripoSR（image_to_3d） | ✅ | models/3d/TripoSR/（1.56GB） | triposr_engine.py + POST /art/image-to-3d（.glb 输出）。原记录"无 API 端点"有误（2026-08-19 修正） |
| M-15 | SAM ViT-H / MiDaS / YOLOv8-nano | ✅ | models/sam-vit-h/（2.39GB）、models/depth/（0.14GB）、models/detect/（0.01GB） | /art/segment + /art/depth + /art/detect 三端点在位（vision_tools.py）。原记录"均无端点"有误（2026-08-19 修正） |
| M-16 | FLUX.1-dev FP8 / Kolors 2.1 / ControlNet-OpenPose / IP-Adapter-Plus / Wan 2.1 / CogVideoX / Real-ESRGAN / CLIP / Blip2 | ❌ | 未下载 | 修正版E 原清单；部分已被 M-02~M-04 新模型替代，建议重裁清单 |
| M-17 | vLLM 推理后端（§1.2.1） | 🟨 | engines/vllm_service.py + py313 子进程（vllm-0.26.0+cu128 wheel 已装）；AWQ/GPTQ 模型自动路由 vLLM；tests/test_vllm_hot_switch_stress.py 等在位（需 OMNISPACE_VLLM_STRESS=1 显式开启） | 2026-08-28 校准：由 ❌ 上调 🟨（已接线）；端到端 vLLM 推理本审计未实测，保守计 |

> **P0-04 决策汇总**（2026-08-19，2026-08-20 刷新；2026-08-28 算术勘误）：🟦 共 **6 条**——接线排期 **4 条**（M-05/M-08 为 P1，M-07/M-09 为 P2）、待确认删除 **2 条**（M-06/M-12 合计 7.79GB，删除属破坏性操作需用户批准）。原文"🟦 剩 5 条/接线排期 3 条"为计数笔误。M-02 已于 2026-08-20 完成接线并上调 ✅（四视图 one-pass 主路径）。
> 磁盘清理候选（非矩阵需求）：sd15 冗余副本 ~22GB。models/ 实测总计 193.25GB / 24 目录（2026-08-28 复测 42 登记 / 29 下载，口径含运行时自动发现）。

## 四、架构与安全需求

| ID | 需求（来源§） | 状态 | 证据 | 备注 |
|----|--------------|------|------|------|
| A-01 | Tauri 2.x 桌面壳（要求#38） | ⚪ | ADR-002 裁剪（2026-08-20）：浏览器+launcher 为正式交付形态，非降级；重启条件与放弃收益见 ADR | 豁免依据 docs/ADR-002-tauri-shell-decision.md |
| A-02 | 用户数据本地加密存储（要求#36/§1.4） | ✅ | schema v3 存量迁移（211 对话+815 行为日志全量加密，明文残留 0，字节级复检通过）；读写路径经 crypto.py AES-256-GCM 接线 | 范围=dialog_messages.content + behavior_logs 四字段；标题/绘画 prompt 等非敏感列留明文（2026-08-20 P2-05） |
| A-03 | API 无认证体系下强制 127.0.0.1 绑定（规格§14 约束2） | ✅ | main.py lifespan 非回环绑定醒目告警 | 测试可见 Host 校验中间件 |
| A-04 | 三.js 3D 渲染端侧化 | ✅ | frontend three 0.170 依赖 | |

## 五、工程基线需求（2026-08-15 新增，本次整改）

| ID | 需求 | 状态 | 证据 |
|----|------|------|------|
| E-01 | 版本控制 | ✅ | git 仓库建立（960fbc2 起三提交；models/pydeps/runtime/keys 已排除） |
| E-02 | 依赖锁定 | ✅ | requirements-lock.txt（154 包；记录 pydeps 空壳包与双 torch 元数据隐患） |
| E-03 | 测试基线 | ✅ | pytest.ini + backend/tests（**2026-08-29 实测 156 passed / 5 skipped，28.31s**；skip 均为需 OMNISPACE_VLLM_STRESS/E2E 显式开启的 GPU 集成测试）+ 前端 vitest + 活后端 E2E（tools/run_tests.py 唯一入口） | 早期口径"16 用例"已大幅增长 |
| E-04 | schema 版本化迁移 | ✅ | PRAGMA user_version + _MIGRATION_GROUPS + 降级守护（**2026-08-28 实测 user_version=7**；本行旧口径 SCHEMA_VERSION=2 已过时，明细见 docs/design/database-er.md §6） |
| E-05 | Lint 配置 | ✅ | ruff.toml + frontend/eslint.config.js 落地；**B1 2026-09-13 勘误：实机为便携 node v20.20.2（runtime/node-v20.20.2-win-x64）+ pnpm 11.22.0（packageManager 钉版）**；旧「node v24.19.0」为 08-29 误记 | 提交前三步门可实际执行 |
| E-06 | CI 流水线 | ❌ | 仍无（无远端仓库；建议先建本地 pre-commit 钩子跑 pytest -m smoke） |
| E-07 | 单一真源交付 | ✅ | tools/build_rc.py 一键镜像（/MIR + 显式排除清单）；2026-08-19 首次全量同步 198.86GB 后 robocopy 列表模式校验零差异 | RC 目录自此仅由脚本产出，禁止手工改动（TASK-P0-01） |

---

## 维护规则

1. **状态变更必须伴随代码提交**：修复 F-01 类缺口时，提交信息引用需求 ID。
2. **新需求先登记再开发**：新增功能在本表追加行，来源指向基线章节（`requirements-baseline.md` 附录 A）。
3. **🟦 状态是债务**：每个"已下载未接线"模型占磁盘却无产出，接线或删除二选一，不允许长期滞留。
4. **审计以本表为口径**：历轮"阉割功能排查"结论如有出入，以本表 + git 历史为准。

## 附录 A：需求基线

见 `docs/requirements-baseline.md`——文档E/B 仍有效需求的仓库内抽取版（六大模块功能清单、五大引擎、协调规则、38 项核心要求全量 ↔ 本矩阵映射表）。本表「来源§」中的 §1.2.x/§1.3.x/§1.5/§2.x/§4.x/附录B/C 均指基线对应章节。
