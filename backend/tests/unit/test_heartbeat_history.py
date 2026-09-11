"""心跳历史落盘回归（2026-09-02 静默死亡取证补强）。

旧缺陷：logs/.heartbeat 只存末跳，新进程首跳覆盖旧进程末跳——
18:49 事故实证历史证据随覆盖丢失，死亡窗口只能靠日志时间戳推断。
修复：每跳 + start/stop/crash_detected 生命周期事件追加
logs/heartbeat_history.jsonl；超 512KB 轮转保留末 1000 行；
末跳文件行为保持兼容（崩溃判定逻辑不变）。
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[2].parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2].parent / "pydeps"))

from backend.services import heartbeat as hb  # noqa: E402


def _patch_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "HEARTBEAT_FILE", tmp_path / ".heartbeat")
    monkeypatch.setattr(hb, "HISTORY_FILE", tmp_path / "history.jsonl")


def test_beats_and_lifecycle_events_append(monkeypatch, tmp_path) -> None:
    _patch_paths(monkeypatch, tmp_path)
    hb._append_history({"event": "start"})
    hb._write()  # beat
    hb._append_history({"event": "stop"})
    lines = (tmp_path / "history.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    events = [json.loads(x)["event"] for x in lines]
    assert events == ["start", "beat", "stop"]
    assert all("pid" in json.loads(x) and "ts" in json.loads(x) for x in lines)
    # 末跳文件兼容仍在
    assert json.loads((tmp_path / ".heartbeat").read_text())["pid"] > 0


def test_crash_detected_records_dead_pid(monkeypatch, tmp_path) -> None:
    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / ".heartbeat").write_text(
        json.dumps({"pid": 999999999, "ts": 1.0}), encoding="utf-8")
    monkeypatch.setattr(hb, "_pid_alive", lambda pid: False)
    info = hb.check_previous_crash()
    assert info and info["pid"] == 999999999
    hist = [json.loads(x) for x in
            (tmp_path / "history.jsonl").read_text(encoding="utf-8")
            .strip().splitlines()]
    assert any(h["event"] == "crash_detected" and h["pid"] == 999999999
               for h in hist), "崩溃取证必须入历史文件"


def test_rotation_keeps_tail(monkeypatch, tmp_path) -> None:
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(hb, "_HISTORY_MAX_BYTES", 1)  # 强制每写必轮转
    for i in range(120):
        hb._append_history({"event": "beat", "seq": i})
    lines = (tmp_path / "history.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    assert len(lines) <= hb._HISTORY_KEEP_LINES
    seqs = [json.loads(x).get("seq") for x in lines]
    assert seqs == sorted(seqs)[-len(seqs):], "轮转必须保留时间顺序末段"
    assert seqs[-1] == 119, "最新一跳必须在"
# 本项目仅供学习使用，商业授权请+Q 3559331368
