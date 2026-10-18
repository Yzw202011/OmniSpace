# 本项目仅供学习使用，商业授权请+Q 3559331368
"""N-2 家族补丁单测（2026-10-19）：连接列表绝不因 KV 读抖动静默变空。

实测背景：cloud.providers KV 读取遇 DB 忙抖动被 _read_kv 吞成默认
空列表，用户视角=全部云端连接间歇消失（5→0→5）。补丁三件：
宽版 _read_kv 保持降级语义但日志升 warning；严格读 _read_kv_strict
短重试一次仍失败上抛；列表端点显式 SYSTEM_DB_BUSY 人话报错。
"""
from __future__ import annotations

import pytest

import src.services.cloud_provider_service as cs


@pytest.fixture()
def flaky_kv(monkeypatch):
    """kv_get 打桩：可控失败序列。"""
    calls: dict = {"seq": [], "state": {"n": 0}}

    def _set(seq):
        calls["seq"] = list(seq)
        calls["state"]["n"] = 0

    def _kv_get(key):
        n = calls["state"]["n"]
        calls["state"]["n"] += 1
        outcome = calls["seq"][min(n, len(calls["seq"]) - 1)]
        if outcome == "busy":
            raise OSError("database is locked")
        if outcome == "empty":
            return None
        return [{"id": "prov_x", "name": "测试连接"}]
    monkeypatch.setattr("src.data.settings_kv.kv_get", _kv_get)
    return _set


def test_strict_read_recovers_after_one_flake(flaky_kv):
    flaky_kv(["busy", "ok"])
    items = cs._load_providers()
    assert len(items) == 1 and items[0]["id"] == "prov_x"


def test_strict_read_raises_when_persistently_busy(flaky_kv):
    flaky_kv(["busy", "busy"])
    with pytest.raises(OSError):
        cs._load_providers()


def test_lenient_read_still_degrades(flaky_kv):
    """宽版读取保持原语义：热路径按默认降级不抛。"""
    flaky_kv(["busy", "busy"])
    assert cs._read_kv("cloud.providers", []) == []


def test_strict_read_empty_is_legit(flaky_kv):
    """键真不存在（None）≠ 读失败：严格读也应返回默认空。"""
    flaky_kv(["empty"])
    assert cs._load_providers() == []
