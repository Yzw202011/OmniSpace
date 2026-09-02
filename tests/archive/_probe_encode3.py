"""复现 COMIC-118/120 编码失败：与后端 generate_fallback_video 同参数同路径。"""
import base64
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, r"e:\OmniSpace")

from backend.services.encoder_service import get_encoder_service  # noqa: E402
from backend.services.inference.video_engine import (  # noqa: E402
    VideoGenerateRequest,
    generate_fallback_video,
)


def tiny_png_b64() -> str:
    # 1x1 透明 PNG
    raw = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000d49444154789c626001000000ffff030000060005"
        "57bfabd40000000049454e44ae426082")
    return base64.b64encode(raw).decode()


def main() -> int:
    enc = get_encoder_service()
    print(f"ffmpeg={enc._ffmpeg} ffprobe={enc._ffprobe} available={enc.available}")
    print(f"encoders={ {k: v for k, v in enc._encoders.items() if v} }")

    tmp = Path(tempfile.mkdtemp(prefix="encprobe_"))
    out = tmp / "out.mp4"
    req = VideoGenerateRequest(
        storyboard_row_id="probe",
        description="主角走入宫殿",
        screenshot_4in1=tiny_png_b64(),
        character_assets=["主角"],
        resolution="720p", fps=24, duration_seconds=1, codec="h264",
    )
    t0 = time.time()
    try:
        info = generate_fallback_video(req, out, None)
        print(f"OK: {info}")
    except Exception as exc:
        print(f"FAIL({time.time()-t0:.2f}s): {type(exc).__name__}: {exc}")
        # 手动逐步诊断：帧是否生成、ffmpeg 命令手工跑一遍
        fdir = tmp / f"{out.stem}_frames"
        print(f"frame_dir exists={fdir.exists()}")
        if fdir.exists():
            files = sorted(fdir.glob("frame_*.jpg"))
            print(f"frames={len(files)} first={files[:2]}")
        print(f"out exists={out.exists()}", end="")
        if out.exists():
            print(f" size={out.stat().st_size}")
        else:
            print()
        # 手工重渲染帧再手工编码，抓 stderr
        from backend.services.inference.video_engine import render_kenburns_frames
        f2 = tmp / "frames2"
        n = render_kenburns_frames(req.description, req.screenshot_4in1,
                                   f2, 1280, 720, 24, 1)
        print(f"rerender frames={n}")
        import subprocess
        args = [enc._ffmpeg, "-y", "-framerate", "24",
                "-i", str(f2 / "frame_%05d.jpg"),
                "-c:v", "libx264", "-crf", "23", "-preset", "medium",
                "-pix_fmt", "yuv420p", "-vf", "scale=1280:720",
                str(tmp / "manual.mp4")]
        print("cmd:", " ".join(args))
        p = subprocess.run(args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        print(f"rc={p.returncode}")
        print("stderr tail:", (p.stderr or "")[-800:])
        m = tmp / "manual.mp4"
        if m.exists():
            print(f"manual size={m.stat().st_size}")
            print("ffprobe:", enc._probe_duration(m))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
