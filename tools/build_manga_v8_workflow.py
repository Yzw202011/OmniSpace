"""生成「漫剧分镜关键帧生视频 V8」ComfyUI 工作流（2026-08-29，分区导览版）。

骨架：H3-分段参考/6、V6_latent传递保证段间一致性/Impact_V6_单次采样.json
（UP主南极来の企鹅与 Theodo­re 绘制的 V6 单采版，循环队列/17k+5 对齐/
MotionContext 潜空间接力/Turbo 加速链全部保留）。

本版在 V8 锚定版基础上做「分区 + 大白话导览」重构：
  四分区（左侧输入 → 右侧产出）：
    ① 文本输入区  —— 12 镜分镜提示词 + 镜数/时长表/运行名/种子 + 按镜号取词
    ② 资产图输入区 —— 关键帧目录（逐镜）+ 资产目录（角色/场景常驻）+ 备用槽
    ③ 流程区      —— 模型加载/加速、参考生视频主引擎、镜间潜空间接力、自动循环
    ④ 产出区      —— 逐镜 MP4、尾帧提取/预览/存档
  每区一块 MarkdownNote 大白话说明（节点 405-408）；
  画布底部四联「节点大白话词典」（409-412），逐节点逐控件解释，
  全部依据节点源码与实机接线核实（防幻觉），包括：
    - MiniMaxH3ReferenceToVideo（comfy_extras/nodes_minimax_h3.py，<Picture i> 标签语义）
    - TurboSampler/TurboLoRA（comfyui-minimax-h3-turbo，bypass/merge 双模式）
    - MotionContext 家族（ComfyUI-MiniMaxH3-Contex-Loop，17 网格/40Hz 音频网格）
    - Impact 循环四件套（ComfyUI-Impact-Pack，signal/value 槽位差异已核实）
    - KJNodes 二开件（PathchSageAttentionKJ 拼写系原版如此/MiniMaxLowVRAMAttention/SaveImageKJ/ImagePass）
    - VHS_LoadImagesPath、ResolutionSelector（comfy_extras/nodes_resolution.py）及核心件

用法：
  python build_manga_v8_workflow.py            # 生成 + 静态校验
  python build_manga_v8_workflow.py --check    # 仅静态校验已生成文件
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMFY = REPO / "tools" / "ComfyUI_windows_portable" / "ComfyUI"
SRC = (COMFY / "user/default/workflows/H3-分段参考"
       / "6、V6_latent传递保证段间一致性" / "Impact_V6_单次采样.json")
OUT_DIR = (COMFY / "user/default/workflows/H3-分段参考"
           / "8、漫剧关键帧锚定版")
OUT = OUT_DIR / "漫剧分镜关键帧生视频_V8.json"

UNET = "MiniMax_H3_Ref2VA_pruned_nvfp4.safetensors"
KF_DIR = "E:/OmniSpace/data/manga_kf"
ASSET_DIR = "E:/OmniSpace/data/manga_asset"

# ══ 大白话文案（依据源码/实机接线核实，防幻觉）══════════════════

NOTE_MAIN = """## 漫剧分镜关键帧生视频 V8（OmniSpace 漫剧模块联动版 · 分区导览版）
### 骨架：Impact V6 单次采样（B站UP南极来の企鹅与抖音博主Theodore绘制，仅允许非商业行为以及传播）

**看图顺序：左 → 右。① 文本输入区写分镜词 → ② 资产图输入区放关键帧/角色/场景 → ③ 流程区自动加工（勿动）→ ④ 产出区收成片。画布最底下有「节点大白话词典」，每个节点每个旋钮什么意思都在那里。**

### 与 OmniSpace 漫剧模块的联动
1. **关键帧**：漫剧模块每镜出图后，把各镜关键帧按 `shot_01.png`…`shot_12.png`
   拷入 `E:/OmniSpace/data/manga_kf/`（后端原图在 `data/keyframes/<关键帧ID>/`）。
   文件名排序 = 镜序，第 1 镜 = index 0。
2. **提示词**：从漫剧分镜表逐镜粘贴完整提示词（画面/运镜/台词/环境音）；
   H3 的文本编码器（Qwen3-VL）原生读中文，**不用翻译成英文**。
3. **角色/场景资产**：放到 `E:/OmniSpace/data/manga_asset/`（第 1 个文件=主角槽、
   第 2 个=场景槽；可在②区两个「取资产」节点上改序号）。
4. **时长表**：逐镜秒数（逗号分隔，条数=镜数），帧数自动对齐 17k+5 网格。

### 成片在哪
`ComfyUI/output/MiniMaxH3_segments/<运行名称>/segment_1..N.mp4`（带音轨）；
尾帧图在同级 `tail_frames/`；镜间接力存档在同级 `latent_context/`。
（注意：④区 SaveVideo 框里显示的路径会被路径拼接节点覆盖，以上面的为准。）

### 硬件口径（RTX 5070 Ti 16GB 实测）
加速链=Turbo LoRA(v4-600 EMA)+SageAttention+LowVRAMAttention(4)；
1344×768 单镜 5s（125帧）约 6–9 分钟；>10s（243帧）显存擦线勿超。
"""

NOTE_Z1 = """### ① 文本输入区（编剧台）
**干什么**：每一格 = 一镜的完整分镜描述。AI 按镜号从这里取词。

