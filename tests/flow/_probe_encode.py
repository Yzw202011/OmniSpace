"""复现 COMIC-118 编码失败：渲染 Ken Burns 帧 → encode_frames_to_video。"""
import sys, tempfile, traceback
from pathlib import Path

sys.path.insert(0, r"e:\OmniSpace")

from backend.services.inference.video_engine import render_kenburns_frames
from backend.services.encoder_service import get_encoder_service

out = Path(tempfile.mkdtemp(prefix="enc_probe_"))
frame_dir = out / "frames"
video = out / "out.mp4"

enc = get_encoder_service()
print("ffmpeg:", enc.ffmpeg_path)
print("ffprobe:", enc.status().get("ffprobe"))
print("encoders:", {k: v for k, v in enc.status()["encoders"].items() if v})
print("hw:", enc.hardware_class)

n = render_kenburns_frames("主角走入宫殿", "", frame_dir, 1280, 720, fps=24, duration_s=2.0)
print("frames rendered:", n)
first = sorted(frame_dir.glob("frame_*.jpg"))[:2]
for f in first:
    print("  ", f.name, f.stat().st_size, "bytes")

# 逐个候选手动执行，捕捉 stderr 与校验细节
import subprocess, time
from backend.services.encoder_service import RESOLUTION_MAP, MIN_OUTPUT_BYTES
print("MIN_OUTPUT_BYTES =", MIN_OUTPUT_BYTES)

for encoder, enc_args in enc._encoder_candidates("h264", "medium"):
    args = [enc.ffmpeg_path, "-y", "-framerate", "24", "-i", str(frame_dir / "frame_%05d.jpg"),
            "-c:v", encoder, *enc_args, "-pix_fmt", "yuv420p", "-vf", "scale=1280:720", str(video)]
    print("\n=== 尝试", encoder, "===")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=120,
                          encoding="utf-8", errors="replace")
    print("rc =", proc.returncode)
    print("stderr tail:", (proc.stderr or "")[-600:])
    exists = video.is_file()
    size = video.stat().st_size if exists else -1
    print("输出存在:", exists, "大小:", size)
    if exists:
        dur = enc._probe_duration(video)
        print("ffprobe duration:", dur)
        print("_verify_output:", enc._verify_output(video))
    video.unlink(missing_ok=True)

print("\n输出目录:", out)
