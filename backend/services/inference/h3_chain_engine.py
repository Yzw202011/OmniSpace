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
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from ...config import DATA_DIR
from ...middleware.error_handler import ApiError
from .h3_engine import _COMFY_OUTPUT, align_h3_frames, get_h3_engine

_TEMPLATE = Path(__file__).parent / "h3_chain_workflow_api.json"
_COMFY_INPUT = (DATA_DIR.parent / "tools" / "ComfyUI_windows_portable"
                / "ComfyUI" / "input")
_QUALITY = {"480p": (864, 480), "720p": (1344, 768)}
_MAX_REFS = 7  # 与模板槽位一致（引擎上限 9,槽 8/9 预留）


def _split_names(value) -> list[str]:
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


def _collect_refs(row: dict, row_id) -> list[dict]:
    """按 角色→场景→道具 收集参考（comic_assets 真源，≤7）。

    绑定名与资产名做最长公共子串模糊匹配(≥2 字):行里写"行李箱"
    能命中资产"黑色行李箱",自由文本场景能命中"别墅区街角大门"。
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


def _six_section(description: str, refs: list[dict], total_seconds: float) -> str:
    subj = []
    retain = []
    for i, r in enumerate(refs, 1):
        kind_zh = {"character": "角色形象", "scene": "场景", "prop": "道具"}[r["kind"]]
        subj.append(f"<Picture {i}> 是{kind_zh}`{r['name']}`的唯一视觉参考。")
        retain.append(f"<Picture {i}> {r['name']}: fully_preserved。")
    return "\n".join([
        "subject_definitions:\n" + "\n".join(subj),
        "summary:\n[reference generation] " + description.strip()[:120],
        "retention_analysis:\n" + "\n".join(retain) +
        "\n<Subject 1>: fully_preserved - 全程保持参考图既定外观与服饰。",
        "detailed_description:\n" + description.strip(),
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
                      loop=None, progress_cb=None) -> dict:
    """链式生成主流程（阻塞,供后台线程调用）。

    row_ids: 多个分镜行 → 计划多镜(参考取各行绑定并集,≤7);
    start_clip>1: 断点续跑(沿用同 run_name 的已完成镜检查点);
    progress_cb(frac, stage) 用于回写 video_tasks.progress。
    返回 {"file_path": 绝对路径, "resolution": "WxH", "duration_seconds": s}。
    """
    from ...data.database import get_db_safe

    if quality not in _QUALITY:
        raise ApiError(60001, f"未知画质档位 {quality}")
    width, height = _QUALITY[quality]
    engine = get_h3_engine()
    run_name = run_name or f"h3chain_{task_id}"

    def report(frac, stage):
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
                    "SELECT description, characters, scene, props "
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
    plan = {"defaults": {"duration_seconds": seconds, "steps": 6},
            "shots": shots}
    plan_json = json.dumps(plan, ensure_ascii=False, indent=1)

    # 3. 组图提交（与导演台同锁,互斥 GUI/其他 H3 任务）;无论成败,结束必卸载权重
    report(0.04, "提交 H3 链式工作流")
    engine._ensure_running(lambda f, s: report(0.02 + f * 0.02, s))
    try:
        with engine._gen_lock:
            graph = _render_graph(json.loads(_TEMPLATE.read_text(encoding="utf-8")),
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

            # 4. 轮询（复用导演台的帧数 ETA + 活性检测）
            video_rel = _poll_chain_history(engine, prompt_id, seconds, frames,
                                            report)
    finally:
        # 跑完必卸载,但仅在队列空闲时执行——若用户/其他任务已排队,
        # 贸然 /free 会把运行中任务的模型抽走(2026-08-30 排查修正)
        try:
            q = engine._api("GET", "/queue", timeout=5.0) or {}
            busy = bool((q.get("queue_running") or [])
                        or (q.get("queue_pending") or []))
            if busy:
                log_note = "队列非空,跳过卸载(由后续任务接管显存)"
                print(f"[h3_chain] {log_note}", flush=True)
            else:
                engine.unload()
        except Exception:  # noqa: BLE001 - 卸载探测失败按原样尝试卸载
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
    shutil.move(str(src), dest)
    shutil.rmtree(Path(_COMFY_INPUT) / f"h3chain_{task_id}",
                  ignore_errors=True)  # 参考图副本回收(防磁盘泄漏)
    report(1.0, "完成")
    total_seconds = sum(s["length"] for s in shots) / 24.0
    return {"file_path": str(dest), "resolution": f"{width}x{height}",
            "duration_seconds": total_seconds,
            "run_name": run_name, "start_clip": start_clip}


def _poll_chain_history(engine, prompt_id: str, seconds: float,
                        frames: int, report) -> str:
    """链式轮询：整体 ETA 按帧数线性 + 队列活性检测（复用导演台口径）。"""
    eta = max(seconds * 3.0, frames * 5.5 + 6 * 5.0)
    deadline = time.monotonic() + eta + 900.0
    start = time.monotonic()
    while time.monotonic() < deadline:
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
