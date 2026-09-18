"""训练公共基座测试（批3，2026-09-18）。

VersionStore 全生命周期（next/set/current/list/prune/write_meta）
+ TrainingLockGuard 成对语义（桩替 feature_lock）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.services.training_common import TrainingLockGuard, VersionStore


@pytest.fixture()
def store(tmp_path: Path) -> VersionStore:
    return VersionStore(tmp_path / "versions", keep=3)


def test_version_lifecycle(store: VersionStore):
    d1, v1 = store.next_version_dir()
    assert v1 == "v1" and d1.is_dir()
    store.write_meta(d1, {"name": "第一版"})
    store.set_current("v1")

    d2, v2 = store.next_version_dir()
    store.write_meta(d2, {"name": "第二版"})
    store.set_current("v2")

    vers = store.versions()
    assert [v["version"] for v in vers] == ["v2", "v1"]
    assert vers[0]["is_current"] is True and vers[0]["name"] == "第二版"
    assert vers[1]["is_current"] is False
    assert store.current() == "v2"
    assert store.version_dir("v1") == d1
    assert store.version_dir("v99") is None


def test_prune_keeps_current(store: VersionStore):
    for i in range(5):
        d, v = store.next_version_dir()
        store.write_meta(d, {"n": i})
        store.set_current(v)
    # current=v5，keep=3 → 保留 v5/v4/v3，删 v1/v2
    store.prune()
    assert [v["version"] for v in store.versions()] == ["v5", "v4", "v3"]


def test_prune_never_deletes_current(store: VersionStore):
    """current 在 keep 之外也不删（数据安全优先）。"""
    for i in range(5):
        d, v = store.next_version_dir()
        store.write_meta(d, {"n": i})
    store.set_current("v1")  # 故意指最旧
    store.prune()
    vers = [v["version"] for v in store.versions()]
    assert "v1" in vers  # current 保住


def test_empty_root(store: VersionStore):
    assert store.versions() == []
    assert store.current() == ""


def test_lock_guard_acquire_release(monkeypatch):
    """锁守卫成对语义（桩替 feature_lock）。"""
    calls: list[str] = []
    guard = TrainingLockGuard()
    guard.set_loop(None)  # 纯线程=同步降级路径

    from src.middleware.feature_lock import get_feature_lock
    fl = get_feature_lock()
    monkeypatch.setattr(fl, "acquire_sync",
                        lambda *a, **k: calls.append("acquire") or True)
    monkeypatch.setattr(fl, "release_sync",
                        lambda *a, **k: calls.append("release"))

    assert guard.acquire("test-task") is True
    guard.release()
    assert calls == ["acquire", "release"]


def test_lock_guard_acquire_failure(monkeypatch):
    calls: list[str] = []
    guard = TrainingLockGuard()
    guard.set_loop(None)
    from src.middleware.feature_lock import get_feature_lock
    fl = get_feature_lock()
    monkeypatch.setattr(fl, "acquire_sync",
                        lambda *a, **k: calls.append("acquire") or False)
    assert guard.acquire("test") is False
    assert calls == ["acquire"]  # 失败不 release
