# 开源大模型一键下载工具

## 快速开始

```bash
# 进入目录
cd E:\models

# 运行下载工具
python model_downloader.py
```

## 功能特性

- **5大模型类别**: 文本大模型 / 多模态大模型 / 语音大模型 / 视频生成大模型 / 代码大模型
- **5档显存阶梯**: 8GB / 12GB / 16GB / 24GB(4090) / 32GB(5090)
- **双下载源**: ModelScope(国内优先) + Hugging Face，自动切换
- **断点续传**: 支持中断后继续下载
- **分类管理**: 自动按「类别/显存/模型名」三级目录存放
- **自定义路径**: 可自由设置下载根目录

## 目录结构

```
E:\models\
├── model_downloader.py    # 主程序
├── README.md              # 说明文档
└── models/                # 下载目录 (默认)
    ├── 文本大模型/
    │   ├── 8GB/
    │   │   └── Qwen3-7B-Instruct/
    │   ├── 12GB/
    │   ├── 16GB/
    │   ├── 24GB-4090/
    │   └── 32GB-5090/
    ├── 多模态大模型/
    ├── 语音大模型/
    │   ├── ASR语音识别/
    │   └── TTS语音合成/
    ├── 视频生成大模型/
    └── 代码大模型/
```

## 使用步骤

1. 运行脚本：`python model_downloader.py`
2. 设置下载目录（回车使用默认 `./models`）
3. 选择下载源（推荐默认「自动选择」）
4. 选择模型类别
5. 选择显存档位
6. 选择要下载的模型（支持多选，逗号分隔）
7. 确认下载

## 依赖说明

首次运行会自动安装以下依赖：
- `modelscope` - 魔搭社区下载
- `huggingface_hub` - Hugging Face 下载

如需手动安装：
```bash
pip install modelscope huggingface_hub
```

## 模型清单

### 文本大模型 (25个)
| 显存 | Top 5 模型 |
|------|-----------|
| 8GB | Qwen3-7B / Llama-3.1-8B / GLM-4-9B / Mistral-7B / DeepSeek-V2-Lite |
| 12GB | Qwen3-14B / DeepSeek-R1-14B / Llama-3.1-8B-FP16 / Qwen3-7B-FP16 / Yi-9B |
| 16GB | Qwen3-32B / DeepSeek-R1-32B / Qwen3-14B-FP16 / Llama-3.3-70B-Q3 / GLM-4-9B-FP16 |
| 24GB | Qwen3-32B-Q8 / DeepSeek-V3-MoE / Llama-3.3-70B-Q4 / DeepSeek-R1-32B-Q8 / Qwen3-72B-Q3 |
| 32GB | Qwen3-72B-Q5 / DeepSeek-R1-70B / Llama-4-70B-Q8 / DeepSeek-V3-Q5 / Qwen3-32B-FP16 |

### 多模态大模型 (25个)
| 显存 | Top 5 模型 |
|------|-----------|
| 8GB | Qwen2-VL-2B / MiniCPM-V-2.6 / Phi-4-multimodal / InternVL2-1B / LLaVA-1.6-7B-Q2 |
| 12GB | Qwen2-VL-7B-Q4 / MiniCPM-V-4.0-Q4 / LLaVA-1.6-7B-FP16 / InternVL2-8B-Q4 / Qwen2-VL-2B-FP16 |
| 16GB | Qwen2-VL-7B-FP16 / Qwen3-VL-8B / Llama3-LLaVA-8B / MiniCPM-V-4.0-FP16 / InternVL2-26B-Q4 |
| 24GB | Qwen2-VL-72B-Q4 / LLaVA-1.6-13B / InternVL2-26B-FP16 / Qwen3-VL-8B-FP16 / MiniCPM-V-4.0-FP16 |
| 32GB | Qwen2-VL-72B-Q8 / InternVL2-76B-Q4 / Qwen3-VL-32B-FP8 / LLaVA-1.6-34B-Q4 / Qwen2-VL-7B-FP16 |

