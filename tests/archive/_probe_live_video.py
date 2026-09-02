"""活体复现 COMIC-118：直接调 /manga/video/generate 并轮询结果。"""
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


def post(path, body):
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def get(path):
    return json.loads(urllib.request.urlopen(API + path, timeout=15).read())


def main() -> int:
    env = post("/manga/video/generate", {
        "storyboard_row_id": "liveprobe1",
        "description": "主角走入宫殿",
        "screenshot_4in1": tiny_png_b64(),
        "character_assets": ["主角"],
        "resolution": "720p", "fps": 24, "duration_seconds": 1,
        "codec": "h264",
    })
    print("create:", json.dumps(env, ensure_ascii=False)[:300])
    tid = (env.get("data") or {}).get("task_id")
    if not tid:
        return 1
    t0 = time.time()
    while time.time() - t0 < 60:
        st = get(f"/manga/video/{tid}/status").get("data") or {}
        s = st.get("status")
        if s in ("done", "error"):
            print(f"final({time.time()-t0:.1f}s):",
                  json.dumps(st, ensure_ascii=False)[:500])
            break
        time.sleep(1.0)
    if s == "done":
        res = get(f"/manga/video/{tid}/result").get("data") or {}
        print("result:", json.dumps(res, ensure_ascii=False)[:400])
    return 0


if __name__ == "__main__":
    sys.exit(main())
