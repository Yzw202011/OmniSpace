"""精确复现 COMIC-118 测试场景：64x64 纯色 PNG + 1s/720p/24fps。"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"e:\OmniSpace")
from backend.services.encoder_service import get_encoder_service
from backend.services.inference.video_engine import render_kenburns_frames
from tests.flow.harness import tiny_png_b64

out = Path(tempfile.mkdtemp(prefix="enc_probe2_"))
frame_dir = out / "frames"
video = out / "out.mp4"

enc = get_encoder_service()
n = render_kenburns_frames("主角走入宫殿", tiny_png_b64(), frame_dir,
                           1280, 720, fps=24, duration_s=1.0)
print("frames:", n)

for encoder, enc_args in enc._encoder_candidates("h264", "medium"):
    args = [enc.ffmpeg_path, "-y", "-framerate", "24", "-i",
            str(frame_dir / "frame_%05d.jpg"),
            "-c:v", encoder, *enc_args, "-pix_fmt", "yuv420p",
            "-vf", "scale=1280:720", str(video)]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=120,
                          encoding="utf-8", errors="replace")
    size = video.stat().st_size if video.is_file() else -1
    dur = enc._probe_duration(video) if video.is_file() else -1
    print(f"{encoder}: rc={proc.returncode} size={size}B duration={dur} "
          f"verify={enc._verify_output(video) if video.is_file() else 'N/A'}")
    if proc.returncode != 0:
        print("  stderr:", (proc.stderr or "")[-300:])
    video.unlink(missing_ok=True)
