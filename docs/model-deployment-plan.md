# OmniSpace AI — 大模型搭配与部署方案

> RTX 5070 Ti 16GB Blackwell 专版 · 激进 FP8 策略
>
> 日期：2026-08-14

---

## 硬件配置

| 组件 | 规格 |
|------|------|
| GPU | NVIDIA GeForce RTX 5070 Ti |
| 显存 | 16GB GDDR7 |
| 架构 | Blackwell（Compute 12.0，70 SM） |
| CPU | Intel Core Ultra 7 270K Plus（24 核 24 线程） |
| 内存 | 32GB |
| CUDA | 12.8 |
| Torch | 2.11.0+cu128 |

### 核心优势：Blackwell FP8 原生加速

RTX 5070 Ti 的 Tensor Core 原生支持 FP8 混合精度运算。相比 FP16：

- 显存缩减 ~2x
- 推理加速 ~2x
- 精度损失 <1%

当前 torch 2.11.0+cu128 已原生支持 FP8，无需额外配置。

### 核心约束：16GB 显存

16GB 是唯一硬约束。策略：轻量模型常驻 + 重量模型按需加载 + FP8 量化优先。同一时刻仅保留 1 个生成模型在显存中，通过项目已有的 `force_unload` + `precision_ladder` 机制切换。

### 关键问题：硬件分级表缺失 RTX 5070 Ti

项目 `backend/data/models.py` 的 `HARDWARE_TIER_TABLE`（第 113-164 行）不包含 RTX 5070 Ti。该 GPU 会被 VRAM 回退逻辑误判为 `rtx3060` 档位（dialog 降到 2B、paint 降到 SDXL），严重浪费硬件能力。必须在部署前新增 `rtx5070ti` 档位。

---

## 模型搭配总览

### 架构分层

```
常驻层 (~7.5GB)
├─ Qwen3-VL-8B (INT4)      4.5 GB   对话/视觉理解
├─ BGE-M3                   2.0 GB   RAG 向量检索
└─ OS/驱动                  1.0 GB

按需切换层（同一时刻仅 1 个）
├─ [模式A] FLUX.2 Klein 4B (FP8)       ~8 GB    文生图（4步出图）
├─ [模式B] HunyuanVideo 1.5 (FP8+off)  ~14 GB   视频生成 720p
├─ [模式C] CosyVoice2-0.5B (FP16)      ~4 GB    TTS 语音合成
├─ [模式D] Hunyuan3D 2.1 (低显存路径)   ~13 GB   3D PBR纹理
└─ [模式E] F5-TTS (FP16)               ~4 GB    语音克隆专家

FP4 未来路径（cu130 升级后）
├─ FLUX.2 dev 32B (FP4)    ~13 GB   旗舰文生图
└─ HunyuanVideo 1.5 (FP4)  ~7 GB    可与VLM共存
```

### 与上一版方案对比

| 模块 | 上一版（保守） | 本版（激进 Blackwell） | 优先级 |
|------|--------------|---------------------|--------|
| 对话 VLM | Qwen3-VL-4B INT4 | **Qwen3-VL-8B INT4** | P0 |
| 文生图 | SDXL 3.5B (25步) | **FLUX.2 Klein 4B (4步)** | P0 |
| 视频生成 | Wan 2.2 1.3B 480p | **HunyuanVideo 1.5 720p** | P0 |
| 3D 生成 | TripoSR (无纹理) | **Hunyuan3D 2.1 (PBR)** | P1 |
| TTS | CosyVoice2 单引擎 | **CosyVoice2 + F5-TTS 双引擎** | P1 |
| Embedding | all-MiniLM-L6-v2 | **BGE-M3** | P1 |
| 风格控制 | SDXL ControlNet | **FLUX ControlNet-Union** | P2 |
| 量化策略 | INT4 保守 | **FP8 原生加速** | NEW |
| 未来路径 | 无 | **FP4 解锁 (cu130)** | P3 |

