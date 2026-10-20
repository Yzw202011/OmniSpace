"""舱壁喂点覆盖哨兵（自愈机制重建批1-②，SH-GAP-02 收口，2026-10-20）。

防两件事：
  1. **喂点被拆**——错误信封 ``error()`` 是 module_health.record_failure
     的唯一喂点（error_handler.py），main.py 的异常处理器全部经信封出；
     任何一处被拆 = 舱壁失明、self_heal 永不触发。
  2. **盲区变多**——src/api 层「静默 except」（既不 raise 也不留痕的
     异常吞噬）让错误既进不了信封也进不了舱壁；存量计数基线锁定
     （tools/architecture_baseline.json::api_silent_except），只减不增。

纯源码扫描（AST），不跑任何业务逻辑；不碰真实进程。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE_FILE = ROOT / "tools" / "architecture_baseline.json"

#: 算「留痕」的日志调用属性名（log.warning / logger.error / logging.info…）
_LOG_ATTRS = {"debug", "info", "warning", "warn", "error", "exception",
              "critical", "log"}
#: 算「留痕」的日志对象名 / 舱壁喂点名
_LOG_NAMES = {"log", "logger", "logging", "record_failure"}


def _handler_is_silent(handler: ast.ExceptHandler) -> bool:
    """except 块内既无 raise 也无任何留痕调用 = 静默吞噬。"""
    for node in ast.walk(handler):
        if isinstance(node, ast.Raise):
            return False
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr in _LOG_ATTRS:
                return False
            if isinstance(fn, ast.Name) and fn.id in _LOG_NAMES:
                return False
    return True


def _scan_silent_excepts() -> list[str]:
    """扫 src/api/**/*.py，返回静默 except 清单（file:line）。"""
    found: list[str] = []
    api_dir = ROOT / "src" / "api"
    for path in sorted(api_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and _handler_is_silent(node):
                found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return found


def _load_silent_baseline() -> int | None:
    if not BASELINE_FILE.exists():
        return None
    data = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    entry = data.get("api_silent_except")
    if entry is None or entry.get("current") is None:
        return None
    return int(entry["current"])


# ── 喂点接线存在性 ───────────────────────────────────────────────


def test_error_envelope_feeds_module_health() -> None:
    """信封必须喂舱壁 + 异常处理器必须注册 + 舱壁参数契约在位。"""
    eh = (ROOT / "src" / "middleware" / "error_handler.py").read_text(
        encoding="utf-8")
    assert "record_failure(sem, msg)" in eh, \
        "error() 信封的 record_failure 喂点被拆——舱壁已失明"

    main = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
    assert "exception_handler(ApiError" in main, \
        "main.py 未注册 ApiError 异常处理器——错误不再走统一信封"

    from src.services.module_health import DEGRADE_THRESHOLD, record_failure

    assert callable(record_failure)
    assert DEGRADE_THRESHOLD == 3


# ── 盲区基线（只减不增）──────────────────────────────────────────


def test_api_silent_except_not_worsening() -> None:
    baseline = _load_silent_baseline()
    assert baseline is not None, \
        "architecture_baseline.json 缺 api_silent_except 基线——哨兵失效"
    found = _scan_silent_excepts()
    assert len(found) <= baseline, (
        f"src/api 新增静默 except（现 {len(found)} > 基线 {baseline}）——"
        f"这些错误进不了信封也进不了舱壁：{found[:10]}"
    )
