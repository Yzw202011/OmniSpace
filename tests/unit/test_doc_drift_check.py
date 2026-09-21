"""文档对账闸单测：tools/doc_drift_check.py 纯函数 + 临时目录集成。

覆盖：contains/not_contains（含失配行号定位）、path_exists/absent、
count_matches（glob 汇总）、未知类型拒绝、block/warn 退出码语义。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

_TOOLS = Path(__file__).resolve().parents[2] / "tools"
_SPEC = importlib.util.spec_from_file_location("doc_drift_check", _TOOLS / "doc_drift_check.py")
assert _SPEC is not None and _SPEC.loader is not None
ddc = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("doc_drift_check", ddc)
_SPEC.loader.exec_module(ddc)


def _claim(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"id": "t", "severity": "block", "why": "test"}
    base.update(kw)
    return base


def test_file_contains_and_not(tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text("vLLM 0.27.1 子进程\n旧稿已删\n", encoding="utf-8")
    ok, _ = ddc.check_claim(tmp_path, _claim(type="file_contains", path="a.md", pattern=r"0\.27\.1"))
    assert ok
    ok, _ = ddc.check_claim(tmp_path, _claim(type="file_not_contains", path="a.md", pattern=r"v0\.26\.0"))
    assert ok
    # not_contains 失配时详情须带首现行号，便于定位漂移点
    ok, detail = ddc.check_claim(tmp_path, _claim(type="file_not_contains", path="a.md", pattern=r"0\.27"))
    assert not ok and ":1" in detail
    # contains 目标缺失 = 失配
    ok, _ = ddc.check_claim(tmp_path, _claim(type="file_contains", path="a.md", pattern=r"9\.9\.9"))
    assert not ok
    # 文件不存在 = 失配而非崩溃
    ok, _ = ddc.check_claim(tmp_path, _claim(type="file_contains", path="gone.md", pattern=r"x"))
    assert not ok


def test_path_exists_absent(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    assert ddc.check_claim(tmp_path, _claim(type="path_exists", path="sub"))[0]
    assert ddc.check_claim(tmp_path, _claim(type="path_absent", path="gone"))[0]
    assert not ddc.check_claim(tmp_path, _claim(type="path_exists", path="gone"))[0]
    assert not ddc.check_claim(tmp_path, _claim(type="path_absent", path="sub"))[0]


def test_count_matches_glob(tmp_path: Path) -> None:
    d = tmp_path / "api"
    d.mkdir()
    (d / "x.py").write_text("@router.get(\n@router.post(\n@router.get(\n", encoding="utf-8")
    (d / "y.py").write_text("@router.delete(\n", encoding="utf-8")
    ok, detail = ddc.check_claim(
        tmp_path,
        _claim(type="count_matches", paths=["api/**/*.py"], pattern=r"@router\.(get|post|delete)\(", expected=4),
    )
    assert ok, detail
    ok, detail = ddc.check_claim(
        tmp_path,
        _claim(type="count_matches", paths=["api/**/*.py"], pattern=r"@router\.(get|post|delete)\(", expected=3),
    )
    assert not ok and "实测 4" in detail


def test_unknown_type_rejected(tmp_path: Path) -> None:
    ok, _ = ddc.check_claim(tmp_path, _claim(type="magic"))
    assert not ok


def test_main_block_vs_warn_semantics(tmp_path: Path, monkeypatch: Any) -> None:
    claims = {
        "claims": [
            {"id": "w", "severity": "warn", "type": "path_exists", "path": "nope_x", "why": ""},
            {"id": "b", "severity": "block", "type": "path_exists", "path": "missing_x", "why": ""},
        ]
    }
    cf = tmp_path / "doc_claims.json"
    cf.write_text(json.dumps(claims), encoding="utf-8")
    monkeypatch.setattr(ddc, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ddc, "CLAIMS_PATH", cf)
    assert ddc.main([]) == 1  # block 失配必须挡下
    claims["claims"] = claims["claims"][:1]  # 仅剩 warn 失配
    cf.write_text(json.dumps(claims), encoding="utf-8")
    assert ddc.main([]) == 0  # warn 只报不挡
    assert ddc.main(["--strict"]) == 1  # strict 下 warn 也挡
    assert ddc.main(["--list"]) == 0  # 列表模式恒过