---

## 显存预算分配

```
总显存: 16.0 GB

常驻层 (~7.5GB):
  Qwen3-VL-8B (INT4)          4.5 GB
  BGE-M3                       2.0 GB
  OS/驱动                      1.0 GB

可用: ~8.5 GB（轻量任务可直接用）
  CosyVoice2 (4GB)            可与常驻共存 (7.5+4=11.5GB)
  F5-TTS (4GB)                可与常驻共存
  TripoSR (6GB)               可与常驻共存 (7.5+6=13.5GB)

重型任务（需卸载常驻层，独占显存）:
  FLUX.2 Klein 4B (FP8)       ~8 GB    卸载TTS后加载，4步出图
  FLUX.2 Klein 9B (FP8)       ~10 GB   高质量模式，需清空常驻
  HunyuanVideo 1.5 (FP8+off)  ~14 GB   卸载全部，720p视频
  Hunyuan3D 2.1 (低显存路径)   ~13 GB   卸载全部，PBR纹理3D
```

### 显存切换矩阵

| 目标任务 | 所需显存 | 卸载VLM(4.5G) | 卸载BGE(2G) | 卸载TTS(4G) | 可用显存 | 可行性 |
|---------|---------|--------------|------------|------------|---------|--------|
| CosyVoice2 TTS | 4 GB | 否 | 否 | — | 8.5 GB | ✓ 直接加载 |
| TripoSR 3D（快速） | 6 GB | 否 | 否 | 是 | 12.5 GB | ✓ 舒适 |
| FLUX.2 Klein 4B | 8 GB | 否 | 是 | 是 | 14.5 GB | ✓ 可行 |
| FLUX.2 Klein 9B | 10 GB | 是 | 是 | 是 | 15 GB | ⚠ 需清空常驻 |
| HunyuanVideo 1.5 | 14 GB | 是 | 是 | 是 | 15 GB | ⚠ 需清空+offload |
| Hunyuan3D 2.1 | 13 GB | 是 | 是 | 是 | 15 GB | ⚠ 需清空常驻 |
| Qwen3-VL-8B 高档路由 | 5 GB | 替换4B | 否 | 是 | 10.5 GB | ✓ 替换式加载 |

---

## P0 质变升级三件套

这三项将项目核心能力从"可用"提升到"好用"，投入产出比最高。

### 4.1 FLUX.2 Klein 4B — 文生图主力

| 指标 | 值 |
|------|-----|
| 参数量 | 4B |
| 推理步数 | 4 步（SDXL 需 25-50 步） |
| FP8 显存 | ~8 GB |
| 磁盘占用 | ~8 GB |

FLUX.2 Klein 4B 仅需 4 步推理即可生成 1024x1024 图像，质量远超 SDXL。在 Blackwell FP8 加持下出图速度极快。

**为何选 4B 而非 9B**：Klein 4B 的 transformer 仅 ~3.9GB（INT8），加上文本编码器和 VAE 约 ~8GB（FP8），16GB 卡可在卸载 TTS 后直接加载。9B 需 ~10GB，必须清空全部常驻层，切换成本高。4B 是速度/质量/显存的最佳平衡点。

**代码修改 — paint_engine.py：**

