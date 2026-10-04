"""资产编号服务单测（2026-10-05 用户令方案二：重图换号/删除释放复用/
借用引入不新增副本——编号流转规则锁定）。"""
from __future__ import annotations

import pytest

from src.api.manga import asset_codes as ac


@pytest.fixture()
def clean_kv(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(ac, "kv_get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(ac, "kv_set", lambda k, v: store.__setitem__(k, v))
    return store


def test_allocate_increments_per_kind(clean_kv) -> None:
    assert ac.allocate("character") == "XM-0001"
    assert ac.allocate("character") == "XM-0002"
    assert ac.allocate("scene") == "CJ-0001"
    assert ac.allocate("prop") == "DJ-0001"


def test_release_and_reuse_lowest(clean_kv) -> None:
    ac.allocate("character")  # XM-0001
    code2 = ac.allocate("character")  # XM-0002
    ac.release(code2)
    # 最小号优先复用
    assert ac.allocate("character") == "XM-0002"


def test_release_ignores_garbage(clean_kv) -> None:
    ac.release("")
    ac.release("XM")
    ac.release("XX-9999")
    ac.allocate("character")
    assert clean_kv[ac._SEQ_KEY]["XM"] == 1


def test_rebuild_if_missing_reconstructs_pool(clean_kv, monkeypatch) -> None:
    ac.allocate("character")
    code2 = ac.allocate("character")
    ac.allocate("scene")
    ac.release(code2)
    clean_kv.pop(ac._FREE_KEY)  # 模拟 kv 丢失
    seen = []

    class _DB:
        def query(self, sql, params=()):
            seen.append(sql)
            return [{"kind": "character", "code": "XM-0001"},
                    {"kind": "scene", "code": "CJ-0001"}]

    ac.rebuild_if_missing(_DB())
    # XM-0002 已释放 → 池里应有 2；CJ 无释放 → 空
    assert clean_kv[ac._FREE_KEY]["XM"] == [2]
    assert clean_kv[ac._FREE_KEY]["CJ"] == []
    # 已在用的 XM-0001 不入池
    assert ac.allocate("character") == "XM-0002"
