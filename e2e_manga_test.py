"""OmniSpace 漫剧模块 E2E 压测脚本（分阶段执行，状态落盘 e2e_state.json）。"""
import base64
import io
import json
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import requests

BASE = "http://127.0.0.1:5800"
API = BASE + "/api/v1"
ROOT = Path(r"e:\OmniSpace")
DATA_DIR = ROOT / "data"
VIDEO_OUT_DIR = DATA_DIR / "generated" / "videos"
STATE_FILE = ROOT / "e2e_state.json"

PROJECT_NAME = "E2E压测-漫剧全流程"
SCRIPT_LINES = [
    "夜晚的天台上，阿杰独自望着城市的灯火。",
    "小雨轻轻推开天台的门，走到阿杰身边。",
    "阿杰转过身，眼里满是惊讶。",
    "小雨微笑着递给他一杯热咖啡。",
    "两人在天台上并肩坐下，聊起了往事。",
    "突然，远处传来一阵警笛声。",
    "阿杰站起身，坚定地说必须马上出发。",
]


def load_state() -> dict:
    if STATE_FILE.is_file():
        return json.loads(STATE_FILE.read_text("utf-8"))
    return {"samples": [], "results": {}, "video_files": [], "task_ids": []}


def save_state(st: dict) -> None:
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), "utf-8")


def vram_mb() -> int:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True, timeout=15).strip()
        return int(out.splitlines()[0])
    except Exception:
        return -1


def loaded_models() -> list:
    try:
        r = requests.get(f"{API}/models/vram", timeout=15)
        d = r.json().get("data") or {}
        return [m["model_id"] for m in d.get("loaded_models", [])]
    except Exception:
        return ["<查询失败>"]


def sample(st: dict, event: str) -> None:
    s = {"t": time.strftime("%H:%M:%S"), "event": event,
         "vram_mb": vram_mb(), "loaded": loaded_models()}
    st["samples"].append(s)
    save_state(st)
    print(f"  [采样] {event} | 显存 {s['vram_mb']}MB | 已加载 {s['loaded']}")


def req(method: str, path: str, timeout: int = 60, raw: bool = False, **kw):
    url = path if path.startswith("http") else API + path
    r = requests.request(method, url, timeout=timeout, **kw)
    if raw:
        return r
    try:
        body = r.json()
    except Exception:
        return r.status_code, {"_non_json": r.text[:500]}
    return r.status_code, body


def report(st: dict, key: str, passed, detail: str) -> None:
    st["results"][key] = {"pass": passed, "detail": detail}
    save_state(st)
    tag = "PASS" if passed is True else ("FAIL" if passed is False else "WARN")
    print(f"  [{tag}] {key}: {detail}")


def p0(st):
    print("== 阶段0 基线 ==")
    r = requests.get(f"{BASE}/health", timeout=15)
    report(st, "p0.health", r.status_code == 200, f"HTTP {r.status_code} {r.text[:200]}")
    code, body = req("GET", "/models/vram")
    gpu = (body.get("data") or {}).get("gpu", {})
    report(st, "p0.gpu", code == 200,
           f"gpu={gpu.get('gpu_name','?')} available={gpu.get('available')}")
    code, body = req("GET", "/models/list")
    models = (body.get("data") or {}).get("models", [])
    dlg = [m["id"] for m in models if m.get("category") == "dialog"]
    dlg_dl = [m["id"] for m in models if m.get("category") == "dialog" and m.get("downloaded")]
    st["dialog_models"] = dlg
    st["dialog_downloaded"] = dlg_dl
    save_state(st)
    print(f"  dialog 模型: {dlg} | 已下载: {dlg_dl}")
    sample(st, "阶段0基线")


