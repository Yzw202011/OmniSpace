"""双采加速产线 A/B 实测驱动（2026-10-24，证据归档副本）。

同一条犬吠分镜三路对比：
  B0 现行链 480p 既有片（免费基线）
  B1 turbo 双采 720p = turbo 6 步粗采（480p 画布 ×1.56 放大）
     + 2 步 denoise 0.24 低σ精修（大神 Deferred Upscale 原方）
  B2 现行链 720p = 非 turbo 10 步两阶段（重出基线）

实测结论（logs 见 driver.log）：
  B1 = 1275s（21.3 分钟）label=h3_chain_turbo2s_s6，1344×768
  B2 = 2478s（41.3 分钟）label=h3_chain_v10_s10_2s，1344×768
  → 同分辨率快 48%；同 seed 构图一致，铁艺围栏/狗脸细节 B1 持平或略优，
  B2 在 7.5s 出现上衣漂灰（B1 保持白T）。
两次失败尝试记录：×2 到 960p 的精修窗口 16G 卡两次整进程崩
（offload_after_upscale=True 也救不了）→ 960p/1440p 档加闸
manga.h3_two_pass_x2 默认关；turbo 节点 bypass 不支持自身叠加
（1963@0.7 二次挂载 NoneType 崩）→ 精修用全强度 turbo 模型。
"""
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(r"E:\OmniSpace")
sys.path.insert(0, str(ROOT))

ROW = "642c4fb8ce4f4f49b2f516113ecb2795"
B0 = (r"E:\OmniSpace\data\videos\3f6f322a4a28434f"
      r"\c12cd3b2d1084cf6a94bf7b6a0049eb9_fr.mp4")
DB_PATH = ROOT / "data" / "omnispace.db"
OUT_DIR = ROOT / "data" / "videos" / "_ab_twopass"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run(quality: str, steps: int | None, tag: str,
        turbo: bool | None = None) -> dict:
    from src.services.inference.h3_chain_engine import run_h3_chain_task
    task_id = uuid.uuid4().hex
    log(f"=== {tag} 开始 quality={quality} steps={steps} turbo={turbo} "
        f"task={task_id} ===")
    t0 = time.time()

    def cb(frac: float, stage: str = "") -> None:
        log(f"{tag} {frac*100:5.1f}% {stage}")

    result = run_h3_chain_task(task_id, [ROW], 10.0, quality, aspect="16:9",
                               progress_cb=cb, steps_override=steps,
                               turbo_override=turbo)
    dt = time.time() - t0
    log(f"=== {tag} 完成 {dt:.0f}s label={result.get('engine_label')} "
        f"file={result.get('file_path')} res={result.get('resolution')} ===")
    return {**result, "_elapsed": dt, "_tag": tag}


def strip_frames(video: str, tag: str, size: tuple[int, int]) -> list:
    from PIL import Image

    from src.services.encoder_service import get_encoder_service
    enc = get_encoder_service().ffmpeg_path
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    ts = [0.5, 2.5, 4.5, 7.5]
    imgs = []
    for t in ts:
        p = OUT_DIR / f"{tag}_t{int(t*10):03d}.png"
        subprocess.run([enc, "-y", "-ss", str(t), "-i", video,
                        "-frames:v", "1", "-update", "1", "-q:v", "1",
                        str(p)], capture_output=True, timeout=60,
                       creationflags=flags)
        if p.is_file():
            imgs.append((t, Image.open(p).convert("RGB").resize(size)))
    return imgs


def contact_sheet(rows: list[tuple[str, str, tuple[int, int]]]) -> Path:
    from PIL import Image, ImageDraw
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    w, h = 560, 315
    cols = 4
    sheets = []
    for label, video, _size in rows:
        sheets.append((label, strip_frames(video, label, (w, h))))
    rh = h + 26
    canvas = Image.new("RGB", (w * cols + 50, rh * len(sheets) + 10),
                       (24, 24, 24))
    d = ImageDraw.Draw(canvas)
    for ri, (label, imgs) in enumerate(sheets):
        for ci, (t, img) in enumerate(imgs):
            x, y = ci * (w + 12), ri * rh + 22
            canvas.paste(img, (x, y))
            d.text((x + 4, y - 17), f"{label}  {t}s", fill=(255, 230, 130))
    out = OUT_DIR / "ab_comparison.png"
    canvas.save(out)
    log(f"对比图: {out}")
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results = {}
    b1 = run("720p", 6, "B1turbo双采720p", turbo=True)
    results["B1"] = {"label": b1["engine_label"],
                     "res": b1["resolution"], "sec": round(b1["_elapsed"]),
                     "file": b1["file_path"]}
    b2 = run("720p", None, "B2现行720p", turbo=False)
    results["B2"] = {"label": b2["engine_label"],
                     "res": b2["resolution"], "sec": round(b2["_elapsed"]),
                     "file": b2["file_path"]}
    if Path(B0).is_file():
        results["B0"] = {"label": "h3_chain_v10_s10(现行480p既有片)",
                         "res": "864x480", "sec": 1156, "file": B0}
    comp = contact_sheet([
        ("B0现行480p", B0, (560, 315)),
        ("B1turbo双采720p", b1["file_path"], (560, 315)),
        ("B2现行720p", b2["file_path"], (560, 315)),
    ])
    results["comparison"] = str(comp)
    (OUT_DIR / "summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    log("SUMMARY " + json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
