"""Mock 云端视频服务商：批3 云端API 单测与实弹验收共用。

纯 stdlib（http.server），DashScope 形态异步三段式（对齐
backend/services/inference/cloud_video_client.py 的调用面）：
- POST /api/v1/services/aigc/video-generation/video-synthesis
  → output.task_id（请求体记入 last_request：prompt/img_url 前缀/
  duration/aspect）
- GET  /api/v1/tasks/{id} → 前 pending_polls 次 PENDING/RUNNING，
  其后 SUCCEEDED + video_url（status_override 可伪造 FAILED）
- GET  /download/{task_id}.mp4 → 极小伪 mp4 字节

用法：线程内嵌（单测）srv = MockCloudVideo(port=0); srv.start()
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 极小伪 mp4（契约校验只需「非空字节」；合法性由真实服务商保证）
_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


class MockCloudVideo:
    """线程化 mock 视频服务商：DashScope 异步三段式。"""

    def __init__(self, port: int = 0, pending_polls: int = 2,
                 status_override: str = "") -> None:
        self.pending_polls = pending_polls
        self.status_override = status_override  # 非空=终态伪造
        self.last_request: dict = {}
        self._tasks: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._port = port

    @property
    def port(self) -> int:
        assert self._httpd is not None, "server 未启动"
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        srv = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # 静默
                pass

            def _json(self, obj: dict, code: int = 200) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - http.server 约定
                path = self.path
                if path.startswith("/download/"):
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", str(len(_MP4)))
                    self.end_headers()
                    self.wfile.write(_MP4)
                    return
                if path.startswith("/api/v1/tasks/"):
                    task_id = path.rsplit("/", 1)[-1]
                    with srv._lock:
                        info = srv._tasks.get(task_id)
                        if info is None:
                            self._json({"output": {"task_status": "FAILED",
                                                   "message": "task 不存在"}}, 404)
                            return
                        info["polls"] += 1
                        polls = info["polls"]
                    if srv.status_override:
                        self._json({"output": {
                            "task_status": srv.status_override,
                            "message": "伪造终态（测试）"}})
                        return
                    if polls < srv.pending_polls:
                        status = "PENDING" if polls == 1 else "RUNNING"
                        self._json({"output": {"task_status": status}})
                        return
                    self._json({"output": {
                        "task_status": "SUCCEEDED",
                        "video_url": f"http://127.0.0.1:{srv.port}/download/{task_id}.mp4"}})
                    return
                self._json({"error": "not found"}, 404)

            def do_POST(self) -> None:  # noqa: N802 - http.server 约定
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    payload = {}
                if self.path.endswith(
                        "/video-generation/video-synthesis"):
                    task_id = f"vtask-{uuid.uuid4().hex[:8]}"
                    with srv._lock:
                        srv._tasks[task_id] = {"polls": 0}
                    srv.last_request = {
                        "authorization":
                            self.headers.get("Authorization") or "",
                        "payload": payload,
                        "img_url_is_dataurl": str(
                            (payload.get("input") or {}).get("img_url", "")
                        ).startswith("data:image/"),
                    }
                    self._json({"output": {"task_id": task_id,
                                           "task_status": "PENDING"}})
                    return
                self._json({"error": "not found"}, 404)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self._port), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None


def main() -> None:  # pragma: no cover - 手动验收进程
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8104)
    args = parser.parse_args()
    srv = MockCloudVideo(port=args.port)
    srv.start()
    print(f"mock cloud video server: {srv.base_url}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()
