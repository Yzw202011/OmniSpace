"""B0 实弹：vLLM 0.27.1 sleep mode 重开验证（tools/scratch/vllm_sleep_ab.py）。

验证链：--enable-sleep-mode 启动健康 → POST /sleep?level=1 → is_sleeping=true
→ 显存回落（权重卸 RAM）→ /wake_up → 显存回涨 → stop 收尾。
0.26 时代 cumem 崩溃则 start=False（诚实记录，回退杀进程让渡）。
"""
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, r"E:\OmniSpace")

from backend.engines.vllm_service import (  # noqa: E402
    VLLM_HOST,
    VLLM_PORT,
    get_vllm_service,
)

MODEL_DIR = r"E:\OmniSpace\models\qwen3-vl-8b-awq"


def vram() -> str:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader"],
        capture_output=True, text=True).stdout.strip()
    return out.splitlines()[0] if out else "?"


def http(method: str, path: str, timeout: float = 30.0):
    try:
        r = urllib.request.urlopen(
            f"http://{VLLM_HOST}:{VLLM_PORT}{path}", timeout=timeout,
            data=None if method == "GET" else b"")
        return r.status, r.read()[:120]
    except Exception as e:  # noqa: BLE001
        return type(e).__name__, str(e)[:90]


def main() -> int:
    svc = get_vllm_service()
    print("VRAM before:", vram())
    t0 = time.time()
    ok = svc.start(model_dir=MODEL_DIR)
    print(f"start={ok} ({time.time()-t0:.0f}s) healthy={svc.is_healthy()}")
    if not ok:
        print("last_error:", getattr(svc, "_last_error", "?"))
        return 1
    print("VRAM ready:", vram())

    print("POST /sleep?level=1 ->", http("POST", "/sleep?level=1"))
    sleeping = False
    for i in range(40):
        st = http("GET", "/is_sleeping", timeout=10)
        if st[0] == 200 and b"true" in st[1]:
            sleeping = True
            print(f"is_sleeping=true ({i*2}s)")
            break
        time.sleep(2)
    print("VRAM after sleep:", vram())

    print("POST /wake_up ->", http("POST", "/wake_up", timeout=60))
    awake = False
    for i in range(60):
        st = http("GET", "/is_sleeping", timeout=10)
        if st[0] == 200 and b"false" in st[1]:
            awake = True
            print(f"is_sleeping=false ({i*2}s)")
            break
        time.sleep(2)
    print("VRAM after wake:", vram())
    ok2 = http("GET", "/health")
    print("health after wake:", ok2[0])

    svc.stop()
    time.sleep(3)
    print("stopped; VRAM final:", vram())
    print("VERDICT:", "PASS" if (sleeping and awake and ok2[0] == 200)
          else "PARTIAL/FAIL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
