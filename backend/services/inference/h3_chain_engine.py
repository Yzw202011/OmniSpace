"""H3 链式引擎：漫剧分镜行 → V10 全能多图参考 Chain → 单条成片（2026-08-30）。

把漫剧工作台的"一镜"(分镜行)接入内置 ComfyUI 的全能参考链式工作流：
  分镜行(描述词+人物/场景/道具绑定) → 六段式计划 JSON → Chain API 图
  → ComfyUI /prompt → 轮询 → 成片回搬 → 交给调用方入库。

设计要点：
  - API 图模板 = 一次成功运行的历史提取（h3_chain_workflow_api.json，
    34 节点，与前端排队提交完全一致），运行时仅替换 plan_json /
    run_name / 宽高 / LoadImage 参考路径 / 成片文件名；
  - 复用 H3Engine 的进程托管/提交/轮询/显存闸门（与导演台同锁）;
  - 参考图来自 comic_assets 真源，按"角色→场景→道具"排序映射
    <Picture 1..N>（≤7，与模板槽位一致；槽 8/9 预留）;
  - 质量档位：480p(864×480, 每镜≤15s) / 720p(1344×768, 每镜≤8s)
    ——16G 显存实测矩阵（2026-08-30）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio

import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from ...config import DATA_DIR, get_config
from ...middleware.error_handler import ApiError
from .comfy_proc import COMFY_INPUT_DIR as _COMFY_INPUT
from .h3_engine import _COMFY_OUTPUT, align_h3_frames, get_h3_engine

# 2026-09-02 修复：comfy_proc 启动参数已把 input/output 重定向到
# data/comfyui/（08-31 生命周期治理），h3_engine 已同步改 import，
# 本引擎漏改仍写死旧安装目录 tools/.../ComfyUI/input——参考图拷入
# 旧目录而运行中的 ComfyUI 读 data/comfyui/input，链式提交必报
# 「Invalid image file」（E2E 双镜实测揪出）。统一从 comfy_proc 取真源。

_TEMPLATE = Path(__file__).parent / "h3_chain_workflow_api.json"


def _load_template_text() -> str:
    """API 图模板文本：优先解密金库（发行包内），开发环境回退明文。

    （P6 锁4：发行包内模板以 assets_enc/h3_chain_workflow_api.json.enc
    存在，明文由 make_dist 在出包时移除；金库密钥由发行公钥派生。）
    """
    from ...asset_vault import read_asset
    blob = read_asset("h3_chain_workflow_api.json.enc")
    if blob is not None:
        return blob.decode("utf-8")
    return _TEMPLATE.read_text(encoding="utf-8")
_QUALITY = {"480p": (864, 480), "720p": (1344, 768)}
_MAX_REFS = 7  # 与模板槽位一致（引擎上限 9,槽 8/9 预留）


def _split_names(value: Any) -> list[str]:
    """分镜行绑定字段 → 名单（容忍 JSON 数组/顿号/逗号/换行分隔）。"""
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(v).strip() for v in parsed if str(v).strip()]
    except Exception:  # noqa: BLE001 - 非 JSON 按分隔符切
        pass
    return [p.strip() for p in re.split(r"[、,，;；\n]+", text) if p.strip()]


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度(绑定名 vs 资产名的模糊匹配打分)。"""
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            best = max(best, k)
    return best


def _parse_id_list(v: Any) -> list[str]:
    """asset_ids 兼容解析：list / JSON 字符串 / 逗号分隔（对齐 keyframe
    的 _fetch_bound_assets 三形态兼容）。"""
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:  # noqa: BLE001 - 非合法 JSON 按逗号分隔兜底
            pass
        return [s.strip() for s in v.split(",") if s.strip()]
    return []