### 语音大模型 (10个)
- **ASR语音识别**: Whisper-large-v3 / Qwen3-ASR-1.7B / NVIDIA-Canary-2.5B / FunASR-Paraformer / Qwen3-ASR-0.6B
- **TTS语音合成**: Qwen3-TTS-1.7B / Fish-Speech-1.5 / Voxtral / CosyVoice-2 / Bark

### 视频生成大模型 (25个 + 5个MiniMax H3)
| 显存 | Top 5 模型 | + MiniMax H3 |
|------|-----------|-------------|
| 8GB | Wan2.1-1.3B / CogVideoX-5B / LTX-Video / AnimateDiff-SDXL / ModelScope-T2V | H3-Base Pruned INT8 |
| 12GB | Wan2.1-1.3B-720p / CogVideoX-5B-720p / HunyuanVideo-8.3B / LTX-Video-720p / AnimateDiff-SDXL-720p | H3-Base Pruned INT8 |
| 16GB | LTX-2.3 / HunyuanVideo-1.5 / Wan2.1-14B-480p / CogVideoX-5B-1080p / AnimateDiff-SD3 | H3-Base INT8 |
| 24GB | Wan2.1-14B-FP8 / LTX-2.3-720p / HunyuanVideo-13B / CogVideoX-5B-1080p / AnimateDiff-FLUX | H3-Base Pruned INT8 |
| 32GB | Wan2.2-14B-1080p / HunyuanVideo-720p / Wan2.1-14B-720p / LTX-2.3-1080p / CogVideoX-5B-2K | H3-Base INT8 |

### 代码大模型 (25个)
| 显存 | Top 5 模型 |
|------|-----------|
| 8GB | DeepSeek-Coder-V2-Lite / Qwen2.5-Coder-7B / CodeLlama-7B / Phi-3-mini / StarCoder2-7B |
| 12GB | DeepSeek-Coder-V2-16B-Q4 / Qwen2.5-Coder-14B-Q4 / DeepSeek-Coder-7B-FP16 / CodeLlama-13B-Q4 / Qwen2.5-Coder-7B-FP16 |
| 16GB | Codestral-22B-Q4 / DeepSeek-Coder-V2-16B-FP16 / Qwen2.5-Coder-14B-FP16 / DeepSeek-Coder-33B-Q3 / CodeLlama-13B-FP16 |
| 24GB | Qwen2.5-Coder-32B-Q4 / DeepSeek-Coder-33B-Q4 / CodeLlama-34B-Q4 / Qwen3-Coder-30B-Q4 / Codestral-22B-FP16 |
| 32GB | Qwen2.5-Coder-32B-Q8 / DeepSeek-Coder-V2-MoE / Qwen3-Coder-30B-FP8 / DeepSeek-Coder-33B-Q8 / CodeLlama-70B-Q4 |

## 注意事项

1. **国内网络**: 优先选择 ModelScope 下载源，速度更快更稳定
2. **磁盘空间**: 大模型文件较大，请确保磁盘有足够空间
3. **断点续传**: 下载中断后重新运行脚本，选择相同模型会自动继续
4. **MiniMax H3**: 视频模型中带 `*` 标记的为 MiniMax H3，单独列出不计入Top5额度
5. **许可证**: 部分模型有商用限制，请使用前查看各自许可证

## OmniSpace AI 推荐配置

| 模块 | 4090 (24GB) | 5090 (32GB) |
|------|------------|------------|
| 文本基座 | Qwen3-14B FP16 (12G) | Qwen3-32B FP8 (16G) |
| 多模态理解 | Qwen2-VL-7B FP16 (14G) | Qwen2-VL-7B FP16 (14G) |
| 视频生成 | Wan 2.1 1.3B + MiniMax H3 | MiniMax H3 INT8 + Wan 2.2 |
| 语音ASR/TTS | Qwen3-ASR+TTS (3G) | Qwen3-ASR+TTS (3G) |
| 代码辅助 | Qwen2.5-Coder-7B (4G) | Qwen2.5-Coder-14B (8G) |