**怎么写**：直接写中文，例如——
`清晨的老巷(<Picture 3>)，少女(<Picture 1>)穿着白裙(<Picture 2>)回眸，镜头缓推。台词:"你终于回来了。" 环境音:鸟鸣与风。`
- `<Picture 1>`=本镜关键帧（②区第1槽）、`<Picture 2>`=主角（第2槽）、`<Picture 3>`=场景（第3槽）。标签序号对应参考图接入顺序，可不写标签纯文字描述。
- 台词直接写"台词:"…"''；H3 会生成对口型的人声与音效。

**其他旋钮**：镜数（=提示词条数=时长表条数，三者必须一致）；时长表（每镜秒数，
逗号分隔）；运行名称（本轮成片文件夹名，每次改）；基础随机种子（固定=可复现，
换=换卷重拍）。改完点右上 Queue，队列会自动逐镜推进。
"""

NOTE_Z2 = """### ② 资产图输入区（选角+取景+道具间）
**干什么**：给 AI 看的"定妆照"，保证每镜人物长一个样。

- **漫剧关键帧目录**：每镜第一张参考（构图/景别锚）。漫剧模块出图后按
  `shot_01.png…shot_12.png` 放进 `E:/OmniSpace/data/manga_kf/`，文件名排序=镜序。
- **漫剧资产目录**：`E:/OmniSpace/data/manga_asset/` 里**第 1 张=主角**（进参考槽2）、
  **第 2 张=场景**（进参考槽3）。想换就替换文件，或在右侧两个「取资产」节点上改序号。
- **备用参考槽③~⑧**：默认空。想加道具、配角、服装时，自己加 LoadImage/目录节点
  接到主引擎对应的空槽上（槽序=<Picture 序号>）。

**大白话**：参考图=给 AI 的"照片记忆"，每一步采样都会回头看一眼，所以人物才不跑脸。
"""

NOTE_Z3 = """### ③ 流程区（AI 加工车间 · 不懂勿动）
**流水线（全自动）**：
1. **模型上电**：主模型(UNET)+文本编码器(读你的中文)+画面/声音解码器(VAE)，
   串三个提速/省显存补丁（Turbo LoRA→SageAttention→分组注意力）。
2. **打包条件**：主引擎把"本镜文字+关键帧+角色+场景"打包成条件，并生成空白音画潜空间。
3. **采样**：4 步快采出本镜画面+声音（种子=基础种子+镜号，可复现）。
4. **镜间接力**：第 2 镜起自动读取上一镜的音画存档（22帧画面+24帧音频），
   人物/场景不跳变；成片头部重复上下文会被自动裁掉并校准音画。
5. **自动循环**：校验通过→放行本镜→存档→尾帧落盘→镜号+1→还有下一镜就自动排单；
   全部完成镜号归零。

**出问题先看**：红色报错节点=模型/文件缺失；第 2 镜报"找不到文件"=上一镜没跑成功。
"""

NOTE_Z4 = """### ④ 产出区（成品间）
- **逐镜保存 MP4**：每镜一个带音轨的成片 →
  `ComfyUI/output/MiniMaxH3_segments/<运行名称>/segment_N.mp4`
- **尾帧三件套**：从刚存的成片里拆出最后一帧 → 画布内预览（不落盘）→ 存档到
  `tail_frames/`（下一镜接力的"接力棒"存档在 `latent_context/`）。

**验收方法**：按运行名进文件夹按序播放 segment_1→N；不满意某一镜，保持种子不动，
只改那一镜的提示词重跑该镜即可。
"""

# —— 底部四联词典（逐节点逐控件；来源=节点源码+实机接线，2026-08-29 核实）——

DICT_1 = """## 节点大白话词典 ①（文本输入区 / 资产图输入区）
**第NN镜·画面+声音提示词**（PrimitiveStringMultiline）：value=这镜的完整描述文字。
H3 文本编码器是 Qwen3-VL（中文原生），无需翻译；`<Picture 序号>` 指代②区接入的参考图。
**镜数**（PrimitiveInt）：value=总镜数。校验子图会强制 提示词条数=时长条数=镜数。
**各镜时长表**（PrimitiveStringMultiline）：每镜秒数（支持小数），英文逗号分隔；
校验子图按镜号取出当前镜的秒数交给帧数换算节点。
**运行名称**（PrimitiveString）：本轮子文件夹名。**基础随机种子**（PrimitiveInt）：
噪声起点；同种子+同输入=同画面（可复现），这是"只重跑某一镜"的基础。

**打包12镜提示词**（JoinStringMulti，Easy-Use）：inputcount=接入条数(12)、
delimiter=条间分隔符（空=不加）、return_list=开 → 输出**列表**而不是拼接串，
列表才能"按镜号挑一条"。
**按镜号选择提示词**（easy indexAnything）：any=候选列表、index=镜号（0=第一镜，
来自循环控制器）。它对列表按下标取元素；对成组图片则按张切片（②区同款用法）。