def _collect_refs(row: dict, row_id: str) -> list[dict]:
    """按 角色→场景→道具 收集参考（comic_assets 真源，≤7）。

    P0 修复（2026-08-31）：绑定真源 = storyboard_rows.asset_ids（资产
    id 列表，关键帧链路 _fetch_bound_assets 同源）；此前只读
    characters/scene/props 名称冗余列——ai-describe 重建/写入的行该
    三列为空（实测 row_1788130850860：asset_ids 4 项全带图而三列全
    空）→ 「均未绑定带图资产」误拒，视频生成即失败。现改为 asset_ids
    批查优先（带图、kind 优先级、同项目优先），名称列 LCS 模糊匹配
    (≥2 字)保留为旧数据兜底。
    """
    from ...data.database import get_db_safe

    db = get_db_safe()
    if db is None:
        raise ApiError(60003, "数据库不可用(参考图收集)")
    refs: list[dict] = []
    used: set[str] = set()
    # 分镜行 → 所属项目(同项目资产优先,防跨项目风格错位)
    proj = db.query_one(
        "SELECT s.project_id AS pid FROM storyboard_rows r "
        "JOIN storyboards s ON r.storyboard_id=s.id WHERE r.id=?",
        (row_id if isinstance(row_id, str) else str(row_id),))
    proj_id = (proj or {}).get("pid")

    # ① asset_ids 真源：id 直查带图资产，character→scene→prop 优先级、
    #    同项目优先排序后截取
    ids = _parse_id_list(row.get("asset_ids"))
    if ids:
        placeholders = ",".join("?" * len(ids))
        try:
            picked = list(db.query(
                f"SELECT name, kind, file_path, project_id FROM comic_assets "
                f"WHERE id IN ({placeholders}) AND file_path!=''", ids))
        except Exception:  # noqa: BLE001 - 查询失败回落名称列兜底
            picked = []
        kind_pri = {"character": 0, "scene": 1, "prop": 2}
        picked.sort(key=lambda a: (kind_pri.get(a["kind"], 9),
                                   0 if proj_id and a["project_id"] == proj_id else 1))
        for a in picked:
            if len(refs) >= _MAX_REFS:
                break
            if a["name"] in used:
                continue
            used.add(a["name"])
            refs.append({"kind": a["kind"], "name": a["name"],
                         "src": DATA_DIR / a["file_path"]})

    # ② 名称列兜底（旧数据/手工绑定行）：最长公共子串模糊匹配(≥2 字),
    #    行里写"行李箱"能命中资产"黑色行李箱"
    for kind, bindings in (("character", _split_names(row.get("characters"))),
                           ("scene", _split_names(row.get("scene"))),
                           ("prop", _split_names(row.get("props")))):
        if len(refs) >= _MAX_REFS:
            break
        assets = [(r["name"], r["file_path"],
                   1 if proj_id and r["project_id"] == proj_id else 0)
                  for r in db.query(
            "SELECT name, file_path, project_id FROM comic_assets "
            "WHERE kind=? AND file_path!=''", (kind,))]
        for name in bindings:
            if len(refs) >= _MAX_REFS:
                break
            best, best_key = None, (0, 0)
            for aname, apath, same_proj in assets:
                if aname in used:
                    continue
                key = (_lcs_len(name, aname), same_proj)
                if key > best_key:
                    best, best_key = (aname, apath), key
            if best and best_key[0] >= 2:
                used.add(best[0])
                refs.append({"kind": kind, "name": best[0],
                             "src": DATA_DIR / best[1]})
    return refs


def _split_abc(description: str) -> dict[str, str]:
    """按段首标记「A.」「B.」「C.」切分分镜描述词。

    标记位置容忍两种（2026-08-31 镜3 实测修复）：行首，或紧跟句读
    （。！？；;）之后——AI 生成的描述词常把「。B.高密度世界观构建：」
    写在同一行，只认行首会令 B 段解析为空、世界观整段塌进 A 段并携带
    原始标记。标记字母必须前置合法（行首/句读后），词中字母（如
    "3D CG"）不会误命中。返回 {"a","b","c"}；无标记的纯文本描述三段
    均为空（调用方兜底原文）。
    """
    import re
    text = description.strip()
    marks = []
    for m in re.finditer(r"[ABC]\s*[.、．]", text):
        prefix = text[:m.start()].rstrip()
        if prefix and prefix[-1] not in "。！？；;\n":
            continue
        marks.append((m.group(0)[0].lower(), m.start(), m.end()))
    marks.sort(key=lambda t: t[1])
    seen: set[str] = set()
    dedup = []
    for key, s, e in marks:
        if key in seen:
            continue  # 同键多次出现取首段
        seen.add(key)
        dedup.append((key, s, e))
    marks = dedup
    out = {"a": "", "b": "", "c": ""}
    for i, (key, _s, content_start) in enumerate(marks):
        seg_end = marks[i + 1][1] if i + 1 < len(marks) else len(text)
        out[key] = text[content_start:seg_end].strip()
    return out


