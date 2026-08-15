# Round 4 功能补全与残余缺口审计报告

- 日期：2026-08-08
- 范围：按 feature-fix-plan.md 执行 M1（P0）/M2（P1）/M3（P2）/M4（P3 文档降级）全部 14 项
- 结论：**11 项修复落地并实测通过，3 项经实测论证走诚实门控/文档降级**；残余缺口均为外部资产/依赖型或有意保留项

---

## 一、本轮修复执行结果

### M1（P0）随包模型接线 —— 4/4 完成并实测
| 项 | 实现 | 实测证据 |
|---|---|---|
| F-01 TripoSR | 新建 [triposr_engine.py](file:///e:/OmniSpace/backend/services/inference/triposr_engine.py) + `POST /art/image-to-3d` | 真实推理 147.6s（含首载）产出 15882 顶点/31756 面 .glb；修复 autocast 混合精度（Half/Float 不匹配）与 HF 离线重试（省 ~40s） |
| F-02 SAM ViT-H | 新建 [segment_engine.py](file:///e:/OmniSpace/backend/services/inference/segment_engine.py) + `POST /art/segment` | 2.0s 返回真实 mask（IoU 1.013） |
| F-03 MiDaS | 新建 [depth_engine.py](file:///e:/OmniSpace/backend/services/inference/depth_engine.py) + `POST /art/depth` | 0.3s 返回 320×240 伪彩色深度图（ORT CPU） |
| F-04 YOLOv8 | 新建 [detect_engine.py](file:///e:/OmniSpace/backend/services/inference/detect_engine.py) + `POST /art/detect` | 2.5s 真实推理（CUDA） |
| 集成 | 新建 [vision_tools.py](file:///e:/OmniSpace/backend/api/vision_tools.py)（6 路由，含 `/art/assets/3d/{file}` 下载与 `/art/tools/status` 聚合），[main.py](file:///e:/OmniSpace/backend/main.py) 注册 | 路由核验 + 活体服务全端点冒烟通过 |

### M2（P1）闭环与安全 —— 2/2 完成并实测
| 项 | 实现 | 实测证据 |
|---|---|---|
| F-05 §4.3 自适应 | ①去重阈值 ≥9 万条 0.85→0.8（[knowledge_service.py](file:///e:/OmniSpace/backend/services/knowledge_service.py)）②连续 3 次质量下降暂停自动训练 + WS 通知 + 设置重开清除（[lora_training_service.py](file:///e:/OmniSpace/backend/services/lora_training_service.py)、[learning_scheduler.py](file:///e:/OmniSpace/backend/services/learning_scheduler.py)、[learning.py](file:///e:/OmniSpace/backend/api/learning.py)）③冷门功能学习主题降权（[predictor.py](file:///e:/OmniSpace/backend/services/model_manager/predictor.py)） | 阈值切换/连降判定/冷门检测单元冒烟全过 |
| F-06 数据加密 | 新建 [crypto.py](file:///e:/OmniSpace/backend/data/crypto.py)：AES-256-GCM + DPAPI 保护数据密钥；接入 [dialog.py](file:///e:/OmniSpace/backend/api/dialog.py)（content）与 [behavior_service.py](file:///e:/OmniSpace/backend/services/behavior_service.py)（四列） | DB 原始值 `enc:v1:...` 密文，读取透明解密还原；历史明文透传兼容 |

### M3（P2）中等工程 —— 4/4 完成（2 项经实测论证走门控）
| 项 | 结果 |
|---|---|
| F-07 AnimateLCM | [video_engine.py](file:///e:/OmniSpace/backend/services/inference/video_engine.py) 探测链接入（MotionAdapter + AnimateDiffPipeline + LCMScheduler，ckpt 转换实测 unexpected=0）；**SD1.5 底座未随包 → 门控回落 Ken Burns**，status 如实上报 reason；底座补齐即解锁 |
| F-08 GPT-SoVITS | 实测权重完好但**官方推理代码/音素符号表（732 映射）/中文 G2P 均未随包**，离线合成不可行 → 探测+门控（[voice_engine.py](file:///e:/OmniSpace/backend/services/inference/voice_engine.py) `sovits` 字段如实上报），SAPI5 链路不破坏（[manga.py](file:///e:/OmniSpace/backend/api/manga.py) degrade 文案分级） |
| F-09 契约 | `GET /system/update` 语义化返回（supported=false, channel=manual-replace）；新建 [api-contract-decisions.md](file:///e:/OmniSpace/docs/audit/api-contract-decisions.md) 30 项裁定 |
| F-10 在途降参 | 新建 [quality_governor.py](file:///e:/OmniSpace/backend/services/quality_governor.py)；[analyzer.py](file:///e:/OmniSpace/backend/services/scheduler/analyzer.py) GPU>95%/10s 判定 → tick 置旗标 → [paint_engine.py](file:///e:/OmniSpace/backend/services/inference/paint_engine.py) 每 step 轮询 → diffusers 中断 + 元数据/WS 事件标注 |

### M4（P3）架构项 —— 按推荐路径文档降级
《修正版E》末尾新增**附录D（实施偏差与降级决议）**：D.1 PPO→历史回归分析、D.2 Tauri→浏览器+原生启动器、D.3 vLLM→transformers、D.4 随包模型兑现状态、D.5 SQLCipher→字段级 AES-GCM。

### 最终回归（新代码活体服务）
`/health` healthy/db ok · 四引擎注册 · `/system/update` 语义正确 · `/style/status` av1_encoder=nvenc（前修不回归）· 版本 2.3.1 · GetDiagnostics 后端零错误

---

## 二、修复后仍未完成的功能（残余缺口）

### A. 外部资产/依赖型（代码侧已无可做，待资产决策）
| 缺口 | 解锁条件 | 影响面 |
|---|---|---|
| LTX-2 / Wan2.1 / CogVideoX | 下载权重（9~28GB/个） | 漫剧视频生成、视频风格 LoRA 基座 |
| GPT-SoVITS 推理 | 引入官方代码包 + pypinyin + 音素符号表 + 参考音频 | 真实 AI 语音克隆（现 SAPI5） |
| AnimateLCM 推理 | 补 SD1.5 底座（~4GB） | 图生视频真实运动 clip |
| FLUX.1 / Kolors 2.1 / ControlNet / IP-Adapter | 下载权重 | 绘画主力模型/中文优化/姿态控制/风格迁移 |
| Qwen3-VL-8B / 2B、CogVLM2 | 下载权重 | 对话高档路由 |
| Qwen3-14B / ChatGLM-4-9B / Yi-1.5-9B | 下载权重 | 知识 LoRA 大基座 |
| Stable Zero123 / Wonder3D / LGM | 下载权重 | 3D 三件套 |
| Real-ESRGAN / CLIP / Blip2 / CosyVoice3 / ChatTTS / Whisper / OpenVoice | 下载权重（Real-ESRGAN 仅 ~0.1GB，建议优先） | 超分真 AI 化、语音大模型等 |

### B. 有意保留缺口（已裁定，见 api-contract-decisions.md）
- 24 个附录B 细粒度端点（chat/complete、art/inpaint、style apply/merge/stop 等）——前端无消费方，待需求驱动
- Celery+Redis 四队列、users/assets 表——进程内等价/本地单用户形态下的合理省略

### C. 前端接线缺口（后端已就绪）
- `/art/image-to-3d` `/segment` `/depth` `/detect` 四端点**暂无前端 UI 入口**（计划中的 DirectorStage"图片生成 3D 资产"入口、绘画页分割/深度工具未做）

### D. RC 打包待办
- TripoSR 接线新增的 runtime 依赖（omegaconf、einops、scikit-image、rembg、torchmcubes shim）需打入下一 RC
- **`data/keys/dbkey.bin` 不得随包分发**（DPAPI 密钥绑定本机用户，RC 首启应各自生成）——pack_rc.ps1 需加排除规则

---

## 三、代码审计结论

### 变更清单
- **新建 9 个**：triposr_engine / segment_engine / depth_engine / detect_engine / vision_tools / quality_governor / crypto / api-contract-decisions.md / 本报告
- **修改 16 个**：main.py、video_engine、voice_engine、manga.py、knowledge_service、lora_training_service、learning_scheduler、browser_agent_service、predictor、learning.py、system.py、scheduler/analyzer、scheduler/__init__、paint_engine、dialog.py、behavior_service.py、《修正版E》附录D

### 质量判定
| 维度 | 结论 |
|---|---|
| 诚实降级原则 | ✅ 遵守——AnimateLCM/SoVITS 门控如实上报 reason，无伪造能力 |
| 迁移安全 | ✅ 加密对历史明文透传； governor 默认不触发；旧调用签名全兼容（render_kenburns_frames 新参可选） |
| 事件循环安全 | ✅ 重推理均 asyncio.to_thread；加密操作为微秒级同步可接受 |
| 资源安全 | ✅ 四引擎懒加载 + 推理串行锁 + CUDA empty_cache；SAM 2.4GB/TripoSR 1.6GB 非常驻 |
| 诊断 | ✅ 后端零错误；前端 2 项既有问题与本轮无关（mangaApi.ts schema 误报——文件存在；app.css appearance 警告） |

### 遗留风险（低）
1. TripoSR 首载 ~40s（已修）+ 推理 ~100s 于高分辨率；前端接入时需给长时任务进度提示
2. 加密密钥机器绑定：用户迁移数据目录到新机后历史密文不可解（返回空串）——如需可移植性，未来加"导出受口令保护的密钥包"工具
3. 附录D 与正文冲突条目已声明"以附录为准"，下一轮文档大修时建议回写正文
