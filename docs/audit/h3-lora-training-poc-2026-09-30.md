# H3 LoRA 本地训练 POC 报告（2026-09-30，方案B 一期）

**性质**：POC 三问三答，"过/不过都留档"（方案书原话）。结论：**本机本地训练路线证伪——真墙是 32GB 系统内存，不是 16GB 显存。二期建议转 fal 云端训练或升 RAM 后复验。**

## 三问三答

### 问 1：16GB 显存训得动吗？——显存不是墙，RAM 是（证伪根因）

- 显存侧：`low_vram + layer_offloading` 生效后 GPU 占用很低，**16GB 显存够用**
- 真墙：H3 训练栈最小权重组合 = DiT（pruned 最小档 21GB）+ 文本编码器（量化后 15GB，AWQ 15.7GB ≈ int4_convrot 14.9GB，无更小档）= **36GB 常驻**，物理大于本机 32GB RAM
- MemoryError 实锤（json 解码层 OOM）+ convrot 反量化阶段的 RAM 峰值段错误（原件+副本并存）
- 权重档位全表（Comfy-Org repack 官方清单，hf-mirror 核实）：
  DiT：bf16 66GB / int8 34GB / **pruned系（int8_convrot=fp8_scaled）21GB** / pruned_w6a8 16GB（ComfyUI 专属格式 toolkit 不吃）
  TE：bf16 51.5GB / int8 27.1GB / **nvfp4_awq 15.7GB ≈ 本地 int4_convrot 14.9GB（下限）**

### 问 2：训一炉要多久？——未进入训练循环，加载段数据如下

- DiT int8 加载 ~2min；float8 量化 2.2s/块 × 50 块 ≈ 2min（triton-windows 装后稳定）
- 训练步耗时未采到（被问 1 物理卡死）

### 问 3：LoRA 对一致性有无实效？——未达（同被问 1 卡死）

## 排掉的六堵墙（换机/复验时的施工图）

1. ComfyUI 的 nvfp4 单文件 safetensors 在 header 层被 toolkit 拒（nvfp4 深绑 ComfyUI 在训练侧复现）→ 用 int8_convrot
2. 无 triton 的 Windows 上 torchao int8 kernel fallback = 段错误（量化 47/50 块崩）→ **triton-windows 必装**（装后 50/50 过）
3. 扩展硬编码 AutoTokenizer/AutoProcessor/AutoConfig 拉 hub，mirror HEAD 校验不稳 → 本地 h3_hub 目录 + 本地补丁 `_tiny_file_source()`（H3_LOCAL_HUB env；git 覆盖风险在案）
4. `name_or_path` 必填（model_kwargs 是覆盖非替代）
5. 32GB RAM 物理墙（36GB 权重裸载 MemoryError）
6. convrot→float8 反量化阶段 RAM 峰值段错误（原件+副本并存）

## 环境资产（已落位，复验即用）

- `tools/ai-toolkit/`（ostris 官方仓 clone）+ uv venv（torch 2.11 cu128 + triton-windows；uv.exe 在 %TEMP%/uv_bin）
- H3 训练扩展=toolkit **内置**（arch: `minimax_h3` / `minimax_h3_ref2va`；mp4 直进数据集）
- `config/xmm_poc.yaml`（rank16/600步/480 档/全套低显存）+ `models/h3_hub/`（tokenizer/processor/TE-config 本地化）+ 本地补丁
- 数据集 `data/lora_datasets/xmm_poc/`（实弹产物 20 帧 + 4 视频 + caption）

## 二期路线建议

- **首选：fal 云端训练**（调研已确认官方支持+开源示例 LoRA+训练指南；本机零占用；成本=付费+资产上云的隐私评估）
- 备选：本机升 RAM 至 64GB 后复验（施工图已全，理论上直接能跑）
- 装载侧无阻塞：训练产物 LoRA 经 LoraLoaderModelOnly 进生成链的通路今天（FaceRefine 链）已验证
