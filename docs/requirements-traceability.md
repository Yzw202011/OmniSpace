# OmniSpace AI 需求追踪矩阵（RTM）

> 版本：v1.0 ｜ 生成：2026-08-15 ｜ 对照基线：《极致细粒度全量文档E》（修正版）+《程序开发计划文档B》
>
> **用途**：终结历轮审计口径漂移——每条需求的状态以本表为唯一真源，状态变更必须伴随 git 提交。
>
> **状态定义**：
> - ✅ 已实现：代码存在、已接线、可验证
> - 🟦 已下载未接线：模型/资产已在本地（models/），但代码未接入（2026-08-14 批量下载后新增状态）
> - 🟨 降级实现：功能可用但使用降级路径，响应带 degraded 标记（属诚实降级）
> - ❌ 缺失：需求明确要求，代码无对应实现
> - ⚪ 合理化豁免：与离线单机定位冲突，经裁定不实现

---

## 一、功能需求（P1 核心主线）

| ID | 需求（来源§） | 状态 | 证据/位置 | 备注 |
|----|--------------|------|-----------|------|
| F-01 | 对话被动补全：检测知识缺口→联网搜索→重推理（§1.2.1/§1.2.4） | ❌ | backend/api/dialog.py 无补全分支；TRIGGER_DIALOG_GAP 仅为常量 | 学习进化闭环断点 1/5 |
| F-02 | 学习触发器框架：定时/空闲>5min/项目驱动/对话缺口（§1.3.5） | ❌ | learning_scheduler.py 的 fire_trigger/is_user_idle 无调用方 | 仅手动启动生效，断点 2/5 |
| F-03 | LoRA 自动微调：数据≥100条/到频自动触发 | ❌ | 仅手动 POST /learn/train，无后台执行器 | 断点 3/5 |
| F-04 | 知识 LoRA 接入推理（训练成果被对话加载） | ❌ | dialog_engine.py 无 PeftModel 引用，永远纯基座 | 闭环"应用"环节断裂，断点 4/5 |
| F-05 | 学习进度 WebSocket 推送 /learn/session/progress | ❌ | 全后端无该 WS 端点，只能轮询 | 断点 5/5 |
| F-06 | 分镜四端点：list / reorder / ai-describe / preview | 🟨 | backend/api/manga.py；storyboard_rows.sort_index 迁移已登记（R2-B06） | reorder 持久化已具备列支撑，端点层待核 |
| F-07 | 导演台 /export 导出端点 | ❌ | 仅 panorama / screenshot-4in1 | |
| F-08 | G1 解说漫剧（work_mode=narrative） | ✅ | projects.work_mode v2 迁移（2026-08-14 修复事故后验证通过） | 本表首个由测试守护的需求 |

## 二、功能需求（P2 次要）

| ID | 需求（来源§） | 状态 | 证据/位置 | 备注 |
|----|--------------|------|-----------|------|
| F-09 | /system/info、/system/update 端点 | ❌ | version 部分等价 info；update 与 RC 免安装定位冲突 | update 建议转⚪ |
| F-10 | 对话模型按硬件等级路由（§4.2 六档） | 🟨 | DIALOG_MODEL_CANDIDATES 仅 4B/2B，8B 永不入选 | RTX 5070 Ti 曾因不在 HARDWARE_TIER_TABLE 被误判档位 |
| F-11 | 附录B 细粒度端点（chat/complete、art/inpaint、ip-adapter、style apply/merge/stop/metrics、model/download） | ❌ | 均未实现 | |
| F-12 | 学习策略自适应 3 项（去重阈值/连续质量下降暂停/冷门降优先级，§4.3） | ❌ | 均未实现 | |
| F-13 | Celery+Redis 四队列（§3.1） | ⚪ | 进程内等价替代（功能锁+P0-P5 优先级） | 离线单机裁定豁免 |
| F-14 | 附录C users / assets 表 | ⚪ | 本地单用户；资产走文件系统+引用 | 裁定豁免 |
| F-15 | PPO 协同调度引擎（状态/动作空间/奖励函数/策略网络，§1.3.3） | ❌ | scheduler/history.py 仅决策落库+回归分析，无策略网络与 PPO 更新 | 文档B描述较轻，修正版E按未实现计 |
| F-16 | GPU>95% 持续10s → 降在途生成参数 | 🟨 | 仅精度降级阶梯（下次加载生效），不调整在途任务 | |

## 三、模型资产需求（§1.7 清单）

