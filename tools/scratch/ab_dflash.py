"""P-5 DFlash vs MTP 投机解码 A/B（2026-09-15，安静窗口专用）。

用法：
    runtime/py312/python.exe tools/scratch/ab_dflash.py mtp     # 跑 MTP 基线
    runtime/py312/python.exe tools/scratch/ab_dflash.py dflash  # 跑 DFlash
    runtime/py312/python.exe tools/scratch/ab_dflash.py both    # 顺序全跑

前置纪律（§16.6 GPU 活动门）：
  - 后端近 5 分钟无生成任务（logs/backend.log 尾部无 submit/generate）
  - nvidia-smi 空闲显存 ≥14.5GB（9B 权重 10.7 + 草稿 2.6 + 开销）
  - 脚本自带两道检查，不过即拒跑。

口径：同模型（qwen35-9b-w4a16）/同种子/同三题（短 256 / 中 768 / 长
2048 tokens），量 TTFT（流式首字）与吞吐（非流式 tokens/s）。DFlash
variant 块大小走草稿缺省 16；MTP c=3 与产线一致。结果落
logs/ab_dflash_result.json。端口 8102（避开产线 8101）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LLM_EXE = ROOT / "runtime" / "py313" / "OmniSpace-LLM.exe"
TARGET = ROOT / "models" / "qwen35-9b-w4a16"
DRAFT = ROOT / "models" / "dflash" / "qwen35-9b-draft"
PORT = 8102
MAX_LEN = 8192  # 压低 KV 预算给草稿让位（产线 9B 本就 4K 钳制档）
READY_TIMEOUT_S = 420  # 与产线冷启动阈值同口径

PROMPTS = [
    ("short-256", 256, "用三句话介绍太平湖的地理特征。"),
    ("mid-768", 768, "写一段武侠小说开场：少年在客栈听到马蹄声，要求环境描写细腻。"),
    ("long-2048", 2048, "以《山城夜雨》为题写一篇短文，散文笔法，主题是旧城改造中的人情。"),
]


def gpu_free_gb() -> float:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.free",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=15).stdout.strip()
    return float(out.splitlines()[0]) / 1024.0


def backend_quiet() -> bool:
    log = ROOT / "logs" / "backend.log"
    try:
        last = log.stat().st_mtime
    except OSError:
        return True
    return time.time() - last > 300  # 5 分钟无日志=无任务


def wait_ready() -> bool:
    t0 = time.time()
    while time.time() - t0 < READY_TIMEOUT_S:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/v1/models", timeout=5) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(3)
    return False


def chat(max_tokens: int, prompt: str, stream: bool = False):
    body = json.dumps({
        "model": "qwen35-9b-w4a16",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "seed": 42,
        "stream": stream,
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    ttft = None
    if stream:
        with urllib.request.urlopen(req, timeout=600) as r:
            usage = {}
            for line in r:
                s = line.decode("utf-8", "ignore").strip()
                if not s.startswith("data: "):
                    continue
                payload = s[6:]
                if payload == "[DONE]":
                    break
                if ttft is None:
                    ttft = time.perf_counter() - t0
                try:
                    u = json.loads(payload).get("usage")
                    if u:
                        usage = u
                except json.JSONDecodeError:
                    continue
        wall = time.perf_counter() - t0
        return ttft, wall, usage
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.loads(r.read().decode("utf-8"))
    wall = time.perf_counter() - t0
    usage = d.get("usage") or {}
    return None, wall, usage


def spec_config(variant: str) -> str:
    if variant == "mtp":
        return json.dumps({"method": "mtp", "num_speculative_tokens": 3})
    return json.dumps({"method": "dflash", "model": str(DRAFT.resolve())})


def run_variant(variant: str, results: dict) -> None:
    if not TARGET.is_dir():
        sys.exit(f"目标模型缺失: {TARGET}")
    if variant == "dflash" and not (DRAFT / "model.safetensors").is_file():
        sys.exit(f"草稿权重缺失: {DRAFT}")

    cmd = [
        str(LLM_EXE), "-m", "vllm.entrypoints.openai.api_server",
        "--model", str(TARGET),
        "--served-model-name", "qwen35-9b-w4a16",
        "--host", "127.0.0.1", "--port", str(PORT),
        "--max-model-len", str(MAX_LEN),
        "--max-num-seqs", "32",
        "--gpu-memory-utilization", "0.92",
        "--enable-prefix-caching", "--no-enable-log-requests",
        "--seed", "42", "--kv-cache-dtype", "fp8",
        "--speculative-config", spec_config(variant),
    ]
    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = "0"
    env.setdefault("VLLM_DISABLE_COMPILE_CACHE", "1")
    print(f"[{variant}] 启动 vLLM …", flush=True)
    log_fh = open(ROOT / "logs" / f"ab_dflash_{variant}.log", "ab",
                  encoding="utf-8", buffering=1)
    proc = subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT,
                            cwd=str(ROOT), env=env,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        if not wait_ready():
            print(f"[{variant}] vLLM 就绪超时（{READY_TIMEOUT_S}s）——"
                  f"看 logs/ab_dflash_{variant}.log 定位（OOM/兼容性）",
                  flush=True)
            results[variant] = {"error": "ready_timeout"}
            return
        rows = []
        # TTFT 探针（流式，短题）
        ttft, wall, usage = chat(128, PROMPTS[0][2], stream=True)
        rows.append({"case": "ttft-128", "ttft_s": ttft, "wall_s": wall})
        for name, mt, prompt in PROMPTS:
            _, wall, usage = chat(mt, prompt)
            comp = usage.get("completion_tokens", mt)
            rows.append({"case": name, "wall_s": round(wall, 2),
                         "tokens": comp,
                         "tok_s": round(comp / wall, 1)})
            print(f"[{variant}] {name}: {wall:.1f}s "
                  f"{comp / wall:.1f} tok/s", flush=True)
        results[variant] = {"boot_ok": True, "rows": rows}
        print(f"[{variant}] 完成", flush=True)
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True)
        log_fh.close()
        time.sleep(8)  # 显存回收窗口


def main() -> None:
    variant = sys.argv[1] if len(sys.argv) > 1 else "both"
    if not backend_quiet():
        sys.exit("后端近 5 分钟有活动（logs/backend.log 新鲜）——按 "
                 "§16.6 活动门拒跑，稍后再试。")
    free = gpu_free_gb()
    if free < 14.5:
        sys.exit(f"空闲显存 {free:.1f}GB < 14.5GB（9B+草稿下限）——"
                 "关闭占显存应用后重试。")
    print(f"活动门通过：显存空闲 {free:.1f}GB", flush=True)
    results = {}
    for v in (["mtp", "dflash"] if variant == "both" else [variant]):
        run_variant(v, results)
    out = ROOT / "logs" / "ab_dflash_result.json"
    old = json.loads(out.read_text("utf-8")) if out.is_file() else {}
    old.update(results)
    out.write_text(json.dumps(old, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"结果已落 {out}", flush=True)
    if "mtp" in results and "dflash" in results and \
            all("rows" in results[v] for v in ("mtp", "dflash")):
        for m, d in zip(results["mtp"]["rows"][1:],
                        results["dflash"]["rows"][1:], strict=False):
            if "tok_s" in m and "tok_s" in d and m["tok_s"]:
                print(f"  {m['case']}: MTP {m['tok_s']} → "
                      f"DFlash {d['tok_s']} tok/s "
                      f"({(d['tok_s'] / m['tok_s'] - 1) * 100:+.0f}%)")


if __name__ == "__main__":
    main()
