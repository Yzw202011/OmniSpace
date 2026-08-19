"""OmniSpace AI v2.3.1 全功能模块测试 harness（极致颗粒细度增强版）。

用法：
    runtime/py310/python.exe -m tests.flow.run <模块名|all> [--write]

模块名：sys / chat / paint / comic / learn / model / style / set / cross / all

结果等级：
    PASS     通过（预期结果全部达成）
    DEGRADED 通过但走诚实降级路径（随包资产缺失导致，功能链路本身正常）
    FAIL     失败（发现异常，需修复）
    SKIP     跳过（纯 UI 交互/物理环境操作，API 层不可测，留待浏览器冒烟）
    BLOCKED  被前置依赖阻塞未执行

结果输出：tests/flow/results/<module>.json 与汇总 summary.json
"""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests

BASE = "http://127.0.0.1:5800"
API = f"{BASE}/api/v1"
RESULT_DIR = Path(__file__).parent / "results"

# ── 结果记录 ─────────────────────────────────────────────────────

class Recorder:
    def __init__(self, module: str) -> None:
        self.module = module
        self.rows: list[dict] = []

    def record(self, tc_id: str, name: str, status: str,
               priority: str = "", detail: str = "",
               duration_ms: int = 0) -> dict:
        row = {
            "tc_id": tc_id, "name": name, "module": self.module,
            "status": status, "priority": priority,
            "detail": detail, "duration_ms": duration_ms,
        }
        self.rows.append(row)
        mark = {"PASS": "✓", "DEGRADED": "◐", "FAIL": "✗",
                "SKIP": "○", "BLOCKED": "⊘"}.get(status, "?")
        print(f"  [{mark}] {tc_id} {name} [{status}] {detail[:110]}")
        return row

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for r in self.rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return {"module": self.module, "total": len(self.rows),
                "counts": counts}


# ── HTTP 客户端 ──────────────────────────────────────────────────

class Client:
    """统一信封 {success, data, error} 解析。"""

    def __init__(self, base: str = BASE) -> None:
        self.base = base
        self.s = requests.Session()
        self.s.headers["Content-Type"] = "application/json"

    def raw(self, method: str, path: str, **kw) -> requests.Response:
        kw.setdefault("timeout", 120)
        return self.s.request(method, self.base + path, **kw)

    def call(self, method: str, path: str, **kw) -> dict:
        """返回解析后信封 dict；网络异常抛 requests 异常。"""
        resp = self.raw(method, path, **kw)
        try:
            return resp.json()
        except Exception:
            return {"success": False, "error": {"code": -1,
                    "message": f"HTTP {resp.status_code} 非JSON: {resp.text[:200]}"},
                    "_http": resp.status_code}

    def get(self, path: str, **params) -> dict:
        return self.call("GET", path, params=params or None)

    def post(self, path: str, body: Any = None, **kw) -> dict:
        if isinstance(body, dict) or body is None:
            return self.call("POST", path, json=body or {}, **kw)
        return self.call("POST", path, data=body, **kw)

    def put(self, path: str, body: Any = None) -> dict:
        return self.call("PUT", path, json=body or {})

    def delete(self, path: str, body: Any = None, **params) -> dict:
        return self.call("DELETE", path, json=body, params=params or None)

    def upload(self, path: str, field: str, filename: str,
               content: bytes, form: dict | None = None) -> dict:
        files = {field: (filename, content)}
        hdr = {k: v for k, v in self.s.headers.items()
               if k.lower() != "content-type"}
        resp = requests.post(self.base + path, files=files, data=form or {},
                             headers=hdr, timeout=180)
        try:
            return resp.json()
        except Exception:
            return {"success": False,
                    "error": {"code": -1, "message": f"HTTP {resp.status_code}"}}


def ok_data(env: dict) -> dict | None:
    """信封成功时返回 data，否则 None。"""
    if isinstance(env, dict) and env.get("success") and env.get("data") is not None:
        return env["data"]
    return None


def err_code(env: dict) -> Any:
    e = (env or {}).get("error") or {}
    return e.get("code")


def err_msg(env: dict) -> str:
    e = (env or {}).get("error") or {}
    return str(e.get("message", ""))


# ── TC 注册表 ────────────────────────────────────────────────────

CaseFn = Callable[[Client, Recorder], None]
_CASES: dict[str, list[tuple[str, str, str, CaseFn]]] = {}


def case(module: str, tc_id: str, name: str, priority: str = "P2"):
    """注册测试用例装饰器。"""
    def deco(fn: CaseFn) -> CaseFn:
        _CASES.setdefault(module, []).append((tc_id, name, priority, fn))
        return fn
    return deco


def run_module(module: str, client: Client) -> Recorder:
    rec = Recorder(module)
    for tc_id, name, prio, fn in _CASES.get(module, []):
        t0 = time.time()
        try:
            fn(client, rec)
        except AssertionError as exc:
            rec.record(tc_id, name, "FAIL", prio, f"断言失败: {exc}",
                       int((time.time() - t0) * 1000))
        except requests.exceptions.RequestException as exc:
            rec.record(tc_id, name, "FAIL", prio, f"网络异常: {exc}",
                       int((time.time() - t0) * 1000))
        except Exception as exc:  # noqa: BLE001
            rec.record(tc_id, name, "FAIL", prio,
                       f"执行异常 {type(exc).__name__}: {exc}",
                       int((time.time() - t0) * 1000))
    return rec


# ── 通用工具 ─────────────────────────────────────────────────────

def uid() -> str:
    return uuid.uuid4().hex


def tiny_png_b64() -> str:
    """1x1 PNG base64（用于多模态/图生图输入）。"""
    import base64
    import io

    from PIL import Image
    img = Image.new("RGB", (64, 64), (120, 80, 160))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def tiny_wav_bytes(seconds: float = 1.0, rate: int = 16000) -> bytes:
    """生成正弦波 WAV（用于 ASR 转写上传）。"""
    import io
    import math
    import struct
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(int(seconds * rate)))
        w.writeframes(frames)
    return buf.getvalue()


def write_results(rec: Recorder) -> Path:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULT_DIR / f"{rec.module}.json"
    path.write_text(json.dumps(
        {"summary": rec.summary(), "cases": rec.rows},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return path