def p1(st):
    print("== 阶段1 项目+剧本 ==")
    code, body = req("POST", "/comic/project/create",
                     json={"name": PROJECT_NAME, "work_mode": "regular"})
    ok = code == 200 and body.get("success")
    if not ok:
        report(st, "p1.create", False, f"HTTP {code} {json.dumps(body, ensure_ascii=False)[:300]}")
        return
    pid = body["data"]["project_id"]
    st["project_id"] = pid
    report(st, "p1.create", True, f"project_id={pid}")

    code, body = req("POST", f"/storyboard/{pid}/auto-split",
                     json={"script": "\n".join(SCRIPT_LINES)})
    ok = code == 200 and body.get("success")
    report(st, "p1.split", ok, f"added={len((body.get('data') or {}).get('added', []))} "
                               f"total={(body.get('data') or {}).get('total')}")
    if not ok:
        return
    rows = body["data"]["added"]
    st["row_ids"] = [r["id"] for r in rows]
    # 为实体推理回填角色/场景（auto-split 只填台词，characters/scene 由行更新写入）
    for rid in st["row_ids"][:5]:
        req("PUT", f"/storyboard/{pid}/rows/{rid}",
            json={"characters": ["阿杰", "小雨"], "scene": "天台", "props": ["热咖啡"]})
    code, body = req("GET", f"/storyboard/{pid}")
    n = len((body.get("data") or {}).get("rows", []))
    report(st, "p1.rows", n == len(SCRIPT_LINES), f"分镜行数={n}（期望 {len(SCRIPT_LINES)}）")
    save_state(st)


def p2(st):
    print("== 阶段2 实体推理+角色生图 ==")
    pid = st["project_id"]
    code, body = req("POST", "/comic/asset/infer-entities", json={"project_id": pid})
    created = (body.get("data") or {}).get("created", {})
    report(st, "p2.infer", code == 200 and created.get("character", 0) >= 2,
           f"created={created}")
    items = (body.get("data") or {}).get("items", [])
    aj = next((a for a in items if a.get("name") == "阿杰"), None)
    if aj is None:
        code, body = req("GET", f"/comic/asset/library?project_id={pid}")
        items = (body.get("data") or {}).get("items", [])
        aj = next((a for a in items if a.get("name") == "阿杰"), None)
    if aj:
        st["aj_asset_id"] = aj.get("asset_id") or aj.get("id")

    sample(st, "阶段2 turnaround 前")
    t0 = time.time()
    code, body = req("POST", "/comic/asset/generate-turnaround", timeout=300,
                     json={"project_id": pid, "name": "阿杰",
                           "prompt": "阿杰，25岁中国青年男性，黑色短发，深灰色夹克，"
                                     "眼神坚毅，写实漫剧风格"})
    dt = time.time() - t0
    data = body.get("data") or {}
    ok = code == 200 and body.get("success")
    report(st, "p2.turnaround", ok,
           f"HTTP {code} 耗时{dt:.0f}s model={data.get('model') or data.get('model_used')} "
           f"degraded={data.get('degraded')} keys={list(data.keys())[:12]}")
    if ok:
        st["turnaround_model"] = data.get("model") or data.get("model_used")
        st["turnaround_degraded"] = data.get("degraded")
    sample(st, "阶段2 turnaround 后（SDXL 应在列）")
    save_state(st)


def _pick_dialog_model(st) -> str:
    dl = st.get("dialog_downloaded") or []
    for m in dl:
        if "qwen3-vl" in m.lower():
            return m
    return dl[0] if dl else ""


def p3(st):
    print("== 阶段3 分镜生词（LLM 加载/显存调度）==")
    pid = st["project_id"]
    sample(st, "阶段3 LLM 加载前")
    mid = _pick_dialog_model(st)
    if not mid:
        report(st, "p3.llm_load", None, "无已下载 dialog 模型，跳过加载，直接试 ai-describe")
    else:
        t0 = time.time()
        code, body = req("POST", "/models/load", json={"model_id": mid}, timeout=600)
        dt = time.time() - t0
        ok = code == 200 and body.get("success")
        err = (body.get("error") or {})
        detail = (f"model={mid} HTTP {code} 耗时{dt:.0f}s "
                  f"loaded={ok} err={err.get('code') or ''}:{(err.get('message') or '')[:120]}")
        report(st, "p3.llm_load", ok, detail)
        st["llm_load_ok"] = ok
    sample(st, "阶段3 LLM 加载后（检查 SDXL 是否被驱逐）")
    described = 0
    for rid in st["row_ids"][:2]:
        t0 = time.time()
        code, body = req("POST", "/storyboard/ai-describe",
                         json={"row_id": rid, "project_id": pid}, timeout=300)
        data = body.get("data") or {}
        desc = (data.get("description") or "").strip()
        err = (body.get("error") or {})
        if code == 200 and desc:
            described += 1
            req("PUT", f"/storyboard/{pid}/rows/{rid}", json={"description": desc})
            print(f"    行 {rid[:8]} 描述词({len(desc)}字, {time.time()-t0:.0f}s, "
                  f"model={data.get('model')}): {desc[:60]}...")
        else:
            print(f"    行 {rid[:8]} 失败 HTTP {code}: "
                  f"{err.get('code')} {(err.get('message') or '')[:150]}")
    # 写回校验
    code, body = req("GET", f"/storyboard/{pid}")
    rows = (body.get("data") or {}).get("rows", [])
    written = sum(1 for r in rows if r.get("id") in st["row_ids"][:2]
                  and (r.get("description") or "").strip()
                  and not r["description"].startswith("（AI 自动生成）"))
    report(st, "p3.ai_describe", described >= 1,
           f"成功 {described}/2 行，写回校验 {written}/2 行")
    save_state(st)