**漫剧关键帧目录/漫剧资产目录**（VHS_LoadImagesPath，VideoHelperSuite）：
directory=文件夹绝对路径（按文件名排序读入，排序即顺序）；image_load_cap=最多读几张
（0=不限）；skip_first_images=跳过前几张；select_every_nth=每隔几张取 1 张（1=全要）；
frame_count 输出=实际读到的张数。目录为空会在开跑时明确报"没有图片"。
**按镜号选取关键帧 / 取主角·取场景**（easy indexAnything）：从上面读入的图片组里
按序号取 1 张；角色固定序号 0、场景固定序号 1（可在控件上改）。
**备用参考槽③~⑧**（ImagePass，KJNodes）：直通节点（输入什么原样输出什么），
这里当"空插座"用——主引擎 9 个参考槽的 3~8 号留空待接；不接不影响运行。
"""

DICT_2 = """## 节点大白话词典 ②（流程区 · 模型与采样）
**主模型**（UNETLoader）：unet_name=扩散主权重（本机选 Ref2VA nvfp4 量化版，
参考生视频专用）；weight_dtype=default=按文件内精度加载。
**文本编码器**（CLIPLoader）：clip_name=Qwen3-VL-32B（把你的中文变成 AI 懂的向量）；
type=minimax=H3 专用加载格式；device=default。
**画面/声音解码器**（VAELoader×2）：分别加载 video_vae(潜空间→画面帧)与
audio_vae(潜空间→音轨)，成片的声音就是它还原的。
**加速补丁**（MiniMaxH3TurboLoRA）：lora_name=官方 4 步快采配重(v4-600 EMA)；
strength=力度(1=全开)；**low_vram 关=bypass 运行时挂载（最锐利，峰值显存略高），
开=merge 折进权重（最省显存，量化底模下画质略软）——爆显存才开**（源码 tooltip 口径）。
**注意力提速**（PathchSageAttentionKJ，节点名拼写系原版如此）：sage_attention=auto
（自动选 sageattention 内核档位，disabled=撤销）；allow_compile=对注意力函数再做
torch.compile（需 sageattention≥2.2，默认关）。
**省显存注意力**（MiniMaxLowVRAMAttention，KJNodes 二开）：head_chunks=把注意力按
头分组计算（4=分4组）。数学结果不变、峰值显存按组数下降；越大越省越慢。

