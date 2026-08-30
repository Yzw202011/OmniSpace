"""生成「漫剧全能多图参考工作流 V10」ComfyUI 工作流（2026-08-30）。

骨架：Contex-Loop 0.5 官方 Ref2V Basic Chain 系统（同 V9），融合本机
节点作者 4 个官方示例 + V6-V9 系谱的设计共识：
  - Ref2V Basic / Ref2V Sequential Motion / Masked AV Extension Chain /
    FL2V Normal / I2V Studio 五例通用的：Chain 循环分段、SigmaShift(12,3)、
    res_multistep+simple、LoopTrim 头部裁剪+5 帧融合、<Picture n>/六段式提示词。
  - FL2V/I2V 官方计划默认 duration_seconds=15（每镜 15s 是作者口径）。

漫剧版规格（用户指定）：
  - 每镜 10-15s（本版默认 10.1s/镜=243 帧；15s/镜=362 帧改 length 即可）
  - 480p 档（864×480）跑 10-15s；720p 档（1344×768）单镜 ≤8s（16G 显存实测）
  - 24fps（H3 原生）
  - 7 图参考：<Picture 1..7> = 角色×2(夏沐沐/刀客) + 场景×2(望云亭苑/别墅区
    街角大门) + 道具×3(黑色行李箱/黑色笔记本/三角梅)——项目 comic_assets
    真资产；第 8/9 槽留空（资产库仅有 2 个角色立绘,如实限制）
  - Turbo 6 步（V8/V9 已验证配方）；审查门默认关；ChainPlan 17 控件全显式

用法：python build_manga_v10_allref7_workflow.py [--check]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMFY = REPO / "tools" / "ComfyUI_windows_portable" / "ComfyUI"
SRC = (COMFY / "custom_nodes/ComfyUI-MiniMaxH3-Contex-Loop/example_workflows"
       / "Ref2V Basic - MiniMax H3.json")
OUT_DIR = COMFY / "user/default/workflows/漫剧创作套件"
OUT = OUT_DIR / "全能多图参考工作流_V10.json"

BYPASS = 4
W, H = 864, 480          # 480p 档（每镜 10-15s 的显存安全档）
SHOTS = [243, 243]       # 每镜帧数：243=10.1s（15s 用 362,720p 档勿超 192）

NOTE_MAIN = """### 漫剧全能多图参考工作流 V10（每镜 10-15s · 480p/720p · 24fps · 带音轨）
设计融合:Contex-Loop 官方五示例(Ref2V Basic/Sequential Motion/Masked AV
Extension Chain/FL2V Normal/I2V Studio)+V8/V9 实测配方。

### 为什么这样搭(官方五例的设计共识)
- **Chain 循环分段**而非一条长采样:16G 显存单镜有帧数上限,长视频=多镜+段间
  上下文接力,每镜落盘可审查/断点续跑;
- **段间上下文 22 帧**:H3 潜空间 17 帧一组,22=17+5 是实测基准;上下文每步
  重注入、永不参与去噪 → 角色身份跨镜锁定;
- **anchor=head + LoopTrim 裁头**:上下文钉头部,成片裁掉重复帧,音轨同步裁
  (不裁会有 208ms 音画偏移,作者 docstring 原话);
- **SigmaShift(12,3)+res_multistep+simple**:官方模板采样配方;
- **六段式提示词+<Picture n> 标签**:Ref2VA 训练数据的原生引用语法;
- **FL2V/I2V 官方计划默认就是每镜 15s**——10-15s/镜是官方口径。

### 本版规格
- **7 图参考**:<Picture 1>=夏沐沐(角色) 2=刀客(角色) 3=望云亭苑(场景)
  4=别墅区街角大门(场景) 5=黑色行李箱 6=黑色笔记本 7=三角梅——全部来自
  项目 comic_assets 真资产;第 8/9 槽留空(资产库现有 2 个角色立绘)。
- **两档画质**(16G 显存实测口径):
  - **480p 档 864×480**:每镜 10.1s(243帧)~15s(362帧) ✓ 本版默认
  - **720p 档 1344×768**:单镜 ≤8s(192帧,峰值 15.6GB);更长会 OOM
  - 交付 720p:480p 生成后 ffmpeg lanczos 放大,或 720p 档多镜拼接
- **每镜时长**:改计划 JSON 每镜 length(帧数):10s=243 / 12s=290 / 15s=362
  (24fps,17k+5 网格);加镜数=加长片子。

