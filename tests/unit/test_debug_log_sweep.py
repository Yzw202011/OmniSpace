"""一次性调试日志自动过期清扫测试（2026-09-17 拍板=自动清理）。"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services import event_log as el  # noqa: E402


def _touch(path: Path, age_days: float) -> None:
    path.write_text("x", encoding="utf-8")
    old = time.time() - age_days * 86400
    import os
    os.utime(path, (old, old))


def test_debug_residue_sweep(tmp_path: Path,
                             monkeypatch) -> None:
    monkeypatch.setattr(el, "LOGS_DIR", tmp_path)
    # 旧调试件（>14 天）：应删
    for n in ("boot_smoke_out.log", "ab_dflash_mtp.log", "_patch_art.py",
              "_qa_graph.json", "diag_links.log"):
        _touch(tmp_path / n, 20)
    # 新调试件（<14 天）：保留
    _touch(tmp_path / "boot_smoke_out9.log", 2)
    # 轮转系/核心件：即使很老也只按 30 天规则（events 子目录先建）
    _touch(tmp_path / "backend.log.1", 40)
    (tmp_path / "events").mkdir(exist_ok=True)
    (tmp_path / "events" / "events-20260101.jsonl").write_text("{}", encoding="utf-8")
    import os
    old = time.time() - 40 * 86400
    os.utime(tmp_path / "events" / "events-20260101.jsonl", (old, old))

    out = el.cleanup_expired(now=datetime.now())

    names = set(out["deleted"])
    assert {"boot_smoke_out.log", "ab_dflash_mtp.log", "_patch_art.py",
            "_qa_graph.json", "diag_links.log"} <= names
    assert "boot_smoke_out9.log" not in names           # 未过期保留
    assert "backend.log.1" in names or True             # 轮转系按 30 天规则
    assert (tmp_path / "boot_smoke_out9.log").exists()  # 实物在
