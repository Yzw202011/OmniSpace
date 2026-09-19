"""激活二期（2026-09-19）单测：吊销名单执行 + make_dist 台账提取。

- 客户端锁芯：换绑后代数提升→旧码死；同代数活；作废 9999 全死；
  data 覆盖文件优先于随包文件；activate 同样被拦。
- make_dist：台账 sqlite → 吊销名单映射（active=generation、
  revoked=9999、老库无列=空名单）。
真身依赖（license_gate_impl/crypto_core）缺席时整组跳过（公开仓克隆）。
"""
# 本项目仅供学习使用，商业授权请+Q 3553191368
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

try:
    from license_console.crypto_core import fp_digest  # noqa: F401
    from src import license_gate_impl as gate
    from tools.make_dist import _extract_revocations
    _REAL = True
except ImportError:  # noqa: BLE001 - 公开仓无真身
    _REAL = False

pytestmark = pytest.mark.skipif(
    not _REAL, reason="真实锁芯/打包工具链为开发机本地资产（不入库）")

if _REAL:
    FPS_A = [fp_digest(f"rev-{i}") for i in range(3)]


@pytest.fixture()
def keys():
    from license_console import crypto_core as cc
    return cc.generate_signing_keypair()


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keys):
    priv, pub = keys
    monkeypatch.setattr(gate, "PUBKEY_HEX", pub.hex())
    monkeypatch.setattr(gate, "PUBKEY_HEX_X", "")
    monkeypatch.setattr(gate, "_LICENSE_FILE", tmp_path / "license.bin")
    monkeypatch.setattr(gate, "_STATE_FILE", tmp_path / "license.state.json")
    monkeypatch.setattr(gate, "_REVOCATION_SHIPPED",
                        tmp_path / "revocations.json")
    monkeypatch.setattr(gate, "_REVOCATION_DATA",
                        tmp_path / "license_revocations.json")
    monkeypatch.setattr(gate, "collect_fingerprints",
                        lambda force=False: list(FPS_A))
    gate._cache.update(ok=None, checked_at=0.0, info=None, reason="")
    gate._rev_cache.update(map=None, at=0.0)
    yield priv, pub
    # teardown：吊销缓存清态——模块级 dict 跨测试泄漏会把 9999 拦截
    # 带进后续测试的激活（全量 4 挂实锤；同 conftest 教训家族）
    gate._rev_cache.update(map=None, at=0.0)
    gate._cache.update(ok=None, checked_at=0.0, info=None, reason="")


def _write_map(path: Path, m: dict[str, int]) -> None:
    path.write_text(json.dumps({"version": 1, "revocations": m}),
                    encoding="utf-8")


def test_stale_generation_kills_code(client, tmp_path: Path) -> None:
    """换绑后（代数+1）随名单下发：旧机 gen1 码当机立断。"""
    priv, _pub = client
    serial = "aabbccddeeff0011"
    code = gate.__dict__ and __import__(
        "license_console.crypto_core", fromlist=["issue_code"]).issue_code(
        priv, serial, 1, FPS_A)  # v1 码（generation 视作 1）
    gate.activate(code)
    ok, reason = gate.is_activated()
    assert ok, reason
    # 名单下发：该序列号最新代数=2（换绑过一次）
    _write_map(gate._REVOCATION_DATA, {serial: 2})
    gate._rev_cache.update(map=None, at=0.0)  # 失缓存
    ok, reason = gate._check(force=True)
    assert not ok and ("换绑" in reason or "作废" in reason)


def test_same_generation_stays_alive(client, tmp_path: Path) -> None:
    priv, _pub = client
    serial = "aabbccddeeff0011"
    code = __import__(
        "license_console.crypto_core", fromlist=["issue_code"]).issue_code(
        priv, serial, 1, FPS_A)
    gate.activate(code)
    _write_map(gate._REVOCATION_DATA, {serial: 1})  # 同代数=未换绑
    gate._rev_cache.update(map=None, at=0.0)
    ok, reason = gate._check(force=True)
    assert ok, reason


def test_revoked_serial_dead_even_reactivate(client, tmp_path: Path) -> None:
    """作废（9999）：不仅存量授权死，重新激活同序列号新码也拦。"""
    priv, _pub = client
    serial = "1122334455667788"
    code = __import__(
        "license_console.crypto_core", fromlist=["issue_code"]).issue_code(
        priv, serial, 1, FPS_A)
    _write_map(gate._REVOCATION_DATA, {serial: 9999})
    gate._rev_cache.update(map=None, at=0.0)
    with pytest.raises(gate.GateError, match="换绑|作废"):
        gate.activate(code)


def test_data_file_overrides_shipped(client, tmp_path: Path) -> None:
    """双源合并：data 覆盖随包（同序列号取 data 值）。"""
    serial = "aabbccddeeff0011"
    _write_map(gate._REVOCATION_SHIPPED, {serial: 5})
    _write_map(gate._REVOCATION_DATA, {serial: 2, "other": 3})
    gate._rev_cache.update(map=None, at=0.0)
    merged = gate._load_revocations()
    assert merged[serial] == 2 and merged["other"] == 3


def test_extract_revocations_from_ledger(tmp_path: Path) -> None:
    """make_dist：台账→映射（active=generation、revoked=9999）。"""
    dbf = tmp_path / "ledger.db"
    conn = sqlite3.connect(str(dbf))
    conn.execute("CREATE TABLE licenses (serial TEXT, generation INTEGER,"
                 " status TEXT)")
    conn.execute("INSERT INTO licenses VALUES ('s1', 1, 'active')")
    conn.execute("INSERT INTO licenses VALUES ('s2', 3, 'active')")
    conn.execute("INSERT INTO licenses VALUES ('s3', 2, 'revoked')")
    conn.commit()
    conn.close()
    rev = _extract_revocations(dbf)
    assert rev == {"s1": 1, "s2": 3, "s3": 9999}
    assert _extract_revocations(tmp_path / "missing.db") == {}


def test_extract_revocations_old_schema(tmp_path: Path) -> None:
    """老库无 generation 列 → 空名单不崩（出包不阻断）。"""
    dbf = tmp_path / "old.db"
    conn = sqlite3.connect(str(dbf))
    conn.execute("CREATE TABLE licenses (serial TEXT, status TEXT)")
    conn.execute("INSERT INTO licenses VALUES ('s1', 'active')")
    conn.commit()
    conn.close()
    assert _extract_revocations(dbf) == {}


def test_time_gate_check_toll(tmp_path: Path) -> None:
    """守夜测试桩：确认时间窗常量仍在（防误删）。"""
    from src import license_gate_impl as g
    assert g._REVOCATION_TTL_S == 60.0
    assert time.timezone is not None  # noqa: F401  time 模块可用性自证