def _timeline_window(c_text: str, seconds: float) -> str:
    """C 段时间轴按时长取窗（2026-08-31 用户反馈「视频与描述词不符」根因）。

    描述词的 C 段是整行完整时间轴（如 0.1-10s 三镜），而单次链式生成
    只有 N 秒——整段塞入会稀释镜头语言。按镜头块**起点**判窗：起点早于
    生成时长的块保留（跨窗块演绎其前半），起点在时长之后的块丢弃；
    无标记/解析失败/全部超窗时返回整段（诚实兜底，宁可多演不可漏演）。
    """
    import re
    if not c_text:
        return ""
    blocks = re.split(r"(?=\[\d+(?:\.\d+)?s\s*[-~]\s*\d+(?:\.\d+)?s\])", c_text)
    header, timed = blocks[0], blocks[1:]
    if not timed:
        return c_text
    kept, dropped = [], 0
    for blk in timed:
        m = re.match(r"\[(\d+(?:\.\d+)?)s\s*[-~]\s*(\d+(?:\.\d+)?)s\]", blk.strip())
        if m and float(m.group(1)) >= seconds:
            dropped += 1
            continue
        kept.append(blk.strip())
    if not kept:
        return c_text
    note = (f"\n（本镜仅演绎前 {seconds:.0f} 秒内的镜头，其后续镜头另行生成）"
            if dropped else "")
    head = header.strip()
    return (head + "\n" if head else "") + "\n".join(kept) + note


def _six_section(description: str, refs: list[dict], total_seconds: float) -> str:
    subj = []
    retain = []
    for i, r in enumerate(refs, 1):
        kind_zh = {"character": "角色形象", "scene": "场景", "prop": "道具"}[r["kind"]]
        subj.append(f"<Picture {i}> 是{kind_zh}`{r['name']}`的唯一视觉参考。")
        retain.append(f"<Picture {i}> {r['name']}: fully_preserved。")
    # 描述词结构化增强（2026-08-31）：此前 summary 仅截前 120 字（几乎
    # 只剩 A 段风格句），detailed_description 塞整行全量时间轴——单镜
    # N 秒生成被稀释成「画风对了、内容不符」。现按 A/B/C 切分并按时长
    # 窗口取 C 段镜头，世界观/镜头语言按结构分位注入。
    abc = _split_abc(description)
    c_win = _timeline_window(abc["c"], total_seconds)
    # 摘要首拍：跳过转场拍（「衔接上一镜头…」是承上启下的空拍，
    # 不是本镜实体画面——镜3 实测它曾被当成主画面写进 summary）
    first_shot = ""
    for blk in c_win.split("画面：")[1:]:
        cand = blk.split("。", 1)[0].strip()
        if not cand or cand.startswith("衔接"):
            continue
        first_shot = cand[:120]
        break
    brief = "；".join(x for x in [
        abc["b"].split("。", 1)[0][:120] if abc["b"] else "",
        first_shot,
    ] if x)
    detailed = "\n\n".join(x for x in [
        "严格按下方时间轴逐拍演绎；场景、光影与人物动作以描述词为准，"
        "参考图仅用于锁定人物与道具的外观既定形态。",
        f"[全局风格] {abc['a'][:400]}" if abc["a"] else "",
        f"[世界与人物状态] {abc['b'][:500]}" if abc["b"] else "",
        (f"[本镜时间轴（共 {total_seconds:.0f} 秒）]\n{c_win[:1500]}"
         if c_win else ""),
    ] if x)
    return "\n".join([
        "subject_definitions:\n" + "\n".join(subj),
        "summary:\n[reference generation] " + (brief or description.strip()[:160]),
        "retention_analysis:\n" + "\n".join(retain) +
        "\n<Subject 1>: fully_preserved - 全程保持参考图既定外观与服饰。",
        "detailed_description:\n" + (detailed or description.strip()),
        f"overall_soundscape:\n环境自然音效与角色台词,全程无字幕。"
        f"单镜时长约 {total_seconds:.0f} 秒。",
        "non_diegetic_music:\n无配乐。",
    ])


def _prepare_refs(task_id: str, refs: list[dict]) -> list[dict]:
    """参考图拷入 ComfyUI input 子目录,返回带 image 路径的引用。"""
    sub = Path(_COMFY_INPUT) / f"h3chain_{task_id}"
    sub.mkdir(parents=True, exist_ok=True)
    out = []
    for i, r in enumerate(refs, 1):
        dst = sub / f"p{i}.png"
        shutil.copy2(r["src"], dst)
        out.append({**r, "image": f"h3chain_{task_id}/p{i}.png"})
    return out


