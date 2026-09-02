"""生成「漫剧全能参考链式工作流 V9」ComfyUI 工作流（2026-08-29）。

骨架：ComfyUI-MiniMaxH3-Contex-Loop 官方示例
  example_workflows/Ref2V Basic - MiniMax H3.json（0.5 Chain 系统：
  计划→预检→循环→每镜 ReferenceToVideo+自动上下文→采样→裁剪→
  分段落盘→审查门→拼装成片；节点作者构建，社区"全能参考/万能
  工作流"一脉的官方实现）。

本脚本突变为漫剧版（漫剧创作套件/全能参考链式工作流_V9.json）：
  1. 权重换本机：ref2va int8_convrot + qwen3vl int4_convrot + 双 VAE（扁平名）。
  2. 参考图扩容：Picture 1=人物资产 / 2=场景资产 / 新增 3=道具资产
     （input 根目录 kf_role/scene/prop_main.png）。
  3. 720p+ 画幅：ReferenceToVideo 1344×768（H3 短边 768 上限原生档，交付>720p）。
  4. ≥15s：计划两镜各 8s（length=192 帧，17k+5 网格）→ 自动上下文接力 →
     拼装单条 16s 成片（带音轨）。
  5. Turbo 加速：插 TurboLoRA(v4-600 EMA)+TurboSampler，plan steps=6
     （与 V8 已验证同配方）；ModelAttentionBackend(comfy-kitchen) 默认旁路。
  6. 审查门默认关闭（无人值守）；六段式提示词改为漫剧中文内容
     （键名保留英文解析约定，Qwen3-VL 中文原生）。
  7. 追加中文总说明 + 链节点词典便签；关键节点中文改名。

用法：python build_manga_v9_allref_workflow.py [--check]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMFY = REPO / "tools" / "ComfyUI_windows_portable" / "ComfyUI"
SRC = (COMFY / "custom_nodes/ComfyUI-MiniMaxH3-Contex-Loop/example_workflows"
       / "Ref2V Basic - MiniMax H3.json")
# 用户区 2026-09-02 起统一收编 data/comfyui/user（--user-directory 同源）
OUT_DIR = (REPO / "data" / "comfyui" / "user" / "default" / "workflows"
           / "漫剧创作套件")
OUT = OUT_DIR / "全能参考链式工作流_V9.json"

BYPASS = 4

NOTE_MAIN = """### 漫剧全能参考链式工作流 V9（文字+多图参考 → ≥15s · 1344×768 · 带音轨单成片）
骨架：Contex-Loop 官方 Ref2V Basic（0.5 Chain 系统）。本版改动：权重换本机扁平名；
参考扩为 人物(P1)/场景(P2)/道具(P3)；1344×768；两镜各 8s 拼装 16s 成片；
Turbo 加速(4步配方)；审查门默认关。

### 怎么用（四步）
1. **换资产**：②区三张资产图——人物正脸(P1)/场景(P2)/道具(P3)，在 LoadImage
   下拉里换 input 目录下的图（或上传）。
2. **写分镜**：①「分镜计划」节点里的 JSON——每镜的 prompt(六段式,中文可写)、
   length(帧数,须 17k+5:3s=73/5s=125/8s=192/10s=243)、seed。要更多镜就往 shots
   数组里加。
3. **Queue**：链自动逐镜生成（段间潜空间+音轨自动接续,首镜自动旁路上下文）,
   全部完成后「拼装成片」输出单条 MP4 → `output/manga_v9_allref_<日期>/`。
4. **审查门**：默认关闭（无人值守）。想每镜人工把关：把「审查门」enabled 勾回
   true 并设 auto_continue_timeout 分钟数。

### 六段式提示词（REF2VA 官方格式,英文节名+中文内容）
subject_definitions（各参考图定义,<Picture n> 标签）/ summary（本镜一句话）/
retention_analysis（一致性保持分析）/ detailed_description（画面细节）/
overall_soundscape（环境音）/ non_diegetic_music（配乐,漫剧一般写"无配乐"）。

