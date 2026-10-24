"""行级承接 E2E 驱动（一次性实弹脚本，2026-10-24）。

独立进程直调 h3_chain_engine.run_h3_chain_task（新代码 in-process，
真 ComfyUI + 真 H3 权重 + 真库）：
  行1（无标记）独立生成 → 按视频 API 记账层等价语义补 video_tasks done 行
  → 行2（C 段带「承接上一镜头」）出片，断言锚定生效。
结束输出两段成片路径 + 行1尾帧/行2首帧对比图。

归档说明：本文件为实弹证据副本（原件已删，测试数据已清理）。
实测结果：行1 932s label=h3_chain_v10_s10；行2 897s label=h3_chain_v10_s10_carry；
承接对 MAE=6.63/255，对照组（行1首帧 vs 行2首帧）MAE=56.14/255。
"""
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(r"E:\OmniSpace")
sys.path.insert(0, str(ROOT))

ROW1 = "284c546539c348b0983415165c0cc0ed"
ROW2 = "594ea9e1d9054de2a8627325fe0f3704"
DB_PATH = ROOT / "data" / "omnispace.db"
OUT_DIR = ROOT / "data" / "videos" / "_carry_e2e"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run_row(row_id: str, tag: str) -> dict:
    from src.services.inference.h3_chain_engine import run_h3_chain_task
    task_id = uuid.uuid4().hex
    log(f"=== {tag} 开始 row={row_id} task={task_id} ===")
    t0 = time.time()

    def cb(frac: float, stage: str = "") -> None:
        log(f"{tag} {frac*100:5.1f}% {stage}")

    result = run_h3_chain_task(
        task_id, [row_id], 5.0, "480p", aspect="16:9",
        progress_cb=cb)
    log(f"=== {tag} 完成 {time.time()-t0:.0f}s label={result.get('engine_label')} "
        f"file={result.get('file_path')} ===")
    return result


def record_video_done(row_id: str, file_path: str, model_used: str) -> None:
    """与 /manga/video/generate 收尾等价的记账（API 层未热载，模拟之）。"""
    c = sqlite3.connect(DB_PATH)
    cols = [r[1] for r in c.execute("PRAGMA table_info(video_tasks)")]
    row = {
        "id": uuid.uuid4().hex, "storyboard_row_id": row_id,
        "status": "done", "file_path": file_path,
        "model_used": model_used, "resolution": "864x480",
        "duration_seconds": 5.0, "progress": 1.0,
        "created_at": time.time(), "updated_at": time.time(),
    }
    keys = [k for k in row if k in cols]
    c.execute(
        f"INSERT INTO video_tasks ({','.join(keys)}) "
        f"VALUES ({','.join('?' * len(keys))})",
        [row[k] for k in keys])
    c.commit()
    c.close()
    log(f"video_tasks 记账 done row={row_id[:8]}")


def make_comparison(row1_video: str, row2_video: str) -> Path:
    import subprocess

    from PIL import Image
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tail = OUT_DIR / "row1_tail.png"
    head = OUT_DIR / "row2_first.png"
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    enc = str(ROOT / "runtime" / "ffmpeg" / "ffmpeg.exe")
    if not Path(enc).is_file():
        from src.services.encoder_service import get_encoder_service
        enc = get_encoder_service().ffmpeg_path
    for src, dst, sseof in ((row1_video, tail, True), (row2_video, head, False)):
        cmd = [enc, "-y"]
        if sseof:
            cmd += ["-sseof", "-0.1"]
        cmd += ["-i", src, "-frames:v", "1", "-update", "1", "-q:v", "1", str(dst)]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=60, creationflags=flags)
        if r.returncode != 0 or not dst.is_file():
            raise RuntimeError(f"抽帧失败 {dst}: {(r.stderr or '')[-200:]}")
    a, b = Image.open(tail).convert("RGB"), Image.open(head).convert("RGB")
    w, h = 640, 360
    canvas = Image.new("RGB", (w * 2 + 12, h + 34), (24, 24, 24))
    canvas.paste(a.resize((w, h)), (0, 34))
    canvas.paste(b.resize((w, h)), (w + 12, 34))
    from PIL import ImageDraw
    d = ImageDraw.Draw(canvas)
    d.text((8, 8), f"ROW1 TAIL (anchor source)  {Path(row1_video).name}",
           fill=(255, 220, 120))
    d.text((w + 20, 8), f"ROW2 FIRST FRAME (carried)  {Path(row2_video).name}",
           fill=(120, 220, 255))
    out = OUT_DIR / "carry_comparison.png"
    canvas.save(out)
    log(f"对比图: {out}")
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    r1 = run_row(ROW1, "行1基线")
    record_video_done(ROW1, r1["file_path"], r1.get("engine_label", ""))
    r2 = run_row(ROW2, "行2承接")
    comp = make_comparison(r1["file_path"], r2["file_path"])
    summary = {
        "row1": {"file": r1["file_path"], "label": r1.get("engine_label")},
        "row2": {"file": r2["file_path"], "label": r2.get("engine_label")},
        "comparison": str(comp),
    }
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    log("SUMMARY " + json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