### 用法
1. ②区换资产图(input 目录 ref_p1..p7,或节点上上传);
2. ①改计划 JSON(每镜六段式提示词+length+seed;加镜往 shots 数组加);
3. Queue → 自动逐镜 → 自动拼装单条 MP4 → `output/h3_chains/manga_v10/final/`。
审查门默认关(「审查门」节点 enabled 勾回可每镜人工把关)。
"""

DICT_CHAIN = """## 链节点大白话词典(Contex-Loop 0.5 Chain)
**分镜计划/场景提示词编辑器**:JSON 定义整片——defaults(duration_seconds/
steps/width/height 走节点控件),shots[] 每镜 {id,prompt(六段式),length帧,seed}。
**预检**:模型加载前校验计划。**循环起点**:start_clip 断点续跑。
**当前镜**:把本镜 prompt/宽高/帧数/种子/步数分发给引擎和采样器。
**全能参考引擎**(ReferenceToVideo):文字+9 图+3 参考视频+3 参考音频;
ref_image_size=match(缩到生成面积,快)/max(短边2048,慢数倍)。
**自动上下文**:首镜旁路;续镜注入上一镜尾部 22 帧画面+音轨(每步重注入,
永不参与去噪)。**裁剪**(LoopTrim):trim_frames 裁掉头部重复;match_tail
校准 40Hz 音频网格;retain_overlap_frames 本版 0(成片不留重复帧)。
**采样**:噪声(种子=当前镜)→CFG→Turbo 4-6 步→步数表→执行器。
**分段落盘→审查门(默认关)→循环推进→拼装成片**(5 帧视觉融合)。
**SigmaShift(12,3)**:官方采样偏移,勿动。**注意力后端**:comfy-kitchen
未安装,保持旁路。*依据:五官方示例+源码+2026-08-29/30 实机核验。*
"""


def build() -> dict:
    d = json.loads(SRC.read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in d["nodes"]}
    next_link = max(l[0] for l in d["links"]) + 1

    # ── 1. 权重换本机 ────────────────────────────────────────────
    nodes[1]["widgets_values"] = ["minimax_h3_ref2va_pruned_int8_convrot.safetensors", "default"]
    nodes[2]["widgets_values"] = ["qwen3vl_32b_minimax_h3_int4_convrot.safetensors", "minimax", "default"]
    nodes[3]["widgets_values"] = ["minimax_h3_video_vae_fp16.safetensors"]
    nodes[4]["widgets_values"] = ["minimax_h3_audio_vae_fp32.safetensors"]
    nodes[1941]["mode"] = BYPASS

    # ── 2. 7 图参考:P1/P2 已接,复用已有空槽+补新条目接 P3..P7 ────
    REF_TITLES = {1945: "② P1·角色夏沐沐", 1946: "② P2·角色刀客"}
    nodes[1945]["widgets_values"] = ["ref_p1.png", "image"]
    nodes[1946]["widgets_values"] = ["ref_p2.png", "image"]
    base_pos = nodes[1946]["pos"]
    order_base = nodes[1946]["order"]
    ref_meta = [
        (3, "ref_p3.png", "② P3·场景望云亭苑"),
        (4, "ref_p4.png", "② P4·场景别墅区街角大门"),
        (5, "ref_p5.png", "② P5·道具黑色行李箱"),
        (6, "ref_p6.png", "② P6·道具黑色笔记本"),
        (7, "ref_p7.png", "② P7·道具三角梅"),
    ]
    for k, (slot, fname, title) in enumerate(ref_meta):
        # 复用已有空槽条目,没有才新增(示例自带 0/1 两个已接条目)
        name = f"ref_images.ref_image_{slot}"
        entry = next((i for i in nodes[110]["inputs"] if i.get("name") == name), None)
        if entry is None:
            entry = {"label": f"ref_image_{slot}", "name": name,
                     "shape": 7, "type": "IMAGE", "link": None}
            nodes[110]["inputs"].append(entry)
        nid = 1951 + k
        nodes[nid] = {"id": nid, "type": "LoadImage", "title": title,
                      "pos": [base_pos[0] + 448 * ((k + 1) % 2),
                              base_pos[1] + 330 * ((k + 1) // 2)],
                      "size": [420, 260], "flags": {},
                      "order": order_base + 1 + k, "mode": 0,
                      "inputs": [], "outputs": [
                          {"name": "IMAGE", "type": "IMAGE", "links": [next_link]},
                          {"name": "MASK", "type": "MASK", "links": []}],
                      "properties": {"Node name for S&R": "LoadImage"},
                      "widgets_values": [fname, "image"]}
        d["nodes"].append(nodes[nid])
        entry["link"] = next_link
        d["links"].append([next_link, nid, 0, 110,
                           nodes[110]["inputs"].index(entry), "IMAGE"])
        next_link += 1

    # ── 3. 画幅 864×480(480p 档,每镜 10-15s 安全)────────────────
    nodes[110]["widgets_values"][1] = W
    nodes[110]["widgets_values"][2] = H

    # ── 4. Turbo 链:UNET → TurboLoRA → 原 1941 下游;Turbo→124 ────
    l1941 = next(l for l in d["links"] if l[1] == 1941)
    mid_node, mid_slot = l1941[3], l1941[4]
    for l in [l for l in d["links"] if l[1] == 1941 or l[3] == 1941]:
        nodes[l[1]]["outputs"][l[2]]["links"].remove(l[0])
        nodes[l[3]]["inputs"][l[4]]["link"] = None
        d["links"].remove(l)
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
    l124s = next(l for l in d["links"] if l[3] == 124 and l[4] == 2)
    nodes[122]["outputs"][0]["links"].remove(l124s[0])
    nodes[124]["inputs"][2]["link"] = None
    d["links"].remove(l124s)
    nodes[1962] = {"id": 1962, "type": "MiniMaxH3TurboSampler",
                   "title": "③ 4-6 步快采（Turbo 配方）",
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

    # ── 5. 计划:两镜 × 243 帧(10.1s/镜)@480p,六段式中文 ─────────
    P = ["<Picture 1> 是主角夏沐沐的唯一形象参考:黑长直发、白衬衫,清透 3D 动画"
         "电影质感,五官与服饰严格以此为准。",
         "<Picture 2> 是第二角色刀客的形象参考:束发、深色劲装、背负长刀,面容"
         "冷峻,与夏沐沐同片世界。",
         "<Picture 3> 是场景一参考:望云亭苑,中式亭廊与石桌石凳,花木环绕。",
         "<Picture 4> 是场景二参考:别墅区街角大门,砖砌门柱与铁艺围栏,柏油路面。",
         "<Picture 5> 是道具一参考:黑色行李箱,拉杆与滚轮细节。",
         "<Picture 6> 是道具二参考:黑色笔记本,皮质封面。",
         "<Picture 7> 是道具三参考:盛开的三角梅,花枝低垂。"]

    def six(sections):
        return ["\n".join(sections)]

    S1 = six([
        "subject_definitions:\n" + "\n".join(P) +
        "\n<Subject 1> 夏沐沐与 <Subject 2> 刀客是同片世界的两个角色,外观严格"
        "遵循各自参考图。",
        "summary:\n[reference generation] 镜1:夏沐沐拖着黑色行李箱站在别墅区"
        "街角大门旁低头读信,三角梅花瓣随风飘落;刀客自街道尽头缓步走来,在远处"
        "驻足;镜头从中景缓慢推近夏沐沐。",
        "retention_analysis:\n<Subject 1> 夏沐沐: fully_preserved - 遵循 "
        "<Picture 1>。\n<Subject 2> 刀客: fully_preserved - 遵循 <Picture 2>。\n"
        "<Picture 4> 场景: fully_preserved。\n<Picture 5> 行李箱 / <Picture 6> "
        "笔记本 / <Picture 7> 三角梅: fully_preserved。",
        "detailed_description:\n清晨侧逆光,街角安静;夏沐沐倚门读信,发丝被风"
        "掀起;刀客的身影由远及近渐清晰;三角梅花瓣落在行李箱与肩头;镜头运动"
        "平稳,浅景深。",
        "overall_soundscape:\n鸟鸣、风声、行李箱滚轮声由远及近,脚步轻缓。",
        "non_diegetic_music:\n无配乐,无字幕。"])
    S2 = six([
        "subject_definitions:\n" + "\n".join(P) +
        "\n<Subject 1> 夏沐沐与 <Subject 2> 刀客延续镜1外观,严格一致。",
        "summary:\n[reference generation] 镜2:望云亭苑中,夏沐沐与刀客隔石桌"
        "相对而立,黑色笔记本摊在石桌上,三角梅自亭檐垂落;两人对视,镜头缓慢"
        "环绕半圈。",
        "retention_analysis:\n<Subject 1> 夏沐沐: fully_preserved。\n"
        "<Subject 2> 刀客: fully_preserved。\n<Picture 3> 望云亭苑: "
        "fully_preserved。\n<Picture 6> 笔记本 / <Picture 7> 三角梅: "
        "fully_preserved。",
        "detailed_description:\n午后光线穿过亭檐,在石桌面投下花影;夏沐沐把"
        "黑色笔记本推到桌子中央,刀客伸手按住;镜头绕两人缓慢环绕半圈,最后"
        "停在两人对视的剪影。",
        "overall_soundscape:\n亭外蝉鸣渐弱,风铃轻响,石桌上的纸页被风掀起。",
        "non_diegetic_music:\n无配乐,无字幕。"])
    plan = {
        "defaults": {"duration_seconds": 10, "steps": 6},
        "shots": [
            {"id": "manga_shot_01", "prompt": S1, "length": SHOTS[0], "seed": "4201"},
            {"id": "manga_shot_02", "prompt": S2, "length": SHOTS[1], "seed": "4202"},
        ],
    }
    nodes[1700]["widgets_values"] = [
        json.dumps(plan, ensure_ascii=False, indent=1),
        "manga_v10",                                    # run_name
        "h3-ref2va-int8convrot+turbo-v4-600+s6",        # generation_fingerprint
        W, H,                                           # width / height(480p)
        22,                                             # context_length
        "video", "head", "disabled",                    # encode/anchor/crop
        "generated_audio", 22,                          # audio_mode / ctx
        10.0, 6, 4201, 19, 5, "guide",                  # dur/steps/seed/crf/blend/cont
    ]

    # ── 6. 审查门默认关;拼装文件名 ──────────────────────────────
    nodes[1944]["widgets_values"][0] = False
    nodes[1706]["widgets_values"][1] = "manga_v10_allref_%date:yyyy-MM-dd%"

    # ── 7. 改名 + 便签 + 分区组 ──────────────────────────────────
    RETITLE = {
        110: "全能参考引擎（文字+7图 → 音画,每镜）",
        1700: "① 分镜计划（两镜×10.1s,480p;15s 改 length=362）",
        1930: "① 场景提示词编辑器",
        1949: "③ 预检", 1701: "③ 链循环起点",
        1702: "③ 当前镜（下发提示词/宽高/帧数/种子/步数）",
        1703: "③ 自动上下文（首镜旁路,续镜 22 帧）",
        123: "③ 步数表(=6)", 120: "③ 噪声种子(=当前镜)",
        132: "③ 裁头+音画校准", 1704: "③ 分段落盘",
        1705: "③ 循环推进", 1944: "④ 审查门（默认关）",
        1706: "④ 拼装成片（单条 MP4 带音轨）",
        1708: "④ 恢复拼装（默认静音）",
    }
    for nid, title in RETITLE.items():
        if nid in nodes:
            nodes[nid]["title"] = title
    maxnid = max(n["id"] for n in d["nodes"])
    d["nodes"].append({"id": maxnid + 1, "type": "MarkdownNote",
                       "title": "漫剧使用说明 + 链节点词典（大白话）",
                       "pos": [-2400, 760], "size": [1000, 1150], "flags": {},
                       "order": 400, "mode": 0, "inputs": [], "outputs": [],
                       "properties": {},
                       "widgets_values": [NOTE_MAIN + "\n\n---\n\n" + DICT_CHAIN]})
    GROUPS = [
        ("① 文本与计划区", [1930, 1700], "#8a6d3b"),
        ("② 资产图输入区（2角色+2场景+3道具=7图）",
         [1945, 1946, 1951, 1952, 1953, 1954, 1955], "#2a6e3f"),
        ("③ 流程区", [1, 2, 3, 4, 5, 110, 120, 121, 122, 123, 124, 130, 131,
                   132, 1701, 1702, 1703, 1704, 1705, 1941, 1949, 1961, 1962],
         "#3f789e"),
        ("④ 产出区", [1706, 1708, 1944], "#6e2a2a"),
    ]
    glist = d.get("groups") or []
    gstart = max([g.get("id", 0) for g in glist], default=0) + 1
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
            errs.append(f"link {lid}: 端点缺失")
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
    wv = nodes[1700]["widgets_values"]
    if [wv[3], wv[4]] != [W, H]:
        errs.append("计划宽高应为 864×480")
    plan = json.loads(wv[0])
    total = sum(s["length"] for s in plan["shots"]) / 24
    if total < 15:
        errs.append(f"总时长 {total:.1f}s < 15s")
    per = [s["length"] / 24 for s in plan["shots"]]
    if any(p < 10 for p in per):
        errs.append(f"每镜时长 {per} < 10s")
    n_refs = len([i for i in nodes[110]["inputs"]
                  if i["name"].startswith("ref_images.ref_image_")
                  and i.get("link") is not None])
    if n_refs != 7:
        errs.append(f"参考图应接 7 张,实际 {n_refs}")
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
