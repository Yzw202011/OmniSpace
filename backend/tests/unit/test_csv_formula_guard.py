"""分镜 CSV 导出公式注入中和回归（2026-09-10 审计 P2-12）。

CSV 单元格以 = + - @ 或制表/回车开头时，Excel/WPS 会当公式执行
（DDE 下载/命令注入经典面）；导出侧前置单引号中和。
"""
from backend.api.manga.storyboard import _csv_safe_cell


def test_formula_prefixes_neutralized() -> None:
    for prefix in ("=", "+", "-", "@", "\t", "\r"):
        cell = _csv_safe_cell(f"{prefix}1+1|cmd")
        assert cell.startswith("'" + prefix), f"{prefix!r} 未中和: {cell!r}"


def test_normal_text_untouched() -> None:
    assert _csv_safe_cell("清晨的教室，樱花瓣沿窗飘落") == "清晨的教室，樱花瓣沿窗飘落"
    assert _csv_safe_cell("旁白：雨夜") == "旁白：雨夜"
    assert _csv_safe_cell("") == ""


def test_non_string_coerced() -> None:
    assert _csv_safe_cell(12) == "12"          # type: ignore[arg-type]
# 本项目仅供学习使用，商业授权请+Q 3559331368
