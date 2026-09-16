"""对话引擎基准脚本（V5 升级基准尺子，2026-09-08）。

直连 vLLM OpenAI 兼容服务（默认 127.0.0.1:8101），流式测量：
TTFT（首字延迟）/ 总耗时 / 输出 token 数 / 解码速度 tokens/s，
并把每轮完整输出文本存进报告（供升级前后 A/B 质量对拍——V1/V6 验收用）。

用法（runtime/py310/python.exe tests/unit/eval/vllm_bench.py）：
  --base-url http://127.0.0.1:8101   vLLM 服务地址（不带 /v1）
  --rounds 5                          每个提示词跑几轮
  --max-tokens 256                    单轮生成上限
  --keep-samples                      报告保留逐秒资源采样

前置纪律：
  1. GPU 活动门（后端近 5 分钟任务 + comfy 进程核查）；
  2. vLLM 未在跑时先由产品链路拉起（未加载必须全自动），
     或 --skip-health 跳过健康检查（仅调试脚本本身用）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bench_common import ResourceSampler, http_json, query_gpu, save_report

DEFAULT_PROMPTS: list[str] = [
    "用三句话给一个小学五年级学生解释什么是显存，然后举一个生活里的比喻。",
    "以下是一段产品描述：『OmniSpace 是一台跑在家用电脑上的 AI 创作站，"
    "能写小说、画漫画、生成短视频。』请从中提炼 5 个关键词并各用一句话说明。",
    "写一个 100 字左右的悬疑小剧场开头，场景是深夜的便利店，主角是店员小周。",
]


def _get(url: str, timeout: int = 10) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - 探活失败统一走返回值
        return -1, str(exc)


def stream_round(base_url: str, model: str, prompt: str,
                 max_tokens: int, seed: int) -> dict[str, Any]:
    """单轮流式测量：TTFT/总耗时/token 数/解码速度/全文。"""
    url = f"{base_url}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,   # 定温定种：升级前后输出可对拍
        "seed": seed,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    ttft_ms: float | None = None
    chunks = 0
    usage_tokens: int | None = None
    text_parts: list[str] = []
    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                evt = json.loads(body)
            except json.JSONDecodeError:
                continue
            if evt.get("usage"):
                usage_tokens = evt["usage"].get("completion_tokens")
            choices = evt.get("choices") or []
            if choices and ttft_ms is None and (
                    (choices[0].get("delta") or {}).get("content")):
                ttft_ms = (time.perf_counter() - t0) * 1000
            for ch in choices:
                piece = (ch.get("delta") or {}).get("content")
                if piece:
                    text_parts.append(piece)
                    chunks += 1
    total_ms = (time.perf_counter() - t0) * 1000
    out_tokens = usage_tokens if usage_tokens else chunks
    decode_s = max((total_ms - (ttft_ms or 0)) / 1000, 1e-6)
    return {
        "prompt_head": prompt[:40],
        "ttft_ms": round(ttft_ms if ttft_ms is not None else -1, 1),
        "total_ms": round(total_ms, 1),
        "out_tokens": out_tokens,
        "tokens_per_s_total": round(out_tokens / max(total_ms / 1000, 1e-6), 1),
        "tokens_per_s_decode": round(max(out_tokens - 1, 0) / decode_s, 1),
        "text": "".join(text_parts),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="vLLM 对话基准")
    parser.add_argument("--base-url", default="http://127.0.0.1:8101")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--keep-samples", action="store_true")
    parser.add_argument("--skip-health", action="store_true")
    args = parser.parse_args()

    if not args.skip_health:
        status, body = _get(f"{args.base_url}/health")
        if status != 200:
            print(f"[abort] vLLM 健康检查未过（status={status}）：{body[:200]}")
            return 2
    _s, models_body = http_json(f"{args.base_url}/v1/models", timeout=10)
    model_ids: list[str] = [
        m.get("id", "") for m in (models_body or {}).get("data", [])
    ] if isinstance(models_body, dict) else []
    if not model_ids:
        print("[abort] /v1/models 为空——vLLM 未加载模型")
        return 2
    model = model_ids[0]
    _s, ver = _get(f"{args.base_url}/version")
    print(f"[info] model={model} rounds×prompts={args.rounds}×"
          f"{len(DEFAULT_PROMPTS)} server_version={ver.strip()[:40]}")

    gpu_before = query_gpu()
    rounds_out: list[dict[str, Any]] = []
    with ResourceSampler(interval_s=1.0) as sampler:
        for pi, prompt in enumerate(DEFAULT_PROMPTS):
            for r in range(args.rounds):
                rec = stream_round(args.base_url, model, prompt,
                                   args.max_tokens, args.seed + pi * 100 + r)
                rounds_out.append(rec)
                print(f"  p{pi + 1}r{r + 1}: ttft={rec['ttft_ms']:.0f}ms "
                      f"total={rec['total_ms']:.0f}ms "
                      f"tok={rec['out_tokens']} "
                      f"tps(dec)={rec['tokens_per_s_decode']}")
    gpu_after = query_gpu()

    ttfts = sorted(x["ttft_ms"] for x in rounds_out if x["ttft_ms"] > 0)
    tps = sorted(x["tokens_per_s_decode"] for x in rounds_out)

    def p50(v: list[float]) -> float:
        return v[len(v) // 2] if v else -1.0
    report: dict[str, Any] = {
        "bench": "vllm_bench",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_url": args.base_url,
        "model": model,
        "server_version": ver.strip(),
        "params": {"rounds": args.rounds, "prompts": len(DEFAULT_PROMPTS),
                   "max_tokens": args.max_tokens, "temperature": 0.0,
                   "seed_base": args.seed},
        "summary": {
            "n": len(rounds_out),
            "ttft_ms_p50": round(p50(ttfts), 1),
            "ttft_ms_min": round(ttfts[0], 1) if ttfts else -1,
            "ttft_ms_max": round(ttfts[-1], 1) if ttfts else -1,
            "tps_decode_p50": round(p50(tps), 1),
            "tps_decode_max": round(tps[-1], 1) if tps else -1,
        },
        "resources": sampler.summary(keep_samples=args.keep_samples),
        "gpu_before": gpu_before,
        "gpu_after": gpu_after,
        "rounds": rounds_out,
    }
    path = save_report("vllm_bench", report, keep_samples=args.keep_samples)
    print(f"[done] p50 TTFT={report['summary']['ttft_ms_p50']}ms "
          f"p50 解码={report['summary']['tps_decode_p50']} tok/s")
    print(f"[done] 报告 → {path}（logs/eval/ 下，升级前后 A/B 用）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
