"""基准脚本共享设施（V5 升级基准尺子，2026-09-08）。

供 vllm_bench.py / paint_bench.py 复用：采样器（GPU/RAM/提交内存）、
JSON 报告落盘、时间戳文件名。纯 stdlib，零第三方依赖，不被 pytest
收集（非 test_ 前缀；目录亦无 __init__）。

纪律：跑基准 = GPU 动作，动手前必须过《GPU 测试活动门》
（后端近 5 分钟任务 + >1GB comfy 进程核查）。
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

# 仓库根（backend/tests/eval/_bench_common.py → 上溯 3 级）
ROOT_DIR = Path(__file__).resolve().parents[3]
EVAL_OUT_DIR = ROOT_DIR / "logs" / "eval"


class _PerformanceInformation(ctypes.Structure):
    """Windows GetPerformanceInfo 结构（Commit 口径与 WER RADAR 同源）。"""

    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", ctypes.c_ulong),
        ("ProcessCount", ctypes.c_ulong),
        ("ThreadCount", ctypes.c_ulong),
    ]


def get_commit_gb() -> tuple[float, float]:
    """返回 (已提交 GB, 提交上限 GB)；取数失败返回 (-1.0, -1.0)。"""
    try:
        psapi = ctypes.WinDLL("psapi")
        info = _PerformanceInformation()
        info.cb = ctypes.sizeof(_PerformanceInformation)
        if not psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
            return -1.0, -1.0
        page = float(info.PageSize)
        return info.CommitTotal * page / 2**30, info.CommitLimit * page / 2**30
    except Exception:  # noqa: BLE001 - 采样失败不致命，报告里标 -1
        return -1.0, -1.0


def get_ram_gb() -> tuple[float, float]:
    """返回 (物理已用 GB, 物理总量 GB)；失败返回 (-1.0, -1.0)。"""
    try:
        import psutil

        vm = psutil.virtual_memory()
        return vm.used / 2**30, vm.total / 2**30
    except Exception:  # noqa: BLE001
        return -1.0, -1.0


def query_gpu() -> dict[str, int]:
    """nvidia-smi 单次采样：used/total MB、util%、temp℃。失败返回空 dict。"""
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=memory.used,memory.total,utilization.gpu,"
             "temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        parts = [int(p.strip()) for p in out.stdout.strip().split(",")]
        if len(parts) >= 4:
            return {"gpu_used_mb": parts[0], "gpu_total_mb": parts[1],
                    "gpu_util_pct": parts[2], "gpu_temp_c": parts[3]}
    except Exception:  # noqa: BLE001
        pass
    return {}


class ResourceSampler:
    """后台线程按固定间隔采样资源，记录峰值。stop() 后取 summary()。"""

    def __init__(self, interval_s: float = 1.0) -> None:
        self._interval = interval_s
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.samples: list[dict[str, Any]] = []
        self.peak_gpu_mb = 0
        self.peak_commit_gb = 0.0
        self.peak_ram_gb = 0.0

    def _tick(self) -> None:
        gpu = query_gpu()
        commit_gb, _limit = get_commit_gb()
        ram_gb, _total = get_ram_gb()
        rec: dict[str, Any] = {"t": round(time.time(), 3), **gpu,
                               "commit_gb": round(commit_gb, 2),
                               "ram_gb": round(ram_gb, 2)}
        with self._lock:
            self.samples.append(rec)
            self.peak_gpu_mb = max(self.peak_gpu_mb,
                                   rec.get("gpu_used_mb", 0))
            self.peak_commit_gb = max(self.peak_commit_gb, commit_gb)
            self.peak_ram_gb = max(self.peak_ram_gb, ram_gb)

    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            self._tick()
            self._stop_evt.wait(self._interval)

    def __enter__(self) -> ResourceSampler:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._tick()  # 收尾补一拍

    def summary(self, keep_samples: bool = False) -> dict[str, Any]:
        with self._lock:
            out: dict[str, Any] = {
                "peak_gpu_mb": self.peak_gpu_mb,
                "peak_commit_gb": round(self.peak_commit_gb, 2),
                "peak_ram_gb": round(self.peak_ram_gb, 2),
            }
            if keep_samples:
                out["samples"] = self.samples
            return out


def save_report(name: str, payload: dict[str, Any],
                keep_samples: bool = False) -> Path:
    """落盘 logs/eval/<name>_<时间戳>.json，返回路径。"""
    EVAL_OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = EVAL_OUT_DIR / f"{name}_{stamp}.json"
    if not keep_samples and isinstance(payload.get("resources"), dict):
        payload["resources"] = {
            k: v for k, v in payload["resources"].items()
            if k != "samples"}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def http_json(url: str, payload: dict[str, Any] | None = None,
              timeout: int = 30, method: str | None = None) -> tuple[int, Any]:
    """极简 JSON HTTP（urllib，零依赖）。返回 (status, 解析后的 body)。"""
    import urllib.request

    data = None
    req_method = method or ("POST" if payload is not None else "GET")
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method=req_method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:  # noqa: PERF203
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body) if body else None
        except json.JSONDecodeError:
            return exc.code, body
