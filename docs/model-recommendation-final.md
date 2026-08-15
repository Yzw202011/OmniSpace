# OmniSpace AI — 大模型搭配方案

> 基于文档《显卡显存阶梯参考》16GB 档位 + RTX 5070 Ti Blackwell FP8 优化
>
> 硬件：RTX 5070 Ti 16GB GDDR7 · Compute 12.0 · 32GB RAM
>
> 日期：2026-08-14

---

## 搭配总览

```
常驻层 (~7.5GB)
├─ Qwen3-VL-8B (INT4)       4.5 GB   对话/视觉理解
├─ BGE-M3 (FP16)             2.0 GB   RAG 向量检索
└─ OS/驱动                    1.0 GB

按需切换层（同一时刻仅 1 个）
├─ [文生图]   FLUX.2 Klein 4B (FP8)       ~8 GB
├─ [视频主力] LTX-2.3 (FP8)               ~10 GB
├─ [视频画质] HunyuanVideo-1.5 (FP8+off)  ~14 GB
├─ [TTS]      Qwen3-TTS-1.7B (FP16)       ~2 GB（可与常驻共存）
├─ [ASR]      Qwen3-ASR-1.7B (FP16)       ~2.5 GB（可与常驻共存）
├─ [3D生成]   Hunyuan3D 2.1 (FP16)        ~13 GB
├─ [推理增强] Qwen3-32B (Q4_K_M)          ~18 GB（CPU offload）
└─ [代码辅助] Codestral 22B (Q4_K_M)      ~12 GB
```

---

## 各模块推荐

### 对话 / 视觉理解（VLM）

| 模型 | 参数 | 量化 | 显存 | 文档评分 | 角色 |
|------|------|------|------|---------|------|
| **Qwen3-VL-8B** | 8B | INT4 | 4.5 GB | ⭐9.3 | 常驻主力 |
| Qwen2-VL-7B-Instruct | 7B | FP16 | 14 GB | ⭐9.4 | 全精度备选（需独占显存） |
| MiniCPM-V 4.0 | 8B | FP16 | 16 GB | ⭐8.9 | 细节还原备选 |

文档 VLM 第 2 名 Qwen3-VL-8B 做常驻主力（INT4 仅 4.5GB），第 1 名 Qwen2-VL-7B FP16 质量更高但占满显存，仅在不运行其他模型时可用。

### 文生图

| 模型 | 参数 | 量化 | 显存 | 步数 | 角色 |
|------|------|------|------|------|------|
| **FLUX.2 Klein 4B** | 4B | FP8 | ~8 GB | 4步 | 主力（Blackwell FP8 加速） |
| SDXL | 3.5B | FP16 | ~8 GB | 25步 | 兜底/LoRA 训练基座 |

文档未覆盖文生图模块。FLUX.2 Klein 4B 仅需 4 步出图（SDXL 需 25 步），Blackwell FP8 下速度极快，质量远超 SDXL。

### 视频生成

| 模型 | 参数 | 量化 | 显存 | 分辨率 | 文档评分 | 角色 |
|------|------|------|------|--------|---------|------|
| **LTX-2.3** | — | FP8 | ~10 GB | 720P/5s | ⭐9.3 | 主力（速度快） |
| HunyuanVideo-1.5 | 8.3B | FP8+offload | ~14 GB | 720P/5s | ⭐9.1 | 画质备选 |
| Wan 2.1 14B | 14B | Q4+offload | ~12 GB | 480P/5s | ⭐9.0 | 大模型备选 |
| MiniMax H3-Base | 33B | INT8+offload | ~16 GB | 720P/8-10s | ⭐9.2 | 高端可选（带同步音频） |

文档视频第 1 名 LTX-2.3 做主力——FP8 量化 16G 流畅、生成速度快。HunyuanVideo-1.5 画质更优但显存占用更大，作为画质备选。MiniMax H3 33B 全模态输入+原生立体声，INT8 offload 可跑，作为高端可选项。

### TTS 语音合成