def p4(st):
    print("== 阶段4 分镜生图（第二次显存 swap）==")
    pid = st["project_id"]
    rid = st["row_ids"][0]
    sample(st, "阶段4 keyframe 前")
    t0 = time.time()
    code, body = req("POST", "/manga/keyframe/generate",
                     json={"row_id": rid, "project_id": pid}, timeout=300)
    dt = time.time() - t0
    data = body.get("data") or {}
    ok = code == 200 and body.get("success")
    report(st, "p4.keyframe", ok,
           f"HTTP {code} 耗时{dt:.0f}s version={data.get('version')} "
           f"file={data.get('file_path')}")
    if ok:
        st["keyframe"] = data
        # media 回读 200 校验
        r = req("GET", f"/manga/media/{data['file_path']}", raw=True, timeout=30)
        report(st, "p4.media", r.status_code == 200,
               f"GET media HTTP {r.status_code} {len(r.content)}B "
               f"png_sig={r.content[:4] == bytes([0x89,0x50,0x4E,0x47])}")
    sample(st, "阶段4 keyframe 后（SDXL 应重新在列）")
    save_state(st)


def _wait_video(st, task_id: str, timeout_s: int = 600) -> dict:
    """轮询视频状态，返回 {final, progresses}。"""
    progresses = []
    t0 = time.time()
    final = {}
    while time.time() - t0 < timeout_s:
        code, body = req("GET", f"/video/{task_id}/status", timeout=30)
        d = body.get("data") or {}
        stt = d.get("status", "?")
        prog = d.get("progress", 0.0)
        if not progresses or progresses[-1][1] != prog or stt in ("done", "error", "cancelled"):
            progresses.append((round(time.time() - t0, 1), prog, stt))
            print(f"    轮询 t={time.time()-t0:6.1f}s status={stt} progress={prog} "
                  f"degraded={d.get('degraded')}")
        if stt in ("done", "error", "cancelled"):
            final = d
            break
        time.sleep(2.5)
    return {"final": final, "progresses": progresses}


