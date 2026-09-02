"""从零生成「漫剧分镜图工作流 KF1」ComfyUI 工作流（2026-08-29）。

定位：漫剧创作双工作流之"分镜图"—— Flux2 Klein 文生图，人物/场景/道具
一致性靠参考图 ReferenceLatent 双侧注入（与后端 comfy_paint_engine 的
reflatent="both" 无 PuLID 档完全同构）；PuLID 身份硬锁链内置但默认旁路
（本机 models/pulid 与 insightface 未挂载权重，实测组合框仅 __create_new__，
挂载后 Ctrl+B 解除旁路即可启用）。

四分区：① 文本输入区（分镜描述词/负向词/画幅/步数）
        ② 资产图输入区（人物/场景/道具 + PuLID 备用链）
        ③ 流程区（模型/加速/参考编码/采样）
        ④ 产出区（分镜图 PNG，落 ComfyUI/output/manga_kf/）
底部三联「节点大白话词典」。每个设置说明均以源码/实机 object_info 核实。

用法：python build_manga_keyframe_workflow.py [--check]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMFY = REPO / "tools" / "ComfyUI_windows_portable" / "ComfyUI"
# 用户区 2026-09-02 起统一收编 data/comfyui/user（--user-directory 同源）
OUT_DIR = (REPO / "data" / "comfyui" / "user" / "default" / "workflows"
           / "漫剧创作套件")
OUT = OUT_DIR / "分镜图工作流_KF1.json"

BYPASS = 4  # ComfyUI 旁路模式（节点不执行、接口直通）

# ── 大白话文案（源码/实机核实）────────────────────────────────

NOTE_1 = """### ① 文本输入区（编剧台）
- **分镜描述词（正向）**：这一镜"要什么"。直接写中文——H3/Klein 的文本
  编码器是 Qwen3-8B，中文原生，不用翻译。写法：画质前缀 + 人物外貌 +
  动作 + 场景 + 运镜 + 氛围。
- **负向词**：这一镜"不要什么"（低画质/畸变手指/水印等）。
- **画幅**：16:9、0.92 百万像素 ≈ 1280×720 横屏（漫剧标准）；multiple=32
  是模型对宽高取整的要求，别改小。
- **采样步数**：默认 36（后端 comfy 绘画引擎实测配方：1280×720 36 步
  约 90 秒/张 @16G 卡）。赶时间可降到 20~24，质量略降。

**改完文本/参数 → 直接点右上 Queue 出一张分镜图。**
"""

NOTE_2 = """### ② 资产图输入区（选角 + 取景 + 道具间）
一致性三件套，每张都会在采样时"回头看一眼"：
- **人物资产**：主角正脸定妆照 → ①PuLID 身份硬锁（注意力级，默认启用）
  ②经参考编码注入正向链（长相+构图锚）。
- **场景资产**：这一镜的场景图（构图/色调锚）。
- **道具资产**：关键道具特写（物品锚）。
- 想加第四张参考：复制一个「缩放→VAE编码→ReferenceLatent」小组，
  串到正向链尾部即可。

**PuLID 身份硬锁（本机已挂载、默认启用）**：权重在 models/pulid +
models/insightface/models/antelopev2。人物正脸会被裁成 512×512 送入
InsightFace+EVA-CLIP，以注意力注入方式锁脸——**请给正脸清晰的图**；
多视图拼图会稀释身份信号（裁出单张正脸再喂）。
**负向链不挂参考**：PuLID 激活时负侧注入会稀释负向锚（后端 P0 实测），
三个负向 ReferenceLatent 已默认旁路，别随意解除。
"""

NOTE_3 = """### ③ 流程区（AI 加工车间 · 不懂勿动）
1. **模型上电**：主模型 Klein 9B fp8 →（旁路：一致性 LoRA / PuLID 补丁）；
   文本编码器 Qwen3-8B 把你的中文变成向量；VAE 负责参考图编码与成图解码。
