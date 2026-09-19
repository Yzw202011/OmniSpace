"""批5（2026-09-19）单测：审计日志（P18）+ 启动清扫/自动备份（P20）+
备份加密（P16）+ 睡眠保护计数（P21）。
"""
# 本项目仅供学习使用，商业授权请+Q 3553191368
from __future__ import annotations

from pathlib import Path

import pytest

from src.data import crypto as crypto_mod
from src.data.database import Database
from src.middleware import feature_lock as fl
from src.services import recovery as rec


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("src.services.audit_log.get_db_safe",
                        lambda: Database(tmp_path / "a.db"))
    Database(tmp_path / "a.db").sql(
        "CREATE TABLE IF NOT EXISTS audit_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,"
        "module TEXT NOT NULL, action TEXT NOT NULL, target TEXT DEFAULT '',"
        "result TEXT DEFAULT 'ok', detail TEXT DEFAULT '')")
    yield


def test_audit_write_list_clear(db) -> None:
    from src.services.audit_log import clear_audit, list_audit, log_audit
    log_audit("novel", "project_delete", target="p1")
    log_audit("models", "model_delete_files", target="m1",
              detail="freed 1.2GB")
    items = list_audit()
    assert len(items) == 2
    assert items[0]["action"] == "model_delete_files"  # 倒序
    assert list_audit(module="novel")[0]["target"] == "p1"
    assert clear_audit() == 2
    assert list_audit() == []


def test_audit_never_raises(db, monkeypatch) -> None:
    """埋点失败绝不抛（审计是观测不是闸门）。"""
    from src.services import audit_log
    monkeypatch.setattr(audit_log, "get_db_safe", lambda: None)
    audit_log.log_audit("x", "y")  # 不抛=过


def test_sweep_stale_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P20：挂死 generating/pending 统一标中断（只改状态不删数据）。"""
    d = Database(tmp_path / "s.db")  # 初始化即建全套真实 schema
    d.insert("keyframes", {"id": "k1", "row_id": "r", "status": "generating"})
    d.insert("keyframes", {"id": "k2", "row_id": "r", "status": "done"})
    d.insert("video_tasks", {"id": "v1", "storyboard_row_id": "r", "status": "pending"})
    d.insert("novel_chapters", {"id": "c1", "project_id": "p", "status": "generating"})
    monkeypatch.setattr(rec, "get_db_safe", lambda: d)
    swept = rec.sweep_stale_tasks()
    assert swept == {"keyframes": 1, "video_tasks": 1, "novel_chapters": 1}
    assert d.query_one("SELECT status,error FROM keyframes WHERE id='k1'")[
        "error"].startswith("已中断")
    assert d.query_one("SELECT status FROM keyframes WHERE id='k2'")[
        "status"] == "done"  # 已完成的不动


def test_backup_encrypt_roundtrip() -> None:
    """P16：bytes 级加解密往返 + 明文遗产直通 + 头嗅探。"""
    raw = b"sqlite-backup-blob" * 100
    blob = crypto_mod.encrypt_bytes_gcm(raw)
    if crypto_mod.is_encrypted_bytes(blob):
        assert crypto_mod.decrypt_bytes_gcm(blob) == raw
    else:
        # 密钥不可用环境（CI）：明文回退且往返一致
        assert blob == raw
    assert crypto_mod.decrypt_bytes_gcm(b"legacy-plain") == b"legacy-plain"
    assert not crypto_mod.is_encrypted_bytes(b"legacy-plain")


def test_sleep_guard_refcount(monkeypatch: pytest.MonkeyPatch) -> None:
    """P21：引用计数从 0→1 请求清醒、归零恢复；调用桩记录次数。"""
    calls: list[int] = []
    import ctypes as _ct

    class _Stub:
        def __init__(self) -> None:
            self.kernel32 = self
        def SetThreadExecutionState(self, flags: int) -> int:  # noqa: N802
            calls.append(flags)
            return 1

    monkeypatch.setattr(_ct, "windll", _Stub(), raising=False)
    fl._sleep_guard_refs = 0
    fl._sleep_guard_hold()
    fl._sleep_guard_hold()  # 重入不重复请求
    fl._sleep_guard_release()
    fl._sleep_guard_release()  # 归零恢复
    assert calls == [0x80000001, 0x80000000]
    assert fl._sleep_guard_refs == 0
