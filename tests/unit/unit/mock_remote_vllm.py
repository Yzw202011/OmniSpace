"""Mock vLLM 服务器（OpenAI 兼容子集）：远程引擎单测与真机验收共用。

行为：
- GET /health          → 200（可配置前 N 次返回 503 模拟远端冷启动装载）
- GET /v1/models       → 200（健康探测回退路径）
- POST /v1/chat/completions → SSE 流式回复（3 个 content 增量 + [DONE]），
  同时把最近一次请求的 Authorization/model/采样扩展字段记录到
  `server.last_request`（单测断言透传链路用）。

用法：
- 线程内嵌（单测）：srv = FakeRemoteVLLM(port=0); srv.start();
  port = srv.server_address[1]
- 独立进程（真机验收）：runtime/py310/python.exe -m
  tests.unit.unit.mock_remote_vllm --port 8102

纯 stdlib（http.server），不依赖 pytest/后端包。
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeRemoteVLLM:
    """线程化 mock 服务器：health 冷启动模拟 + SSE 对话 + 请求捕获。"""

    def __init__(self, port: int = 0, fail_health_first: int = 0) -> None:
        self.fail_health_first = fail_health_first
        self._health_hits = 0
        self._lock = threading.Lock()
        self.last_request: dict = {}   # 最近一次 chat 请求快照（headers/payload）
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._port = port

    # ── 生命周期 ────────────────────────────────────────────────
    @property
    def port(self) -> int:
        assert self._httpd is not None, "server 未启动"
        return int(self._httpd.server_address[1])

    def start(self) -> None:
        srv = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # 静默
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - http.server 命名
                if self.path == "/health":
                    with srv._lock:
                        srv._health_hits += 1
                        should_fail = srv._health_hits <= srv.fail_health_first
                    if should_fail:
                        self._send(503, b'{"error":"loading"}',
                                   "application/json")
                        return
                    self._send(200, b'{"status":"ok"}', "application/json")
                    return
                if self.path == "/v1/models":
                    self._send(200, b'{"data":[{"id":"mock-32b"}]}',
                               "application/json")
                    return
                self._send(404, b'{"error":"not found"}', "application/json")

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/v1/chat/completions":
                    self._send(404, b'{"error":"not found"}',
                               "application/json")
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = {}
                with srv._lock:
                    srv.last_request = {
                        "authorization": self.headers.get("Authorization"),
                        "payload": payload,
                    }
                self.send_response(200)
                self.send_header("Content-Type",
                                 "text/event-stream; charset=utf-8")
                self.end_headers()
                for piece in ("你好", "，", "世界"):
                    chunk = {"choices": [{"delta": {"content": piece}}]}
                    self.wfile.write(
                        f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                        .encode())
                self.wfile.write(b"data: [DONE]\n\n")

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self._port), _Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock vLLM（OpenAI 兼容子集）")
    parser.add_argument("--port", type=int, default=8102)
    parser.add_argument("--fail-health-first", type=int, default=0,
                        help="前 N 次 /health 返回 503（模拟冷启动装载）")
    args = parser.parse_args()
    srv = FakeRemoteVLLM(port=args.port,
                         fail_health_first=args.fail_health_first)
    srv.start()
    print(f"mock vLLM listening on http://127.0.0.1:{srv.port}", flush=True)
    threading.Event().wait()  # 常驻


if __name__ == "__main__":
    main()
# 本项目仅供学习使用，商业授权请+Q 3559331368
