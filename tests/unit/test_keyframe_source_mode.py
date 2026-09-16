"""关键帧来源标注哨兵（2026-09-03 方案A 用户裁定）。

契约：无描述词出图（原文直出兜底）必须留痕——keyframes.source_mode
在生成落库时如实记录（describe/fallback，旧数据空=未知不标注），列
表接口带出，前端据此打「原文直出」角标。防「描述词失败 + 兜底出图
成功」同行误导复发。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

KF_PY = Path(__file__).resolve().parents[2] / "src" / "api" / "manga" / "keyframe.py"
SRC = KF_PY.read_text(encoding="utf-8")


def test_fallback_capture_and_insert() -> None:
    assert 'used_fallback = not (row.get("description") or "").strip()' in SRC, \
        "兜底事实捕获被删"
    assert '"fallback" if used_fallback else "describe"' in SRC, \
        "落库 source_mode 被删"


def test_serializer_and_migration() -> None:
    assert '"source_mode": r.get("source_mode", "")' in SRC, \
        "列表序列化丢字段"
    assert "_ensure_source_mode_column" in SRC, "幂等迁移被删"
    from src.api.manga.common import _KF_COLS
    assert "source_mode" in _KF_COLS, "_KF_COLS 缺 source_mode"


def test_serializer_roundtrip() -> None:
    from src.api.manga.keyframe import _kf_row_to_dict
    d = _kf_row_to_dict({"id": "k1", "row_id": "r1", "version": 1,
                         "source_mode": "fallback"})
    assert d["source_mode"] == "fallback"
    d2 = _kf_row_to_dict({"id": "k2", "row_id": "r2", "version": 1})
    assert d2["source_mode"] == "", "旧数据缺列时必须返回空串（未知，不臆测）"