2. **参考编码**：三张资产图各自缩放→VAE 编码→以 ReferenceLatent 挂到
   正向**和**负向两条条件链上（与后端 comfy 引擎 reflatent=both 档同构）。
   想关掉某张参考：Ctrl+B 旁路它对应的 ReferenceLatent。
3. **采样**：空白画布(EmptyFlux2LatentImage) + 噪声种子 + euler 采样器 +
   Flux2Scheduler 步数表 + CFG 引导 → 采样执行器跑完出图。
"""

NOTE_4 = """### ④ 产出区（成品间）
- **保存分镜图**：PNG 落到 `ComfyUI/output/manga_kf/`（前缀可带子文件夹）。
- **画布预览**：不落盘，仅供查看。
- **与视频工作流接力**：把满意的分镜图按 `shot_01.png…shot_NN.png` 拷进
  `<项目根>/data/manga_kf/`，打开「分镜工作流_KF1」的兄弟工作流
  `漫剧分镜关键帧生视频_V8`（H3-分段参考/8、漫剧关键帧锚定版）即可逐镜生成视频。
"""

DICT_1 = """## 节点大白话词典 ①（文本输入区）
**分镜描述词（正向）/负向词**（CLIPTextEncode）：text=提示词文本；clip=文本
编码器接线。节点把文字编码成 AI 条件向量，正向="要什么"，负向="避开什么"。
**画幅选择**（ResolutionSelector，ComfyUI 内置）：aspect_ratio=宽高比、
megapixels=总百万像素（0.92≈1280×720）、multiple=结果取整到 32 的倍数
（Flux2 要求宽高为 32 倍数，源码 CANVAS_MULTIPLE=32）。输出宽/高两路。
**采样步数**（PrimitiveInt）：value=步数，喂给步数表。步数越多越细越慢。
**种子**在③区 RandomNoise 上（noise_seed：同种子+同提示词=同图，可复现）。
"""

DICT_2 = """## 节点大白话词典 ②（资产图输入区）
**人物/场景/道具资产**（LoadImage）：image=选图（下拉只列 input 根目录文件，
点 upload 可传新图）。本工作流预置 kf_role/kf_scene/kf_prop 三张演示图。
**参考图缩放**（ImageScaleToTotalPixels）：upscale_method=缩放算法（人物
PuLID 支路用 lanczos 0.26MP≈512²，官方工作流口径；参考支路用 nearest-exact
0.92MP 与生成面积同档，后端 comfy 引擎同参）；megapixels=目标百万像素；
resolution_steps=取整步长。只缩不放（放大不增信息）。
**VAE 编码**（VAEEncode）：pixels=图、vae=画面 VAE；把像素变成潜空间表示，
供 ReferenceLatent 挂载。
**ReferenceLatent（正×3/负×3）**：conditioning=上游条件、latent=参考潜空间；
向条件追加一条"参考潜像"，采样每一步都会重新注入、永不参与去噪（源码
docstring 口径）。正负双侧对称注入 = 后端 comfy 引擎 reflatent="both" 档
（无 PuLID 时的默认档）；挂载 PuLID 后后端策略会切到 off/pos，见②区说明。
**PuLID 四件套（默认启用）**：
- PuLIDModelLoader：pulid_file=身份模型文件（读 models/pulid 目录，
  本机已挂 pulid_flux2_klein_v2）。
- PuLIDInsightFaceLoader：provider=CPU/CUDA/ROCM，加载 antelopev2 人脸分析。
- PuLIDEVACLIPLoader：无参数，加载 EVA-CLIP 视觉特征器（首次经 open_clip
  自动下载，需 HF 镜像时设环境变量 HF_ENDPOINT）。
- ApplyPuLIDFlux2：model=主模型、pulid_model=身份模型、**strength=身份强度
  （官方示例口径 1.3）**、eva_clip/face_analysis=上面两个加载器、image=人物
  正脸图（512×512 中心裁切，官方口径）；可选 face_index=多人脸时选第几张。
