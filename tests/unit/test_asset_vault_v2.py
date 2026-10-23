"""H6 方案 A 批 1（2026-10-24 拍板）：资产金库 v2 双格式单测。

覆盖：
- v2 roundtrip（独立随机密钥+密钥 id，经锁芯注入态解密）
- v1 永久兼容（公钥派生路径回归不变）
- v2 密钥 id 不匹配/未注入/畸形密钥 → 明确 ValueError/降级
- read_asset 双格式自动分发 + v2 缺钥降级 None（调用方回退明文）
密钥全部运行期 os.urandom 生成（无凭据字面量）。
"""
from __future__ import annotations

import os

import pytest

import src.license_gate as gate
from src.asset_vault import (
    MAGIC_V2,
    decrypt_bytes,
    encrypt_bytes,
    encrypt_bytes_v2,
    read_asset,
)


def _rand_keypair() -> tuple[bytes, bytes]:
    return os.urandom(32), os.urandom(8)


def test_v2_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    key, kid = _rand_keypair()
    monkeypatch.setattr(gate, "ASSET_KEY_HEX", key.hex(), raising=False)
    monkeypatch.setattr(gate, "ASSET_KEY_ID", kid.hex(), raising=False)
    data = "值钱的提示词资产 v2".encode()
    blob = encrypt_bytes_v2(data, key, kid)
    assert blob.startswith(MAGIC_V2)
    assert decrypt_bytes(blob) == data


def test_v1_path_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1 公钥派生永久兼容（用户拍板）：回归不变。"""
    monkeypatch.setattr(gate, "PUBKEY_HEX", os.urandom(32).hex(),
                        raising=False)
    data = b"legacy asset"
    blob = encrypt_bytes(data, gate.PUBKEY_HEX)
    assert blob.startswith(b"OMNIENC1")
    assert decrypt_bytes(blob, gate.PUBKEY_HEX) == data
    # 无显式公钥走锁芯路径
    assert decrypt_bytes(blob) == data


def test_v2_key_id_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    key, kid = _rand_keypair()
    monkeypatch.setattr(gate, "ASSET_KEY_HEX", key.hex(), raising=False)
    monkeypatch.setattr(gate, "ASSET_KEY_ID", os.urandom(8).hex(),
                        raising=False)
    blob = encrypt_bytes_v2(b"x", key, kid)
    with pytest.raises(ValueError, match="密钥 id 不匹配"):
        decrypt_bytes(blob)


def test_v2_without_injected_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "ASSET_KEY_HEX", "", raising=False)
    monkeypatch.setattr(gate, "ASSET_KEY_ID", "", raising=False)
    key, kid = _rand_keypair()
    blob = encrypt_bytes_v2(b"x", key, kid)
    with pytest.raises(ValueError, match="v2 密钥未注入"):
        decrypt_bytes(blob)


def test_v2_malformed_injected_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """畸形注入（错长度/非 hex）按未注入处理，不得漏成 AESGCM(None)。"""
    key, kid = _rand_keypair()
    blob = encrypt_bytes_v2(b"x", key, kid)
    monkeypatch.setattr(gate, "ASSET_KEY_HEX", "zz" * 32, raising=False)
    monkeypatch.setattr(gate, "ASSET_KEY_ID", kid.hex(), raising=False)
    with pytest.raises(ValueError, match="v2 密钥未注入"):
        decrypt_bytes(blob)
    monkeypatch.setattr(gate, "ASSET_KEY_HEX", os.urandom(31).hex(),
                        raising=False)
    with pytest.raises(ValueError, match="v2 密钥未注入"):
        decrypt_bytes(blob)


def test_read_asset_dual_format_dispatch(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import src.asset_vault as av
    monkeypatch.setattr(av, "ASSET_DIR", tmp_path)
    key, kid = _rand_keypair()
    pubkey = os.urandom(32).hex()
    monkeypatch.setattr(gate, "ASSET_KEY_HEX", key.hex(), raising=False)
    monkeypatch.setattr(gate, "ASSET_KEY_ID", kid.hex(), raising=False)
    monkeypatch.setattr(gate, "PUBKEY_HEX", pubkey, raising=False)
    (tmp_path / "a_v1.json.enc").write_bytes(
        encrypt_bytes(b'{"v": 1}', pubkey))
    (tmp_path / "a_v2.json.enc").write_bytes(
        encrypt_bytes_v2(b'{"v": 2}', key, kid))
    assert read_asset("a_v1.json.enc") == b'{"v": 1}'
    assert read_asset("a_v2.json.enc") == b'{"v": 2}'
    assert read_asset("不存在.enc") is None


def test_read_asset_v2_degrades_to_none_without_key(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """v2 密钥未注入：read_asset 降级 None（调用方回退明文），不抛。"""
    import src.asset_vault as av
    monkeypatch.setattr(av, "ASSET_DIR", tmp_path)
    monkeypatch.setattr(gate, "ASSET_KEY_HEX", "", raising=False)
    monkeypatch.setattr(gate, "ASSET_KEY_ID", "", raising=False)
    key, kid = _rand_keypair()
    (tmp_path / "only_v2.enc").write_bytes(
        encrypt_bytes_v2(b"secret", key, kid))
    assert read_asset("only_v2.enc") is None