def _render_graph(template: dict, plan_json: str, run_name: str,
                  width: int, height: int, refs: list[dict], task_id: str,
                  start_clip: int = 1) -> dict:
    graph = json.loads(json.dumps(template))
    p = graph["1700"]["inputs"]
    p["plan_json"] = plan_json
    p["run_name"] = run_name
    p["width"], p["height"] = width, height
    graph["1701"]["inputs"]["start_clip"] = start_clip
    graph["1706"]["inputs"]["filename"] = f"{run_name}_%date:yyyy-MM-dd%"
    # 参考图:动态识别模板里的参考 LoadImage 节点及其在 110 上的槽位键
    # (自动生长槽位的编号由前端决定,不能硬编码顺序)
    slot_map = []  # [(loadimage_nid, input_key)]
    for key, val in graph["110"]["inputs"].items():
        if key.startswith("ref_images.ref_image_") and isinstance(val, list):
            nid = str(val[0])
            if nid in graph and graph[nid]["class_type"] == "LoadImage":
                slot_map.append((nid, key))
    slot_map.sort(key=lambda x: int(x[0]))
    for k, (nid, key) in enumerate(slot_map):
        if k < len(refs):
            graph[nid]["inputs"]["image"] = refs[k]["image"]
        else:
            graph.pop(nid, None)
            graph["110"]["inputs"].pop(key, None)
    return graph


