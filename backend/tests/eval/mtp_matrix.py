"""MTP 投机解码四组对照矩阵冒烟（V6，2026-09-09）。

背景（方案 V6）：对话 9B 自带 MTP 权重（models/qwen35-9b-w4a16/
model_mtp.safetensors ✅），vLLM 0.26 支持 method=mtp（speculative.py:53
包内实证）。**已知雷**：官方 #53912——prefix caching + MTP 在 hybrid
Mamba/GDN 模型上输出损坏（v0.28 未修）；qwen3.5 正是 GDN 且产品默认开
前缀缓存 → 必须四组矩阵证明没踩中。

矩阵（模型/参数全同，仅两轴变化）：
  A = MTP关 × 缓存开（产品现状基线复刻）
  B = MTP关 × 缓存关（缓存轴对照）
  C = MTP开 × 缓存开（**#53912 危险格**）
  D = MTP开 × 缓存关

判定（同服复跑 0/9 逐字一致=内核非确定性噪声底，精确对拍不可行，
2026-09-08 实测）：
  1. 语义相似度带：sim(A,B)=噪声底；若 sim(C,·) 显著低于噪声底 → 踩雷
  2. 文本劣化启发式：重复环/乱码率/长度坍缩
  3. 速度：C/D 对 A 的 tokens/s 提升
用法：runtime/py310/python.exe backend/tests/eval/mtp_matrix.py
前置纪律：GPU 活动门；9B 需近乎空卡（~14.9GB）；全程 ~15 分钟。
"""

from __future__ import annotations

import argparse
import difflib
import json
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

MODEL_DIR = Path("E:/OmniSpace/models/qwen35-9b-w4a16")
PORT = 8103
BASE = f"http://127.0.0.1:{PORT}"
PY313 = Path("E:/OmniSpace/runtime/py313/python.exe")


def _wait_health(timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - 轮询
            pass
        time.sleep(4.0)
    return False


def _launch(mtp: bool, cache: bool, util: float,
            max_len: int, max_seqs: int = 32) -> tuple[subprocess.Popen, list[str]]:
    cmd = [
        str(PY313), "-m", "vllm.entrypoints.openai.api_server",
        "--model", str(MODEL_DIR),
        "--served-model-name", "qwen35-9b-w4a16",
        "--host", "127.0.0.1", "--port", str(PORT),
        "--dtype", "float16", "--max-model-len", str(max_len),
        "--max-num-seqs", str(max_seqs),
        "--gpu-memory-utilization", str(util),
        "--kv-cache-dtype", "fp8",
        "--no-enable-log-requests", "--seed", "42",
    ]
    # 注：不带 --reasoning-parser——qwen3 parser 会把思考段剥进
    # reasoning_content，采样（只读 delta.content）会全空（首跑实测
    # ttft=-1/garle=1.0 即此因）。矩阵四格同口径拿「思考混正文」的
    # 原始流，跨格可比性不受影响。
    cmd.append("--enable-prefix-caching" if cache
               else "--no-enable-prefix-caching")
    if mtp:
        cmd += ["--speculative-config",
                '{"method": "mtp", "num_speculative_tokens": 3}']
    env = os.environ.copy()
    env.update({
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
        "VLLM_HOST_IP": "127.0.0.1", "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1", "VLLM_DISABLE_COMPILE_CACHE": "1",
        "VLLM_CACHE_ROOT": "E:/OmniSpace/.cache/vllm",
        "TRITON_CACHE_DIR": "E:/OmniSpace/.cache/triton",
    })
    log_fh = open(  # noqa: SIM115 - 随进程树关闭前显式关
        f"E:/OmniSpace/logs/eval/mtp_cell_{'mtp' if mtp else 'base'}"
        f"_{'cache' if cache else 'nocache'}.log", "w",
        encoding="utf-8", buffering=1)
    proc = subprocess.Popen(
        cmd, stdout=log_fh, stderr=subprocess.STDOUT,
        cwd="E:/OmniSpace", env=env,
        creationflags=subprocess.CREATE_NO_WINDOW)
    return proc, cmd


def _kill(proc: subprocess.Popen) -> None:
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"taskkill /PID {proc.pid} /T /F"],
        capture_output=True, timeout=30)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # noqa: PERF203
        pass


def _degradation(text: str) -> dict[str, float]:
    """文本劣化启发式：重复环 + 非中日英常用字符密度 + 长度。"""
    t = text.strip()
    if not t:
        return {"len": 0.0, "rep_ratio": 1.0, "garble": 1.0}
    grams = [t[i:i + 8] for i in range(0, max(len(t) - 8, 1), 4)]
    rep = (max([grams.count(g) for g in set(grams)]) /
           max(len(grams), 1)) if grams else 1.0
    ok = sum(1 for ch in t if ('\u4e00' <= ch <= '\u9fff')
             or ch.isascii() or ch in "，。！？；：、""''（）…—\n ")
    return {"len": float(len(t)),
            "rep_ratio": round(rep, 3),
            "garble": round(1 - ok / len(t), 4)}


