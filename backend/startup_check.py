"""OmniSpace AI v2.1 启动自检模块（规格 §6.4 启动时序 / §9 安装器自检）。

执行 26 项启动前自检，覆盖：Python 环境、GPU/CUDA、系统资源、
核心依赖包、目录权限、端口可用性等。
返回检查结果列表，供 Launcher 与前端展示。

规格引用：
  - §6.4 启动时序：自检 → 端口绑定 → 就绪
  - §9 安装器：26 项系统自检
  - §14 约束：最低硬件要求
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import concurrent.futures
import importlib
import logging
import os
import platform
import shutil
import socket
import sys
import time
from collections.abc import Callable

from .config import (
    APP_VERSION,
    DATA_DIR,
    DISK_MODELS_MIN_GB,  # 磁盘"含模型部署"可用门槛（P2 统一口径，读取 config.yaml `disk` 节）
    HOST,
    LOGS_DIR,
    MODELS_DIR,
    PORT,
)

log = logging.getLogger("omnispace.startup_check")

# 最低 Python 版本
_MIN_PYTHON = (3, 10)

# 最低内存（GB）
_MIN_RAM_GB = 8

# 核心依赖包清单
_CORE_DEPS = [
    ("fastapi", "FastAPI Web 框架"),
    ("uvicorn", "ASGI 服务器"),
    ("pydantic", "数据模型校验"),
    ("yaml", "YAML 配置解析"),
    ("psutil", "系统资源监控"),
]

# AI 依赖包清单
_AI_DEPS = [
    ("torch", "PyTorch 深度学习框架"),
    ("transformers", "HuggingFace 模型库"),
    ("chromadb", "ChromaDB 向量数据库"),
    ("lz4", "LZ4 压缩库"),
    ("redis", "Redis 客户端"),
]


class CheckResult:
    """单项检查结果。"""

    def __init__(self, idx: int, name: str, passed: bool,
                 detail: str = "", level: str = "info",
                 data: dict | None = None) -> None:
        self.idx = idx
        self.name = name
        self.passed = passed
        self.detail = detail
        self.level = level       # info / warning / error
        self.data = data or {}

    def to_dict(self) -> dict:
        return {
            "idx": self.idx,
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "level": self.level,
            "data": self.data,
        }

    def __repr__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"[{self.idx:02d}] {status} {self.name}: {self.detail}"


# ═══════════════════════════════════════════════════════════════════
#  26 项检查函数
# ═══════════════════════════════════════════════════════════════════

def _check_python_version() -> CheckResult:
    """01. Python 版本（>=3.10）。"""
    cur = sys.version_info[:2]
    passed = cur >= _MIN_PYTHON
    return CheckResult(
        1, "Python 版本", passed,
        f"当前 {cur[0]}.{cur[1]}，要求 >={_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}",
        "error" if not passed else "info",
        {"current": f"{cur[0]}.{cur[1]}", "required": f"{_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}"},
    )


def _check_os_compat() -> CheckResult:
    """02. 操作系统兼容性。"""
    system = platform.system()
    release = platform.release()
    machine = platform.machine()
    supported = system in ("Windows", "Linux", "Darwin")
    return CheckResult(
        2, "操作系统兼容性", supported,
        f"{system} {release} ({machine})",
        "error" if not supported else "info",
        {"system": system, "release": release, "machine": machine},
    )


def _check_gpu_detection() -> CheckResult:
    """03. GPU 检测。"""
    try:
        import pynvml
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        pynvml.nvmlShutdown()
        return CheckResult(3, "GPU 检测", count > 0,
                           f"检测到 {count} 块 NVIDIA GPU",
                           "warning" if count == 0 else "info",
                           {"gpu_count": count})
    except Exception:
        pass
    # 回退：尝试 torch
    try:
        import torch
        if torch.cuda.is_available():
            count = torch.cuda.device_count()
            return CheckResult(3, "GPU 检测", count > 0,
                               f"检测到 {count} 块 CUDA GPU（via torch）",
                               "info", {"gpu_count": count, "via": "torch"})
    except Exception:
        pass
    return CheckResult(3, "GPU 检测", False,
                       "未检测到独立显卡，部分功能受限",
                       "warning", {"gpu_count": 0})


def _check_gpu_vendor() -> CheckResult:
    """04. GPU 厂商识别。"""
    try:
        import pynvml
        pynvml.nvmlInit()
        name = pynvml.nvmlDeviceGetName(0)
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        pynvml.nvmlShutdown()
        return CheckResult(4, "GPU 厂商", True,
                           f"NVIDIA: {name}", "info",
                           {"vendor": "nvidia", "name": name})
    except Exception:
        pass
    try:
        import torch
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            return CheckResult(4, "GPU 厂商", True,
                               f"NVIDIA (CUDA): {name}", "info",
                               {"vendor": "nvidia", "name": name})
    except Exception:
        pass
    return CheckResult(4, "GPU 厂商", False,
                       "未识别到 GPU 厂商", "warning",
                       {"vendor": "none"})


def _check_cuda_available() -> CheckResult:
    """05. CUDA 可用性。"""
    try:
        import torch
        available = torch.cuda.is_available()
        return CheckResult(5, "CUDA 可用性", available,
                           "CUDA 可用" if available else "CUDA 不可用（CPU 模式）",
                           "warning" if not available else "info",
                           {"cuda_available": available})
    except ImportError:
        return CheckResult(5, "CUDA 可用性", False,
                           "PyTorch 未安装，无法检测 CUDA", "warning",
                           {"cuda_available": False})


def _check_cuda_version() -> CheckResult:
    """06. CUDA 版本。"""
    try:
        import torch
        if torch.cuda.is_available():
            ver = torch.version.cuda or "unknown"
            return CheckResult(6, "CUDA 版本", True,
                               f"CUDA {ver}", "info", {"version": ver})
        return CheckResult(6, "CUDA 版本", False,
                           "CUDA 不可用", "warning", {"version": None})
    except ImportError:
        return CheckResult(6, "CUDA 版本", False,
                           "PyTorch 未安装", "warning", {"version": None})


def _check_cudnn_version() -> CheckResult:
    """07. cuDNN 版本。"""
    try:
        import torch
        if torch.cuda.is_available() and torch.backends.cudnn.is_available():
            ver = torch.backends.cudnn.version()
            return CheckResult(7, "cuDNN 版本", True,
                               f"cuDNN {ver}", "info", {"version": ver})
        return CheckResult(7, "cuDNN 版本", False,
                           "cuDNN 不可用", "warning", {"version": None})
    except (ImportError, Exception):
        return CheckResult(7, "cuDNN 版本", False,
                           "无法检测 cuDNN", "warning", {"version": None})


def _check_gpu_vram() -> CheckResult:
    """08. GPU 显存总量。"""
    try:
        import pynvml
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        total = pynvml.nvmlDeviceGetMemoryInfo(handle).total
        pynvml.nvmlShutdown()
        vram_gb = round(total / (1024**3), 1)
        passed = vram_gb >= 6
        return CheckResult(8, "GPU 显存", passed,
                           f"{vram_gb} GB" + ("（建议 >=6GB）" if not passed else ""),
                           "warning" if not passed else "info",
                           {"vram_gb": vram_gb})
    except Exception:
        pass
    try:
        import torch
        if torch.cuda.is_available():
            total = torch.cuda.get_device_properties(0).total_memory
            vram_gb = round(total / (1024**3), 1)
            passed = vram_gb >= 6
            return CheckResult(8, "GPU 显存", passed,
                               f"{vram_gb} GB (via torch)",
                               "warning" if not passed else "info",
                               {"vram_gb": vram_gb})
    except Exception:
        pass
    return CheckResult(8, "GPU 显存", False,
                       "无法检测显存", "warning", {"vram_gb": 0})


def _check_gpu_driver() -> CheckResult:
    """09. GPU 驱动版本。"""
    try:
        import pynvml
        pynvml.nvmlInit()
        ver = pynvml.nvmlSystemGetDriverVersion()
        if isinstance(ver, bytes):
            ver = ver.decode("utf-8", errors="replace")
        pynvml.nvmlShutdown()
        return CheckResult(9, "GPU 驱动版本", True,
                           f"NVIDIA 驱动 {ver}", "info",
                           {"driver_version": ver})
    except Exception:
        return CheckResult(9, "GPU 驱动版本", False,
                           "无法检测驱动版本", "warning",
                           {"driver_version": "unknown"})


def _check_cpu_cores() -> CheckResult:
    """10. CPU 物理核心数。"""
    try:
        import psutil
        cores = psutil.cpu_count(logical=False) or 0
        passed = cores >= 4
        return CheckResult(10, "CPU 物理核心数", passed,
                           f"{cores} 核" + ("（建议 >=4 核）" if not passed else ""),
                           "warning" if not passed else "info",
                           {"cores": cores})
    except Exception:
        return CheckResult(10, "CPU 物理核心数", False,
                           "无法检测 CPU 核心数", "warning", {"cores": 0})


def _check_cpu_threads() -> CheckResult:
    """11. CPU 逻辑线程数。"""
    try:
        import psutil
        threads = psutil.cpu_count(logical=True) or 0
        passed = threads >= 8
        return CheckResult(11, "CPU 逻辑线程数", passed,
                           f"{threads} 线程", "info",
                           {"threads": threads})
    except Exception:
        return CheckResult(11, "CPU 逻辑线程数", False,
                           "无法检测线程数", "warning", {"threads": 0})


def _check_ram_total() -> CheckResult:
    """12. 内存总量。"""
    try:
        import psutil
        total_gb = round(psutil.virtual_memory().total / (1024**3), 1)
        passed = total_gb >= _MIN_RAM_GB
        return CheckResult(12, "内存总量", passed,
                           f"{total_gb} GB" + (f"（最低 {_MIN_RAM_GB} GB）" if not passed else ""),
                           "error" if not passed else "info",
                           {"total_gb": total_gb, "min_gb": _MIN_RAM_GB})
    except Exception:
        return CheckResult(12, "内存总量", False,
                           "无法检测内存", "warning", {"total_gb": 0})


def _check_ram_available() -> CheckResult:
    """13. 可用内存。"""
    try:
        import psutil
        avail_gb = round(psutil.virtual_memory().available / (1024**3), 1)
        passed = avail_gb >= 2
        return CheckResult(13, "可用内存", passed,
                           f"{avail_gb} GB 可用",
                           "warning" if not passed else "info",
                           {"available_gb": avail_gb})
    except Exception:
        return CheckResult(13, "可用内存", False,
                           "无法检测可用内存", "warning", {"available_gb": 0})


def _check_disk_free() -> CheckResult:
    """14. 磁盘可用空间（门槛 = config.yaml disk.models_min_gb，P2 统一口径）。"""
    try:
        import psutil
        usage = psutil.disk_usage(str(DATA_DIR))
        free_gb = round(usage.free / (1024**3), 1)
        passed = free_gb >= DISK_MODELS_MIN_GB
        return CheckResult(14, "磁盘可用空间", passed,
                           f"{free_gb} GB 可用" + (f"（最低 {DISK_MODELS_MIN_GB:.0f} GB）" if not passed else ""),
                           "error" if not passed else "info",
                           {"free_gb": free_gb, "min_gb": DISK_MODELS_MIN_GB})
    except Exception:
        return CheckResult(14, "磁盘可用空间", False,
                           "无法检测磁盘空间", "warning", {"free_gb": 0})


def _check_disk_path() -> CheckResult:
    """15. 数据目录可写性。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        test_file = DATA_DIR / ".write_test"
        test_file.write_text("ok")
        test_file.unlink()
        return CheckResult(15, "数据目录可写", True,
                           str(DATA_DIR), "info", {"path": str(DATA_DIR)})
    except Exception as exc:
        return CheckResult(15, "数据目录可写", False,
                           f"不可写: {exc}", "error", {"path": str(DATA_DIR)})


