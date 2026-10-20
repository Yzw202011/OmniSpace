"""自愈战役 S-01 被动浸泡探针（2026-10-21 起，24h）。

只观察不动手（零干扰口径）：每 60s 采一行 /health + RAM + VRAM 快照追加
logs/soak_selfheal.jsonl；异常事件（后端死亡/重启/RAM 告急）追加
logs/soak_selfheal_events.log。24h 后自动退出。
运行：runtime/py312/pythonw.exe scripts/soak_selfheal_probe.py（工作目录=仓库根，脱离会话）。
复核：docs/test-plan-selfhealing-2026-10-20.md S 组（S-01/S-05/S-11 判据）。
"""
import json
import time
import urllib.request
from datetime import datetime

OUT = "logs/soak_selfheal.jsonl"
EVT = "logs/soak_selfheal_events.log"
DEADLINE = time.time() + 24 * 3600
_last_uptime = None


def log_evt(msg: str) -> None:
    with open(EVT, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def snap() -> dict:
    rec: dict = {"ts": time.time()}
    try:
        with urllib.request.urlopen("http://127.0.0.1:5800/health", timeout=5) as r:
            d = json.load(r)["data"]
        rec["status"] = d.get("status")
        rec["uptime_s"] = round(d.get("uptime_s") or 0, 1)
    except Exception as exc:  # noqa: BLE001 - 观察器自身不抛
        rec["status"] = "down"
        rec["err"] = str(exc)[:60]
    try:
        import psutil
        rec["ram_pct"] = psutil.virtual_memory().percent
    except Exception:  # noqa: BLE001
        pass
    try:
        import torch
        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            rec["vram_used_gb"] = round((total - free) / 1024 ** 3, 2)
    except Exception:  # noqa: BLE001
        pass
    return rec


def main() -> None:
    global _last_uptime
    log_evt("soak start (24h)")
    while time.time() < DEADLINE:
        rec = snap()
        try:
            with open(OUT, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass
        u = rec.get("uptime_s")
        if rec.get("status") == "down":
            log_evt("down: /health 不可达")
        elif (_last_uptime is not None and isinstance(u, (int, float))
              and u < _last_uptime - 30):
            log_evt(f"restart: uptime {_last_uptime:.0f} -> {u:.0f}")
        if (rec.get("ram_pct") or 0) >= 85:
            log_evt(f"ramlow: {rec['ram_pct']:.0f}%")
        if isinstance(u, (int, float)):
            _last_uptime = u
        time.sleep(60)
    log_evt("soak end (24h reached)")


if __name__ == "__main__":
    main()
