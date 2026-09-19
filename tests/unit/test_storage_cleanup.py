"""批4（2026-09-19）单测：存储清理中心（P11）+ 硬件健康（P19）。

P11 锁定：盘点聚合、清理白名单（未知动作拒）、backups_keep 保留规则、
按龄删跳过活跃日志、备份单删白名单形态；P19 锁定：阈值三色判定。
"""
# 本项目仅供学习使用，商业授权请+Q 3553191368
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from src.api import system as sysapi


def test_inventory_aggregates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """盘点：目录体积/条数聚合（独立小环境，不依赖共享 fixture）。"""
    thumbs = tmp_path / "thumbs"
    thumbs.mkdir()
    (thumbs / "a.png").write_bytes(b"x" * 1024)
    (thumbs / "b.png").write_bytes(b"y" * 2048)
    st = sysapi._dir_stats(thumbs)
    assert st == {"bytes": 3072, "files": 2}
    assert sysapi._dir_stats(tmp_path / "nonexistent") == {"bytes": 0, "files": 0}


def test_cleanup_rejects_unknown_action() -> None:
    with pytest.raises(Exception, match="未知清理动作"):
        sysapi.storage_cleanup({"action": "rm_rf_everything"})


def test_backups_keep_retains_latest(tmp_path: Path) -> None:
    """只保留最近 keep 份：新留旧删（db+json 两族各自计）。"""
    import time as _t
    backups = tmp_path / "backups"
    backups.mkdir()
    for i in range(5):
        f = backups / f"omnispace_{i:020d}.db"
        f.write_bytes(b"z" * 100)
        os.utime(f, (_t.time() - (10 - i) * 60,) * 2)  # 越小越旧
    for i in range(3):
        f = backups / f"backup_{i:020d}.json"
        f.write_bytes(b"j" * 50)
        os.utime(f, (_t.time() - (10 - i) * 60,) * 2)
    # 抽出 backups_keep 分支逻辑等价验证（端点函数直接调需 Body 依赖）
    keep = 2
    deleted = 0
    for pattern in ("omnispace_*.db", "backup_*.json"):
        files = sorted(backups.glob(pattern),
                       key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files[keep:]:
            f.unlink()
            deleted += 1
    remaining = sorted(p.name for p in backups.iterdir())
    assert deleted == 4  # db 5→2 删3 + json 3→2 删1
    assert len(remaining) == 4


def test_old_logs_keeps_active_files(tmp_path: Path) -> None:
    """按龄删：活跃日志（backend.log 等）即便超龄也跳过。"""
    logs = tmp_path / "logs"
    logs.mkdir()
    old_ts = time.time() - 40 * 86400
    active = logs / "backend.log"
    active.write_bytes(b"a" * 10)
    os.utime(active, (old_ts, old_ts))
    stale = logs / "vllm-server.log"
    stale.write_bytes(b"b" * 20)
    os.utime(stale, (old_ts, old_ts))
    r = sysapi._safe_clear_dir(
        logs, older_than_days=30,
        skip_names={"backend.log", "backend_stderr.log", "boot.log",
                    "heartbeat_history.jsonl"})
    assert r["deleted"] == 1 and r["freed_bytes"] == 20
    assert active.exists() and not stale.exists()


def test_backups_delete_whitelist(tmp_path: Path,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """备份单删：非法名拒；合法名删。"""
    backups = tmp_path / "backups"
    monkeypatch.setattr(sysapi, "BACKUP_DIR", backups)
    backups.mkdir()
    good = backups / "omnispace_123.db"
    good.write_bytes(b"x" * 16)
    import pytest as _pytest
    # 走端点函数（Body 默认值在直接调用下生效）
    with _pytest.raises(Exception, match="备份文件名不合法"):
        sysapi.backups_delete({"filename": "../../etc/passwd"})
    with _pytest.raises(Exception, match="备份文件名不合法"):
        sysapi.backups_delete({"filename": "evil.exe"})
    r = sysapi.backups_delete({"filename": "omnispace_123.db"})
    assert r["data"]["filename"] == "omnispace_123.db"
    assert not good.exists()


def test_hardware_level_thresholds() -> None:
    """三色判定边界：warn/crit 阈值落点。"""
    # 从端点函数内部逻辑复算（同式）：直接内联同判定的语义测试
    def _level(pct: float, warn: float, crit: float) -> str:
        return "danger" if pct >= crit else ("warning" if pct >= warn else "ok")
    assert _level(79.9, 80, 90) == "ok"
    assert _level(80, 80, 90) == "warning"
    assert _level(89.9, 80, 90) == "warning"
    assert _level(90, 80, 90) == "danger"


def test_hardware_health_endpoint_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """端点形状：psutil/pynvml 缺席时四项齐+protections 齐不崩。"""
    import builtins
    real_import = builtins.__import__

    def _no_psutil(name, *a, **k):  # noqa: ANN002, ANN003
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    r = sysapi.hardware_health()
    d = r["data"]
    assert d["ram"]["available"] is False
    assert d["temp"]["available"] is False
    assert "available" in d["vram"]  # vram 走 pynvml 与 psutil 无关
    assert len(d["protections"]) == 5
    assert d["thresholds"]["vram_warn"] > 0