def _check_core_deps() -> CheckResult:
    """16. 核心依赖包完整性。"""
    missing = []
    for pkg, desc in _CORE_DEPS:
        try:
            importlib.import_module(pkg)
        except ImportError:
            missing.append(f"{pkg} ({desc})")
    passed = len(missing) == 0
    return CheckResult(16, "核心依赖包", passed,
                       "全部就绪" if passed else f"缺失: {', '.join(missing)}",
                       "error" if not passed else "info",
                       {"missing": missing})


def _check_torch_dep() -> CheckResult:
    """17. PyTorch 依赖。"""
    try:
        import torch
        ver = torch.__version__
        return CheckResult(17, "PyTorch", True,
                           f"PyTorch {ver}", "info", {"version": ver})
    except ImportError:
        return CheckResult(17, "PyTorch", False,
                           "未安装（AI 推理功能不可用）", "error",
                           {"version": None})


def _check_fastapi_dep() -> CheckResult:
    """18. FastAPI 依赖。"""
    try:
        import fastapi
        ver = fastapi.__version__
        return CheckResult(18, "FastAPI", True,
                           f"FastAPI {ver}", "info", {"version": ver})
    except ImportError:
        return CheckResult(18, "FastAPI", False,
                           "未安装", "error", {"version": None})


