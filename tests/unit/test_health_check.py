"""自愈批4 一键体检与修复单测（2026-09-11，docs/自愈与横切内建方案 §批4）。

不碰真实进程/真实日志：
  - 体检报告形状（8 项、每项 key/level/friendly、汇总计数）
  - 日志保留：过期文件→warn+可修；修复后复验转绿（验收主链）
  - 目录重建：只 mkdir 固定清单，缺则建、齐则不动
  - 孤儿清理守卫：口径匹配才 terminate，python 系进程绝不碰
  - 白名单外修复动作一律拒绝
"""
from __future__ import annotations

import os
import time
from typing import Any

import pytest

from src.middleware.error_handler import ApiError
from src.services import event_log
from src.services import health_check as hc

# ── 体检报告形状 ─────────────────────────────────────────────────

def test_run_health_check_shape() -> None:
    report = hc.run_health_check()
    assert set(report) >= {"items", "summary", "counts", "checked_at"}
    assert len(report["items"]) == len(hc.CHECKS)
    for item in report["items"]:
        assert {"key", "level", "friendly"} <= set(item)
        assert item["level"] in {"ok", "warn", "fail", "unknown"}
        assert str(item["friendly"]).strip()
    assert report["counts"]["total"] == len(hc.CHECKS)
    assert report["summary"].strip()


# ── 日志保留：造可修项 → 修复 → 复验转绿（方案验收主链）─────────

@pytest.fixture()
def fake_log_dirs(tmp_path, monkeypatch: pytest.MonkeyPatch):
    ev = tmp_path / "events"
    ev.mkdir()
    lg = tmp_path / "logs"
    lg.mkdir()
    monkeypatch.setattr(event_log, "EVENTS_DIR", ev)
    monkeypatch.setattr(event_log, "LOGS_DIR", lg)
    return ev, lg


def _make_old_file(path, age_days: int = 40) -> None:
    path.write_text("{}\n", encoding="utf-8")
    old = time.time() - age_days * 86400
    os.utime(path, (old, old))


def test_logs_retention_green_when_no_expired(fake_log_dirs) -> None:
    ev, _ = fake_log_dirs
    (ev / "events-20991231.jsonl").write_text("{}\n", encoding="utf-8")
    item = hc._check_logs_retention()
    assert item["level"] == "ok"
    assert item["fixable"] is False


def test_logs_retention_warns_and_repair_turns_green(fake_log_dirs) -> None:
    ev, lg = fake_log_dirs
    _make_old_file(ev / "events-20200101.jsonl")
    _make_old_file(lg / "backend.log.1")
    item = hc._check_logs_retention()
    assert item["level"] == "warn"
    assert item["fixable"] is True
    assert item["fix_action"] == "clean_logs"
    # 一键修
    result = hc.repair("clean_logs")
    assert result["action"] == "clean_logs"
    assert "清理" in result["friendly"]
    # 复验转绿
    assert hc._check_logs_retention()["level"] == "ok"


# ── 目录重建：固定清单，缺则建、齐则不动 ─────────────────────────

