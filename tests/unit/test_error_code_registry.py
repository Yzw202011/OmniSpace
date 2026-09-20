"""错误码注册反向哨兵（散乱度根查 G1，2026-09-20）。

扫描 src/ 全部 ``raise ApiError("CODE" ...``（含跨行形态），断言每个
在用语义码都已登记进 SEMANTIC_CODES——防止再产出「未知错误」黑户
（2026-09-20 实测曾有 47/116 未登记）。

正向完整性（SEMANTIC_CODES ⊆ SUGGESTION_DEFAULTS）由
test_error_suggestion_defaults.py 负责；本测试补齐反向半边。
"""
from __future__ import annotations

import re
from pathlib import Path

from src.middleware.error_handler import SEMANTIC_CODES

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
# 跨行形态：ApiError(\n    "CODE",
RAISE_CODE_RE = re.compile(r'ApiError\(\s*["\']([A-Z][A-Z0-9_]+)["\']',
                           re.S)


def _raised_codes() -> set[str]:
    codes: set[str] = set()
    for py in SRC_ROOT.rglob("*.py"):
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        codes.update(RAISE_CODE_RE.findall(text))
    return codes


def test_all_raised_codes_registered() -> None:
    raised = _raised_codes()
    assert raised, "扫描结果为空＝扫描器失效，不是没有 raise"
    missing = sorted(raised - set(SEMANTIC_CODES))
    assert not missing, (
        f"以下 {len(missing)} 个在用错误码未登记 SEMANTIC_CODES"
        f"（用户将看到「未知错误」+兜底出路）：{missing}"
    )


def test_scanner_sanity() -> None:
    """哨兵自检：确保扫描器本身没有静默失效。"""
    codes = _raised_codes()
    assert "SYSTEM_PARAM_INVALID" in codes, (
        "扫描器未命中已知必在用码，请检查正则与扫描路径")