def _check_pydantic_dep() -> CheckResult:
    """19. Pydantic 依赖。"""
    try:
        import pydantic
        ver = pydantic.__version__
        return CheckResult(19, "Pydantic", True,
                           f"Pydantic {ver}", "info", {"version": ver})
    except ImportError:
        return CheckResult(19, "Pydantic", False,
                           "未安装", "error", {"version": None})


def _check_sqlite() -> CheckResult:
    """20. SQLite3 模块。"""
    try:
        import sqlite3
        ver = sqlite3.sqlite_version
        return CheckResult(20, "SQLite3", True,
                           f"SQLite {ver}", "info", {"version": ver})
    except ImportError:
        return CheckResult(20, "SQLite3", False,
                           "sqlite3 模块不可用", "error", {"version": None})


def _check_chromadb() -> CheckResult:
    """21. ChromaDB 依赖。"""
    try:
        import chromadb
        ver = getattr(chromadb, "__version__", "unknown")
        return CheckResult(21, "ChromaDB", True,
                           f"ChromaDB {ver}", "info", {"version": ver})
    except ImportError:
        return CheckResult(21, "ChromaDB", False,
                           "未安装（降级为内存向量检索）", "warning",
                           {"version": None})


def _check_ffmpeg() -> CheckResult:
    """22. FFmpeg 可用性。"""
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        return CheckResult(22, "FFmpeg", True,
                           f"FFmpeg 已安装: {ffmpeg_path}", "info",
                           {"path": ffmpeg_path})
    # 尝试常见安装路径（Windows）
    for candidate in (r"C:\ffmpeg\bin\ffmpeg.exe",
                      r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"):
        if os.path.isfile(candidate):
            return CheckResult(22, "FFmpeg", True,
                               f"FFmpeg 已安装: {candidate}", "info",
                               {"path": candidate})
    return CheckResult(22, "FFmpeg", False,
                       "FFmpeg 未安装（视频编码功能不可用）", "warning",
                       {"path": None})


def _check_lz4() -> CheckResult:
    """23. LZ4 压缩库。"""
    import importlib.util
    if importlib.util.find_spec("lz4.frame") is not None:
        return CheckResult(23, "LZ4 压缩库", True,
                           "lz4 已安装（缓存压缩可用）", "info",
                           {"available": True})
    return CheckResult(23, "LZ4 压缩库", False,
                       "lz4 未安装（缓存不压缩）", "warning",
                       {"available": False})


def _check_models_dir() -> CheckResult:
    """24. 模型目录存在性。"""
    exists = MODELS_DIR.exists()
    return CheckResult(24, "模型目录", exists,
                       str(MODELS_DIR) if exists else "目录不存在（将自动创建）",
                       "warning" if not exists else "info",
                       {"path": str(MODELS_DIR), "exists": exists})


def _check_models_manifest() -> CheckResult:
    """25. 模型清单文件（ADR-003 P1：升级为 manifest↔磁盘一致性校验）。

    校验三类失真：required 模型磁盘缺失、幽灵条目（登记但磁盘无权重）、
    未登记模型目录（孤儿）。任何一类失真均为 warning（不阻断启动，
    与本检查项既有语义一致），明细进 detail 供前端面板展示。
    """
    manifest = MODELS_DIR / "models_manifest.json"
    if not manifest.exists():
        return CheckResult(25, "模型清单", False,
                           "模型清单不存在（首次运行）", "warning",
                           {"path": str(manifest), "exists": False})
    try:
        import json
        data = json.loads(manifest.read_text(encoding="utf-8"))
        count = len(data.get("models", {})) if isinstance(data, dict) else 0
    except Exception as exc:
        return CheckResult(25, "模型清单", False,
                           f"清单解析失败: {exc}", "warning",
                           {"path": str(manifest)})
    if count == 0:
        return CheckResult(25, "模型清单", False,
                           "清单无模型条目（结构非法）", "warning",
                           {"path": str(manifest), "count": 0})
    try:
        from .data.model_registry import validate_against_disk
        report = validate_against_disk()
    except Exception as exc:  # noqa: BLE001 - 校验器异常不阻断启动
        log.warning("模型清单一致性校验异常: %s", exc)
        return CheckResult(25, "模型清单", True,
                           f"已加载 {count} 条模型记录（一致性校验异常: {exc}）",
                           "warning",
                           {"path": str(manifest), "count": count})
    detail = {"path": str(manifest), "count": count, **report}
    problems: list[str] = []
    if report["required_missing"]:
        problems.append("必需模型磁盘缺失: "
                        + ", ".join(report["required_missing"]))
    if report["ghost_entries"]:
        problems.append("幽灵条目(登记但磁盘无权重): "
                        + ", ".join(report["ghost_entries"]))
    if report["orphan_dirs"]:
        problems.append("未登记模型目录: "
                        + ", ".join(report["orphan_dirs"]))
    if problems:
        return CheckResult(25, "模型清单", False,
                           f"已加载 {count} 条模型记录；" + "；".join(problems),
                           "warning", detail)
    return CheckResult(25, "模型清单", True,
                       f"已加载 {count} 条模型记录，磁盘一致",
                       "info", detail)


def _check_logs_dir() -> CheckResult:
    """26. 日志目录可写性。"""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        test_file = LOGS_DIR / ".write_test"
        test_file.write_text("ok")
        test_file.unlink()
        return CheckResult(26, "日志目录可写", True,
                           str(LOGS_DIR), "info", {"path": str(LOGS_DIR)})
    except Exception as exc:
        return CheckResult(26, "日志目录可写", False,
                           f"不可写: {exc}", "error", {"path": str(LOGS_DIR)})


# ═══════════════════════════════════════════════════════════════════
#  检查注册表（26 项）
# ═══════════════════════════════════════════════════════════════════

_CHECKS = [
    _check_python_version,       # 01
    _check_os_compat,            # 02
    _check_gpu_detection,        # 03
    _check_gpu_vendor,           # 04
    _check_cuda_available,       # 05
    _check_cuda_version,         # 06
    _check_cudnn_version,        # 07
    _check_gpu_vram,             # 08
    _check_gpu_driver,           # 09
    _check_cpu_cores,            # 10
    _check_cpu_threads,          # 11
    _check_ram_total,            # 12
    _check_ram_available,        # 13
    _check_disk_free,            # 14
    _check_disk_path,            # 15
    _check_core_deps,            # 16
    _check_torch_dep,            # 17
    _check_fastapi_dep,          # 18
    _check_pydantic_dep,         # 19
    _check_sqlite,               # 20
    _check_chromadb,             # 21
    _check_ffmpeg,               # 22
    _check_lz4,                  # 23
    _check_models_dir,           # 24
    _check_models_manifest,      # 25
    _check_logs_dir,             # 26
]


def _run_check(idx: int, check_fn: Callable[[], CheckResult]) -> dict:
    """执行单项目检并返回结果 dict（含异常兜底与日志）。"""
    try:
        result = check_fn()
        res_dict = result.to_dict()
        log.log(
            logging.ERROR if not res_dict["passed"] and res_dict["level"] == "error"
            else logging.WARNING if not res_dict["passed"]
            else logging.INFO,
            "自检 [%02d] %s: %s", res_dict["idx"], res_dict["name"],
            res_dict["detail"],
        )
        return res_dict
    except Exception as exc:
        log.error("自检项 %s 执行异常: %s", check_fn.__name__, exc)
        return {
            "idx": idx + 1,
            "name": check_fn.__doc__.strip().split(".")[0] if check_fn.__doc__ else check_fn.__name__,
            "passed": False,
            "detail": f"检查执行异常: {exc}",
            "level": "error",
            "data": {},
        }


# P2 启动并行化：GPU/pynvml 与 torch、chromadb 检查共用 CUDA/pynvml 资源
# （nvmlInit/Shutdown 与 torch 首 import 均非线程安全，并发会触发竞态/DLL
# 加载失败），归入串行组；其余相互独立的自检用线程池并行，压缩就绪窗口。
# 0-based 序号 → 检查：2-8=GPU/CUDA(3-9)、16=PyTorch(17)、20=ChromaDB(21)。
_SERIAL_CHECK_INDEXES = {2, 3, 4, 5, 6, 7, 8, 16, 20}


def run_startup_check() -> list[dict]:
    """执行全部 26 项启动自检，返回结果列表（P2 并行化）。

    Returns:
        [{"idx":1, "name":..., "passed":bool, "detail":..., "level":..., "data":{}}, ...]
    """
    serial = [(i, fn) for i, fn in enumerate(_CHECKS)
              if i in _SERIAL_CHECK_INDEXES]
    parallel = [(i, fn) for i, fn in enumerate(_CHECKS)
                if i not in _SERIAL_CHECK_INDEXES]

    results_by_idx: dict[int, dict] = {}

    # 并行组：轻量自检（psutil / 文件系统 / 轻依赖 import）彼此独立
    workers = max(1, min(8, (os.cpu_count() or 4)))
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="startup") as executor:
        futures = {executor.submit(_run_check, i, fn): i for i, fn in parallel}
        for fut in concurrent.futures.as_completed(futures):
            idx = futures[fut]
            try:
                results_by_idx[idx] = fut.result()
            except Exception as exc:  # noqa: BLE001 - 并行包装兜底
                log.error("自检并行项执行异常: %s", exc)
                results_by_idx[idx] = {
                    "idx": idx + 1, "name": "未知自检", "passed": False,
                    "detail": f"并行执行异常: {exc}", "level": "error", "data": {},
                }

    # 串行组：pynvml / torch import 非线程安全，避免并发触发竞态
    for idx, fn in serial:
        results_by_idx[idx] = _run_check(idx, fn)

    return [results_by_idx[i] for i in range(len(_CHECKS))]