def test_rebuild_dirs_creates_only_missing(tmp_path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    existing = tmp_path / "exists"
    existing.mkdir()
    missing = tmp_path / "missing" / "deep"
    monkeypatch.setattr(hc, "_SYSTEM_DIRS", (existing, missing))
    result = hc.repair("rebuild_dirs")
    assert missing.is_dir()
    assert result["created"] == [str(missing)]
    # 复跑：已齐则零动作
    result2 = hc.repair("rebuild_dirs")
    assert result2["created"] == []


# ── 孤儿清理守卫：匹配口径 + 绝不碰 python 系 ────────────────────

class _FakeProc:
    def __init__(self, pid: int, name: str, exe: str = "",
                 cmdline: list[str] | None = None) -> None:
        self.info: dict[str, Any] = {"pid": pid, "name": name,
                                     "exe": exe, "cmdline": cmdline or []}
        self.terminated = False
        self._parent: _FakeProc | None = None

    def terminate(self) -> None:
        self.terminated = True

    def parent(self) -> _FakeProc | None:
        return self._parent

    def name(self) -> str:
        # 对齐真 psutil.Process：parent() 返回的对象没有 .info，只有 name()
        return str(self.info["name"])


def _chain_fake_iter(*procs: _FakeProc):
    def fake_iter(_attrs: dict) -> list:
        return list(procs)
    return fake_iter


def test_kill_orphans_guard_never_touches_python(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import psutil
    victim = _FakeProc(111, "llama-server.exe",
                       exe="D:/x/llama-poc/llama-server.exe")  # 父死=真孤儿
    python_like = _FakeProc(222, "python.exe",
                            cmdline=["python", "-m", "llama-poc-thing"])
    myself = _FakeProc(psutil.Process().pid, "llama-server.exe",
                       exe="llama-poc")
    monkeypatch.setattr(psutil, "process_iter",
                        _chain_fake_iter(victim, python_like, myself))
    result = hc.repair("kill_orphans")
    # 真孤儿被清理；python 系与自身绝不碰
    assert victim.terminated is True
    assert python_like.terminated is False
    assert myself.terminated is False
    assert result["killed"] == [111]


def test_kill_orphans_spares_procs_owned_by_live_backend(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-09-11 实弹教训：多实例共存时，活后端合法持有的推理子进程
    不是孤儿——父链上有活着的 python 属主，绝不清理。"""
    import psutil
    live_backend = _FakeProc(900, "python.exe")  # 活着的属主（不进匹配口径）
    engine_owned = _FakeProc(910, "OmniSpace-LLM.exe", exe="llama-poc")
    engine_owned._parent = live_backend
    true_orphan = _FakeProc(920, "OmniSpace-LLM.exe", exe="llama-poc")  # 父死
    monkeypatch.setattr(psutil, "process_iter",
                        _chain_fake_iter(live_backend, engine_owned,
                                         true_orphan))
    result = hc.repair("kill_orphans")
    assert result["killed"] == [920]
    assert engine_owned.terminated is False


def test_kill_orphans_engine_chain_root_dead_counts_orphan(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """引擎父子链的根才是属主判定点：llama 子进程挂在已死的 llama 父下，
    穿过同类引擎进程继续向上，最终判真孤儿（今天的实弹现场形状）。"""
    import psutil
    dead_root = _FakeProc(11836, "OmniSpace-LLM.exe", exe="llama-poc")
    child = _FakeProc(2996, "OmniSpace-LLM.exe", exe="llama-poc")
    child._parent = dead_root  # 父是同名引擎进程但其自身父已死
    monkeypatch.setattr(psutil, "process_iter", _chain_fake_iter(child, dead_root))
    result = hc.repair("kill_orphans")
    assert sorted(result["killed"]) == [2996, 11836]


def test_kill_orphans_green_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import psutil

    def fake_iter(_attrs: dict) -> list:
        return []

    monkeypatch.setattr(psutil, "process_iter", fake_iter)
    result = hc.repair("kill_orphans")
    assert result["killed"] == []
    assert "没有" in result["friendly"]


# ── 白名单外拒绝 ─────────────────────────────────────────────────

def test_repair_rejects_unknown_action() -> None:
    with pytest.raises(ApiError) as ei:
        hc.repair("delete_database")
    assert ei.value.code == "SYSTEM_PARAM_INVALID"


def test_check_orphan_engines_green_and_warn(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import psutil
    item = hc._check_orphan_engines()
    assert item["key"] == "orphan_engines"  # 真环境跑通（结果不假设）

    def fake_iter(_attrs: dict) -> list:
        return [_FakeProc(333, "OmniSpace-LLM.exe")]

    monkeypatch.setattr(psutil, "process_iter", fake_iter)
    item2 = hc._check_orphan_engines()
    assert item2["level"] == "warn"
    assert item2["fixable"] is True
    assert item2["fix_action"] == "kill_orphans"
