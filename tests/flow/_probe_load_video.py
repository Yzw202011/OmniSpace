"""负载复现 COMIC-118 编码失败：SDXL 在载 + 20步生成后立即视频生成。

镜像 2026-08-09 04:00 失败上下文（paint 20步完成 1s 后 video_gen 启动）。
"""
import base64
import json
import sys
import time
import urllib.request

API = "http://127.0.0.1:5800/api/v1"


def tiny_png_b64() -> str:
    raw = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000d49444154789c626001000000ffff030000060005"
        "57bfabd40000000049454e44ae426082")
    return base64.b64encode(raw).decode()


def call(method, path, body=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def main() -> int:
    # 1) 触发绘画模型加载 + 20 步生成（制造 VRAM/CPU 高压上下文）
    print("[1] paint generate (20 steps, 保持模型在载)...")
    t0 = time.time()
    env = call("POST", "/paint/generate", {
        "prompt": "a castle in the sky, masterpiece",
        "width": 512, "height": 512, "steps": 20,
    }, timeout=300)
    ptid = (env.get("data") or {}).get("task_id")
    if not ptid:
        print("    paint create failed:", json.dumps(env, ensure_ascii=False)[:200])
        return 1
    # 轮询绘画任务直到完成（模型保持在载）
    while time.time() - t0 < 300:
        st = call("GET", f"/paint/result/{ptid}").get("data") or {}
        if st.get("status") in ("done", "error", "failed"):
            print(f"    paint {st.get('status')} ({time.time()-t0:.1f}s)")
            break
        time.sleep(1.0)

    # 2) 立即视频生成（1s 后，镜像失败时序）
    print("[2] video generate immediately...")
    env = call("POST", "/manga/video/generate", {
        "storyboard_row_id": "loadrepro1",
        "description": "主角走入宫殿",
        "screenshot_4in1": tiny_png_b64(),
        "character_assets": ["主角"],
        "resolution": "720p", "fps": 24, "duration_seconds": 1,
        "codec": "h264",
    })
    tid = (env.get("data") or {}).get("task_id")
    print("    task:", tid)
    if not tid:
        print("    create failed:", json.dumps(env, ensure_ascii=False)[:300])
        return 1
    t0 = time.time()
    status = "?"
    while time.time() - t0 < 60:
        st = call("GET", f"/manga/video/{tid}/status").get("data") or {}
        status = st.get("status")
        if status in ("done", "error"):
            print(f"    final({time.time()-t0:.1f}s): "
                  f"{json.dumps(st, ensure_ascii=False)[:400]}")
            break
        time.sleep(0.8)
    print("VERDICT:", status)
    return 0 if status == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
