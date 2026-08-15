# OmniSpace AI — 大模型搭配方案

> 基于上传文档《显卡显存阶梯参考》16GB 档位 + RTX 5070 Ti Blackwell 架构优化
>
> 硬件：RTX 5070 Ti 16GB GDDR7 · Compute 12.0 · Intel Core Ultra 7 270K Plus · 32GB RAM
>
> 日期：2026-08-14

---

## 一、文档 16GB 档位推荐汇总

上传文档将 RTX 5070 Ti 归入 **16GB 中高端档位**（RTX 5070 Ti / 5080 / 5060 Ti 16G），各模块 Top 5 推荐如下：

### 文本大模型（LLM）

| 排名 | 模型 | 参数 | 量化 | 评分 | 定位 |
|------|------|------|------|------|------|
| 1 | Qwen3-32B-Instruct | 32B | Q4_K_M | ⭐9.5 | 16G 能跑的最大通用模型，接近 GPT-3.5 |
| 2 | DeepSeek-R1-Distill-32B | 32B | Q4 | ⭐9.3 | 推理/数学天花板，科研刷题首选 |
| 3 | Qwen3-14B-Instruct | 14B | FP16 | ⭐9.2 | 全精度运行，复杂任务质量有保障 |
| 4 | Llama 3.3-70B-Instruct | 70B | Q3_K_M | ⭐9.0 | 70B 极限压缩，体验旗舰模型能力 |
| 5 | GLM-4-9B-Chat | 9B | FP16 | ⭐8.7 | 全精度 Agent，工具调用最稳 |

### 多模态大模型（VLM）

| 排名 | 模型 | 参数 | 量化 | 评分 | 定位 |
|------|------|------|------|------|------|
| 1 | Qwen2-VL-7B-Instruct | 7B | FP16 | ⭐9.4 | 全精度运行，画质无损，中文场景首选 |
| 2 | Qwen3-VL-8B | 8B | FP16 | ⭐9.3 | 最新 Qwen3 架构，性能全面提升 |
| 3 | Llama3-LLaVA-Next-8B | 8.3B | FP16 | ⭐9.0 | 基于 Llama3，英文场景+代码理解强 |
| 4 | MiniCPM-V 4.0 | 8B | FP16 | ⭐8.9 | 全精度，视觉细节还原最佳 |
| 5 | InternVL2-26B | 26B | Q4 | ⭐8.7 | 大参数多模态，质量接近闭源 |

### 视频生成

| 排名 | 模型 | 参数 | 分辨率/时长 | 评分 | 定位 |
|------|------|------|-----------|------|------|
| 1 | LTX-2.3 | — | 720P / 5s | ⭐9.3 | FP8 量化，16G 流畅，生成速度快 |
| 2 | HunyuanVideo-1.5 | 8.3B | 720P / 5s | ⭐9.1 | 腾讯混元旗舰，CFG 蒸馏，画质第一梯队 |
| 3 | Wan 2.1 14B | 14B | 480P / 5s | ⭐9.0 | 高质量大模型，需量化+卸载 |
| 4 | CogVideoX-5B | 5B | 1080P / 6s | ⭐8.7 | 超分辨率生成 |
| 5 | AnimateDiff + SD3 | — | 720P / 4s | ⭐8.5 | 最新扩散模型底座 |
| ★ | MiniMax H3-Base (INT8) | 33B | 720P / 8-10s | ⭐9.2 | INT8 offload 运行，带同步音频 |

### 语音模型

**ASR 语音识别：**

| 排名 | 模型 | 参数 | 显存 | 评分 | 定位 |
|------|------|------|------|------|------|
| 1 | Whisper large-v3 | 1.5B | ~2 GB | ⭐9.3 | 通用最强，99 种语言 |
| 2 | Qwen3-ASR-1.7B | 1.7B | ~2.5 GB | ⭐9.2 | 中文识别精度最高，方言支持好 |
| 3 | NVIDIA Canary-Qwen-2.5B | 2.5B | ~4 GB | ⭐9.0 | 高精度低 WER (~5.6%) |
| 4 | FunASR Paraformer-large | 220M | <1 GB | ⭐8.8 | 阿里开源，中文专用 |
| 5 | Qwen3-ASR-0.6B | 0.6B | ~1 GB | ⭐8.5 | 轻量快速，适合实时转写 |

**TTS 语音合成：**

