"""实证 CROSS-023：paint 在载 → chat 触发驱逐 → 显存是否回来。"""
import json
import sys
import time
import urllib.request

API = "http://127.0.0.1:5800/api/v1"


def call(method, path, body=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def vram(tag):
    d = call("GET", "/models/vram").get("data") or {}
    gpu = d.get("gpu") or d
    print(f"[vram|{tag}] total={gpu.get('total_gb')} used={gpu.get('used_gb')} "
          f"free={gpu.get('free_gb')} loaded={d.get('loaded_models') or d.get('loaded')}")
    return gpu


def main() -> int:
    vram("起始")
    # 1) paint 生成（加载 SDXL 保持在载）
    env = call("POST", "/paint/generate", {
        "prompt": "a red apple on a table", "width": 512, "height": 512,
        "steps": 4}, timeout=300)
    ptid = (env.get("data") or {}).get("task_id")
    t0 = time.time()
    while time.time() - t0 < 120:
        st = call("GET", f"/paint/result/{ptid}").get("data") or {}
        if st.get("status") in ("done", "error", "failed"):
            break
        time.sleep(1.0)
    print(f"[paint] {st.get('status')} ({time.time()-t0:.1f}s)")
    vram("paint在载")

    # 2) chat/send 触发对话模型加载（内含驱逐 paint 逻辑）
    t0 = time.time()
    env = call("POST", "/chat/send", {
        "content": "用一句话说明什么是机器学习", "stream": False,
        "max_new_tokens": 32}, timeout=600)
    ms = int((time.time() - t0) * 1000)
    if env.get("success"):
        d = env["data"]
        print(f"[chat] OK {ms}ms content={str((d.get('message') or {}).get('content'))[:40]!r}")
    else:
        print(f"[chat] FAIL {ms}ms: {json.dumps(env.get('error'), ensure_ascii=False)[:250]}")
    vram("chat之后")
    return 0 if env.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