def run_startup_check_summary() -> dict:
    """执行自检并返回汇总信息。

    Returns:
        {
            "total": 26,
            "passed": N,
            "failed": N,
            "warnings": N,
            "errors": N,
            "can_start": bool,
            "results": [...],
            "version": "2.1.0",
            "timestamp": float,
        }
    """
    results = run_startup_check()
    passed = sum(1 for r in results if r["passed"])
    failed = len(results) - passed
    errors = sum(1 for r in results if not r["passed"] and r["level"] == "error")
    warnings = sum(1 for r in results if not r["passed"] and r["level"] == "warning")
    # 存在 error 级别失败时不允许启动
    can_start = errors == 0

    return {
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "warnings": warnings,
        "errors": errors,
        "can_start": can_start,
        "results": results,
        "version": APP_VERSION,
        "timestamp": time.time(),
    }


# ═══════════════════════════════════════════════════════════════════
#  端口可用性检查（附加，不计入 26 项）
# ═══════════════════════════════════════════════════════════════════

def check_port_available(host: str = HOST, port: int = PORT) -> bool:
    """检查指定端口是否可用（未被占用）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            result = s.connect_ex((host, port))
            return result != 0  # 连接失败 = 端口空闲
    except Exception:
        return True


# ═══════════════════════════════════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════════════════════════════════

def _main() -> None:
    """命令行执行自检并输出报告（审计 R1-09：print 改为 logging）。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    summary = run_startup_check_summary()
    log.info("%s", "=" * 60)
    log.info("OmniSpace AI v%s 启动自检报告", APP_VERSION)
    log.info("%s", "=" * 60)
    for r in summary["results"]:
        status = "PASS" if r["passed"] else "FAIL"
        log.info("  [%02d] %-4s %s: %s",
                 r["idx"], status, r["name"], r["detail"])
    log.info("%s", "=" * 60)
    log.info("总计: %d 项 | 通过: %d | 失败: %d (错误: %d, 警告: %d)",
             summary["total"], summary["passed"], summary["failed"],
             summary["errors"], summary["warnings"])
    log.info("可以启动: %s", "是" if summary["can_start"] else "否")
    port_ok = check_port_available()
    log.info("端口 %s:%d 可用: %s", HOST, PORT,
             "是" if port_ok else "否（被占用）")
    log.info("%s", "=" * 60)


if __name__ == "__main__":
    _main()