| 排名 | 模型 | 参数 | 显存 | 评分 | 定位 |
|------|------|------|------|------|------|
| 1 | Qwen3-TTS-1.7B | 1.7B | ~1-2 GB | ⭐9.4 | 零样本声音克隆，中文自然度最高 |
| 2 | Fish Speech 1.5 | — | ~3 GB | ⭐9.2 | 开源语音克隆标杆，音色还原度极高 |
| 3 | Voxtral (Mistral) | — | ~2.5 GB | ⭐9.0 | Mistral 官方 TTS，多语言支持 |
| 4 | CosyVoice 2 | 0.5B | <2 GB | ⭐8.8 | 阿里开源，中文自然度高，情感丰富 |
| 5 | Bark | — | ~2 GB | ⭐8.3 | 多语言+音效生成，创意场景适用 |

### 代码大模型

| 排名 | 模型 | 参数 | 量化 | 评分 | 定位 |
|------|------|------|------|------|------|
| 1 | Codestral 22B | 22B | Q4_K_M | ⭐9.4 | Mistral 代码旗舰，补全最准 |
| 2 | DeepSeek-Coder-V2 16B | 16B | FP16 | ⭐9.3 | MoE 全精度，IDE 集成首选 |
| 3 | Qwen2.5-Coder-14B | 14B | FP16 | ⭐9.2 | 全精度运行，复杂代码生成强 |
| 4 | DeepSeek-Coder-33B | 33B | Q3 | ⭐9.0 | 33B 极限压缩 |
| 5 | CodeLlama-13B-Instruct | 13B | FP16 | ⭐8.8 | 全精度，稳定可靠 |

### 文档 OmniSpace 项目快速选型表（16GB 列）

| 模块 | 16GB 推荐 |
|------|----------|
| 文本 | Qwen3-32B Q4 |
| 多模态 | Qwen2-VL-7B FP16 |
| 视频 | LTX-2.3 720p |
| 视频(+H3) | +H3 INT8 offload |
| 语音 | Qwen3-ASR+TTS |
| 代码 | Codestral 22B Q4 |

---

## 二、RTX 5070 Ti Blackwell 优化调整

文档基于通用 GPU 编写，未考虑 Blackwell 架构的 FP8 原生加速。RTX 5070 Ti 的 Compute 12.0 Tensor Core 原生支持 FP8 混合精度，相比文档推荐的量化方案有以下调整空间：

### 调整 1：VLM 从 FP16 降为 INT4 常驻，腾出显存

文档推荐 Qwen2-VL-7B FP16（~14GB）或 Qwen3-VL-8B FP16（~16GB），但这会占满全部显存，无法运行其他模型。

**调整**：Qwen3-VL-8B 使用 INT4 量化（~4.5GB）常驻。虽然文档排名中 Qwen3-VL-8B 是第 2 名（⭐9.3），但 Qwen3 架构比 Qwen2-VL 更新，且 INT4 量化后精度损失极小。关键是省出的 11.5GB 显存让其他模块可以运行。

### 调整 2：文本 LLM 选 Qwen3-32B Q4 作为推理增强档

文档 16GB 文本第 1 名是 Qwen3-32B Q4_K_M（⭐9.5）。OmniSpace 的对话引擎（dialog_engine.py）支持动态发现模型——当需要复杂推理时，临时卸载 VLM，加载 Qwen3-32B Q4 作为"推理增强档"。

### 调整 3：视频首选 LTX-2.3（文档第 1），HunyuanVideo 1.5 作为画质备选

文档 16GB 视频第 1 名是 LTX-2.3（⭐9.3），理由是"FP8 量化，16G 流畅，生成速度快"。这正好匹配 Blackwell FP8 原生加速。项目视频引擎已内置 `LTXVideoPipeline` 和 `HunyuanVideoPipeline`，两者均可自动发现加载。

**调整**：LTX-2.3 作为主力（速度快），HunyuanVideo-1.5 作为画质备选。

### 调整 4：TTS 首选 Qwen3-TTS-1.7B（文档第 1）

文档 TTS 第 1 名是 Qwen3-TTS-1.7B（⭐9.4），高于 CosyVoice 2（⭐8.8）。项目 voice_engine.py 已有 `-tts` 后缀检测→`tts_qwen`（Qwen3-TTS）的代码路径，仅需安装包 + 解除 gate。

**调整**：Qwen3-TTS-1.7B 作为主力 TTS，CosyVoice2 作为备选。

### 调整 5：文档未覆盖的模块补充

文档未包含文生图、Embedding、3D 生成三个模块。根据 OmniSpace 项目需求补充：