```python
# 1. PAINT_MODEL_CANDIDATES 新增（第 54 行附近）
PAINT_MODEL_CANDIDATES = {
    "flux2-klein-4b": {"path": "paint/flux2-klein-4b", "vram_gb": 8, "priority": 1},
    "sdxl-base-1.0":  {"path": "paint/sdxl-base-1.0",  "vram_gb": 7, "priority": 2},  # 降级为兜底
}

# 2. 新增 FLUX 管线支持（load_model 方法内）
from diffusers import FluxPipeline, FluxTransformer2DModel

def load_model(self, model_id: str):
    # ... 现有逻辑 ...
    if "flux" in model_id.lower():
        pipe = FluxPipeline.from_pretrained(
            model_path,
            torch_dtype=torch.float8_e4m3fn,  # FP8 原生
        )
        pipe.enable_model_cpu_offload()
        pipe.vae.enable_tiling()
        self._pipe = pipe
        return

    # ... 现有 SDXL 逻辑保留为 fallback ...

# 3. 新增 FP8 dtype 支持
def _preferred_load_dtype(self):
    # 在现有 fp16/bf16/int8/int4 基础上新增
    if precision == "fp8":
        return torch.float8_e4m3fn  # Blackwell 原生 FP8
```

### 4.2 Qwen3-VL-8B — 对话/视觉理解主力

| 指标 | 值 |
|------|-----|
| 参数量 | 8B |
| INT4 显存 | ~4.5 GB |
| 上下文长度 | 256K |
| 视觉基准分 | 0.773（开源 Instruct VLM 第一） |

Qwen3-VL-8B 在反直觉视觉场景测试中得分 0.773，为开源 Instruct VLM 第一。INT4 量化后仅 4.5GB，与 BGE-M3 共常驻仅 6.5GB，留 9.5GB 给生成任务。256K 上下文覆盖整部漫画剧本。

**现有代码已部分支持**：`dialog_engine.py` 第 48-54 行 `DIALOG_MODEL_CANDIDATES` 已有 `qwen3-vl-8b` 条目，但条件为 GPU tier >= 12GB VRAM。由于 RTX 5070 Ti 被误判为 rtx3060 档，8B 永不入选。**修复硬件分级表即可解锁。**

**代码修改 — backend/data/models.py：**

```python
# HARDWARE_TIER_TABLE 新增 rtx5070ti 档位（第 113-164 行之间）
"rtx5070ti": {
    "label": "RTX 5070 Ti 16GB",
    "name_patterns": ["rtx 5070 ti", "5070 ti", "5070ti"],
    "min_vram_gb": 15,
    "dialog_model": "qwen3-vl-8b",
    "paint_model": "flux2-klein-4b",
    "video_model": "hunyuan-video-1.5",
    "learn_tabs": 4,
},

# 同时在 detect_hardware_tier() 的 VRAM 回退顺序中加入：
# ("rtx5090", "rtx5070ti", "rtx4090", "rtx3060", "rx6600")
# 16GB VRAM -> rtx5070ti（而非 rtx3060）
```

### 4.3 HunyuanVideo 1.5 — 视频生成主力

| 指标 | 值 |
|------|-----|
| 参数量 | 8.3B |
| FP8+offload 显存 | ~14 GB |
| 输出分辨率 | 720p |
| 磁盘占用 | ~16 GB |

HunyuanVideo 1.5 官方最低 14GB 显存（开启 model offloading），支持 T2V + I2V，720p 输出。使用 SSTA（选择性滑动分块注意力）+ FP8 量化，16GB 卡刚好可跑。完全替代当前 Ken Burns 推拉帧降级方案。

**视频引擎已内置支持**：`video_engine.py` 第 102 行 `_VIDEO_PIPELINE_CLASSES` 已包含 `HunyuanVideoPipeline` 和 `HunyuanVideoImageToVideoPipeline`。只需下载模型到正确路径，引擎的 `discover_video_models()` 会自动发现并加载。**零代码修改即可激活。**

**唯一需要的修改 — VIDEO_ROUTING_TABLE：**

```python
# backend/data/models.py 第 79 行
# 新增 VideoModel 枚举值 + 路由表条目
class VideoModel(str, Enum):
    # ... 现有 ...
    HUNYUAN_VIDEO_15 = "hunyuan-video-1.5"  # 新增

VIDEO_ROUTING_TABLE = [
    # ... 现有 24GB->LTX2 ...
    {"min_vram_gb": 14, "model": VideoModel.HUNYUAN_VIDEO_15},  # 新增 14GB->HunyuanVideo
    {"min_vram_gb": 8,  "model": VideoModel.WAN21_1_3B},         # 8GB->Wan2.1-1.3B（降级兜底）
    # ...
]
```