| 模型 | 参数 | 显存 | 文档评分 | 角色 |
|------|------|------|---------|------|
| **Qwen3-TTS-1.7B** | 1.7B | ~2 GB | ⭐9.4 | 主力（零样本克隆，中文自然度最高） |
| CosyVoice 2 | 0.5B | ~2 GB | ⭐8.8 | 备选（情感丰富） |
| Fish Speech 1.5 | — | ~3 GB | ⭐9.2 | 克隆备选（音色还原度极高） |

文档 TTS 第 1 名 Qwen3-TTS-1.7B——零样本声音克隆，4GB 可跑，中文自然度最高。仅 ~2GB 显存，可与常驻层共存。

### ASR 语音识别

| 模型 | 参数 | 显存 | 文档评分 | 角色 |
|------|------|------|---------|------|
| **Qwen3-ASR-1.7B** | 1.7B | ~2.5 GB | ⭐9.2 | 主力（中文精度最高，方言支持好） |
| Whisper large-v3 | 1.5B | ~2 GB | ⭐9.3 | 通用备选（99 种语言） |

文档 ASR 第 2 名 Qwen3-ASR-1.7B 做主力——中文识别精度最高。第 1 名 Whisper large-v3 通用性更强但中文略逊。

### 文本 LLM（推理增强档）

| 模型 | 参数 | 量化 | 显存 | 文档评分 | 角色 |
|------|------|------|------|---------|------|
| **Qwen3-32B-Instruct** | 32B | Q4_K_M | ~18 GB* | ⭐9.5 | 推理增强（卸载 VLM 后加载） |
| DeepSeek-R1-Distill-32B | 32B | Q4 | ~18 GB* | ⭐9.3 | 数学推理备选 |
| Qwen3-14B-Instruct | 14B | FP16 | ~14 GB | ⭐9.2 | 全精度备选 |

> *32B Q4 约 18GB，通过 CPU offload 部分层到 32GB 系统内存，16GB 显存可跑。

文档 LLM 第 1 名 Qwen3-32B Q4_K_M——16G 能跑的最大通用模型，接近 GPT-3.5 水平。当需要复杂推理时临时加载，完成后切回 VLM。

### 代码辅助

| 模型 | 参数 | 量化 | 显存 | 文档评分 | 角色 |
|------|------|------|------|---------|------|
| **Codestral 22B** | 22B | Q4_K_M | ~12 GB | ⭐9.4 | 代码主力（补全最准） |
| Qwen2.5-Coder-14B | 14B | FP16 | ~14 GB | ⭐9.2 | 全精度备选 |

文档代码第 1 名 Codestral 22B Q4——Mistral 代码旗舰，Fill-in-Middle 补全最准。

### Embedding / RAG

| 模型 | 参数 | 显存 | 角色 |
|------|------|------|------|
| **BGE-M3** | 568M | ~2 GB | 常驻（1024维，100+语言，三路混合检索） |

文档未覆盖。BGE-M3 支持稠密+稀疏+多向量三路混合检索，8192 token 长文档，仅 2GB 显存。

### 3D 生成

| 模型 | 显存 | 输出 | 角色 |
|------|------|------|------|
| **Hunyuan3D 2.1** | ~13 GB（低显存路径） | Mesh + PBR 纹理 | 主力 |
| TripoSR | ~6 GB | Mesh（无纹理） | 快速兜底 |

文档未覆盖。Hunyuan3D 2.1 几何生成仅需 6GB，PBR 纹理通道低显存路径 ~13GB 可跑。

---

## 显存切换矩阵