### 硬件口径（RTX 5070 Ti 16GB 实测）
Turbo 6 步配方;1344×768 单镜 8s(192帧) 约 12-15 分钟,两镜约 25-30 分钟;
每镜帧数别超 192(显存安全线),更长的片子加镜数而不是加单镜时长。
"""

DICT_CHAIN = """## 链节点大白话词典（Contex-Loop 0.5 Chain 系统）
**分镜计划/场景提示词编辑器**（ChainPlan→ScenePromptEditor）：JSON 定义整部片子
——defaults(duration_seconds 每镜秒数 / steps 采样步数 / seed 基种子),shots[]
每镜 {id, prompt(六段式数组), length(帧数,17k+5), seed}。
**预检**（ChainPreflight）：模型加载前把计划先校验一遍（镜数/字段/上下文预算）。
**循环起点**（ChainLoopStart）：start_clip=从第几镜开始（1=整片;断点续跑改这里,
配合「恢复拼装」节点）。**当前镜**（ChainCurrent）：把本镜的 prompt/宽高/帧数/
种子/步数分发给参考引擎和采样器——它是循环的"Curor"。
**全能参考引擎**（MiniMaxH3ReferenceToVideo）：文字+最多 9 张参考图（另有参考
视频×3/参考音频×3 槽位,本图未用）→ 条件+空白音画潜空间;ref_image_size=match
（参考图等比缩到生成面积,快）/ max（短边2048 保细节,慢数倍）。
**自动上下文**（ChainContext）：第 1 镜自动旁路（无历史）;第 2 镜起把上一镜交付段
的尾部画面+音轨作为上下文注入条件与潜空间,并输出 trim_frames（重复帧数）。
**采样四件套**：噪声(种子来自当前镜)→CFG 引导→Turbo 4步采样器→步数表(6)→
采样执行器。**裁剪**（LoopTrim）：按 trim_frames 裁掉头部重复上下文并校准音画。
**分段落盘**（ChainSegmentSave）：本镜成片+接续用段写入 manifest（可断点恢复）。
**审查门**（ChainReview）：enabled=true 时每镜暂停等你审（回退/重摇/继续）,
auto_continue_timeout 分钟后自动继续;unload_models_while_waiting=等待时卸权重。
**循环推进**（ChainLoopEnd）：推进到下一镜;最后一代出 manifest。
**拼装成片**（ChainAssemble）：manifest 里的分段按序拼装（5 帧视觉融合）,
audio_source=plan（用计划口径音轨）,filename 支持日期通配;输出在 output/ 下。
**恢复拼装**（ChainManifestLoad+第二个 Assemble,默认静音）：已跑完的 manifest
重新拼装,不重渲染。**SigmaShift**：H3 采样偏移参数（12,3 官方默认,勿动）。
**注意力后端**（ModelAttentionBackend,默认旁路）：comfy-kitchen 加速包的接口,
未安装该包时必须保持旁路,注意力走 ComfyUI 内置实现。
*依据：ComfyUI-MiniMaxH3-Contex-Loop 0.5 源码/H3_CHAIN_FORMAT_GUIDE 与示例
工作流作者注释,2026-08-29 实机核验。*
"""


def build() -> dict:
    d = json.loads(SRC.read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in d["nodes"]}
    links = {l[0]: l for l in d["links"]}
    next_link = max(links) + 1

    # ── 1. 权重换本机（扁平文件名）────────────────────────────────
    nodes[1]["widgets_values"] = ["minimax_h3_ref2va_pruned_int8_convrot.safetensors", "default"]
    nodes[2]["widgets_values"] = ["qwen3vl_32b_minimax_h3_int4_convrot.safetensors", "minimax", "default"]
    nodes[3]["widgets_values"] = ["minimax_h3_video_vae_fp16.safetensors"]
    nodes[4]["widgets_values"] = ["minimax_h3_audio_vae_fp32.safetensors"]

    # ── 2. comfy-kitchen 注意力后端默认旁路 ──────────────────────
    nodes[1941]["mode"] = BYPASS

    # ── 3. 参考图三件套（人物/场景/道具）─────────────────────────
    nodes[1945]["widgets_values"] = ["kf_role_main.png", "image"]
    nodes[1946]["widgets_values"] = ["kf_scene_main.png", "image"]
    nodes[1951] = {"id": 1951, "type": "LoadImage", "title": "② 道具资产(Picture 3)",
                   "pos": [-1152, -224], "size": [420, 314], "flags": {},
                   "order": nodes[1946]["order"] + 1, "mode": 0,
                   "inputs": [], "outputs": [
                       {"name": "IMAGE", "type": "IMAGE", "links": [next_link]},
                       {"name": "MASK", "type": "MASK", "links": []}],
                   "properties": {"Node name for S&R": "LoadImage"},
                   "widgets_values": ["kf_prop_main.png", "image"]}
    d["nodes"].append(nodes[1951])
    # 110 接 ref_image_2:示例 inputs 里已含未连接的空槽条目,复用之
    entry = next((i for i in nodes[110]["inputs"]
                  if i.get("name") == "ref_images.ref_image_2"), None)
    if entry is None:
        entry = {"label": "ref_image_2", "name": "ref_images.ref_image_2",
                 "shape": 7, "type": "IMAGE", "link": None}
        nodes[110]["inputs"].append(entry)
    entry["link"] = next_link
    d["links"].append([next_link, 1951, 0, 110,
                       nodes[110]["inputs"].index(entry), "IMAGE"])
    next_link += 1

    # ── 4. 画幅 1344×768（>720p;width/height 由每镜 Current 下发,
    #     链计划未含宽高字段时回落到本节点控件,故两处一致）────────
    wv = nodes[110]["widgets_values"]
    wv[1], wv[2] = 1344, 768

    # ── 5. Turbo 加速链:UNET → TurboLoRA → (原 1941 下游);
    #     TurboSampler → 124.sampler;1941 已旁路直通 ────────────────
    l1941 = next(l for l in d["links"] if l[1] == 1941)
    mid_node, mid_slot = l1941[3], l1941[4]
    # 摘除 1941 的全部连线,并清空两端登记
    for l in [l for l in d["links"] if l[1] == 1941 or l[3] == 1941]:
        nodes[l[1]]["outputs"][l[2]]["links"].remove(l[0])
        nodes[l[3]]["inputs"][l[4]]["link"] = None
        d["links"].remove(l)
    # UNET → TurboLoRA → 原 1941 下游
    nodes[1961] = {"id": 1961, "type": "MiniMaxH3TurboLoRA",
                   "title": "③ 加速补丁（Turbo LoRA v4-600）",
                   "pos": nodes[1941]["pos"], "size": [360, 140],
                   "flags": {}, "order": nodes[1941]["order"] + 1, "mode": 0,
                   "inputs": [{"name": "model", "type": "MODEL", "link": next_link}],
                   "outputs": [{"name": "MODEL", "type": "MODEL",
                                "links": [next_link + 1]}],
                   "properties": {"Node name for S&R": "MiniMaxH3TurboLoRA"},
                   "widgets_values": ["minimax_h3_turbo_v4_step600_ema.safetensors",
                                      1.0, False]}
    d["nodes"].append(nodes[1961])
    nodes[1]["outputs"][0]["links"].append(next_link)
    d["links"].append([next_link, 1, 0, 1961, 0, "MODEL"])
    nodes[mid_node]["inputs"][mid_slot]["link"] = next_link + 1
    d["links"].append([next_link + 1, 1961, 0, mid_node, mid_slot, "MODEL"])
    next_link += 2
    # 采样器换 Turbo:摘 122→124,接 1962→124
    l124s = next(l for l in d["links"] if l[3] == 124 and l[4] == 2)
    nodes[122]["outputs"][0]["links"].remove(l124s[0])
    nodes[124]["inputs"][2]["link"] = None
    d["links"].remove(l124s)
    nodes[1962] = {"id": 1962, "type": "MiniMaxH3TurboSampler",
                   "title": "③ 4 步快采（配 Turbo LoRA）",
                   "pos": nodes[122]["pos"], "size": [330, 60], "flags": {},
                   "order": nodes[122]["order"] + 1, "mode": 0,
                   "inputs": [], "outputs": [{"name": "SAMPLER", "type": "SAMPLER",
                                              "links": [next_link]}],
                   "properties": {"Node name for S&R": "MiniMaxH3TurboSampler"},
                   "widgets_values": []}
    d["nodes"].append(nodes[1962])
    nodes[124]["inputs"][2]["link"] = next_link
    d["links"].append([next_link, 1962, 0, 124, 2, "SAMPLER"])
    next_link += 1

    # ── 6. 计划 JSON:两镜 × 8s（192帧）,steps=6（Turbo 配方）─────
    def six(sections):
        return ["\n".join(sections)]

    P1 = ("<Picture 1> 是主角夏沐沐的唯一形象参考:棕色马尾、白色衬衫、牛仔短裤、"
          "白色帆布鞋,清透 3D 动画电影质感。")
    P2 = ("<Picture 2> 是场景参考:清晨阳光下的郊区柏油街道、两层住宅、行道树、"
          "飘落的白色花瓣。")
    P3 = "<Picture 3> 是道具参考:白色行李箱,拉杆与滚轮细节清晰。"
    S1 = six([
        "subject_definitions:\n" + "\n".join([P1, P2, P3]) +
        "\n<Subject 1> 是由 <Picture 1> 定义的同一角色,全片外观严格一致。",
        "summary:\n[reference generation] 镜1:夏沐沐拉着白色行李箱站在清晨街道上"
        "低头读信,镜头从中景缓慢推近到上半身。",
        "retention_analysis:\n<Subject 1> 夏沐沐: fully_preserved - 五官/发型/"
        "服装严格遵循 <Picture 1>。\n<Picture 2> 场景: fully_preserved。\n"
        "<Picture 3> 行李箱: fully_preserved。",
        "detailed_description:\n清晨逆光洒满街道,白色花瓣随风飘落;夏沐沐静立读信,"
        "发丝轻扬;镜头从中景匀速推近至上半身,浅景深,背景微虚。",
        "overall_soundscape:\n鸟鸣、微风拂叶、远处洒水器声。",
        "non_diegetic_music:\n无配乐。"])
    S2 = six([
        "subject_definitions:\n" + "\n".join([P1, P2, P3]) +
        "\n<Subject 1> 是由 <Picture 1> 定义的同一角色,与镜1外观完全一致。",
        "summary:\n[reference generation] 镜2:接镜1,夏沐沐双手持信特写,眉头微蹙后"
        "释然微笑,把信轻轻贴在胸口;镜头绕人物缓慢环绕半圈。",
        "retention_analysis:\n<Subject 1> 夏沐沐: fully_preserved。\n"
        "<Picture 2> 场景: fully_preserved。\n<Picture 3> 行李箱: fully_preserved。",
        "detailed_description:\n特写:泪光在眼眶打转,嘴角渐渐上扬;环绕运镜半圈,"
        "背景街道缓慢滑过,光线由暖金转为明亮。",
        "overall_soundscape:\n风铃轻响,鸟鸣渐弱,布料轻微摩擦声。",
        "non_diegetic_music:\n无配乐。"])
    plan = {
        "defaults": {"duration_seconds": 8, "steps": 6},
        "shots": [
            {"id": "manga_shot_01", "prompt": S1, "length": 192, "seed": "4201"},
            {"id": "manga_shot_02", "prompt": S2, "length": 192, "seed": "4202"},
        ],
    }
    # ChainPlan 共 17 个控件(示例只存了第 1 个,其余走 schema 默认——
    # 宽高 960×544 即 schema 默认,必须显式覆盖为 1344×768)
    nodes[1700]["widgets_values"] = [
        json.dumps(plan, ensure_ascii=False, indent=1),  # plan_json
        "manga_v9_run01",                                # run_name(每部片子唯一)
        "h3-ref2va-int8convrot+turbo-v4-600+s6",         # generation_fingerprint
        1344, 768,                                       # width / height
        22,                                              # context_length(实测基准值)
        "video",                                         # encode_mode
        "head",                                          # anchor_mode
        "disabled",                                      # crop
        "generated_audio",                               # audio_mode(H3 生成台词/音效)
        22,                                              # audio_context_length
        8.0,                                             # default_duration_seconds
        6,                                               # default_steps(Turbo)
        4201,                                            # base_seed
        19,                                              # segment_crf
        5,                                               # video_blend_frames(5 帧融合)
        "guide",                                         # continuation_mode
    ]

    # ── 7. 审查门默认关闭(无人值守;widgets[0]=enabled);
    #     拼装文件名漫剧化 ─────────────────────────────────────────
    rv = nodes[1944]["widgets_values"]
    rv[0] = False  # enabled(想每镜人工把关:改回 true + 设超时分钟)
    nodes[1706]["widgets_values"][1] = "manga_v9_allref_%date:yyyy-MM-dd%"

    # ── 8. 中文改名 + 便签 + 分区组 ──────────────────────────────
    RETITLE = {
        110: "全能参考引擎（文字+多图 → 音画,每镜）",
        1700: "① 分镜计划（每镜六段式提示词/帧数/种子）",
        1930: "① 场景提示词编辑器（计划流经此处）",
        1949: "③ 预检（模型加载前校验计划）",
        1701: "③ 链循环起点（start_clip=断点续跑）",
        1702: "③ 当前镜（下发提示词/宽高/帧数/种子/步数）",
        1703: "③ 自动上下文（首镜旁路,续镜注入）",
        123: "③ 步数表（步数来自计划=6）",
        120: "③ 噪声种子（来自当前镜）",
        132: "③ 裁掉重复上下文并校准音画",
        1704: "③ 分段落盘（成片+接续段）",
        1705: "③ 循环推进（末镜出 manifest）",
        1944: "④ 审查门（默认关;开启后每镜人工把关）",
        1706: "④ 拼装成片（单条 MP4,带音轨）",
        1708: "④ 恢复拼装（默认静音,断点续用）",
        1945: "② 人物资产(Picture 1)", 1946: "② 场景资产(Picture 2)",
    }
    for nid, title in RETITLE.items():
        if nid in nodes:
            nodes[nid]["title"] = title

    maxnid = max(n["id"] for n in d["nodes"])
    note = {"id": maxnid + 1, "type": "MarkdownNote",
            "title": "漫剧使用说明 + 链节点词典（大白话）",
            "pos": [-2048, 700], "size": [1000, 1100], "flags": {},
            "order": 400, "mode": 0, "inputs": [], "outputs": [],
            "properties": {}, "widgets_values": [NOTE_MAIN + "\n\n---\n\n" + DICT_CHAIN]}
    d["nodes"].append(note)

    # 分区组（按成员现有位置自动包围）
    GROUPS = [
        ("① 文本与计划区（分镜六段式+时长帧数种子）", [1930, 1700], "#8a6d3b"),
        ("② 资产图输入区（人物P1/场景P2/道具P3）", [1945, 1946, 1951], "#2a6e3f"),
        ("③ 流程区（模型/参考引擎/链循环/采样）", [1, 2, 3, 4, 5, 110, 120, 121, 122,
                                                   123, 124, 130, 131, 132, 1701, 1702,
                                                   1703, 1704, 1705, 1941, 1949, 1961,
                                                   1962], "#3f789e"),
        ("④ 产出区（审查门/拼装/恢复）", [1706, 1708, 1944], "#6e2a2a"),
    ]
    glist = d.get("groups") or []
    gstart = (max([g.get("id", 0) for g in glist], default=0)) + 1
    for i, (title, members, color) in enumerate(GROUPS):
        x0 = y0 = float("inf"); x1 = y1 = float("-inf")
        for m in members:
            if m not in nodes:
                continue
            px, py = nodes[m]["pos"]; pw, ph = nodes[m].get("size", [300, 100])
            x0 = min(x0, px); y0 = min(y0, py)
            x1 = max(x1, px + pw); y1 = max(y1, py + ph)
        pad, top = 40.0, 60.0
        glist.append({"id": gstart + i, "title": title,
                      "bounding": [x0 - pad, y0 - top, (x1 - x0) + 2 * pad,
                                   (y1 - y0) + top + pad],
                      "color": color, "flags": {}, "font_size": 26})
    d["groups"] = glist

    d["last_node_id"] = max(n["id"] for n in d["nodes"])
    d["last_link_id"] = max(l[0] for l in d["links"])
    return d


KNOWN = {
    "UNETLoader", "CLIPLoader", "VAELoader", "MiniMaxH3SigmaShift",
    "MiniMaxH3ReferenceToVideo", "RandomNoise", "BasicGuider", "KSamplerSelect",
    "BasicScheduler", "SamplerCustomAdvanced", "VAEDecode", "VAEDecodeAudio",
    "MiniMaxH3LoopTrim", "MiniMaxH3ChainPlan", "MiniMaxH3ChainLoopStart",
    "MiniMaxH3ChainCurrent", "MiniMaxH3ChainContext", "MiniMaxH3ChainSegmentSave",
    "MiniMaxH3ChainLoopEnd", "MiniMaxH3ChainAssemble", "MiniMaxH3ChainManifestLoad",
    "Note", "Reroute", "MiniMaxH3ChainScenePromptEditor", "ModelAttentionBackend",
    "MiniMaxH3ChainReview", "LoadImage", "MiniMaxH3ChainPreflight",
    "MiniMaxH3ChainPolicy", "MarkdownNote", "MiniMaxH3TurboSampler",
    "MiniMaxH3TurboLoRA",
}


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
            errs.append(f"link {lid}: 端点缺失({s},{dst})")
            continue
        outs = nodes[s].get("outputs", [])
        ins = nodes[dst].get("inputs", [])
        if ss >= len(outs) or lid not in (outs[ss].get("links") or []):
            errs.append(f"link {lid}: 源 {s} 输出槽未登记")
        if ds >= len(ins) or ins[ds].get("link") != lid:
            errs.append(f"link {lid}: 目标 {dst} 槽 {ds} 不符")
    for n in d["nodes"]:
        if n["type"] not in KNOWN:
            errs.append(f"节点 {n['id']}: 未知识别类 {n['type']}")
    # 关键断言
    if nodes[1941].get("mode") != BYPASS:
        errs.append("1941 应旁路")
    if nodes[110]["widgets_values"][1:3] != [1344, 768]:
        errs.append("110 画幅应为 1344×768")
    plan = json.loads(nodes[1700]["widgets_values"][0])
    if plan["defaults"].get("steps") != 6:
        errs.append("计划 steps 应为 6(Turbo)")
    total = sum(s["length"] for s in plan["shots"]) / 24
    if total < 15:
        errs.append(f"总时长 {total:.1f}s < 15s")
    for n in d["nodes"]:
        if n["type"] not in KNOWN and n.get("mode") != BYPASS:
            pass
    return errs


def main() -> int:
    if "--check" not in sys.argv:
        d = build()
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
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
