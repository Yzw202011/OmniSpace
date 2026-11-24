"""批6 ASR 朗读泄漏检测（B段对照实验的量化指标）。

对四臂产物抽音轨 → whisper-tiny 转写 → 检出 B 段朗读/任何人声台词。
判读口径：
- 转写为空/纯环境噪声标记 = 无泄漏 ✓（现行吞除臂的预期）
- 转写出现成句中文 = 朗读泄漏（保留臂的预期风险）
whisper-tiny 幻觉敏感：空音频常输出「谢谢观看」类幻觉串——口径以
「是否出现与 B 段内容词重叠」为准，幻觉串不计泄漏。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BASE = Path(r"E:/OmniSpace/data/videos/h3_ab")
FFMPEG = r"E:/OmniSpace/runtime/ffmpeg/bin/ffmpeg.exe"
MODEL = r"E:/OmniSpace/models/whisper-tiny"

# B段内容词（两题材各取高辨识度词；转写命中即计泄漏）
B_WORDS = {
    "P6a": ["客栈", "掌柜", "灯笼", "烧酒", "江湖", "边镇", "恩怨", "风雪"],
    "P6b": ["实验室", "舷窗", "星海", "空间站", "研究员", "数据", "轨道", "失重"],
}
# whisper 空音频常见幻觉串（不计泄漏）
_HALLUCINATION = ("谢谢", "观看", "字幕", "请不", "by", "订阅")


def transcribe(mp4: Path) -> str:
    wav = mp4.with_suffix(".wav")
    if not wav.exists():
        subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-i", str(mp4), "-vn",
             "-ac", "1", "-ar", "16000", str(wav)], check=True)
    import wave

    import numpy as np
    with wave.open(str(wav), "rb") as w:
        raw = w.readframes(w.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0
    from transformers import pipeline
    asr = pipeline("automatic-speech-recognition", model=MODEL)
    result = asr(audio)
    return str(result.get("text") or "").strip()


def main() -> None:
    targets = sys.argv[1:] or ["P6a-on", "P6a-off", "P6b-on", "P6b-off"]
    report: dict[str, dict] = {}
    for tid in targets:
        mp4 = BASE / f"{tid}.mp4"
        if not mp4.exists():
            print(f"{tid}: MISSING")
            continue
        text = transcribe(mp4)
        fam = tid.split("-")[0]
        words = B_WORDS.get(fam, [])
        leaked = [w for w in words if w in text]
        # 幻觉串过滤后再判
        suspicious = bool(leaked) and not any(h in text for h in _HALLUCINATION)
        report[tid] = {"transcript": text[:120], "b_words_hit": leaked,
                       "leak": suspicious}
        print(f"{tid}: leak={suspicious} 命中={leaked} transcript={text[:80]!r}")
    import json
    (BASE / "p6_asr_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("report saved: p6_asr_report.json")


if __name__ == "__main__":
    main()