---

## P1 功能补全

### 5.1 Hunyuan3D 2.1 — 3D 生成（PBR 纹理）

- 低显存路径：~13 GB
- 输出：Mesh + PBR 物理纹理
- 磁盘占用：~8 GB

Hunyuan3D 2.1 的 PBR 纹理通道在 16GB 卡上可跑（低显存路径约 13GB），输出带物理纹理的电影级 3D 模型。几何生成仅需 6GB，可与常驻层共存。

**代码修改 — 新建 backend/services/inference/hunyuan3d_engine.py：**

```python
# 参照现有 triposr_engine.py 结构
from diffusers import Hunyuan3DDiTFlowMatchingPipeline

class Hunyuan3DEngine:
    def load_model(self, model_path: str, device: str = "cuda"):
        self._pipe = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
            model_path, torch_dtype=torch.float16
        )
        self._pipe.to(device)

    def generate_3d(self, image, out_dir):
        mesh = self._pipe(image=image, num_inference_steps=30)[0]
        mesh_path = os.path.join(out_dir, "model.glb")
        mesh.export(mesh_path)
        return mesh_path
```

同时修改 `backend/api/manga.py` 的 `director_text_to_3d()`（第 3115 行），优先使用 Hunyuan3D，TripoSR 兜底。

### 5.2 CosyVoice2-0.5B + F5-TTS — 双 TTS 引擎

CosyVoice2 作为常驻 TTS（4GB），F5-TTS 作为按需克隆专家（4GB）。两者互补：CosyVoice2 擅长日常合成和跨语言，F5-TTS 擅长零样本克隆。

**GPT-SoVITS 零成本激活**：2.6GB 模型已随包但 voice_engine 零接线。只需安装 `pypinyin` 包并取消 `_load_sovits()` 的 gate（第 617 行），即可激活第三条 TTS 路径。

**代码修改 — voice_engine.py：**

```python
# 1. _VOICE_NAME_HINTS 新增 cosyvoice2 / f5-tts 识别（第 88 行）
_VOICE_NAME_HINTS = {
    # ... 现有 ...
    "cosyvoice2": "tts_cosyvoice2",
    "f5-tts": "tts_f5",
    "f5tts": "tts_f5",
}

# 2. 新增 F5-TTS 加载方法
def _load_f5_tts(self, model_path: str, device: str = "cuda"):
    from f5_tts.api import F5TTS
    self._f5_model = F5TTS(ckpt_path=model_path, device=device)
    return True

# 3. _load_sovits() 取消 gate（第 617 行）
# 将 return False 改为实际加载逻辑
def _load_sovits(self, model_path: str, device: str = "cuda"):
    # 删除 "return False" gate
    # 安装 pypinyin 后执行实际加载
    from tts import GPT_SoVITS
    # ...
```

### 5.3 BGE-M3 — RAG 向量检索升级

| 指标 | 值 |
|------|-----|
| 参数量 | 568M |
| 向量维度 | 1024 |
| 支持语言 | 100+ |
| 最大 token | 8192 |

BGE-M3 支持稠密+稀疏+多向量三路混合检索，100+ 语言含中文，8192 token 长文档。替代当前 all-MiniLM-L6-v2（384 维、512 token），RAG 检索质量大幅提升。仅 2GB 显存，可直接常驻。

**代码修改 — 更新 models/models_manifest.json：**

