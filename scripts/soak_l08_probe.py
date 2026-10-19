# -*- coding: utf-8 -*-
"""L-08 24h 浸泡监控探针（2026-10-19）：健康/RAM/GPU/版本单行追加 logs/soak_l08.jsonl。

事件写 logs/soak_l08_events.log：
- restart: uptime 下降（后端重启）——F-3 抓现行
- down:    /health 不可达——后端死亡
- ramlow:  可用内存 < 2GB——WER RADAR 前兆
"""
import json
import sys
import time

import psutil
import requests

sys.stdout.reconfigure(encoding="utf-8")
REC = "logs/soak_l08.jsonl"
EVT = "logs/soak_l08_events.log"


def prev_uptime():
    try:
        with open(REC, encoding="utf-8") as f:
            lines = [x for x in f.read().splitlines() if x.strip()]
        for line in reversed(lines):
            r = json.loads(line)
            if r.get("uptime_s") is not None:
                return float(r["uptime_s"]), r.get("version")
    except Exception:
        pass
    return None, None


def log_event(kind, detail):
    with open(EVT, "a", encoding="utf-8") as f:
        f.write(time.strftime("%m-%d %H:%M:%S ") + f"[{kind}] {detail}\n")


rec = {"ts": time.time(), "time": time.strftime("%m-%d %H:%M:%S")}
try:
    r = requests.get("http://127.0.0.1:5800/health", timeout=8).json()
    d = r.get("data") or {}
    rec.update({
        "status": d.get("status"), "version": d.get("version"),
        "uptime_s": d.get("uptime_s"), "copy_id": d.get("copy_id"),
    })
except Exception as exc:
    rec["status"] = "down"
    rec["error"] = repr(exc)[:80]
    log_event("down", f"health 不可达: {rec['error']}")

try:
    for p in psutil.process_iter(["name", "memory_info"]):
        n = (p.info["name"] or "").lower()
        if n.startswith("omnispace-backend"):
            m = p.info["memory_info"]
            rec["backend_rss_gb"] = round(m.rss / 2**30, 2)
            break
    vm = psutil.virtual_memory()
    rec["ram_used_pct"] = vm.percent
    rec["ram_avail_gb"] = round(vm.available / 2**30, 2)
    if rec["ram_avail_gb"] < 2:
        log_event("ramlow", f"可用内存 {rec['ram_avail_gb']}GB < 2GB")
except Exception:
    pass

try:
    import pynvml
    pynvml.nvmlInit()
    h = pynvml.nvmlDeviceGetHandleByIndex(0)
    u = pynvml.nvmlDeviceGetUtilizationRates(h)
    m = pynvml.nvmlDeviceGetMemoryInfo(h)
    rec["gpu_pct"] = u.gpu
    rec["vram_gb"] = round(m.used / 2**30, 2)
except Exception:
    pass

cur_up = rec.get("uptime_s")
pu, pv = prev_uptime()
if cur_up is not None and pu is not None and cur_up < pu - 60:
    log_event("restart", f"uptime {pu:.0f}s → {cur_up:.0f}s（后端重启；version {pv} → {rec.get('version')}）")

with open(REC, "a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
print(json.dumps(rec, ensure_ascii=False))