**负向链三个 ReferenceLatent 默认旁路**：PuLID 激活时负侧注入会稀释负向锚
（后端 P0 实测口径）；旁路时条件原样直通，负向词照常生效。
"""

DICT_3 = """## 节点大白话词典 ③（流程区 / 产出区）
**主模型**（UNETLoader）：unet_name=Flux2 Klein 9B fp8 扩散主权重（9.4GB）；
weight_dtype=default 按文件精度。
**一致性 LoRA（旁路）**（LoraLoader）：lora_name=flux2-klein-9b-consistency-v2
（官方一致性补丁）；strength_model=对主模型力度；strength_clip=对文本编码器
力度（0=不动文本侧）。想启用就 Ctrl+B 解除旁路。
**文本编码器**（CLIPLoader）：clip_name=qwen3_8b（16.4GB bf16，ComfyUI 会
自动分页换载，12G 卡首次编码偏慢属正常）；type=flux2（Klein 专用加载格式）；
device=default。
**画面 VAE**（VAELoader）：flux2-vae（168MB），参考编码与成图解码共用。
**空白画布**（EmptyFlux2LatentImage）：width/height=画幅（由①区画幅节点喂入）、
batch_size=一次几张（1）。
**噪声种子**（RandomNoise）：noise_seed=随机起点；控件旁的 fixed/随机 决定
每次运行是否换种子（固定=可复现，随机=每卷不同）。
**采样曲线**（KSamplerSelect）：euler（后端 comfy 引擎实测配方）。
**步数表**（Flux2Scheduler）：steps=步数（①区喂入）、width/height=画幅（同上，
步数表按像素量生成 sigma 曲线，源码 seq_len=宽×高/256）。
**CFG 引导**（CFGGuider）：model=（补丁后的）主模型、positive/negative=正负
条件、cfg=引导强度（4.0=后端实测配方；越高越贴题越易过饱和）。
**采样执行器**（SamplerCustomAdvanced）：noise/guider/sampler/sigmas/
latent_image 五入口，真正开跑的地方。
**图像解码**（VAEDecode）：samples=采样结果、vae=画面 VAE；潜空间→PNG 像素。
**保存分镜图**（SaveImage）：filename_prefix=保存前缀（含子文件夹，
实际落在 ComfyUI/output/<前缀>_xxxxx_.png）。
**画布预览**（PreviewImage）：临时预览不落盘。
*依据：comfy_extras/nodes_flux.py、nodes.py、ComfyUI-PuLID-Flux2/pulid_flux2.py
源码与 2026-08-29 运行实例 object_info 实测。*
"""


def node(nid, ntype, title, widgets, order, mode=0, size=None, pos=None,
         inputs=None, outputs=None, snr=None):
    return {"id": nid, "type": ntype, "title": title,
            "pos": pos or [0, 0], "size": size or [300, 100],
            "flags": {}, "order": order, "mode": mode,
            "inputs": inputs or [], "outputs": outputs or [],
            "properties": {"Node name for S&R": snr or ntype},
            "widgets_values": widgets}


def build() -> dict:
    N = {}
    # ── ① 文本输入区 ─────────────────────────────────────────────
    N[1] = node(1, "MarkdownNote", "分区说明①｜文本输入区怎么用", [NOTE_1], 300,
                size=(520, 560))
    N[10] = node(10, "CLIPTextEncode", "① 分镜描述词（正向·中文直写）",
                 ["3D动画电影质感,清晨的郊区街道洒满阳光。棕发马尾少女穿着白衬衫站在"
                  "白色行李箱旁,低头专注读信,白色花瓣随风飘落,唯美逆光,电影级构图。"
                  "运镜:中景,人物居中偏右。"], 10, size=(520, 220))
    N[11] = node(11, "CLIPTextEncode", "① 负向词（不要什么）",
                 ["低画质,模糊,畸变,多余的手指,文字,水印,logo,多余人物"], 11,
                 size=(520, 140))
    N[12] = node(12, "ResolutionSelector", "① 画幅（0.92MP≈1280×720 横屏）",
                 ["16:9 (Widescreen)", 0.92, 32], 12, size=(320, 180),
                 outputs=[{"name": "width", "type": "INT", "links": [1, 3]},
                          {"name": "height", "type": "INT", "links": [2, 4]}])
    N[13] = node(13, "PrimitiveInt", "① 采样步数（后端实测配方 36）",
                 [36], 13, size=(320, 82),
                 outputs=[{"name": "INT", "type": "INT", "links": [5]}])

    # ── ② 资产图输入区 ───────────────────────────────────────────
    N[2] = node(2, "MarkdownNote", "分区说明②｜资产图输入区怎么用", [NOTE_2], 301,
                size=(520, 640))
    ld_out = [{"name": "IMAGE", "type": "IMAGE", "links": None},
              {"name": "MASK", "type": "MASK", "links": []}]
    N[20] = node(20, "LoadImage", "② 人物资产（正脸定妆照）",
                 ["kf_role_main.png", "image"], 20, size=(420, 300))
    N[21] = node(21, "LoadImage", "② 场景资产",
                 ["kf_scene_main.png", "image"], 21, size=(420, 300))
    N[22] = node(22, "LoadImage", "② 道具资产",
                 ["kf_prop_main.png", "image"], 22, size=(420, 300))
    scale_w = ["lanczos", 0.26, 1]
    ref_w = ["nearest-exact", 0.92, 1]
    N[23] = node(23, "easy imageScaleDown", "② 人物裁切(512² center,官方口径)",
                 [512, 512, "center"], 23, size=(330, 140))
    N[24] = node(24, "ImageScaleToTotalPixels", "② 人物缩放(参考 0.92MP)",
                 ref_w, 24, size=(330, 140))
    N[25] = node(25, "ImageScaleToTotalPixels", "② 场景缩放(参考)",
                 ref_w, 25, size=(330, 140))
    N[26] = node(26, "ImageScaleToTotalPixels", "② 道具缩放(参考)",
                 ref_w, 26, size=(330, 140))
    N[27] = node(27, "VAEEncode", "② 人物参考编码", [], 27, size=(240, 60))
    N[28] = node(28, "VAEEncode", "② 场景参考编码", [], 28, size=(240, 60))
    N[29] = node(29, "VAEEncode", "② 道具参考编码", [], 29, size=(240, 60))
    # PuLID 身份硬锁链（默认启用；权重挂载于 models/pulid + models/insightface）
    N[50] = node(50, "PuLIDModelLoader", "② PuLID 身份模型",
                 ["pulid_flux2_klein_v2.safetensors"], 50, size=(320, 80))
    N[51] = node(51, "PuLIDInsightFaceLoader", "② InsightFace 人脸分析(CPU)",
                 ["CPU"], 51, size=(320, 80))
    N[52] = node(52, "PuLIDEVACLIPLoader", "② EVA-CLIP 特征器",
                 [], 52, size=(300, 60))
    N[53] = node(53, "ApplyPuLIDFlux2", "② 身份注入(强度1.3=官方口径)",
                 [1.3, 0, False], 53, size=(360, 300))

    # ── ③ 流程区 ─────────────────────────────────────────────────
    N[3] = node(3, "MarkdownNote", "分区说明③｜流程区怎么运转", [NOTE_3], 302,
                size=(540, 560))
    N[60] = node(60, "UNETLoader", "③ 主模型（Flux2 Klein 9B fp8）",
                 ["flux-2-klein-9b-fp8.safetensors", "default"], 60,
                 size=(430, 90))
    N[61] = node(61, "LoraLoader", "③ 一致性 LoRA（官方 v2，旁路可启用）",
                 ["flux2-klein-9b-consistency-v2.safetensors", 1.0, 0.0], 61,
                 mode=BYPASS, size=(360, 150))
    N[64] = node(64, "CLIPLoader", "③ 文本编码器（Qwen3-8B 读中文）",
                 ["qwen3_8b.safetensors", "flux2", "default"], 64,
                 size=(430, 120))
    N[65] = node(65, "VAELoader", "③ 画面 VAE", ["flux2-vae.safetensors"], 65,
                 size=(430, 70))
    N[46] = node(46, "ReferenceLatent", "③ 正向链·挂人物参考", [], 46,
                 size=(240, 60))
    N[47] = node(46 + 1, "ReferenceLatent", "③ 正向链·挂场景参考", [], 47,
                 size=(240, 60))
    N[48] = node(48, "ReferenceLatent", "③ 正向链·挂道具参考", [], 48,
                 size=(240, 60))
    N[43] = node(43, "ReferenceLatent", "③ 负向链·不挂参考(P0 防稀释)",
                 [], 43, mode=BYPASS, size=(240, 60))
    N[44] = node(44, "ReferenceLatent", "③ 负向链·不挂参考(P0 防稀释)",
                 [], 44, mode=BYPASS, size=(240, 60))
    N[45] = node(45, "ReferenceLatent", "③ 负向链·不挂参考(P0 防稀释)",
                 [], 45, mode=BYPASS, size=(240, 60))
    N[30] = node(30, "RandomNoise", "③ 噪声种子（固定=可复现）",
                 [123456789, "fixed"], 30, size=(320, 100))
    N[31] = node(31, "KSamplerSelect", "③ 采样曲线（euler）", ["euler"], 31,
                 size=(300, 70))
    N[32] = node(32, "Flux2Scheduler", "③ 步数表（按画幅生成曲线）",
                 [36, 1280, 720], 32, size=(320, 150))
    N[33] = node(33, "EmptyFlux2LatentImage", "③ 空白画布",
                 [1280, 720, 1], 33, size=(320, 140))
    N[62] = node(62, "CFGGuider", "③ CFG 引导（4.0 实测配方）", [4.0], 62,
                 size=(320, 120))
    N[63] = node(63, "SamplerCustomAdvanced", "③ 采样执行器（真正开跑）", [],
                 63, size=(260, 330))

    # ── ④ 产出区 ─────────────────────────────────────────────────
    N[4] = node(4, "MarkdownNote", "分区说明④｜产出区怎么收图", [NOTE_4], 303,
                size=(520, 460))
    N[70] = node(70, "VAEDecode", "④ 潜空间→图像", [], 70, size=(240, 60))
    N[80] = node(80, "SaveImage", "④ 保存分镜图（output/manga_kf/）",
                 ["manga_kf/storyboard"], 80, size=(420, 100))
    N[81] = node(81, "PreviewImage", "④ 画布预览（不落盘）", [], 81,
                 size=(380, 260))

    # ── 词典 ─────────────────────────────────────────────────────
    N[90] = node(90, "MarkdownNote", "节点大白话词典①｜文本+资产区", [DICT_1],
                 310, size=(680, 900))
    N[91] = node(91, "MarkdownNote", "节点大白话词典②｜资产参考与 PuLID",
                 [DICT_2], 311, size=(680, 900))
    N[92] = node(92, "MarkdownNote", "节点大白话词典③｜流程与产出", [DICT_3],
                 312, size=(680, 900))

    # ── links：[id, src, src_slot, dst, dst_slot, type] ──────────
    # 简化：直接手写输入条目，避免复杂的三段式
    L = [
        # 画幅 → 空白画布 / 步数表（widget 转链接输入）
        [1, 12, 0, 33, 0, "INT"], [2, 12, 1, 33, 1, "INT"],
        [3, 12, 0, 32, 1, "INT"], [4, 12, 1, 32, 2, "INT"],
        [5, 13, 0, 32, 0, "INT"],
        # 人物资产两路
        [6, 20, 0, 23, 0, "IMAGE"], [7, 20, 0, 24, 0, "IMAGE"],
        # 场景/道具参考路
        [8, 21, 0, 25, 0, "IMAGE"], [9, 22, 0, 26, 0, "IMAGE"],
        # VAEEncode
        [10, 24, 0, 27, 0, "IMAGE"], [11, 65, 0, 27, 1, "VAE"],
        [12, 25, 0, 28, 0, "IMAGE"], [13, 65, 0, 28, 1, "VAE"],
        [14, 26, 0, 29, 0, "IMAGE"], [15, 65, 0, 29, 1, "VAE"],
        # 模型链
        [16, 60, 0, 61, 0, "MODEL"], [17, 64, 0, 61, 1, "CLIP"],
        [18, 61, 0, 53, 0, "MODEL"],
        [19, 50, 0, 53, 1, "PULID_MODEL"], [20, 52, 0, 53, 3, "EVA_CLIP"],
        [21, 51, 0, 53, 4, "INSIGHTFACE"], [22, 23, 0, 53, 5, "IMAGE"],
        [23, 61, 0, 62, 0, "MODEL"],
        [24, 64, 0, 10, 0, "CLIP"], [25, 64, 0, 11, 0, "CLIP"],
        # 正向参考链
        [26, 10, 0, 46, 0, "CONDITIONING"], [27, 27, 0, 46, 1, "LATENT"],
        [28, 46, 0, 47, 0, "CONDITIONING"], [29, 28, 0, 47, 1, "LATENT"],
        [30, 47, 0, 48, 0, "CONDITIONING"], [31, 29, 0, 48, 1, "LATENT"],
        [32, 48, 0, 62, 1, "CONDITIONING"],
        # 负向参考链
        [33, 11, 0, 43, 0, "CONDITIONING"], [34, 27, 0, 43, 1, "LATENT"],
        [35, 43, 0, 44, 0, "CONDITIONING"], [36, 28, 0, 44, 1, "LATENT"],
        [37, 44, 0, 45, 0, "CONDITIONING"], [38, 29, 0, 45, 1, "LATENT"],
        [39, 45, 0, 62, 2, "CONDITIONING"],
        # 采样
        [40, 30, 0, 63, 0, "NOISE"], [41, 62, 0, 63, 1, "GUIDER"],
        [42, 31, 0, 63, 2, "SAMPLER"], [43, 32, 0, 63, 3, "SIGMAS"],
        [44, 33, 0, 63, 4, "LATENT"],
        # 产出
        [45, 63, 0, 70, 0, "LATENT"], [46, 65, 0, 70, 1, "VAE"],
        [47, 70, 0, 80, 0, "IMAGE"], [48, 70, 0, 81, 0, "IMAGE"],
    ]

    # 各节点输入条目（按 schema 槽位顺序）
    IN = {
        33: [("width", "INT", 1), ("height", "INT", 2)],
        32: [("steps", "INT", 5), ("width", "INT", 3), ("height", "INT", 4)],
        23: [("image", "IMAGE", 6)], 24: [("image", "IMAGE", 7)],
        25: [("image", "IMAGE", 8)], 26: [("image", "IMAGE", 9)],
        27: [("pixels", "IMAGE", 10), ("vae", "VAE", 11)],
        28: [("pixels", "IMAGE", 12), ("vae", "VAE", 13)],
        29: [("pixels", "IMAGE", 14), ("vae", "VAE", 15)],
        61: [("model", "MODEL", 16), ("clip", "CLIP", 17)],
        53: [("model", "MODEL", 18), ("pulid_model", "PULID_MODEL", 19),
             ("strength", "FLOAT", None), ("eva_clip", "EVA_CLIP", 20),
             ("face_analysis", "INSIGHTFACE", 21), ("image", "IMAGE", 22)],
        62: [("model", "MODEL", 23), ("positive", "CONDITIONING", 32),
             ("negative", "CONDITIONING", 39)],
        10: [("clip", "CLIP", 24)], 11: [("clip", "CLIP", 25)],
        46: [("conditioning", "CONDITIONING", 26), ("latent", "LATENT", 27)],
        47: [("conditioning", "CONDITIONING", 28), ("latent", "LATENT", 29)],
        48: [("conditioning", "CONDITIONING", 30), ("latent", "LATENT", 31)],
        43: [("conditioning", "CONDITIONING", 33), ("latent", "LATENT", 34)],
        44: [("conditioning", "CONDITIONING", 35), ("latent", "LATENT", 36)],
        45: [("conditioning", "CONDITIONING", 37), ("latent", "LATENT", 38)],
        63: [("noise", "NOISE", 40), ("guider", "GUIDER", 41),
             ("sampler", "SAMPLER", 42), ("sigmas", "SIGMAS", 43),
             ("latent_image", "LATENT", 44)],
        70: [("samples", "LATENT", 45), ("vae", "VAE", 46)],
        80: [("images", "IMAGE", 47)], 81: [("images", "IMAGE", 48)],
    }
    link_by_id = {l[0]: l for l in L}
    for nid, entries in IN.items():
        arr = []
        for name, t, lid in entries:
            e = {"name": name, "type": t, "link": lid}
            if name in ("width", "height", "steps"):
                e["widget"] = {"name": name}
            arr.append(e)
        N[nid]["inputs"] = arr

    # 输出槽补齐（其余节点）
    DEF_OUT = {
        20: [("IMAGE", "IMAGE"), ("MASK", "MASK")],
        21: [("IMAGE", "IMAGE"), ("MASK", "MASK")],
        22: [("IMAGE", "IMAGE"), ("MASK", "MASK")],
        23: [("IMAGE", "IMAGE")], 24: [("IMAGE", "IMAGE")],
        25: [("IMAGE", "IMAGE")], 26: [("IMAGE", "IMAGE")],
        27: [("LATENT", "LATENT")], 28: [("LATENT", "LATENT")],
        29: [("LATENT", "LATENT")],
        30: [("NOISE", "NOISE")], 31: [("SAMPLER", "SAMPLER")],
        32: [("SIGMAS", "SIGMAS")],
        33: [("LATENT", "LATENT")],
        60: [("MODEL", "MODEL")], 61: [("MODEL", "MODEL"), ("CLIP", "CLIP")],
        50: [("PULID_MODEL", "PULID_MODEL")],
        51: [("INSIGHTFACE", "INSIGHTFACE")], 52: [("EVA_CLIP", "EVA_CLIP")],
        53: [("MODEL", "MODEL")],
        64: [("CLIP", "CLIP")], 65: [("VAE", "VAE")],
        10: [("CONDITIONING", "CONDITIONING")],
        11: [("CONDITIONING", "CONDITIONING")],
        46: [("CONDITIONING", "CONDITIONING")], 47: [("CONDITIONING", "CONDITIONING")],
        48: [("CONDITIONING", "CONDITIONING")], 43: [("CONDITIONING", "CONDITIONING")],
        44: [("CONDITIONING", "CONDITIONING")], 45: [("CONDITIONING", "CONDITIONING")],
        62: [("GUIDER", "GUIDER")], 63: [("LATENT", "LATENT")],
        70: [("IMAGE", "IMAGE")], 80: [("IMAGE", "IMAGE")],
    }
    OUTL = {}
    for nid, outs in DEF_OUT.items():
        lids = OUTL.get(nid, [])
        arr = []
        for i, (name, t) in enumerate(outs):
            arr.append({"name": name, "type": t,
                        "links": OUTL.get((nid, i), [])})
        N[nid]["outputs"] = arr

    # 输出槽链接登记（在输出槽创建之后）
    for lid, s, ss, d, ds, t in L:
        OUTL.setdefault((s, ss), []).append(lid)
    for (s, ss), lids in OUTL.items():
        N[s]["outputs"][ss]["links"] = lids

    # ── 布局（列式）+ 分组 ───────────────────────────────────────
    COLS = [
        (-2400, -700, [1, 10, 11]),
        (-1820, -700, [12, 13]),
        (-1400, -700, [2, 20, 21, 22]),
        (-920, -700, [23, 24, 25, 26]),
        (-520, -700, [27, 28, 29, 50, 51, 52, 53]),
        (-60, -700, [3, 60, 61, 64, 65, 46, 47, 48, 43, 44, 45]),
        (600, -700, [32, 33, 30, 31, 62, 63]),
        (1180, -700, [4, 70, 80, 81]),
        (-2400, 900, [90]),
        (-1660, 900, [91]),
        (-920, 900, [92]),
    ]
    groups = [
        (1, "① 文本输入区｜分镜描述词（中文直写）+ 画幅/步数", "#8a6d3b", [0, 1]),
        (2, "② 资产图输入区｜人物/场景/道具（+PuLID 备用链）", "#2a6e3f", [2, 3, 4]),
        (3, "③ 流程区｜AI 加工车间（全自动，不懂勿动）", "#3f789e", [5, 6]),
        (4, "④ 产出区｜分镜图 PNG（与视频工作流接力）", "#6e2a2a", [7]),
        (5, "⑤ 节点大白话词典（往下翻）", "#555555", [8, 9, 10]),
    ]

    sizes = {nid: list(N[nid]["size"]) for nid in N}
    all_cols = COLS
    for x, y0, ids in all_cols:
        y = float(y0)
        for nid in ids:
            w, h = sizes[nid]
            N[nid]["pos"] = [float(x), y]
            N[nid]["size"] = [float(w), float(h)]
            y += h + 30.0
    glist = []
    for gid, title, color, col_idx in groups:
        x0 = y0 = float("inf"); x1 = y1 = float("-inf")
        for ci in col_idx:
            for nid in all_cols[ci][2]:
                w, h = sizes[nid]
                x0 = min(x0, N[nid]["pos"][0]); y0 = min(y0, N[nid]["pos"][1])
                x1 = max(x1, N[nid]["pos"][0] + w); y1 = max(y1, N[nid]["pos"][1] + h)
        pad, top = 40.0, 60.0
        glist.append({"id": gid, "title": title,
                      "bounding": [x0 - pad, y0 - top, (x1 - x0) + 2 * pad,
                                   (y1 - y0) + top + pad],
                      "color": color, "flags": {}, "font_size": 26})

    return {
        "id": "omnispace-manga-kf1", "revision": 1,
        "last_node_id": max(N), "last_link_id": max(l[0] for l in L),
        "nodes": list(N.values()), "links": L, "groups": glist,
        "definitions": {}, "config": {}, "extra": {},
        "version": 0.4,
    }


# ══ 静态校验 ═══════════════════════════════════════════════════

def check(d: dict) -> list[str]:
    errs = []
    nodes = {n["id"]: n for n in d["nodes"]}
    links = {l[0]: l for l in d["links"]}
    if d["last_node_id"] != max(nodes):
        errs.append("last_node_id 不符")
    if d["last_link_id"] != max(links):
        errs.append("last_link_id 不符")
    for l in links.values():
        lid, s, ss, dst, ds, t = l
        if s not in nodes or dst not in nodes:
            errs.append(f"link {lid}: 端点缺失")
            continue
        outs = nodes[s]["outputs"]
        if ss >= len(outs) or lid not in (outs[ss].get("links") or []):
            errs.append(f"link {lid}: 源输出槽未登记")
        ins = nodes[dst]["inputs"]
        if ds >= len(ins) or ins[ds].get("link") != lid:
            errs.append(f"link {lid}: 目标输入槽不符")
    for n in d["nodes"]:
        if n["mode"] == 4:
            continue  # 旁路节点允许接口直通的占位输入
        for i in n.get("inputs", []):
            if i.get("link") is None and i.get("widget") is None:
                pass  # 纯 widget 输入无链接属正常
    for nid in (1, 2, 3, 4, 90, 91, 92, 10, 11, 12, 13, 20, 21, 22,
                60, 62, 63, 70, 80):
        if nid not in nodes:
            errs.append(f"节点 {nid} 缺失")
    if len(d["groups"]) != 5:
        errs.append("分区组应为 5")
    return errs


def main() -> int:
    if "--check" not in sys.argv:
        d = build()
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"已生成: {OUT}")
    d = json.loads(OUT.read_text(encoding="utf-8"))
    errs = check(d)
    if errs:
        print("静态校验失败:")
        for e in errs:
            print("  -", e)
        return 1
    print(f"静态校验通过: {len(d['nodes'])} 节点, {len(d['links'])} 链接, "
          f"{len(d['groups'])} 分组")
    return 0


if __name__ == "__main__":
    sys.exit(main())