def run_h3_chain_task(task_id: str, row_ids: list[str], seconds: float,
                      quality: str, start_clip: int = 1, run_name: str = "",
                      loop: asyncio.AbstractEventLoop | None = None,
                      progress_cb: Callable[[float, str], None] | None = None,
                      aspect: str = "16:9",
                      check_cancel: Callable[[], None] | None = None,
                      keep_loaded: bool = False) -> dict:
    """链式生成主流程（阻塞,供视频队列 worker 调用）。

    row_ids: 多个分镜行 → 计划多镜(参考取各行绑定并集,≤7);
    start_clip>1: 断点续跑(沿用同 run_name 的已完成镜检查点);
    progress_cb(frac, stage) 用于回写 video_tasks.progress;
    aspect: "16:9"|"9:16"（2026-08-31 模型配置接线：默认画幅此前从不
    进请求，宽高按画幅翻转）;
    check_cancel: 取消检查点（2026-09-02 视频队列接线）——提交前与
    采样轮询（3s 一查）调用，命中抛调用方异常并 best-effort POST
    ComfyUI /interrupt 止损（此前排队/采样中取消完全无效，空跑整镜）;
    keep_loaded: 队列还有后继 H3 任务时跳过收尾卸载，镜间接力免整轮
    权重重载（此前前端逐镜接力每镜都重载 H3 权重）。
    返回 {"file_path": 绝对路径, "resolution": "WxH", "duration_seconds": s}。
    """
    from ...data.database import get_db_safe

    def _cancel_check() -> None:
        if check_cancel is not None:
            check_cancel()

    def _cancel_interrupt(engine_ref: Any) -> None:
        """取消时 best-effort 中断 ComfyUI 当前 prompt（止损 GPU）。"""
        try:
            engine_ref._api("POST", "/interrupt", timeout=5.0)
        except Exception:  # noqa: BLE001 - 中断失败不影响取消主流程
            pass

    if quality not in _QUALITY:
        raise ApiError(60001, f"未知画质档位 {quality}")
    width, height = _QUALITY[quality]
    if aspect == "9:16":
        width, height = height, width  # 模型配置·默认画幅接线（竖屏）
    engine = get_h3_engine()
    run_name = run_name or f"h3chain_{task_id}"

    def report(frac: float, stage: str) -> None:
        if progress_cb:
            try:
                progress_cb(frac, stage)
            except Exception:  # noqa: BLE001
                pass

    # 1. 分镜行 + 参考收集(多镜取并集,同项目优先去重;库瞬时锁重试)
    db = get_db_safe()
    rows: list[dict] = []
    for _attempt in range(3):
        try:
            if db is None:
                db = get_db_safe()
            if db is None:
                time.sleep(1.0)
                continue
            for rid in row_ids:
                r = db.query_one(
                    "SELECT description, characters, scene, props, asset_ids "
                    "FROM storyboard_rows WHERE id=?", (rid,))
                if r and (r.get("description") or "").strip():
                    rows.append({**r, "id": rid})
            break
        except Exception:  # noqa: BLE001 - sqlite busy 瞬时错误重试
            time.sleep(1.0)
    if not rows:
        raise ApiError(40005, f"分镜行不存在或描述词为空: {row_ids}")
    seen: set[tuple] = set()
    refs: list[dict] = []
    for r in rows:
        for ref in _collect_refs(r, r["id"]):
            key = (ref["kind"], ref["name"])
            if key not in seen and len(refs) < _MAX_REFS:
                seen.add(key)
                refs.append(ref)
    if not refs:
        raise ApiError(60001, "所选分镜行均未绑定带图资产(人物/场景/道具)")

    # 2. 参考图落盘 + 计划(每镜一 shot,长度=17k+5)
    report(0.02, "准备参考图")
    refs = _prepare_refs(task_id, refs)
    shots = []
    for k, r in enumerate(rows, 1):
        frames = align_h3_frames(seconds)
        seed = str(int(hashlib.md5(f"{r['id']}|{seconds}".encode()).hexdigest()[:8], 16) % 1_000_000)
        shots.append({"id": f"shot_{k:02d}",
                      "prompt": [_six_section(r["description"], refs, seconds)],
                      "length": frames, "seed": seed})
    # 采样步数档位（批1b 2026-09-12）：config manga.h3_steps，标准 6 /
    # 快速 4（Turbo LoRA 有效区间 4~8，钳制防呆）
    try:
        _steps = int((get_config().get("manga") or {}).get(
            "h3_steps", 6) or 6)
    except Exception:  # noqa: BLE001 - 配置异常按标准档
        _steps = 6
    plan = {"defaults": {"duration_seconds": seconds,
                         "steps": max(4, min(8, _steps))},
            "shots": shots}
    plan_json = json.dumps(plan, ensure_ascii=False, indent=1)

    # 3. 组图提交（与导演台同锁,互斥 GUI/其他 H3 任务）;无论成败,结束必卸载权重
    _cancel_check()
    report(0.04, "提交 H3 链式工作流")
    engine._ensure_running(lambda f, s: report(0.02 + f * 0.02, s))
    # 忙碌标记（2026-08-31 事故）：链式走自有提交路径不经 h3_engine.generate
    # 的 mark 配对，单 prompt 运行 >300s 被 comfy_proc 空闲关杀（链式中断）。
    from .comfy_proc import get_comfy_proc as _get_comfy_proc
    _get_comfy_proc().mark_busy()
    try:
        with engine._gen_lock:
            graph = _render_graph(json.loads(_load_template_text()),
                                  plan_json, run_name, width, height,
                                  refs, task_id, start_clip)
            import urllib.error
            try:
                resp = engine._api("POST", "/prompt",
                                   body={"prompt": graph,
                                         "client_id": f"omnispace-{task_id}"},
                                   timeout=15.0)
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "ignore")[:500]
                except Exception:  # noqa: BLE001
                    pass
                raise ApiError(60003,
                               f"链式工作流被拒绝(HTTP {exc.code}): {body}") from exc
            if resp is None:
                raise ApiError(60003, "ComfyUI 不可达（链式提交失败）")
            if resp.get("error") or resp.get("node_errors"):
                import json as _j
                raise ApiError(60003, "链式工作流校验失败: " + _j.dumps(
                    resp.get("node_errors") or resp.get("error"),
                    ensure_ascii=False)[:400])
            prompt_id = str(resp["prompt_id"])
            report(0.06, "链式采样中")

            # 4. 轮询（复用导演台的帧数 ETA + 活性检测；3s 一查取消）
            video_rel = _poll_chain_history(engine, prompt_id, seconds, frames,
                                            report, check_cancel=check_cancel,
                                            on_cancel=_cancel_interrupt)
    finally:
        _get_comfy_proc().mark_idle()
        # 跑完必卸载,但仅在队列空闲时执行——若用户/其他任务已排队,
        # 贸然 /free 会把运行中任务的模型抽走(2026-08-30 排查修正);
        # keep_loaded: 后端视频队列还有后继 H3 任务(2026-09-02),接力
        # 镜免整轮权重重载
        try:
            if keep_loaded:
                log_note = "队列后继为 H3,跳过卸载(接力免重载)"
                print(f"[h3_chain] {log_note}", flush=True)
            else:
                q = engine._api("GET", "/queue", timeout=5.0) or {}
                busy = bool((q.get("queue_running") or [])
                            or (q.get("queue_pending") or []))
                if busy:
                    log_note = "队列非空,跳过卸载(由后续任务接管显存)"
                    print(f"[h3_chain] {log_note}", flush=True)
                else:
                    engine.unload()
        except Exception:  # noqa: BLE001 - 卸载探测失败按原样尝试卸载
            if not keep_loaded:
                engine.unload()
        finally:
            shutil.rmtree(Path(_COMFY_INPUT) / f"h3chain_{task_id}",
                          ignore_errors=True)

    # 5. 收片：final MP4 → 项目视频目录
    src = (_COMFY_OUTPUT / video_rel)
    if not src.is_file():
        raise ApiError(60003, f"链式成片缺失: {video_rel}")
    dest_dir = DATA_DIR / "videos" / str(row_ids[0])[:16]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{task_id}.mp4"
    # 中间帧工作目录定位（move 前取，src 移走后路径仍在但文件不在）：
    # 只回收本任务 h3chain_<task_id> 目录，绝不触碰 h3_chains 根
    run_dir = next((a for a in src.parents
                    if a.name == f"h3chain_{task_id}"), None)
    shutil.move(str(src), dest)
    shutil.rmtree(Path(_COMFY_INPUT) / f"h3chain_{task_id}",
                  ignore_errors=True)  # 参考图副本回收(防磁盘泄漏)
    # 中间帧工作目录回收：成片已剪切走，目录里只剩链式过程帧，留存会
    # 无限累积（2026-08-31 实测 output/h3_chains 积压 5.4G）
    if run_dir is not None and run_dir != _COMFY_OUTPUT \
            and _COMFY_OUTPUT in run_dir.parents:
        shutil.rmtree(run_dir, ignore_errors=True)
    report(1.0, "完成")
    total_seconds = sum(s["length"] for s in shots) / 24.0
    return {"file_path": str(dest), "resolution": f"{width}x{height}",
            "duration_seconds": total_seconds,
            "run_name": run_name, "start_clip": start_clip}


