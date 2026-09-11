"""绘画链基准脚本（V5 升级基准尺子，2026-09-08）。

走**生产路径**（后端 /api/v1/draw/generate 异步任务 → 统一图像队列 →
本地绘画引擎/ComfyUI），后台逐秒采样 GPU/RAM/提交内存并记峰值。
适用于 V2 动态显存收益取证、V7 SageAttention 前后对拍、V8 torch 升级对拍。

用法（runtime/py310/python.exe backend/tests/eval/paint_bench.py）：
  --backend http://127.0.0.1:5800     后端地址
  --model sdxl-base-1.0               显式指定模型（默认走服务端语言感知路由）
  --width 768 --height 768 --steps 20 固定出图参数（默认对齐 13.6s 历史锚点）
  --seed 42                           固定种子（前后对拍可比）
  --rounds 3                          轮数
  --timeout 900                       单轮超时（冷加载含模型装载，留足）

前置纪律：
  1. GPU 活动门（后端近 5 分钟任务 + comfy 进程核查）——本脚本会触发
     vLLM 让渡（杀进程+后台重启）与模型冷加载，只在安静窗口跑；
  2. 绘画工位不得绑定云端端点（V4 已延后）：脚本检测到任务走云端道
     会立即中止并在报告标记 cloud_routed=true。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bench_common import ResourceSampler, http_json, query_gpu, save_report  # noqa: E402

DEFAULT_PROMPT = (
    "樱花树下的少女半身像，粉色长发，校服，柔和逆光，细节丰富，"
    "高质量插画")


def submit(backend: str, params: dict[str, Any]) -> str:
    status, body = http_json(f"{backend}/api/v1/draw/generate",
                             payload=params, timeout=60)
    if status != 200 or not (body or {}).get("success"):
        raise RuntimeError(f"提交失败 status={status} body={str(body)[:300]}")
    data = body.get("data") or {}
    task_id = data.get("task_id")
    if not task_id:
        raise RuntimeError(f"响应缺 task_id：{str(body)[:300]}")
    return str(task_id)


def poll_until_done(backend: str, task_id: str,
                    timeout_s: int) -> dict[str, Any]:
    """轮询到终态；返回 /draw/result 的 data 部分。"""
    deadline = time.time() + timeout_s
    last: dict[str, Any] = {}
    while time.time() < deadline:
        status, body = http_json(
            f"{backend}/api/v1/draw/result/{task_id}", timeout=30)
        if status != 200 or not (body or {}).get("success"):
            raise RuntimeError(f"轮询失败 status={status} "
                               f"body={str(body)[:200]}")
        last = body.get("data") or {}
        st = last.get("status")
        if st in ("done", "error", "cancelled"):
            return last
        time.sleep(2)
    raise TimeoutError(f"任务 {timeout_s}s 未到终态，last={str(last)[:200]}")


def bench_round(backend: str, params: dict[str, Any],
                timeout_s: int) -> dict[str, Any]:
    t0 = time.perf_counter()
    task_id = submit(backend, params)
    submit_ms = round((time.perf_counter() - t0) * 1000, 1)
    with ResourceSampler(interval_s=1.0) as sampler:
        result = poll_until_done(backend, task_id, timeout_s)
        wall_ms = round((time.perf_counter() - t0) * 1000, 1)
    backend_field = str(result.get("backend", ""))
    cloud_routed = ("cloud" in backend_field.lower()
                    or "dashscope" in backend_field.lower())
    return {
        "task_id": task_id,
        "status": result.get("status"),
        "submit_ms": submit_ms,
        "wall_ms": wall_ms,
        "server_elapsed_ms": result.get("elapsed_ms", -1),
        "model": result.get("model", ""),
        "backend": backend_field,
        "cloud_routed": cloud_routed,
        "degraded": bool(result.get("degraded", False)),
        "seed": result.get("seed", -1),
        "file_path": result.get("file_path", ""),
        "error": result.get("error", ""),
        "resources": sampler.summary(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="绘画链基准（生产路径）")
    parser.add_argument("--backend", default="http://127.0.0.1:5800")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--negative", default="")
    parser.add_argument("--model", default="",
                        help="留空=服务端默认路由（语言感知）")
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    # 前置：后端活着（main.py 顶层 /health，实测口径见 CLAUDE.md §4）
    status, _body = http_json(f"{args.backend}/health", timeout=10)
    if status != 200:
        print(f"[abort] 后端不可达（status={status}，试 {args.backend}/health）")
        return 2

    params: dict[str, Any] = {
        "prompt": args.prompt, "negative": args.negative,
        "width": args.width, "height": args.height,
        "steps": args.steps, "seed": args.seed, "batch_size": 1,
    }
    if args.model:
        params["model"] = args.model

    rounds_out: list[dict[str, Any]] = []
    aborted_cloud = False
    for i in range(args.rounds):
        print(f"[round {i + 1}/{args.rounds}] 提交中…")
        try:
            rec = bench_round(args.backend, params, args.timeout)
        except (RuntimeError, TimeoutError) as exc:
            print(f"[fail] {exc}")
            rounds_out.append({"status": "exception", "error": str(exc)})
            break
        rounds_out.append(rec)
        print(f"  status={rec['status']} wall={rec['wall_ms']}ms "
              f"server={rec['server_elapsed_ms']}ms "
              f"model={rec['model']} backend={rec['backend'] or '-'} "
              f"peakGPU={rec['resources']['peak_gpu_mb']}MB "
              f"peakCommit={rec['resources']['peak_commit_gb']}GB")
        if rec.get("cloud_routed"):
            print("[abort] 任务走了云端道（绘画工位绑定了云端端点）——"
                  "V4 已延后，基准必须走本地；请解绑后重跑")
            aborted_cloud = True
            break
        if rec.get("status") != "done":
            break

    ok_rounds = [r for r in rounds_out if r.get("status") == "done"]
    walls = sorted(r["wall_ms"] for r in ok_rounds) if ok_rounds else []
    report: dict[str, Any] = {
        "bench": "paint_bench",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "backend": args.backend,
        "params": params,
        "summary": {
            "n_done": len(ok_rounds),
            "wall_ms_min": walls[0] if walls else -1,
            "wall_ms_max": walls[-1] if walls else -1,
            "peak_gpu_mb_max": max(
                (r.get("resources", {}).get("peak_gpu_mb", 0)
                 for r in rounds_out), default=0),
            "peak_commit_gb_max": max(
                (r.get("resources", {}).get("peak_commit_gb", 0.0)
                 for r in rounds_out), default=0.0),
            "aborted_cloud": aborted_cloud,
        },
        "gpu_before": query_gpu(),
        "rounds": rounds_out,
    }
    path = save_report("paint_bench", report)
    print(f"[done] 完成 {len(ok_rounds)}/{args.rounds} 轮，报告 → {path}")
    return 1 if (aborted_cloud or not ok_rounds) else 0


if __name__ == "__main__":
    raise SystemExit(main())
# 本项目仅供学习使用，商业授权请+Q 3559331368