```json
"bge-m3": {
    "name": "BGE-M3",
    "description": "多语言混合检索嵌入模型，1024维，100+语言",
    "type": "embedding",
    "source": "local",
    "path": "embed/bge-m3",
    "size_gb": 2.0,
    "min_ram_gb": 4,
    "min_vram_gb": 2,
    "required": false,
    "fallback": "keyword_search",
    "capabilities": ["embedding", "dense_retrieval", "sparse_retrieval", "multi_vector"],
    "resident": true
}
```

---

## P2 增强能力

### 6.1 FLUX ControlNet-Union — 风格/姿态控制

FLUX.1-ControlNet-Union 支持同时输入 Canny、深度、姿态、软边缘、灰度 5 种条件，一个模型替代多个独立 ControlNet。增量显存仅 ~2GB，与 FLUX.2 Klein 一并加载总占 ~10GB。

**代码修改 — paint_engine.py 新增：**

```python
from diffusers import FluxControlNetPipeline, FluxControlNetModel

def load_controlnet(self, controlnet_path: str):
    self._controlnet = FluxControlNetModel.from_pretrained(
        controlnet_path, torch_dtype=torch.float8_e4m3fn
    )
    self._cn_pipe = FluxControlNetPipeline(
        transformer=self._pipe.transformer,
        controlnet=self._controlnet,
        # ... 共享现有 pipe 组件 ...
    )
```

### 6.2 GPT-SoVITS 补全接线

2.6GB 模型已在磁盘上（`models/gpt-sovits/`），`voice_engine.py` 第 617 行 `_load_sovits()` 是 gate-only（永远返回 False）。安装 `pypinyin` + 取消 gate 即可激活语音克隆能力，**零下载成本**。

### 6.3 Qwen3-VL-8B 高档路由

当用户请求复杂视觉推理（多图对比、OCR 密集场景）时，临时卸载 BGE-M3 + TTS，加载 Qwen3-VL-8B 的 FP8 版本（~8GB），完成后重新加载常驻层。`DIALOG_ROUTING_TABLE` 已有 12GB->8b 条目，修复硬件分级表后自动生效。

---

## P3 FP4 未来路径（cu130 升级）

NVFP4 是 Blackwell Tensor Core 原生支持的 4-bit 浮点格式：

- 显存占用 ~3.5x 缩减 vs FP16
- 推理速度 ~2x 加速 vs FP8
- 精度损失接近 FP8

当前 torch cu128 不支持 NVFP4，需升级到 cu130 torch。

### FP8 vs FP4 显存对比

| 模型 | FP8 显存 | FP4 显存 | 16GB 可跑？ |
|------|---------|---------|-----------|
| Qwen3-VL-8B | 8 GB | ~4 GB | 两者均可 |
| FLUX.2 Klein 9B | 10 GB | ~5 GB | 两者均可 |
| FLUX.2 dev 32B | 32 GB | ~13 GB | 仅 FP4 |
| HunyuanVideo 1.5 | 14 GB | ~7 GB | 两者均可 |
| Hunyuan3D 2.1 | 13 GB | ~6.5 GB | 两者均可 |

升级后 FLUX.2 dev 32B 旗舰模型（FP4 约 13GB）也能塞进 16GB 卡。NVIDIA 已官方支持 FLUX.2 在 RTX 50 系列上的 FP4 推理。

### 升级步骤（未来执行）

1. 安装 cu130 版 torch：`pip install torch --index-url https://download.pytorch.org/whl/cu130`
2. 各引擎新增 FP4 dtype：在 `_preferred_load_dtype()` 新增 `torch.float4_e2m1_fn` 选项
3. 下载 FLUX.2 dev 32B FP4 量化版，替换 Klein 4B 作为旗舰模式

---

## 代码修改清单