def _poll_chain_history(engine: Any, prompt_id: str, seconds: float,
                        frames: int, report: Callable[[float, str], None],
                        check_cancel: Callable[[], None] | None = None,
                        on_cancel: Callable[[], None] | None = None) -> str:
    """链式轮询：整体 ETA 按帧数线性 + 队列活性检测（复用导演台口径）。

    check_cancel：每轮（3s）取消检查，命中经 on_cancel 中断 ComfyUI
    当前 prompt 后原样上抛（2026-09-02 视频队列取消接线）。
    """
    eta = max(seconds * 3.0, frames * 5.5 + 6 * 5.0)
    deadline = time.monotonic() + eta + 900.0
    start = time.monotonic()
    while time.monotonic() < deadline:
        if check_cancel is not None:
            try:
                check_cancel()
            except Exception:
                if on_cancel is not None:
                    on_cancel(engine)
                raise
        time.sleep(3.0)
        report(min(0.06 + 0.9 * (time.monotonic() - start) / eta,
                   0.96), "链式采样中")
        hist = engine._api("GET", f"/history/{prompt_id}", timeout=5.0)
        if hist is None:
            continue
        entry = hist.get(prompt_id) or {}
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            import json as _j
            raise ApiError(60003, "链式执行失败: " + _j.dumps(
                status.get("messages") or [], ensure_ascii=False)[:400])
        for node_out in (entry.get("outputs") or {}).values():
            for item in (node_out.get("images") or node_out.get("videos") or []):
                fn = str(item.get("filename") or "")
                sub = str(item.get("subfolder") or "")
                if fn.endswith(".mp4") and "final" in sub:
                    report(0.98, "拼装完成")
                    return f"{sub}/{fn}" if sub else fn
        if status.get("completed"):
            raise ApiError(60003, "链式执行完成但无成片产物")
        q = engine._api("GET", "/queue", timeout=5.0)
        if q is not None:
            busy = {str(it[1]) for it in (q.get("queue_running") or [])}
            pend = {str(it[1]) for it in (q.get("queue_pending") or [])}
            if prompt_id not in busy | pend:
                raise ApiError(60004, "链式任务已被中断（不在队列且无产物）")
    raise ApiError(60004, "链式生成超时", )
