"""Mock 云端图片服务商：批2 云端API 单测与实弹验收共用。

纯 stdlib（http.server），实现两套协议（对齐
backend/services/inference/cloud_image_client.py 的调用面）：

openai_image（同步）：
- POST /v1/images/generations → {"data": [{"b64_json" | "url"}]}
  （b64_mode=True 返回 b64_json，否则返回 /files/xxx URL）
- GET  /files/{name} → PNG 字节

task_image（DashScope 形态异步三段式）：
- POST /api/v1/services/aigc/text2image/image-synthesis → output.task_id
- POST /api/v1/services/aigc/multimodal-generation/generation
  → output.task_id（带参考图，请求体记入 last_request.ref_count）
- GET  /api/v1/tasks/{id} → 前 pending_polls 次 PENDING/RUNNING，
  其后 SUCCEEDED（text2image 形态 results[].url；multimodal 形态
  choices[].message.content[].image）
- GET  /download/{task_id}.png → PNG 字节

用法：
- 线程内嵌（单测）：srv = MockCloudImage(port=0); srv.start()
- 独立进程：runtime/py310/python.exe -m backend.tests.unit.mock_cloud_image
  --port 8103
"""
from __future__ import annotations

import argparse
import base64
import json
import struct
import threading
import time
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _make_png(width: int = 4, height: int = 4) -> bytes:
    """程序化生成最小合法 RGBA PNG（纯 stdlib：zlib + struct）。"""
    raw = b"".join(
        b"\x00" + bytes([200, 30, 30, 255] * width)  # filter 0 + 红色行
        for _ in range(height))

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, 9))
            + _chunk(b"IEND", b""))


_PNG = _make_png()


class MockCloudImage:
    """线程化 mock 图片服务商：同步出图 + DashScope 异步三段式。"""

    def __init__(self, port: int = 0, b64_mode: bool = True,
                 pending_polls: int = 2,
                 task_status_override: str = "") -> None:
        self.b64_mode = b64_mode
        self.pending_polls = pending_polls
        self.task_status_override = task_status_override  # 非空=终态伪造
        self.last_request: dict = {}      # 最近一次提交请求快照
        self._tasks: dict[str, dict] = {}  # task_id → {polls, kind, refs}
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

            def _png(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(_PNG)))
                self.end_headers()
                self.wfile.write(_PNG)

            def do_GET(self) -> None:  # noqa: N802 - http.server 约定
                path = self.path
                if path.startswith("/files/") or path.startswith("/download/"):
                    self._png()
                    return
                if path.startswith("/api/v1/tasks/"):
                    task_id = path.rsplit("/", 1)[-1].split(".")[0]
                    with srv._lock:
                        info = srv._tasks.get(task_id)
                        if info is None:
                            self._json({"output": {"task_status": "FAILED",
                                                   "message": "task 不存在"}}, 404)
                            return
                        info["polls"] += 1
                        polls = info["polls"]
                    if srv.task_status_override:
                        self._json({"output": {
                            "task_status": srv.task_status_override,
                            "message": "伪造终态（测试）"}})
                        return
                    if polls < srv.pending_polls:
                        status = "PENDING" if polls == 1 else "RUNNING"
                        self._json({"output": {"task_status": status}})
                        return
                    if info["kind"] == "multimodal":
                        self._json({"output": {"task_status": "SUCCEEDED",
                                               "choices": [{"message": {"content": [
                                                   {"image": f"http://127.0.0.1:{srv.port}/download/{task_id}.png"}]}}]}})
                    else:
                        self._json({"output": {"task_status": "SUCCEEDED",
                                               "results": [
                                                   {"url": f"http://127.0.0.1:{srv.port}/download/{task_id}.png"}]}})
                    return
                self._json({"error": "not found"}, 404)

            def do_POST(self) -> None:  # noqa: N802 - http.server 约定
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    payload = {}
                auth = self.headers.get("Authorization") or ""
                if self.path == "/v1/images/generations":
                    srv.last_request = {"path": self.path,
                                        "authorization": auth,
                                        "payload": payload}
                    if srv.b64_mode:
                        item = {"b64_json": base64.b64encode(_PNG).decode()}
                    else:
                        item = {"url": f"http://127.0.0.1:{srv.port}/files/x.png"}
                    self._json({"data": [item]})
                    return
                if self.path.endswith("/text2image/image-synthesis"):
                    task_id = f"task-{uuid.uuid4().hex[:8]}"
                    with srv._lock:
                        srv._tasks[task_id] = {"polls": 0, "kind": "t2i",
                                               "refs": 0}
                    srv.last_request = {"path": self.path,
                                        "authorization": auth,
                                        "payload": payload}
                    self._json({"output": {"task_id": task_id,
                                           "task_status": "PENDING"}})
                    return
                if self.path.endswith("/multimodal-generation/generation"):
                    task_id = f"task-{uuid.uuid4().hex[:8]}"
                    refs = 0
                    try:
                        content = payload["input"]["messages"][0]["content"]
                        refs = sum(1 for c in content if "image" in c)
                    except Exception:  # noqa: BLE001 - 计数失败不阻断
                        refs = 0
                    with srv._lock:
                        srv._tasks[task_id] = {"polls": 0, "kind": "multimodal",
                                               "refs": refs}
                    srv.last_request = {"path": self.path,
                                        "authorization": auth,
                                        "payload": payload,
                                        "ref_count": refs}
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8103)
    args = parser.parse_args()
    srv = MockCloudImage(port=args.port, b64_mode=False)
    srv.start()
    print(f"mock cloud image server: {srv.base_url}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()