**参考生视频主引擎**（MiniMaxH3ReferenceToVideo）：prompt=当前镜文字（支持
`<Picture i>/<Video k>/<Audio j>` 标签，序号=参考接入顺序，1 起）；width/height=画幅
（由画幅节点喂入，节点内部还会按"短边768、面积≤768×1344"二次适配）；length=帧数
（24fps、必须 17n+5 网格，124≈5 秒，训练域 124~362）；ref_image_size=**match**（参考图
等比缩到与生成面积一致，快）/**max**（短边 2048 保身份细节，慢数倍）；ref_image_0~8=
9 个参考图插座（0=关键帧、1=主角、2=场景）；输出 positive=打包条件、LATENT=本镜
空白音画潜空间。
**画幅选择**（ResolutionSelector，ComfyUI 内置）：aspect_ratio=宽高比(16:9)、
megapixels=总像素(0.4≈1344×768)、multiple=取整步长(32，模型要求宽高是 32 的倍数)。
**时长→帧数换算**（ComfyMathExpression）：expression=公式
`max(5,round(秒×24)) 再向上补到 17n+5`；values.a=校验子图分发的当前镜秒数。
"""

DICT_3 = """## 节点大白话词典 ③（流程区 · 采样与镜间接力）
**噪声种子**（RandomNoise）：noise_seed=基础种子+镜号（由 mathInt 算出）——每镜
不同、整体可复现。**采样曲线**（KSamplerSelect）：res_multistep（官方模板推荐）。
**步数表**（BasicScheduler）：scheduler=simple、steps=6（sigma 表，配合 4 步快采）、
denoise=1（从纯噪声生成，文生视频标准）。**引导器**（BasicGuider）：把模型+条件
接成"每步怎么去噪"的引导。**4步快采**（MiniMaxH3TurboSampler）：官方 Turbo LoRA
配套采样器，本体无参数。**采样执行器**（SamplerCustomAdvanced）：真正开跑的地方，
五个入口 noise/guider/sampler/sigmas/latent_image 全接好后，Queue 一次跑一镜。
**画面/声音还原**（VAEDecode / VAEDecodeAudio）：潜空间→画面帧 / 音轨。

**读取上一镜存档**（MiniMaxH3MotionContextLoadLatent）：latent_path=存档文件夹
（相对 ComfyUI/output），clip_index=读第几号槽（本图接的是当前镜号：第 2 镜读 1 号槽，
一一对应；0=取最新文件，重跑有风险——本图不走这个模式所以安全）。
**镜间上下文**（MiniMaxH3MotionContext）：conditioning=本镜条件、latent=本镜**空白**
潜空间（源码强调：不能接上一镜已采样的潜空间）、context_latent=上一镜存档、
context_length=携带的画面上下文帧（只在 17 帧网格上有效，22=本图设置）、
audio_context_length=音频上下文帧（24，与画面窗口尾对齐）、encode_mode=video（整段
一次 VAE 编码）、anchor_mode=head（上下文钉在头部，成片需裁掉）、crop=disabled。
context_frames/context_audio 是像素域备选入口，**本图未接**（上下文走存档潜空间）。
**裁上下文**（MiniMaxH3MotionContextTrim，Contex-Loop 的 LoopTrim）：images/audio=
本镜解码结果、trim_frames=要裁掉的开头帧数（第一镜 0，后续镜 22）、fps=24、
match_tail=开——H3 音频 latent 是 40Hz、画面 24fps，个别合法长度会有 ±8ms 误差，
它把每镜时长校准成 帧数/24 精确值且不补静音（源码 docstring 口径）。
**保存本镜存档**（MiniMaxH3MotionContextSaveLatent）：latent=采样结果潜空间、
filename_prefix=存档文件夹（自动拼到 运行名/latent_context/）、clip_index=存进第几号
槽（=镜号+1，重跑覆盖自己的废案、不堆文件）。
"""

DICT_4 = """## 节点大白话词典 ④（循环控制 / 校验 / 产出区）
**当前镜序号**（PrimitiveInt）：value=当前第几镜（0 起），由"写回"节点自动改，勿手点。
**校验闸门**（ImpactExecutionOrderController）：纯直通（源码 doit=原样返回），作用是
"前面校验没执行完，后面就不许开始"。两个输出槽不同：**signal=镜数**（来自分段校验，
供循环判断还有没有下一镜）、**value=当前镜号**（分给取词/取帧/种子/存档槽位）。
**是否第一镜**（ComfyMathExpression，a==0）：真→条件直连+裁 0 帧；假→走镜间上下文+裁 22 帧。
**还有下一镜?**（easy compare，a<b）：a=镜号+1、b=镜数。**二选一**（ImpactConditionalBranch）：
cond 真→输出 tt_value、假→ff_value（懒执行：没选中的分支不计算，所以第一镜不会去读
不存在的上一镜存档）。**写回镜号**（ImpactSetWidgetValue）：node_id=目标节点编号+widget_name=
控件名+int_value=新值——每镜结束把"下一镜号"写进镜号框，最后一镜写 0 复位。
**排下一镜**（ImpactQueueTrigger）：mode=真时向 ComfyUI 队列再排一单（signal 触发、
mode 控制开/关）。
**校验子图×3**（可双击展开的折叠子流程）：307「提示词与段落校验」检查提示词列表
完整性；363「防重复校验」检查上一镜存档文件是否存在（isFileExist），防重跑串档；
364「分段合法性校验」强制 镜数=提示词条数=时长条数，并分发"镜数"与"当前镜时长秒数"。
**路径拼装**（SomethingToString 前后缀拼接 / JoinStrings 连接）：自动生成
成片/尾帧/存档的保存路径——纯起名机器，不用管。
**从成片拆帧**（GetVideoComponents）：video=刚保存的成片，拆出画面帧+音轨+帧率。
**取本镜最后一帧**（easy indexAnything，index=-1=倒数第一帧）。**尾帧预览**
（PreviewImage）：画布内看一眼，存临时目录不占成品。**尾帧存档**（SaveImageKJ）：
filename_prefix=保存路径（自动拼）、output_folder=输出根目录、caption_file_extension=
配套 txt 后缀（本图未用 caption）。
**逐镜保存 MP4**（SaveVideo）：filename_prefix=保存路径（被路径拼接节点覆盖为
`MiniMaxH3_segments/<运行名>/segment_N`）、format=auto（默认 mp4 容器）、
codec=auto（默认 h264）。

*词典依据：各节点源码（comfy_extras/nodes_minimax_h3.py、comfyui-minimax-h3-turbo、
ComfyUI-MiniMaxH3-Contex-Loop、ComfyUI-Impact-Pack、ComfyUI-KJNodes、
ComfyUI-VideoHelperSuite）与 2026-08-29 实机逐槽位接线核实。*
"""

SHOT_TITLES = {
    221: "第01镜·画面+声音提示词", 222: "第02镜·画面+声音提示词",
    223: "第03镜·画面+声音提示词", 224: "第04镜·画面+声音提示词",
    225: "第05镜·画面+声音提示词", 226: "第06镜·画面+声音提示词",
    227: "第07镜·画面+声音提示词", 228: "第08镜·画面+声音提示词",
    229: "第09镜·画面+声音提示词", 230: "第10镜·画面+声音提示词",
    231: "第11镜·画面+声音提示词", 232: "第12镜·画面+声音提示词",
}
RETITLE = {
    200: "镜数（=提示词条数=时长表条数）",
    314: "当前镜序号（自动更新，0=第一镜，勿手动递增）",
    328: "最后一镜完成后重置为 0",
    329: "校验闸门（signal=镜数 / value=当前镜号）",
    251: "按镜号选择提示词",
    272: "取本镜最后一帧（-1=倒数第一）",
    341: "各镜时长表（秒，英文逗号分隔，条数=镜数）",
    388: "读取上一镜存档（第2镜起自动）",
    389: "镜间上下文（22帧画面+24帧音频）",
    392: "成片裁掉重复上下文并校准音画",
    393: "保存本镜存档（镜间一致性）",
    394: "存档落盘后才放行尾帧保存",
    92: "逐镜保存 MP4（成片在这，带音轨）",
    115: "画幅选择（0.4MP≈1344×768 横屏）",
    131: "时长→帧数换算（17n+5 网格）",
    136: "参考生视频主引擎（文字+图片→音画）",
    127: "主模型（画面+声音的大脑）",
    128: "文本编码器（读你的中文分镜词）",
    119: "画面解码器 VAE",
    120: "声音解码器 VAE",
    331: "加速补丁（4步快采 LoRA）",
    141: "注意力提速（SageAttention）",
    334: "省显存注意力（分组计算）",
    129: "噪声种子（基础种子+镜号）",
    333: "采样曲线（官方推荐）",
    124: "步数表（配 4 步快采）",
    126: "引导器",
    125: "采样执行器（真正开跑）",
    315: "是否第一镜？",
    324: "还有下一镜？（镜号+1 < 镜数）",
    327: "二选一：下一镜号 或 归零",
    325: "把下一镜号写回镜号框",
    326: "排下一镜（最后一镜自动停）",
    252: "本镜种子 = 基础种子 + 镜号",
    271: "从成片拆出画面+声音",
    330: "尾帧预览（不落盘）",
    240: "打包 12 镜提示词为列表",
    307: "校验子图·提示词完整性",
    363: "校验子图·防重复（查上镜存档）",
    364: "校验子图·镜数/时长合法性",
    280: "成片路径①（文件夹前缀）",
    282: "成片路径②（/segment_+镜号）",
    283: "成片路径拼接",
    316: "尾帧路径①",
    319: "运行名中转",
    320: "尾帧路径拼接",
    386: "存档路径①",
    387: "存档路径拼接",
    396: "尾帧文件名①",
    397: "尾帧文件名拼接",
    398: "尾帧文件名",
    263: "备用参考槽③（道具/配角，空置不影响）",
    264: "备用参考槽④（道具/配角，空置不影响）",
    265: "备用参考槽⑤（道具/配角，空置不影响）",
    266: "备用参考槽⑥（道具/配角，空置不影响）",
    267: "备用参考槽⑦（道具/配角，空置不影响）",
    268: "备用参考槽⑧（道具/配角，空置不影响）",
    **SHOT_TITLES,
}

# ══ 分区布局：列式打包（x 列 + 纵向堆叠，尺寸取自原 JSON/覆盖表）══════

NOTE_SIZES = {
    290: (1000, 560),
    405: (540, 760), 406: (540, 700), 407: (560, 660), 408: (560, 560),
    409: (680, 1300), 410: (680, 1300), 411: (680, 1300), 412: (680, 1300),
}
SIZE_OVERRIDES = {
    **NOTE_SIZES,
    221: (460, 170), 222: (460, 170), 223: (460, 170), 224: (460, 170),
    225: (460, 170), 226: (460, 170), 227: (460, 170), 228: (460, 170),
    229: (460, 170), 230: (460, 170), 231: (460, 170), 232: (460, 170),
    341: (460, 280),
    240: (360, 340), 251: (320, 100),
    400: (430, 170), 402: (430, 170),
    401: (320, 100), 403: (320, 100), 404: (320, 100),
    263: (330, 70), 264: (330, 70), 265: (330, 70),
    266: (330, 70), 267: (330, 70), 268: (330, 70),
    92: (560, 130), 321: (360, 200), 330: (340, 260), 271: (280, 90),
    127: (440, 90), 128: (440, 120), 119: (440, 70), 120: (440, 70),
}

# 每列: (x, y0, [节点id...])；列从上到下按实际尺寸堆叠，间隙 30px
COLUMNS = [
    # ① 文本输入区
    (-3980, -700, [405, 221, 223, 225, 227, 229, 231]),
    (-3460, -700, [222, 224, 226, 228, 230, 232]),
    (-2940, -700, [200, 341, 203, 202, 240, 251]),
    # ② 资产图输入区
    (-2380, -700, [406, 400, 402]),
    (-1890, -700, [401, 403, 404, 263, 264, 265]),
    (-1500, -700, [266, 267, 268]),
    # ③ 流程区
    (-1120, -700, [407, 127, 128, 119, 120, 331, 141, 334]),
    (-540, -700, [136, 115, 131]),
    (0, -700, [129, 333, 123, 124, 126, 125, 390, 391]),
    (470, -700, [388, 389, 392, 393, 394, 413, 414]),
    (950, -700, [314, 329, 315, 324, 327, 325, 326, 328, 252]),
    (1420, -700, [307, 363, 364, 280, 282, 283, 316, 319, 320, 386, 387,
                  396, 397, 398]),
    # ④ 产出区
    (2050, -700, [408, 92, 271, 272, 330, 321]),
    # 总说明（悬浮在最上方）
    (-540, -1560, [290]),
]
# 底部词典带
DICT_COLS = [
    (-3980, 1400, [409]),
    (-3260, 1400, [410]),
    (-2540, 1400, [411]),
    (-1820, 1400, [412]),
]

# 分组定义:（id, 标题, 颜色, 成员列索引）——包围盒按成员实排位置自动计算
# 列索引对应 COLUMNS + DICT_COLS 拼接后的顺序
GROUPS = [
    (1, "① 文本输入区｜分镜描述词（每镜一段，AI 按镜号取词）",
     "#8a6d3b", [0, 1, 2]),
    (2, "② 资产图输入区｜关键帧/主角/场景（定妆照，保证不跑脸）",
     "#2a6e3f", [3, 4, 5]),
    (3, "③ 流程区｜AI 加工车间（全自动，不懂勿动）",
     "#3f789e", [6, 7, 8, 9, 10, 11]),
    (4, "④ 产出区｜成片 MP4 与尾帧",
     "#6e2a2a", [12]),
    (5, "⑤ 节点大白话词典（每个节点、每个旋钮什么意思，往下翻）",
     "#555555", [13, 14, 15, 16]),
]


def _size_of(nid: int, sizes: dict, n) -> tuple[float, float]:
    if nid in SIZE_OVERRIDES:
        return SIZE_OVERRIDES[nid]
    s = sizes.get(nid)
    if s and len(s) == 2 and s[0] > 0:
        return (s[0], s[1])
    return (300, 100)


def layout(d: dict) -> None:
    """全图重排：按 COLUMNS 列式打包；删除旧分组；写入四区+词典分组。"""
    sizes = {n["id"]: list(n.get("size") or []) for n in d["nodes"]}
    nodes = {n["id"]: n for n in d["nodes"]}
    all_cols = COLUMNS + DICT_COLS
    for x, y0, ids in all_cols:
        y = y0
        for nid in ids:
            n = nodes.get(nid)
            if n is None:
                continue
            w, h = _size_of(nid, sizes, n)
            n["pos"] = [float(x), float(y)]
            n["size"] = [float(w), float(h)]
            y += h + 30.0
    # 分组包围盒 = 成员节点实排范围的并集 + 内边距（顶部留标题位）
    new_groups = []
    for gid, title, color, col_idx in GROUPS:
        x0 = y0 = float("inf")
        x1 = y1 = float("-inf")
        for ci in col_idx:
            for nid in all_cols[ci][2]:
                n = nodes.get(nid)
                if n is None:
                    continue
                w, h = _size_of(nid, sizes, n)
                x0 = min(x0, n["pos"][0])
                y0 = min(y0, n["pos"][1])
                x1 = max(x1, n["pos"][0] + w)
                y1 = max(y1, n["pos"][1] + h)
        pad, top = 40.0, 60.0
        new_groups.append({"id": gid, "title": title,
                           "bounding": [x0 - pad, y0 - top,
                                        (x1 - x0) + 2 * pad,
                                        (y1 - y0) + top + pad],
                           "color": color, "flags": {}, "font_size": 26})
    d["groups"] = new_groups


def _note_node(nid: int, title: str, text: str, pos: list[float],
               size: list[float]) -> dict:
    return {"id": nid, "type": "MarkdownNote", "title": title,
            "pos": pos, "size": size, "flags": {}, "order": 300 + nid,
            "mode": 0, "inputs": [], "outputs": [], "properties": {},
            "widgets_values": [text]}


def build() -> dict:
    d = json.loads(SRC.read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in d["nodes"]}

    # ── 1. 权重换本机已装 ────────────────────────────────────────
    nodes[127]["widgets_values"] = [UNET, "default"]

    # ── 2. 删除旧占位：309/261/262 及其 3 条 link ────────────────
    drop_links = {210, 34, 35}
    d["links"] = [l for l in d["links"] if l[0] not in drop_links]
    for nid in (309, 261, 262):
        d["nodes"] = [n for n in d["nodes"] if n["id"] != nid]
        nodes.pop(nid)

    # ── 3. 新节点（400 关键帧目录 / 401 镜号选帧 / 402 资产目录 /
    #     403 取主角 / 404 取场景）─────────────────────────────────
    def base_node(nid, ntype, title, pos, size, order):
        return {"id": nid, "type": ntype, "title": title, "pos": pos,
                "size": size, "flags": {}, "order": order, "mode": 0,
                "properties": {"Node name for S&R": ntype}}

    n400 = base_node(400, "VHS_LoadImagesPath",
                     "漫剧关键帧目录（文件名排序=镜序）",
                     [-2380, -240], [430, 170], 200)
    n400["inputs"] = []
    n400["outputs"] = [
        {"label": "IMAGE", "localized_name": "图像", "name": "IMAGE",
         "type": "IMAGE", "links": [244]},
        {"label": "MASK", "localized_name": "遮罩", "name": "MASK",
         "type": "MASK", "links": []},
        {"label": "frame_count", "localized_name": "帧数", "name": "frame_count",
         "type": "INT", "links": []},
    ]
    n400["widgets_values"] = [KF_DIR, 0, 0, 1]

    n401 = base_node(401, "easy indexAnything",
                     "按镜号选取关键帧（0=第一镜）",
                     [-1890, -240], [320, 100], 201)
    n401["properties"] = json.loads(json.dumps(nodes[251].get("properties") or {}))
    n401["inputs"] = [
        {"label": "any", "localized_name": "输入任何", "name": "any",
         "type": "*", "link": 244},
        {"label": "index", "localized_name": "索引", "name": "index",
         "type": "INT", "widget": {"name": "index"}, "link": 245},
    ]
    n401["outputs"] = [{"label": "out", "localized_name": "输出", "name": "out",
                        "type": "*", "links": [246]}]
    n401["widgets_values"] = [0]

    n402 = base_node(402, "VHS_LoadImagesPath",
                     "漫剧资产目录（第1个=主角，第2个=场景）",
                     [-2380, -40], [430, 170], 202)
    n402["inputs"] = []
    n402["outputs"] = [
        {"label": "IMAGE", "localized_name": "图像", "name": "IMAGE",
         "type": "IMAGE", "links": [249, 250]},
        {"label": "MASK", "localized_name": "遮罩", "name": "MASK",
         "type": "MASK", "links": []},
        {"label": "frame_count", "localized_name": "帧数", "name": "frame_count",
         "type": "INT", "links": []},
    ]
    n402["widgets_values"] = [ASSET_DIR, 0, 0, 1]

    n403 = base_node(403, "easy indexAnything", "取主角角色资产（列表第 1 个）",
                     [-1890, -40], [320, 100], 203)
    n403["properties"] = json.loads(json.dumps(nodes[251].get("properties") or {}))
    n403["inputs"] = [
        {"label": "any", "localized_name": "输入任何", "name": "any",
         "type": "*", "link": 249},
    ]
    n403["outputs"] = [{"label": "out", "localized_name": "输出", "name": "out",
                        "type": "*", "links": [247]}]
    n403["widgets_values"] = [0]

    n404 = base_node(404, "easy indexAnything", "取场景资产（列表第 2 个）",
                     [-1890, 90], [320, 100], 204)
    n404["properties"] = json.loads(json.dumps(nodes[251].get("properties") or {}))
    n404["inputs"] = [
        {"label": "any", "localized_name": "输入任何", "name": "any",
         "type": "*", "link": 250},
    ]
    n404["outputs"] = [{"label": "out", "localized_name": "输出", "name": "out",
                        "type": "*", "links": [248]}]
    n404["widgets_values"] = [1]

    d["nodes"] += [n400, n401, n402, n403, n404]

    # ── 4. 新 link（[id, src, src_slot, dst, dst_slot, type]）─────
    d["links"] += [
        [244, 400, 0, 401, 0, "IMAGE"],
        [245, 329, 1, 401, 1, "INT"],
        [246, 401, 0, 136, 3, "IMAGE"],
        [247, 403, 0, 136, 4, "IMAGE"],
        [248, 404, 0, 136, 5, "IMAGE"],
        [249, 402, 0, 403, 0, "IMAGE"],
        [250, 402, 0, 404, 0, "IMAGE"],
    ]

    for inp in nodes[136]["inputs"]:
        if inp["name"] == "ref_images.ref_image_0":
            inp["link"] = 246
        elif inp["name"] == "ref_images.ref_image_1":
            inp["link"] = 247
        elif inp["name"] == "ref_images.ref_image_2":
            inp["link"] = 248
    nodes[329]["outputs"][1]["links"].append(245)

    # ── 5. 分区大白话注释 + 总说明 + 底部词典 ────────────────────
    notes = [
        (405, "分区说明①｜文本输入区怎么用", NOTE_Z1, [-3980, -700], [540, 760]),
        (406, "分区说明②｜资产图输入区怎么用", NOTE_Z2, [-2380, -700], [540, 700]),
        (407, "分区说明③｜流程区怎么运转", NOTE_Z3, [-1120, -700], [560, 660]),
        (408, "分区说明④｜产出区怎么收片", NOTE_Z4, [2050, -700], [560, 560]),
        (409, "节点大白话词典①｜文本+资产区", DICT_1, [-3980, 1400], [680, 1300]),
        (410, "节点大白话词典②｜模型与采样", DICT_2, [-3260, 1400], [680, 1300]),
        (411, "节点大白话词典③｜镜间接力与采样链", DICT_3, [-2540, 1400], [680, 1300]),
        (412, "节点大白话词典④｜循环控制+产出区", DICT_4, [-1820, 1400], [680, 1300]),
    ]
    for nid, title, text, pos, size in notes:
        d["nodes"].append(_note_node(nid, title, text, pos, size))
    nodes[290]["widgets_values"] = [NOTE_MAIN]

    # ── 5.5 镜长补偿分支：第一镜足秒；接续镜 +22 帧补偿接力上下文 ──
    # （修复:接续镜的 22 帧上下文重复帧原被计入生成预算,成片裁掉后时长少 0.92s）
    n413 = {"id": 413, "type": "ImpactConditionalBranch",
            "title": "镜长分支：第一镜足秒 / 接续镜+22帧",
            "pos": [470.0, 500.0], "size": [310.0, 110.0], "flags": {},
            "order": 204, "mode": 0,
            "inputs": [
                {"name": "tt_value", "type": "*", "link": 251},
                {"name": "ff_value", "type": "*", "link": 255},
                {"name": "cond", "type": "BOOLEAN", "link": 253}],
            "outputs": [{"name": "*", "type": "*", "links": [254]}],
            "properties": {"cnr_id": "comfyui-impact-pack",
                           "Node name for S&R": "ImpactConditionalBranch"},
            "widgets_values": [False]}
    n414 = {"id": 414, "type": "ComfyMathExpression",
            "title": "接续镜帧数 = 足秒帧 + 22(对齐17n+5)",
            "pos": [950.0, 760.0], "size": [320.0, 116.0], "flags": {},
            "order": 205, "mode": 0,
            "inputs": [{"name": "values.a", "type": "INT",
                        "widget": {"name": "values.a"}, "link": 252}],
            "outputs": [{"name": "FLOAT", "type": "FLOAT", "links": []},
                        {"name": "INT", "type": "INT", "links": [255]},
                        {"name": "BOOL", "type": "BOOLEAN", "links": []}],
            "properties": {"Node name for S&R": "ComfyMathExpression"},
            "widgets_values": ["(a + 22) + (5 - ((a + 22) % 17)) % 17"]}
    d["nodes"] += [n413, n414]
    # 136.length 原接 131 → 改接 413 分支；登记新链接
    len_idx = next(i for i, inp in enumerate(nodes[136]["inputs"])
                   if inp["name"] == "length")
    old_lid = nodes[136]["inputs"][len_idx]["link"]
    nodes[136]["inputs"][len_idx]["link"] = 254
    d["links"] = [l for l in d["links"] if l[0] != old_lid]
    for o in nodes[131]["outputs"]:
        if o.get("links") and old_lid in o["links"]:
            o["links"].remove(old_lid)
    d["links"] += [
        [251, 131, 1, 413, 0, "INT"],
        [252, 131, 1, 414, 0, "INT"],
        [253, 315, 2, 413, 2, "BOOLEAN"],
        [254, 413, 0, 136, len_idx, "INT"],
        [255, 414, 1, 413, 1, "INT"],
    ]
    for o in nodes[131]["outputs"]:
        if o.get("name") == "INT":
            o["links"] += [251, 252]
    for o in nodes[315]["outputs"]:
        if o.get("name") == "BOOL":
            o["links"].append(253)

    # ── 6. 改名 + 运行名/前缀 ────────────────────────────────────
    for nid, title in RETITLE.items():
        if nid in nodes:
            nodes[nid]["title"] = title
    nodes[203]["widgets_values"] = ["漫剧试播_001"]
    nodes[92]["widgets_values"] = [
        "MiniMaxH3_segments/漫剧试播_001/segment_1", "auto", "auto"]

    # ── 7. 全图分区布局 ──────────────────────────────────────────
    layout(d)

    d["last_node_id"] = 414
    d["last_link_id"] = 255
    return d


# ══ 静态校验 ═══════════════════════════════════════════════════

KNOWN_CLASSES = {
    "VAEDecodeAudio", "CreateVideo", "ImagePass", "GetVideoComponents",
    "BasicGuider", "easy indexAnything", "SamplerCustomAdvanced",
    "VAEDecode", "RandomNoise", "easy mathInt", "JoinStringMulti",
    "CLIPLoader", "VAELoader", "MiniMaxH3ReferenceToVideo",
    "UNETLoader", "SomethingToString", "ImpactExecutionOrderController",
    "PrimitiveInt", "ImpactQueueTrigger", "easy compare",
    "ImpactConditionalBranch", "ImpactSetWidgetValue", "SaveImageKJ",
    "SaveVideo", "ComfyMathExpression", "PrimitiveStringMultiline",
    "BasicScheduler", "KSamplerSelect",
    "MiniMaxH3TurboSampler", "PathchSageAttentionKJ",
    "MiniMaxH3TurboLoRA", "MiniMaxLowVRAMAttention", "ResolutionSelector",
    "MiniMaxH3MotionContextLoadLatent", "MiniMaxH3MotionContext",
    "MiniMaxH3MotionContextTrim", "MiniMaxH3MotionContextSaveLatent",
    "MarkdownNote", "PrimitiveString", "PreviewImage", "JoinStrings",
    "VHS_LoadImagesPath", "LoadImage",
}


def check(d: dict) -> list[str]:
    errs: list[str] = []
    nodes = {n["id"]: n for n in d["nodes"]}
    links = {l[0]: l for l in d["links"]}

    if len(nodes) != len(d["nodes"]):
        errs.append("节点 id 重复")
    if d["last_node_id"] != max(nodes):
        errs.append(f"last_node_id {d['last_node_id']} != 最大节点 id {max(nodes)}")
    if d["last_link_id"] != max(links):
        errs.append(f"last_link_id {d['last_link_id']} != 最大 link id {max(links)}")

    for l in links.values():
        lid, src, sslot, dst, dslot, typ = l[:6]
        if src not in nodes:
            errs.append(f"link {lid}: 源节点 {src} 不存在")
        elif dst not in nodes:
            errs.append(f"link {lid}: 目标节点 {dst} 不存在")
        else:
            dn = nodes[dst]
            ins = dn.get("inputs", [])
            if dslot >= len(ins) or ins[dslot].get("link") != lid:
                errs.append(f"link {lid}: 目标 {dst} 输入槽 {dslot} 与 entry 不符")
            src_outs = nodes[src].get("outputs", [])
            if sslot >= len(src_outs) or lid not in (
                    src_outs[sslot].get("links") or []):
                errs.append(f"link {lid}: 源 {src} 输出槽 {sslot} 未登记")
            ttyp = ins[dslot].get("type") if dslot < len(ins) else None
            allowed = set((ttyp or "*").split(",")) if ttyp else {"*"}
            if "*" not in allowed and typ not in allowed:
                errs.append(f"link {lid}: 类型不符 {typ} → {ttyp}")

    subgraph_ids = {s.get("id") for s in
                    (d.get("definitions") or {}).get("subgraphs", [])}
    for n in d["nodes"]:
        for i in n.get("inputs", []):
            lid = i.get("link")
            if lid is not None and lid not in links:
                errs.append(f"节点 {n['id']} 输入 {i.get('name')} 悬挂 link {lid}")
        for o in n.get("outputs", []):
            for lid in (o.get("links") or []):
                if lid not in links:
                    errs.append(f"节点 {n['id']} 输出 {o.get('name')} 悬挂 link {lid}")
        if n["type"] not in KNOWN_CLASSES and n["type"] not in subgraph_ids:
            errs.append(f"节点 {n['id']}: 未知识别类 {n['type']}")

    # 新链与分区专属断言
    ref0 = next(i for i in nodes[136]["inputs"]
                if i["name"] == "ref_images.ref_image_0")
    if ref0.get("link") != 246:
        errs.append("ref_image_0 未接到关键帧选择器")
    for nid in (400, 401, 402, 403, 404, 405, 406, 407, 408,
                409, 410, 411, 412):
        if nid not in nodes:
            errs.append(f"锚定/注释节点 {nid} 缺失")
    if any(n["type"] == "LoadImage" for n in d["nodes"]):
        errs.append("不应再含 LoadImage（本快照下拉不递归子目录，恒 has_errors）")
    if len(d.get("groups") or []) != 5:
        errs.append("分区分组应为 5 个")
    for gone in (309, 261, 262):
        if gone in nodes:
            errs.append(f"旧占位节点 {gone} 未删除")
    return errs


def main() -> int:
    if "--check" not in sys.argv:
        d = build()
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        (OUT_DIR / "使用说明.md").write_text(NOTE_MAIN, encoding="utf-8")
        print(f"已生成: {OUT}")
    errs = check(json.loads(OUT.read_text(encoding="utf-8")))
    if errs:
        print("静态校验失败:")
        for e in errs:
            print("  -", e)
        return 1
    dd = json.loads(OUT.read_text(encoding="utf-8"))
    print(f"静态校验通过: {len(dd['nodes'])} 节点, {len(dd['links'])} 链接, "
          f"{len(dd['groups'])} 分组")
    return 0


if __name__ == "__main__":
    sys.exit(main())