| 模块 | 推荐模型 | 理由 |
|------|---------|------|
| 文生图 | FLUX.2 Klein 4B (FP8) | 4 步出图，Blackwell FP8 加速，质量远超 SDXL |
| Embedding | BGE-M3 (FP16) | 568M，1024 维，100+ 语言，三路混合检索 |
| 3D 生成 | Hunyuan3D 2.1 | 低显存路径 ~13GB，PBR 物理纹理 |

---

## 三、OmniSpace AI 项目最终推荐

### 常驻层（~7.5GB）

| 模块 | 模型 | 量化 | 显存 | 文档排名 | 来源 |
|------|------|------|------|---------|------|
| 对话/VLM | **Qwen3-VL-8B** | INT4 | 4.5 GB | ⭐9.3 (VLM #2) | 文档 16GB VLM |
| Embedding | **BGE-M3** | FP16 | 2.0 GB | — | 补充（文档未覆盖） |
| OS/驱动 | — | — | 1.0 GB | — | — |

### 按需切换层（同一时刻仅 1 个）

| 模块 | 模型 | 量化 | 显存 | 文档排名 | 来源 |
|------|------|------|------|---------|------|
| 文生图 | **FLUX.2 Klein 4B** | FP8 | ~8 GB | — | 补充（文档未覆盖） |
| 视频生成（主力） | **LTX-2.3** | FP8 | ~10 GB | ⭐9.3 (视频 #1) | 文档 16GB 视频 |
| 视频生成（画质） | **HunyuanVideo-1.5** | FP8+offload | ~14 GB | ⭐9.1 (视频 #2) | 文档 16GB 视频 |
| TTS（主力） | **Qwen3-TTS-1.7B** | FP16 | ~2 GB | ⭐9.4 (TTS #1) | 文档 TTS |
| TTS（备选） | **CosyVoice 2** | FP16 | ~2 GB | ⭐8.8 (TTS #4) | 文档 TTS |
| ASR | **Qwen3-ASR-1.7B** | FP16 | ~2.5 GB | ⭐9.2 (ASR #2) | 文档 ASR |
| 3D 生成 | **Hunyuan3D 2.1** | FP16 | ~13 GB | — | 补充（文档未覆盖） |
| 推理增强 | **Qwen3-32B-Instruct** | Q4_K_M | ~18 GB* | ⭐9.5 (LLM #1) | 文档 16GB LLM |
| 代码辅助 | **Codestral 22B** | Q4_K_M | ~12 GB | ⭐9.4 (代码 #1) | 文档 16GB 代码 |

> *Qwen3-32B Q4 约 18GB，需 CPU offload 部分层到 32GB 系统内存，16GB 显存可跑但速度较慢。

### 可选增强：MiniMax H3-Base

文档特别推荐 MiniMax H3-Base（33B，INT8，720P/8-10s，⭐9.2），支持全模态输入+原生立体声。16GB 卡通过 INT8 offload 可运行，作为视频生成的高端可选项。

---

## 四、与项目现有代码的兼容性

| 模型 | 引擎文件 | 现有支持情况 | 需要的改动 |
|------|---------|------------|-----------|
| Qwen3-VL-8B | dialog_engine.py | ✅ 已有 `DIALOG_MODEL_CANDIDATES` 条目，支持 Qwen3-VL 架构检测 | 仅需修复硬件分级表 |
| Qwen3-32B (Q4) | dialog_engine.py | ✅ 动态发现机制支持，GGUF 格式已支持 | 下载模型即可自动发现 |
| LTX-2.3 | video_engine.py | ✅ `_VIDEO_PIPELINE_CLASSES` 已含 `LTXVideoPipeline` | 下载模型即可自动发现 |
| HunyuanVideo-1.5 | video_engine.py | ✅ 已含 `HunyuanVideoPipeline` | 下载模型即可自动发现 |
| Qwen3-TTS-1.7B | voice_engine.py | ⚠️ 已有 `tts_qwen` 检测路径，但被 gate | 安装包 + 解除 gate |
| CosyVoice 2 | voice_engine.py | ⚠️ 已有 `cosyvoice` 检测路径 | 安装 cosyvoice 包 |
| Qwen3-ASR-1.7B | voice_engine.py | ⚠️ ASR 加载已支持 Whisper，需扩展 Qwen3-ASR | 新增加载方法 |
| FLUX.2 Klein 4B | paint_engine.py | ❌ 仅有 SDXL，无 FLUX 管线 | 新增 FluxPipeline + FP8 |
| BGE-M3 | manifest | ❌ 当前用 all-MiniLM-L6-v2 | 更新 manifest + 加载逻辑 |
| Hunyuan3D 2.1 | 新文件 | ❌ 无 | 新建 hunyuan3d_engine.py |
| Codestral 22B | dialog_engine.py | ✅ 动态发现支持纯文本 LLM | 下载模型即可自动发现 |

### 关键发现

1. **视频生成零代码改动**：LTX-2.3 和 HunyuanVideo-1.5 都已在 `_VIDEO_PIPELINE_CLASSES` 中注册，下载模型后 `discover_video_models()` 自动发现加载
2. **Qwen3-TTS 已有代码路径**：voice_engine.py 的 `_VOICE_NAME_HINTS` 已有 `-tts` → `tts_qwen` 映射，仅需解除 gate
3. **硬件分级表是最大阻碍**：RTX 5070 Ti 不在 `HARDWARE_TIER_TABLE` 中，会被误判为 RTX 3060 档（dialog 降到 2B），必须首先修复

---

## 五、模型下载清单

### P0 — 质变升级（~40 GB）

| 模型 | HuggingFace 仓库 | 存放路径 | 大小 |
|------|-----------------|---------|------|
| Qwen3-VL-8B-Instruct | `Qwen/Qwen3-VL-8B-Instruct` | `models/qwen3-vl-8b/` | ~16 GB |
| LTX-2.3 | `Lightricks/LTX-Video` | `models/video_gen/ltx-2.3/` | ~10 GB |
| Qwen3-TTS-1.7B | `Qwen/Qwen3-TTS-1.7B` | `models/qwen3-tts/` | ~3 GB |
| FLUX.2 Klein 4B | `black-forest-labs/FLUX.2-Klein-4B-diffusers` | `models/paint/flux2-klein-4b/` | ~8 GB |
| BGE-M3 | `BAAI/bge-m3` | `models/embed/bge-m3/` | ~2 GB |

### P1 — 功能补全（~25 GB）

| 模型 | HuggingFace 仓库 | 存放路径 | 大小 |
|------|-----------------|---------|------|
| HunyuanVideo-1.5 | `tencent/HunyuanVideo-1.5` | `models/video_gen/hunyuan-video-1.5/` | ~16 GB |
| Hunyuan3D 2.1 | `tencent/Hunyuan3D-2.1` | `models/3d/hunyuan3d-2.1/` | ~8 GB |
| CosyVoice 2 | `FunAudioLLM/CosyVoice2-0.5B` | `models/cosyvoice2/` | ~2 GB |

### P2 — 增强能力（按需）

| 模型 | HuggingFace 仓库 | 存放路径 | 大小 |
|------|-----------------|---------|------|
| Qwen3-32B-Instruct (Q4) | `Qwen/Qwen3-32B-Instruct-GGUF` | `models/qwen3-32b/` | ~18 GB |
| Qwen3-ASR-1.7B | `Qwen/Qwen3-ASR-1.7B` | `models/qwen3-asr/` | ~3 GB |
| Codestral 22B (Q4) | `mistralai/Codestral-22B-v0.1-GGUF` | `models/codestral-22b/` | ~12 GB |

### 已有（无需下载）

| 模型 | 路径 | 大小 | 说明 |
|------|------|------|------|
| GPT-SoVITS | `models/gpt-sovits/` | 2.6 GB | 已随包，装 pypinyin + 解除 gate |
| TripoSR | `models/3d/TripoSR/` | 1.6 GB | 已随包，3D 快速兜底 |
| SDXL | `models/paint/sdxl-base-1.0/` | 6.9 GB | 已随包，LoRA 训练基座 + 兜底 |

**新增总计**：P0 约 40GB + P1 约 25GB + P2 按需 ≈ 65-95GB

---

## 六、显存预算

```
总显存: 16.0 GB

常驻层 (~7.5GB):
  Qwen3-VL-8B (INT4)          4.5 GB   ← 文档 VLM #2
  BGE-M3                       2.0 GB   ← 补充
  OS/驱动                      1.0 GB

可用: ~8.5 GB

按需切换（同一时刻仅 1 个）:
  Qwen3-TTS-1.7B              ~2 GB    ← 文档 TTS #1，可与常驻共存
  Qwen3-ASR-1.7B              ~2.5 GB  ← 文档 ASR #2，可与常驻共存
  FLUX.2 Klein 4B (FP8)       ~8 GB    ← 补充，需卸载 TTS
  LTX-2.3 (FP8)               ~10 GB   ← 文档 视频 #1，需卸载常驻
  HunyuanVideo-1.5 (FP8+off)  ~14 GB   ← 文档 视频 #2，需清空常驻
  Hunyuan3D 2.1               ~13 GB   ← 补充，需清空常驻
  Qwen3-32B (Q4+offload)      ~18 GB   ← 文档 LLM #1，CPU offload
  Codestral 22B (Q4)          ~12 GB   ← 文档 代码 #1，需清空常驻
```

---

## 七、与之前方案的对比

| 模块 | 之前方案 | 本版（基于文档） | 变化理由 |
|------|---------|----------------|---------|
| VLM | Qwen3-VL-8B INT4 | **Qwen3-VL-8B INT4** | 不变 |
| 文生图 | FLUX.2 Klein 4B FP8 | **FLUX.2 Klein 4B FP8** | 不变 |
| 视频主力 | HunyuanVideo 1.5 | **LTX-2.3** | 文档评分更高（⭐9.3 > ⭐9.1），FP8 更流畅 |
| 视频画质 | — | **HunyuanVideo 1.5（备选）** | 保留作为画质选项 |
| TTS 主力 | CosyVoice2 | **Qwen3-TTS-1.7B** | 文档评分更高（⭐9.4 > ⭐8.8） |
| TTS 备选 | F5-TTS | **CosyVoice 2** | 文档排名更高 |
| ASR | — | **Qwen3-ASR-1.7B** | 新增，文档推荐 |
| 推理增强 | — | **Qwen3-32B Q4** | 新增，文档 LLM #1 |
| 代码辅助 | — | **Codestral 22B Q4** | 新增，文档代码 #1 |
| Embedding | BGE-M3 | **BGE-M3** | 不变 |
| 3D 生成 | Hunyuan3D 2.1 | **Hunyuan3D 2.1** | 不变 |
| 视频可选 | — | **MiniMax H3 INT8** | 新增，文档特别推荐 |

### 核心变化

1. **视频主力从 HunyuanVideo 改为 LTX-2.3**：文档评分 LTX-2.3 更高（速度优势），且项目原始设计就以 LTX-2 为视频主力
2. **TTS 主力从 CosyVoice2 改为 Qwen3-TTS-1.7B**：文档评分 Qwen3-TTS 更高（⭐9.4 vs ⭐8.8），且项目代码已有 Qwen3-TTS 检测路径
3. **新增 ASR/推理增强/代码辅助**：文档提供了 ASR、LLM、代码模型的推荐，补充了 OmniSpace 的学习进化和语音识别能力
4. **新增 MiniMax H3 可选项**：文档特别推荐的 33B 全模态视频模型，16GB 可通过 INT8 offload 运行

---

## 八、实施优先级

| 优先级 | 动作 | 磁盘 | 改动量 | 收益 |
|--------|------|------|--------|------|
| **P0-1** | 修复硬件分级表（新增 rtx5070ti） | 0 | 小 | 解锁 8B VLM 自动路由 |
| **P0-2** | 下载 LTX-2.3 + Qwen3-VL-8B | ~26 GB | 无 | 视频引擎零改动自动发现 |
| **P0-3** | 下载 Qwen3-TTS-1.7B + 解除 gate | ~3 GB | 小 | TTS 质量飞跃 |
| **P0-4** | 下载 FLUX.2 Klein 4B + 改 paint_engine | ~8 GB | 中 | 文生图质变 |
| **P0-5** | 下载 BGE-M3 + 更新 manifest | ~2 GB | 小 | RAG 检索质量提升 |
| P1-1 | 下载 HunyuanVideo-1.5 | ~16 GB | 无 | 视频画质备选 |
| P1-2 | 下载 Hunyuan3D 2.1 + 新建引擎 | ~8 GB | 中 | 3D PBR 纹理 |
| P1-3 | 下载 CosyVoice 2 + 安装包 | ~2 GB | 小 | TTS 备选 |
| P2-1 | 下载 Qwen3-32B Q4 | ~18 GB | 无 | 推理增强档 |
| P2-2 | 下载 Qwen3-ASR-1.7B | ~3 GB | 小 | 语音识别 |
| P2-3 | 下载 Codestral 22B Q4 | ~12 GB | 无 | 代码辅助 |

**P0 五项是质变升级**，其中 P0-1（硬件分级表）和 P0-2（视频+VLM）改动量最小但收益最大——LTX-2.3 和 Qwen3-VL-8B 下载后零代码改动即可激活。