| 优先级 | 文件 | 函数/位置 | 改动内容 | 工作量 |
|--------|------|----------|---------|--------|
| P0 | `backend/data/models.py` | `HARDWARE_TIER_TABLE` (L113) | 新增 `rtx5070ti` 档位 + VRAM 回退顺序 | 小 |
| P0 | `backend/data/models.py` | `VideoModel` 枚举 + `VIDEO_ROUTING_TABLE` (L79) | 新增 `hunyuan-video-1.5` + 14GB 路由 | 小 |
| P0 | `backend/services/inference/paint_engine.py` | `PAINT_MODEL_CANDIDATES` (L54) + `load_model()` (L295) | 新增 FLUX.2 Klein 4B + FluxPipeline + FP8 dtype | 中 |
| P0 | `models/models_manifest.json` | 顶层 models 对象 | 新增 flux2-klein-4b / qwen3-vl-8b / hunyuan-video-1.5 条目 | 小 |
| P1 | `backend/services/inference/voice_engine.py` | `_VOICE_NAME_HINTS` (L88) + 新增 `_load_f5_tts()` + `_load_sovits()` (L617) | 新增 F5-TTS 后端 + CosyVoice2 识别 + 解除 SoVITS gate | 中 |
| P1 | 新建 `backend/services/inference/hunyuan3d_engine.py` | 整个文件 | 参照 triposr_engine.py 实现 Hunyuan3D 2.1 引擎 | 中 |
| P1 | `backend/api/manga.py` | `director_text_to_3d()` (L3115) | 新增 Hunyuan3D 引擎选择逻辑（优先 Hunyuan3D，兜底 TripoSR） | 小 |
| P1 | `models/models_manifest.json` | 顶层 models 对象 | 新增 bge-m3 / cosyvoice2 / f5-tts / hunyuan3d-2.1 条目 | 小 |
| P2 | `backend/services/inference/paint_engine.py` | 新增 `load_controlnet()` | FLUX ControlNet-Union 支持 | 中 |
| P2 | `backend/services/inference/dialog_engine.py` | `DIALOG_MODEL_CANDIDATES` (L48) | 确认 qwen3-vl-8b 条目已存在（仅需硬件分级表修复） | 无 |
| P3 | 全部引擎文件 | `_preferred_load_dtype()` | 新增 `torch.float4_e2m1_fn` (NVFP4) 支持 | 中 |

### 代码修改依赖关系

```
1. models.py (硬件分级表 + 路由表)  ←── 最先改
   ├─→ paint_engine.py (FLUX2 Klein + FP8)
   ├─→ dialog_engine.py (8B 自动路由，零改动)
   └─→ video_engine.py (HunyuanVideo，零改动)
         ↓
2. models_manifest.json (更新清单)   ←── 最后改

3. voice_engine.py (F5-TTS + SoVITS) ──→ models_manifest.json
4. hunyuan3d_engine.py (新建) ──→ manga.py (3D 端点) ──→ models_manifest.json
```

---

## 模型下载清单

| 优先级 | 模型 | HuggingFace 仓库 | 存放路径 | 大小 | 说明 |
|--------|------|-----------------|---------|------|------|
| P0 | FLUX.2 Klein 4B | `black-forest-labs/FLUX.2-Klein-4B-diffusers` | `models/paint/flux2-klein-4b/` | ~8 GB | 下载 diffusers 格式（含 model_index.json） |
| P0 | Qwen3-VL-8B-Instruct | `Qwen/Qwen3-VL-8B-Instruct` | `models/qwen3-vl-8b/` | ~16 GB | 完整 safetensors，运行时 INT4 量化 |
| P0 | HunyuanVideo 1.5 | `tencent/HunyuanVideo-1.5` | `models/video_gen/hunyuan-video-1.5/` | ~16 GB | diffusers 格式，含 model_index.json |
| P1 | BGE-M3 | `BAAI/bge-m3` | `models/embed/bge-m3/` | ~2 GB | 含 pytorch_model.bin + config.json |
| P1 | CosyVoice2-0.5B | `FunAudioLLM/CosyVoice2-0.5B` | `models/cosyvoice2/` | ~2 GB | 需安装 cosyvoice 包 |
| P1 | F5-TTS | `SWivid/F5-TTS` | `models/f5-tts/` | ~2 GB | 需安装 f5-tts 包 |
| P1 | Hunyuan3D 2.1 | `tencent/Hunyuan3D-2.1` | `models/3d/hunyuan3d-2.1/` | ~8 GB | 含 shape + texture 模型 |
| P2 | FLUX ControlNet-Union | `InstantX/FLUX.1-dev-Controlnet-Union` | `models/paint/flux-controlnet-union/` | ~3 GB | 适配 FLUX.2 Klein |
| 已有 | GPT-SoVITS | — | `models/gpt-sovits/` | 2.6 GB | 已随包，仅需安装 pypinyin + 解除 gate |
| 已有 | TripoSR | — | `models/3d/TripoSR/` | 1.6 GB | 已随包，保留为 3D 快速兜底 |
| 已有 | SDXL | — | `models/paint/sdxl-base-1.0/` | 6.9 GB | 已随包，降级为 LoRA 训练基座 + 兜底 |