| 目标任务 | 所需显存 | 卸载VLM | 卸载BGE | 卸载TTS | 可用显存 | 可行性 |
|---------|---------|---------|---------|---------|---------|--------|
| Qwen3-TTS / ASR | 2-2.5 GB | 否 | 否 | — | 8.5 GB | ✓ 共存 |
| FLUX.2 Klein 4B | 8 GB | 否 | 是 | 是 | 14.5 GB | ✓ 可行 |
| LTX-2.3 | 10 GB | 是 | 是 | 是 | 15 GB | ⚠ 清空常驻 |
| HunyuanVideo-1.5 | 14 GB | 是 | 是 | 是 | 15 GB | ⚠ 清空+offload |
| Hunyuan3D 2.1 | 13 GB | 是 | 是 | 是 | 15 GB | ⚠ 清空常驻 |
| Qwen3-32B Q4 | 18 GB | 是 | 是 | 是 | 15 GB+offload | ⚠ CPU offload |
| Codestral 22B Q4 | 12 GB | 是 | 是 | 是 | 15 GB | ⚠ 清空常驻 |

---

## 下载清单

### P0 — 核心搭搭（~40 GB）

| 模型 | HuggingFace 仓库 | 存放路径 | 大小 |
|------|-----------------|---------|------|
| Qwen3-VL-8B-Instruct | `Qwen/Qwen3-VL-8B-Instruct` | `models/qwen3-vl-8b/` | ~16 GB |
| LTX-2.3 | `Lightricks/LTX-Video` | `models/video_gen/ltx-2.3/` | ~10 GB |
| FLUX.2 Klein 4B | `black-forest-labs/FLUX.2-Klein-4B-diffusers` | `models/paint/flux2-klein-4b/` | ~8 GB |
| Qwen3-TTS-1.7B | `Qwen/Qwen3-TTS-1.7B` | `models/qwen3-tts/` | ~3 GB |
| BGE-M3 | `BAAI/bge-m3` | `models/embed/bge-m3/` | ~2 GB |

### P1 — 功能补全（~25 GB）

| 模型 | HuggingFace 仓库 | 存放路径 | 大小 |
|------|-----------------|---------|------|
| HunyuanVideo-1.5 | `tencent/HunyuanVideo-1.5` | `models/video_gen/hunyuan-video-1.5/` | ~16 GB |
| Hunyuan3D 2.1 | `tencent/Hunyuan3D-2.1` | `models/3d/hunyuan3d-2.1/` | ~8 GB |
| CosyVoice 2 | `FunAudioLLM/CosyVoice2-0.5B` | `models/cosyvoice2/` | ~2 GB |

### P2 — 增强能力（按需，~33 GB）

| 模型 | HuggingFace 仓库 | 存放路径 | 大小 |
|------|-----------------|---------|------|
| Qwen3-32B-Instruct (Q4) | `Qwen/Qwen3-32B-Instruct-GGUF` | `models/qwen3-32b/` | ~18 GB |
| Qwen3-ASR-1.7B | `Qwen/Qwen3-ASR-1.7B` | `models/qwen3-asr/` | ~3 GB |
| Codestral 22B (Q4) | `mistralai/Codestral-22B-v0.1-GGUF` | `models/codestral-22b/` | ~12 GB |

### 已有（无需下载）

| 模型 | 路径 | 大小 |
|------|------|------|
| GPT-SoVITS | `models/gpt-sovits/` | 2.6 GB |
| TripoSR | `models/3d/TripoSR/` | 1.6 GB |
| SDXL | `models/paint/sdxl-base-1.0/` | 6.9 GB |

---

## FP4 未来升级路径

当前 torch cu128 支持 FP8。未来升级 cu130 后解锁 NVFP4（显存再砍半，速度再翻倍）：

| 模型 | FP8 显存 | FP4 显存 | 16GB 可跑？ |
|------|---------|---------|-----------|
| Qwen3-VL-8B | 8 GB | ~4 GB | 两者均可 |
| FLUX.2 Klein 9B | 10 GB | ~5 GB | 两者均可 |
| FLUX.2 dev 32B | 32 GB | ~13 GB | **仅 FP4** |
| LTX-2.3 | 10 GB | ~5 GB | 两者均可 |
| HunyuanVideo-1.5 | 14 GB | ~7 GB | 两者均可 |

FP4 后 FLUX.2 dev 32B 旗舰模型也能塞进 16GB 卡。
