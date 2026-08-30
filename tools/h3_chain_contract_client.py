"""计划单 v1 契约测试客户端（第 6 轮·契约测试）。

模拟"外部软件":只通过 HTTP 使用漫剧模块的视频能力,
不 import 任何后端代码、不知道 ComfyUI 存在。
用法:
  python h3_chain_contract_client.py --backend http://127.0.0.1:5800 \
      --row-id e5222766158f4e9aaa0cddf6cd66cf72 --seconds 10 --quality 480p
依据: docs/h3-chain-plan-contract-v1.md
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="http://127.0.0.1:5800")
    ap.add_argument("--row-id", required=True)
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--quality", default="480p", choices=["480p", "720p"])
    a = ap.parse_args()

    # 1. 递计划单(契约 §2)
    body = json.dumps({"row_ids": [a.row_id],
                       "seconds_per_shot": a.seconds,
                       "quality": a.quality}).encode()
    req = urllib.request.Request(
        f"{a.backend}/api/v1/manga/video/generate_h3_chain",
        data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=20))
    except urllib.error.HTTPError as exc:
        print("[契约] 被拒绝:", exc.read().decode("utf-8", "ignore")[:300])
        return 1
    if not resp.get("success"):
        print("[契约] 参数错误:", resp.get("error", {}).get("message"))
        return 1
    task_id = resp["data"]["task_id"]
    print(f"[契约] 受理 task_id={task_id}")

    # 2. 轮询(契约 §4)
    deadline = time.time() + 1500
    status = ""
    while time.time() < deadline:
        time.sleep(10)
        r = json.load(urllib.request.urlopen(
            f"{a.backend}/api/v1/manga/video/{task_id}/status", timeout=10))
        d = r.get("data") or {}
        status, prog = d.get("status"), d.get("progress")
        print(f"[契约] 状态={status} 进度={prog}")
        if status in ("done", "failed"):
            break
    if status != "done":
        print("[契约] 未完成(见契约 §5 错误码)")
        return 1

    # 3. 取结果(契约 §4)
    r = json.load(urllib.request.urlopen(
        f"{a.backend}/api/v1/manga/video/{task_id}/result", timeout=10))
    result = (r.get("data") or {}).get("result") or {}
    print("[契约] 成片:", result.get("file_path"),
          "| 时长:", result.get("duration_seconds"),
          "| 分辨率:", result.get("resolution"),
          "| file_exists:", result.get("file_exists"))
    print("[契约] 下载:", a.backend + result.get("download_url", ""))
    return 0 if result.get("file_exists") else 1


if __name__ == "__main__":
    raise SystemExit(main())