新增模型总计约 57GB。加上现有模型 46.6GB，总计约 104GB。建议优先下载 P0 三项（~40GB），验证通过后再下载 P1。

---

## 部署步骤

### 阶段一：P0 质变升级（预计 2-3 小时）

**步骤 1：修复硬件分级表**

编辑 `backend/data/models.py`，在 `HARDWARE_TIER_TABLE` 新增 `rtx5070ti` 档位，修改 `detect_hardware_tier()` 的 VRAM 回退顺序。

验证：启动后端，`GET /api/v1/hardware/info` 返回 `tier.id == "rtx5070ti"`

**步骤 2：下载 P0 模型**

```bash
# FLUX.2 Klein 4B (~8GB)
huggingface-cli download black-forest-labs/FLUX.2-Klein-4B-diffusers \
  --local-dir models/paint/flux2-klein-4b

# Qwen3-VL-8B-Instruct (~16GB)
huggingface-cli download Qwen/Qwen3-VL-8B-Instruct \
  --local-dir models/qwen3-vl-8b

# HunyuanVideo 1.5 (~16GB)
huggingface-cli download tencent/HunyuanVideo-1.5 \
  --local-dir models/video_gen/hunyuan-video-1.5
```

**步骤 3：修改 paint_engine.py**

新增 FLUX.2 Klein 4B 候选 + FluxPipeline 加载逻辑 + FP8 dtype 支持。参照"代码修改清单"中的代码。

**步骤 4：修改 VIDEO_ROUTING_TABLE**

新增 `hunyuan-video-1.5` 枚举 + 14GB 路由条目。视频引擎已内置 `HunyuanVideoPipeline`，零代码修改即可自动发现和加载。

**步骤 5：更新 models_manifest.json**

新增 flux2-klein-4b / qwen3-vl-8b / hunyuan-video-1.5 三个条目。

**步骤 6：验证 P0**

- [ ] 启动后端，健康检查通过
- [ ] `GET /api/v1/hardware/info` 返回 rtx5070ti 档位
- [ ] `GET /api/v1/models` 列出 qwen3-vl-8b
- [ ] 对话功能正常，VLM 使用 8B 模型
- [ ] 文生图使用 FLUX.2 Klein 4B，4 步出图
- [ ] 视频生成使用 HunyuanVideo 1.5，720p 输出
- [ ] 显存峰值不超过 16GB

### 阶段二：P1 功能补全（预计 2-3 小时）

**步骤 7：下载 P1 模型**

```bash
# BGE-M3 (~2GB)
huggingface-cli download BAAI/bge-m3 --local-dir models/embed/bge-m3

# CosyVoice2-0.5B (~2GB)
huggingface-cli download FunAudioLLM/CosyVoice2-0.5B --local-dir models/cosyvoice2

# F5-TTS (~2GB)
huggingface-cli download SWivid/F5-TTS --local-dir models/f5-tts

# Hunyuan3D 2.1 (~8GB)
huggingface-cli download tencent/Hunyuan3D-2.1 --local-dir models/3d/hunyuan3d-2.1
```

