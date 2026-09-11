"""vLLM 冷启动探针（V9 尾款② / V5 工具箱，2026-09-09）。

独立拉起 vLLM（镜像产品参数），测 kill-to-ready 秒数 + 可选快速解码
基准，供：编译缓存复用试验（V9）/ 启动参数矩阵（V9）/ V8 升级前后
冷启动回归。产物报告落 logs/eval/cold_boot_*.json。

用法（runtime/py310/python.exe backend/tests/eval/cold_boot_probe.py）：
  --label A_control --disable-cache          现状对照（禁编译缓存）
  --label B_fresh --cache-root E:/.../.cache/vllm-exp   开缓存首启
  --label B_hit  --cache-root 同上           复启吃缓存（钱数在这）
  --bench-rounds 1                           就绪后跑 N×3 题解码基准
纪律：GPU 活动门先行；结束后自动清进程树。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bench_common import save_report  # noqa: E402
from vllm_bench import DEFAULT_PROMPTS, stream_round  # noqa: E402

PY313 = Path("E:/OmniSpace/runtime/py313/python.exe")


def _launch(args: argparse.Namespace) -> subprocess.Popen:
    cmd = [
        str(PY313), "-m", "vllm.entrypoints.openai.api_server",
        "--model", f"E:/OmniSpace/models/{args.model}",
        "--served-model-name", args.model,
        "--host", "127.0.0.1", "--port", str(args.port),
        "--dtype", "float16", "--max-model-len", str(args.max_len),
        "--max-num-seqs", "32",
        "--gpu-memory-utilization", str(args.util),
        "--enable-prefix-caching", "--no-enable-log-requests",
        "--seed", "42",
    ]
    if args.enforce_eager:
        cmd.append("--enforce-eager")
    if args.kv_fp8:
        cmd += ["--kv-cache-dtype", "fp8"]
    env = os.environ.copy()
    env.update({
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
        "VLLM_HOST_IP": "127.0.0.1", "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    })
    if args.cache_root:
        env["VLLM_CACHE_ROOT"] = args.cache_root
        env["TRITON_CACHE_DIR"] = str(
            Path(args.cache_root) / "triton")
    if args.disable_cache:
        env["VLLM_DISABLE_COMPILE_CACHE"] = "1"
    else:
        env["VLLM_DISABLE_COMPILE_CACHE"] = "0"
    log_fh = open(  # noqa: SIM115 - 结束前显式交给进程生命周期
        f"E:/OmniSpace/logs/eval/cold_boot_{args.label}.log", "w",
        encoding="utf-8", buffering=1)
    return subprocess.Popen(
        cmd, stdout=log_fh, stderr=subprocess.STDOUT,
        cwd="E:/OmniSpace", env=env,
        creationflags=subprocess.CREATE_NO_WINDOW)


def _wait_health(port: int, timeout_s: float) -> tuple[bool, float]:
    t0 = time.time()
    url = f"http://127.0.0.1:{port}/health"
    while time.time() - t0 < timeout_s:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return True, time.time() - t0
        except Exception:  # noqa: BLE001 - 轮询
            pass
        time.sleep(3.0)
    return False, time.time() - t0


def _kill(proc: subprocess.Popen) -> None:
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"taskkill /PID {proc.pid} /T /F"],
        capture_output=True, timeout=30)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # noqa: PERF203
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="vLLM 冷启动探针")
    parser.add_argument("--label", required=True)
    parser.add_argument("--port", type=int, default=8103)
    parser.add_argument("--model", default="qwen3-vl-8b-awq")
    parser.add_argument("--max-len", type=int, default=8192)
    parser.add_argument("--util", type=float, default=0.85)
    parser.add_argument("--enforce-eager", action="store_true")
    parser.add_argument("--kv-fp8", action="store_true",
                        help="带 --kv-cache-dtype fp8（V3/V8 SM120 专项）")
    parser.add_argument("--cache-root", default="")
    parser.add_argument("--disable-cache", action="store_true")
    parser.add_argument("--bench-rounds", type=int, default=0)
    args = parser.parse_args()

    print(f"[{args.label}] 拉起 {args.model} …", flush=True)
    proc = _launch(args)
    ok, boot_s = _wait_health(args.port, 300.0)
    report: dict[str, Any] = {
        "bench": "cold_boot_probe", "label": args.label,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": args.model, "port": args.port,
        "max_len": args.max_len, "util": args.util,
        "cache_root": args.cache_root,
        "compile_cache_disabled": bool(args.disable_cache),
        "enforce_eager": bool(args.enforce_eager),
        "boot_ok": ok, "boot_s": round(boot_s, 1),
    }
    if ok and args.bench_rounds > 0:
        base = f"http://127.0.0.1:{args.port}"
        tps_all: list[float] = []
        for pi, prompt in enumerate(DEFAULT_PROMPTS):
            for r in range(args.bench_rounds):
                rec = stream_round(base, args.model, prompt, 256,
                                   42 + pi * 100 + r)
                tps_all.append(rec["tokens_per_s_decode"])
        tps_all.sort()
        report["tps_p50"] = tps_all[len(tps_all) // 2]
        print(f"[{args.label}] tps_p50={report['tps_p50']}")
    _kill(proc)
    path = save_report("cold_boot", report)
    print(f"[{args.label}] boot_ok={ok} boot_s={report['boot_s']} → {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
# 本项目仅供学习使用，商业授权请+Q 3559331368