def p5(st):
    print("== 阶段5 视频生成（诚实性核心验证）==")
    rid = st["row_ids"][0]
    kf = st.get("keyframe") or {}
    shot_b64 = ""
    kf_file = DATA_DIR / (kf.get("file_path") or "")
    if kf_file.is_file():
        shot_b64 = base64.b64encode(kf_file.read_bytes()).decode()
    else:
        shot_b64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
                    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    sample(st, "阶段5 视频生成前")
    code, body = req("POST", "/video/generate", timeout=60,
                     json={"storyboard_row_id": rid,
                           "description": "阿杰与小雨在夜晚天台并肩交谈，镜头缓慢推近",
                           "screenshot_4in1": shot_b64,
                           "duration_seconds": 3, "resolution": "720p", "fps": 24})
    data = body.get("data") or {}
    ok = code == 200 and body.get("success") and data.get("task_id")
    if not ok:
        report(st, "p5.create", False,
               f"HTTP {code} {json.dumps(body, ensure_ascii=False)[:300]}")
        return
    tid = data["task_id"]
    st["task_ids"].append(tid)
    # 诚实性验收点1：创建响应不得恒带 degraded:true；不得预设 model_used
    honest_create = ("degraded" not in data or data.get("degraded") is False) \
        and not data.get("model_used")
    report(st, "p5.create", True, f"task_id={tid} 创建响应 keys={list(data.keys())}")
    report(st, "p5.honest_create", honest_create,
           f"创建响应 degraded={data.get('degraded')} model_used={data.get('model_used')!r}")
    save_state(st)

    result = _wait_video(st, tid, 600)
    final = result["final"]
    progs = result["progresses"]
    st["p5_progress_trace"] = progs
    # 验收点2：进度分段（出现过中间段值，非 0.1 钉死后直接 1.0）
    vals = [p[1] for p in progs]
    mids = [v for v in vals if 0.15 <= v <= 0.95]
    segmented = bool(mids) and max(vals) >= 1.0 if final.get("status") == "done" else bool(mids)
    report(st, "p5.progress_segments", segmented,
           f"进度轨迹={[(p[1], p[2]) for p in progs]}")
    # 验收点3：完成后 model_used 如实回填 + degraded 三处一致
    code, body = req("GET", f"/video/{tid}/result", timeout=30)
    rdata = (body.get("data") or {}).get("result") or {}
    mu = rdata.get("model_used", "")
    degraded_result = rdata.get("degraded")
    code2, body2 = req("GET", f"/video/{tid}/status", timeout=30)
    degraded_status = (body2.get("data") or {}).get("degraded")
    is_fb = "fallback" in mu or "kenburns" in mu
    consistent = (final.get("status") == "done" and bool(mu)
                  and ((is_fb and degraded_result and degraded_status)
                       or (not is_fb and not degraded_result and not degraded_status)))
    report(st, "p5.model_used_honest", consistent,
           f"status={final.get('status')} model_used={mu!r} "
           f"degraded(result)={degraded_result} degraded(status)={degraded_status} "
           f"gen_ms={rdata.get('generation_time_ms')}")
    st["p5_model_used"] = mu
    # 下载 + ftyp 魔数
    r = req("GET", f"/video/{tid}/download", raw=True, timeout=60)
    head = r.content[:16]
    has_ftyp = b"ftyp" in head
    report(st, "p5.download", r.status_code == 200 and has_ftyp,
           f"HTTP {r.status_code} {len(r.content)}B head={head!r}")
    if r.status_code == 200 and rdata.get("file_path"):
        st["video_files"].append(rdata["file_path"])
    sample(st, "阶段5 视频生成后")
    save_state(st)


def p6(st):
    print("== 阶段6 取消路径+孤本清理 ==")
    rid = st["row_ids"][1]
    before = {p.name for p in VIDEO_OUT_DIR.glob("*.mp4")} if VIDEO_OUT_DIR.is_dir() else set()
    code, body = req("POST", "/video/generate", timeout=60,
                     json={"storyboard_row_id": rid,
                           "description": "小雨推开天台的门走向阿杰，镜头横移",
                           "screenshot_4in1": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
                                              "AAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
                           "duration_seconds": 5, "resolution": "720p", "fps": 24})
    data = body.get("data") or {}
    tid = data.get("task_id")
    if not tid:
        report(st, "p6.cancel", False,
               f"第二任务创建失败 HTTP {code} {json.dumps(body, ensure_ascii=False)[:200]}")
        return
    st["task_ids"].append(tid)
    save_state(st)
    time.sleep(6)  # 等任务进入渲染循环
    code, body = req("POST", f"/video/{tid}/cancel", timeout=30)
    cstat = (body.get("data") or {}).get("status")
    # 轮询确认终态
    time.sleep(3)
    code, body = req("GET", f"/video/{tid}/status", timeout=30)
    final_stat = (body.get("data") or {}).get("status")
    after = {p.name for p in VIDEO_OUT_DIR.glob("*.mp4")} if VIDEO_OUT_DIR.is_dir() else set()
    orphans = after - before
    cancelled = cstat == "cancelled" and final_stat == "cancelled"
    report(st, "p6.cancel", cancelled,
           f"cancel 响应 status={cstat}，3s 后 status={final_stat}")
    report(st, "p6.no_orphan", not orphans,
           f"视频目录新增 mp4={sorted(orphans) or '无'}")
    save_state(st)