def run_cell(name: str, mtp: bool, cache: bool, rounds: int,
             util: float, max_len: int,
             max_seqs: int = 32) -> dict[str, Any]:
    print(f"[cell {name}] MTP={'开' if mtp else '关'} "
          f"缓存={'开' if cache else '关'} → 拉起 9B …", flush=True)
    proc, cmd = _launch(mtp, cache, util, max_len, max_seqs)
    cell: dict[str, Any] = {"name": name, "mtp": mtp, "cache": cache}
    try:
        if not _wait_health(300.0):
            cell["boot"] = "failed"
            cell["error"] = "300s 健康未就绪（显存/参数问题，见 cell 日志）"
            print(f"[cell {name}] 启动失败（见日志）", flush=True)
            return cell
        cell["boot"] = "ok"
        outs: list[dict[str, Any]] = []
        for pi, prompt in enumerate(DEFAULT_PROMPTS):
            for r in range(rounds):
                rec = stream_round(BASE, "qwen35-9b-w4a16", prompt,
                                   512, 42 + pi * 100 + r)
                rec["degrade"] = _degradation(rec["text"])
                outs.append(rec)
        cell["rounds"] = outs
        tps = sorted(x["tokens_per_s_decode"] for x in outs)
        tt = sorted(x["ttft_ms"] for x in outs if x["ttft_ms"] > 0)
        cell["summary"] = {
            "tps_p50": tps[len(tps) // 2] if tps else -1,
            "ttft_p50": tt[len(tt) // 2] if tt else -1,
            "garble_max": max(x["degrade"]["garble"] for x in outs),
            "rep_max": max(x["degrade"]["rep_ratio"] for x in outs),
        }
        print(f"[cell {name}] tps_p50={cell['summary']['tps_p50']} "
              f"ttft_p50={cell['summary']['ttft_p50']}ms "
              f"garble_max={cell['summary']['garble_max']}", flush=True)
        return cell
    finally:
        _kill(proc)


def analyze(cells: list[dict[str, Any]]) -> dict[str, Any]:
    """跨格语义相似度带：sim(A,B)=噪声底，C 对其余若显著更低=踩雷。"""
    def texts(c: dict[str, Any]) -> list[str]:
        return [r["text"] for r in c.get("rounds", [])]

    out: dict[str, Any] = {}
    by_name = {c["name"]: c for c in cells if c.get("boot") == "ok"}
    if {"A", "B", "C"} <= set(by_name):
        def cross_sim(n1: str, n2: str) -> float:
            t1, t2 = texts(by_name[n1]), texts(by_name[n2])
            sims = [difflib.SequenceMatcher(None, a, b).ratio()
                    for a, b in zip(t1, t2, strict=False)]
            return round(sum(sims) / len(sims), 3) if sims else -1.0
        out["sim"] = {
            "AB_noise_floor": cross_sim("A", "B"),
            "AC": cross_sim("A", "C"),
            "BC": cross_sim("B", "C"),
            "CD": cross_sim("C", "D") if "D" in by_name else None,
        }
        floor = out["sim"]["AB_noise_floor"]
        ac = out["sim"]["AC"]
        out["verdict_53912"] = (
            "危险：C 格相似度显著低于噪声底（疑似 #53912 输出损坏）"
            if floor > 0 and ac >= 0 and ac < floor - 0.15
            else "未踩雷：C 格相似度在噪声带内")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="MTP 四组对照矩阵冒烟")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--util", type=float, default=0.86)
    parser.add_argument("--max-len", type=int, default=4096)
    parser.add_argument("--cells", default="A,B,C,D",
                        help="只跑指定格（如 C），补格用")
    parser.add_argument("--max-seqs", type=int, default=32,
                        help="并发上限（8=省 ~0.6GB 图捕获显存，刀刃档）")
    args = parser.parse_args()

    if not (MODEL_DIR / "model_mtp.safetensors").is_file():
        print(f"[abort] 9B 目录无 MTP 权重: {MODEL_DIR}")
        return 2

    cells_spec = [("A", False, True), ("B", False, False),
                  ("C", True, True), ("D", True, False)]
    wanted = {x.strip().upper() for x in args.cells.split(",")}
    results: list[dict[str, Any]] = []
    for name, mtp, cache in cells_spec:
        if name not in wanted:
            continue
        results.append(run_cell(name, mtp, cache, args.rounds,
                                args.util, args.max_len, args.max_seqs))
        time.sleep(6.0)  # 显存回收间隙

    report: dict[str, Any] = {
        "bench": "mtp_matrix", "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": str(MODEL_DIR),
        "params": {"rounds": args.rounds, "util": args.util,
                   "max_len": args.max_len, "kv_dtype": "fp8",
                   "spec": "mtp c=3"},
        "cells": results, "analysis": analyze(results),
    }
    path = save_report("mtp_matrix", report)
    print(json.dumps(report["analysis"], ensure_ascii=False, indent=1))
    for c in results:
        s = c.get("summary")
        print(f"  {c['name']}: boot={c['boot']} "
              f"{s or c.get('error', '')}")
    print(f"[done] 报告 → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# 本项目仅供学习使用，商业授权请+Q 3559331368