**步骤 8：修改 voice_engine.py**

新增 F5-TTS 后端 + CosyVoice2 识别 + 解除 GPT-SoVITS gate。安装 `pypinyin` 包。

**步骤 9：新建 hunyuan3d_engine.py + 修改 manga.py**

参照 triposr_engine.py 实现 Hunyuan3D 2.1 引擎，在 `director_text_to_3d()` 中优先使用 Hunyuan3D，TripoSR 兜底。

**步骤 10：更新 manifest + 验证 P1**

- [ ] RAG 检索使用 BGE-M3，1024 维向量
- [ ] TTS 使用 CosyVoice2，非 SAPI5 降级
- [ ] 语音克隆使用 F5-TTS 或 GPT-SoVITS
- [ ] 3D 生成使用 Hunyuan3D 2.1，输出 PBR 纹理
- [ ] 全流程显存不超过 16GB

### 阶段三：P2 增强能力（预计 1-2 小时）

**步骤 11：下载 FLUX ControlNet-Union + 修改 paint_engine.py**

新增 ControlNet 加载逻辑，支持 Canny/Depth/Pose 等多条件输入。

### 阶段四：P3 FP4 升级（未来，待 cu130 稳定）

暂不执行。cu130 torch 目前可能尚未稳定发布。建议持续关注 PyTorch 官方动态，待 cu130 稳定后执行 FP4 升级，解锁 FLUX.2 dev 32B 旗舰模型。

---

## 参考资料

1. NVIDIA, NVFP4 Quantization — Blackwell Tensor Core 混合精度执行，FP4 权重+FP16 累加，显存 ~3.5x 缩减。 https://build.nvidia.com/station/nvfp4-quantization
2. FLUX.2 Klein Ultimate Guide — 4B 参数，4 步推理，1024x1024 亚秒级生成。 https://flux2klein.ai/pt/blog/flux-2-klein-ultimate-guide-fastest-ai-image-generator
3. Seeing vs. Believing: Evaluating Language Bias of Open-Source MLLMs — Qwen3-VL-8B-Instruct 得分 0.773，开源 Instruct VLM 第一。 https://arxiv.org/html/2601.07737v2
4. HunyuanVideo 1.5 Local Setup Guide 2026 — 8.3B 参数，最低 14GB VRAM（offloading），720p，SSTA 注意力，FP8 量化。 https://localaimaster.com/blog/hunyuan-video-guide
5. Hunyuan3D 2.1 Complete Installation Guide — 8GB VRAM 几何生成，16GB PBR 纹理通道，低显存路径 ~13GB。 https://github.com/ethanstoner/Hunyuan3D-2.1-Complete-Install-Guide
6. Best Local TTS Models 2026 — CosyVoice2 90%+ 克隆相似度，F5-TTS 流匹配零样本克隆，显存 ~4GB。 https://localaimaster.com/blog/best-local-tts-models
7. BGE-M3 — 568M 参数，1024 维，100+ 语言，稠密+稀疏+多向量三路混合检索，8192 token。 https://bge-model.com/bge/bge_m3.html
8. FLUX.1-ControlNet-Union 配置指南 — 多模态条件输入（Canny/Depth/Pose/SoftEdge/Gray），Diffusers 兼容。 https://blog.csdn.net/weixin_42361478/article/details/155975429
9. NVIDIA Developer Blog, Scaling NVFP4 Inference for FLUX.2 on Blackwell — RTX 50 系列 FP4 推理官方支持，FLUX.2 dev 32B FP4 约 13GB。 https://developer.nvidia.com/blog/scaling-nvfp4-inference-for-flux-2-on-nvidia-blackwell-data-center-gpus/