| ID | 要求 | 状态 | 本地路径 | 备注 |
|----|------|------|---------|------|
| M-01 | Qwen3-VL-8B 对话/视觉理解（8K 上下文高档路由） | 🟦 | models/qwen3-vl-8b/（16.34GB，2026-08-14 下载） | 代码仍固定加载 4B |
| M-02 | FLUX 系文生图主力 | 🟦 | models/paint/flux2-klein-4b/（13.22GB） | paint_engine 仍走 SDXL diffusers 管线 |
| M-03 | LTX-2 系视频主力（720p） | 🟦 | models/video_gen/ltx-2.3/（95.34GB，含 3 版本） | video_engine 无 LTX-2 管线，Ken Burns 降级 |
| M-04 | HunyuanVideo 1.5 画质备选 | 🟦 | models/video_gen/hunyuan-video-1.5/（98.05GB，已裁剪至 720p 四版本） | 同上未接线 |
| M-05 | Qwen3-TTS-1.7B（文档评分⭐9.4） | 🟦 | models/qwen3-tts/（4.21GB） | voice_engine 只认 cosyvoice/chattts，现走 SAPI5 回退 |
| M-06 | CosyVoice 2 TTS 备选 | 🟦 | models/cosyvoice2/（5.23GB） | 同上 |
| M-07 | Qwen3-ASR-1.7B | 🟦 | models/qwen3-asr/（4.38GB） | 无 ASR 管线接线 |
| M-08 | BGE-M3 Embedding | 🟦 | models/embed/bge-m3/（7.91GB，含冗余双副本可清理） | 现役嵌入模型为 bge-large-zh（lifespan 预加载） |
| M-09 | Hunyuan3D 2.1（Mesh+PBR） | 🟦 | models/3d/hunyuan3d-2.1/（13.89GB） | 无 API 端点 |
| M-10 | Qwen3-32B Q4 推理增强 | 🟦 | models/qwen3-32b/（19.26GB GGUF） | 无 llama.cpp/GGUF 加载路径（pydeps 的 llama-cpp-python 为空壳） |
| M-11 | Codestral 22B Q4 代码辅助 | 🟦 | models/codestral-22b/（12.42GB GGUF） | 同上 |
| M-12 | GPT-SoVITS（2.6GB，manifest 声明 tts/voice_clone） | 🟦 | models/gpt-sovits/ | 零接线老问题：voice_engine 不认 sovits 路径 |
| M-13 | AnimateLCM（1.7GB，img2video） | 🟦 | models/ | video_engine 无此管线 |
| M-14 | TripoSR（1.6GB，image_to_3d） | 🟦 | models/3d/TripoSR/ | 无 API 端点 |
| M-15 | SAM ViT-H（2.4GB）/ MiDaS（0.2GB）/ YOLOv8-nano（6MB） | 🟦 | models/ | 三者均无端点；SAM/MiDaS 可被 vision_tools 潜在复用 |
| M-16 | FLUX.1-dev FP8 / Kolors 2.1 / ControlNet-OpenPose / IP-Adapter-Plus / Wan 2.1 / CogVideoX / Real-ESRGAN / CLIP / Blip2 | ❌ | 未下载 | 修正版E 原清单；部分已被 M-02~M-04 新模型替代，建议重裁清单 |
| M-17 | vLLM 推理后端（§1.2.1） | ❌ | transformers 直接推理 | 离线单机可容忍，吞吐差距未量化 |

## 四、架构与安全需求

| ID | 需求（来源§） | 状态 | 证据 | 备注 |
|----|--------------|------|------|------|
| A-01 | Tauri 2.x 桌面壳（要求#38） | ❌ | 无 src-tauri；交付=浏览器+launcher.bat | 降级为浏览器形态 |
| A-02 | 用户数据本地加密存储（要求#36/§1.4） | ❌ | SQLite 明文（SQLCipher shim 已移除并诚实标注） | backend/data/crypto.py 存在但未接 DB 层 |
| A-03 | API 无认证体系下强制 127.0.0.1 绑定（规格§14 约束2） | ✅ | main.py lifespan 非回环绑定醒目告警 | 测试可见 Host 校验中间件 |
| A-04 | 三.js 3D 渲染端侧化 | ✅ | frontend three 0.170 依赖 | |

## 五、工程基线需求（2026-08-15 新增，本次整改）

| ID | 需求 | 状态 | 证据 |
|----|------|------|------|
| E-01 | 版本控制 | ✅ | git 仓库建立（960fbc2 起三提交；models/pydeps/runtime/keys 已排除） |
| E-02 | 依赖锁定 | ✅ | requirements-lock.txt（154 包；记录 pydeps 空壳包与双 torch 元数据隐患） |
| E-03 | 测试基线 | ✅ | pytest.ini + 16 用例（schema 5 + API 4 + 既有 knowledge 8）全部通过 |
| E-04 | schema 版本化迁移 | ✅ | PRAGMA user_version + _MIGRATION_GROUPS + 降级守护（SCHEMA_VERSION=2） |
| E-05 | Lint 配置 | 🟨 | ruff.toml + frontend/eslint.config.js 已落地；ruff/node 二进制未安装，语法基线 compileall 通过 |
| E-06 | CI 流水线 | ❌ | 仍无（无远端仓库；建议先建本地 pre-commit 钩子跑 pytest -m smoke） |
| E-07 | 单一真源交付 | ❌ | e:\OmniSpace 与 E:\RC1002 双真源靠手工 robocopy，历史已漂移一次 |

---

## 维护规则

1. **状态变更必须伴随代码提交**：修复 F-01 类缺口时，提交信息引用需求 ID。
2. **新需求先登记再开发**：新增功能在本表追加行，来源指向规格章节。
3. **🟦 状态是债务**：每个"已下载未接线"模型占磁盘却无产出，接线或删除二选一，不允许长期滞留。
4. **审计以本表为口径**：历轮"阉割功能排查"结论如有出入，以本表 + git 历史为准。