def p7(st):
    print("== 阶段7 导出+清理 ==")
    pid = st["project_id"]
    code, body = req("POST", "/comic/export/bundle", json={"project_id": pid}, timeout=120)
    data = body.get("data") or {}
    ok = code == 200 and body.get("success")
    counts = data.get("contents", {})
    report(st, "p7.export", ok, f"contents={counts} file={data.get('file_path')}")
    if ok:
        r = req("GET", f"/manga/media/{data['file_path']}", raw=True, timeout=60)
        zip_ok = r.status_code == 200 and r.content[:2] == b"PK"
        names = []
        manifest = {}
        if zip_ok:
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            names = zf.namelist()
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        exp_rows = len(st["row_ids"])
        cnt_ok = (manifest.get("contents", {}).get("storyboard_rows") == exp_rows
                  and manifest.get("contents", {}).get("videos") == counts.get("videos"))
        report(st, "p7.zip", zip_ok and cnt_ok,
               f"HTTP {r.status_code} zip_entries={len(names)} manifest={manifest.get('contents')}")
        try:
            (DATA_DIR / data["file_path"]).unlink(missing_ok=True)
        except Exception:
            pass
    # 删除项目（级联验收）
    code, body = req("DELETE", f"/comic/project/{pid}", timeout=60)
    ok = code == 200 and body.get("success")
    report(st, "p7.delete", ok, f"HTTP {code} {json.dumps(body, ensure_ascii=False)[:200]}")
    # 残留检查
    leftovers = []
    code, body = req("GET", "/comic/project/list", timeout=30)
    items = (body.get("data") or {}).get("items", [])
    if any(p.get("id") == pid for p in items):
        leftovers.append("项目列表仍有该项目")
    code, body = req("GET", f"/storyboard/{pid}", timeout=30)
    rows = (body.get("data") or {}).get("rows", [])
    if code == 200 and rows:
        leftovers.append(f"分镜行残留 {len(rows)} 行")
    adir = DATA_DIR / "comic_assets" / pid
    if adir.is_dir() and any(adir.iterdir()):
        leftovers.append(f"磁盘残留 {adir}")
    for rid in st.get("row_ids", []):
        kdir = DATA_DIR / "keyframes" / rid
        if kdir.is_dir() and any(kdir.iterdir()):
            leftovers.append(f"磁盘残留 {kdir}")
    for vf in st.get("video_files", []):
        if vf and Path(vf).is_file():
            leftovers.append(f"视频文件残留 {vf}")
    for tid in st.get("task_ids", []):
        code, body = req("GET", f"/video/{tid}/status", timeout=15)
        if code == 200 and body.get("success"):
            leftovers.append(f"video_tasks 残留 {tid[:8]}")
    report(st, "p7.cascade", not leftovers,
           "；".join(leftovers) if leftovers else "DB+磁盘全清，无残留")
    save_state(st)


def p8(st):
    print("== 阶段8 收尾 ==")
    sample(st, "阶段8 收尾（卸载后显存应回落）")
    r = requests.get(f"{BASE}/health", timeout=15)
    report(st, "p8.health", r.status_code == 200, f"HTTP {r.status_code}")
    peak = max((s["vram_mb"] for s in st["samples"] if s["vram_mb"] > 0), default=0)
    report(st, "p8.vram_peak", 0 < peak <= 16384, f"显存峰值 {peak}MB（上限 16384MB）")
    save_state(st)
    print("\n== 采样序列 ==")
    for s in st["samples"]:
        print(f"  {s['t']} {s['event']}: {s['vram_mb']}MB loaded={s['loaded']}")
    print("\n== 结果汇总 ==")
    for k, v in st["results"].items():
        tag = "PASS" if v["pass"] is True else ("FAIL" if v["pass"] is False else "WARN")
        print(f"  [{tag}] {k}: {v['detail']}")


PHASES = {"0": p0, "1": p1, "2": p2, "3": p3, "4": p4, "5": p5,
          "6": p6, "7": p7, "8": p8}

if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "all"
    st = load_state()
    if phase == "all":
        for k in sorted(PHASES):
            PHASES[k](st)
    else:
        for k in phase.split(","):
            PHASES[k](st)
    save_state(st)
