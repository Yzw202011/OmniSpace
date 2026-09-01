"""T3: 对话请求持锁期间 release(paint) 应跳过 vLLM（功能锁保护验证）。

场景：用户发消息（WS 流式推理中，dialog 功能锁持有）→ 前端路由切到
paint 触发 release-for-module → vLLM（正在服务该对话）不得被杀，
流式回复必须继续完整收尾。
"""
import asyncio
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8765/api/v1"
WS_URL = "ws://127.0.0.1:8765/api/v1/dialog/stream/t3-lock-test"


def http(method: str, path: str, body=None, timeout: float = 15.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


async def main() -> None:
    # 1. warmup dialog -> vLLM 8b-awq cold start
    r = http("POST", "/models/warmup",
             {"feature": "dialog", "model_id": "qwen3-vl-8b-awq"})
    print(f"[1] warmup: started={r['data']['started']} "
          f"reason={r['data'].get('reason', '')}")

    # 2. wait until vLLM healthy
    t0 = time.time()
    while time.time() - t0 < 300:
        st = http("GET", "/models/vllm/status")
        if st["data"].get("healthy"):
            print(f"[2] vLLM healthy after {time.time() - t0:.0f}s "
                  f"(model={st['data'].get('served_name')})")
            break
        await asyncio.sleep(3)
    else:
        print("[2] FAILED: vLLM not healthy within 300s")
        return

    # 3. WS connect + send message (acquires dialog feature lock)
    import websockets
    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({
            "type": "message",
            "data": {"content": "请从1数到20，每个数字单独一行输出"},
        }))
        got_first = False
        tokens_after_release = 0
        released = False
        t_send = time.time()
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=180)
            except asyncio.TimeoutError:
                print("[!] stream recv timeout")
                break
            except Exception as exc:  # noqa: BLE001
                print(f"[!] stream closed: {exc}")
                break
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype == "status":
                print(f"[3s] status push: {msg['data'].get('message', '')[:60]}")
            elif mtype == "token":
                if not got_first:
                    got_first = True
                    print(f"[3] first token after {time.time() - t_send:.1f}s "
                          "(dialog lock held, inference running)")
                    # 4. release for paint while the lock is held
                    tr = time.time()
                    rel = http("POST", "/models/release-for-module",
                               {"module": "paint", "timeout_ms": 3000})
                    dt = time.time() - tr
                    d = rel.get("data") or {}
                    print(f"[4] release in {dt:.2f}s success={rel['success']} "
                          f"completed={d.get('completed')} "
                          f"skipped={d.get('skipped')}")
                    released = True
                elif released:
                    tokens_after_release += 1
            elif mtype in ("meta", "error", "done"):
                print(f"[5] {mtype}: "
                      f"{json.dumps(msg.get('data', {}), ensure_ascii=False)[:160]}")
                if mtype in ("error", "done"):
                    break

    # 6. vLLM must still be alive after the release + full stream
    st = http("GET", "/models/vllm/status")
    running = st["data"].get("running")
    healthy = st["data"].get("healthy")
    print(f"[6] vllm after release+stream: running={running} "
          f"healthy={healthy} (expect True/True)")
    print(f"[7] tokens received after release: {tokens_after_release} "
          "(expect >0: stream not interrupted)")
    ok = bool(running and healthy and tokens_after_release > 0 and released)
    print("T3 " + ("PASS" if ok else "FAIL"))


asyncio.run(main())
