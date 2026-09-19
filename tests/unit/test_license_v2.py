"""激活加固批（2026-09-19）单测：v2 码格式 / 时限码 / 解绑令牌 / 回拨守卫 /
公钥双藏 / 旧码兼容。

两侧覆盖：
- license_console.crypto_core（发码侧：v2 签发、验码、解绑令牌验证）
- src.license_gate（客户端锁芯同构副本：到期硬拦、防伪改、墓碑、回拨、
  双藏交叉验证；经 monkeypatch 注入测试公钥与临时路径，不跑真实指纹采集）
"""
# 本项目仅供学习使用，商业授权请+Q 3553191368
from __future__ import annotations

import time

import pytest

try:  # 真实锁芯+发码台只在开发/出包机（均不入库）；克隆者整组跳过
    from license_console import crypto_core as cc
    from src import license_gate_impl as gate  # 直连真身（不经占位壳）
    _REAL_IMPL = True
except ImportError:  # noqa: BLE001 - 公开仓库无 license_console/真身
    _REAL_IMPL = False

pytestmark = pytest.mark.skipif(
    not _REAL_IMPL,
    reason="真实锁芯/发码台为开发机本地资产（不入开源仓库）")

# 模块级常量须守卫：skipif 只挡执行不挡 import，克隆者机器上 cc 不存在
if _REAL_IMPL:
    FPS_A = [cc.fp_digest(f"comp-{i}-A") for i in range(3)]
    FPS_B = [cc.fp_digest(f"comp-{i}-B") for i in range(3)]


@pytest.fixture()
def keys():
    priv, pub = cc.generate_signing_keypair()
    return priv, pub


@pytest.fixture()
def client(tmp_path, monkeypatch, keys):
    """客户端锁芯测试环境：注入测试公钥/临时路径/固定指纹，清缓存。"""
    priv, pub = keys
    monkeypatch.setattr(gate, "PUBKEY_HEX", pub.hex())
    monkeypatch.setattr(gate, "PUBKEY_HEX_X", "")
    monkeypatch.setattr(gate, "_LICENSE_FILE", tmp_path / "license.bin")
    monkeypatch.setattr(gate, "_STATE_FILE", tmp_path / "license.state.json")
    monkeypatch.setattr(gate, "collect_fingerprints",
                        lambda force=False: list(FPS_A))
    gate._cache.update(fp=None, fp_at=0.0, ok=None, checked_at=0.0,
                       info=None, reason="")
    return priv, pub


# ── 发码侧（crypto_core）──────────────────────────────────────────

def test_v1_code_still_verifies(keys):
    """旧码兼容：v1 永久码（62B）签发/验证语义不变。"""
    priv, pub = keys
    code = cc.issue_code(priv, "aabbccddeeff0011", cc.TYPE_BUYOUT, FPS_A)
    info = cc.verify_code(code, FPS_A, pub)
    assert info["type"] == "buyout"
    assert info["generation"] == 1 and info["permanent"] is True


def test_timed_code_v2_roundtrip(keys):
    """时限码 v2：类型/到期日/非过期标注正确。"""
    priv, pub = keys
    expires = time.strftime("%Y-%m-%d", time.localtime(time.time() + 30 * 86400))
    code = cc.issue_code(priv, "1122334455667788", cc.TYPE_TIMED, FPS_A,
                         expires=expires)
    info = cc.verify_code(code, FPS_A, pub)
    assert info["type"] == "timed" and info["expires"] == expires
    assert info["permanent"] is False and info["expired"] is False
    assert info["generation"] == 1


def test_timed_code_expired_flag(keys):
    """已过期的时限码在发码台验码时标注 expired（硬拦在客户端）。"""
    priv, pub = keys
    code = cc.issue_code(priv, "1122334455667788", cc.TYPE_TIMED, FPS_A,
                         expires="2020-01-01")
    info = cc.verify_code(code, FPS_A, pub)
    assert info["expired"] is True


def test_generation_bump_on_reissue(keys):
    """换绑重发：代数透传（generation=2 → 验码读回 2）。"""
    priv, pub = keys
    code = cc.issue_code(priv, "aabbccddeeff0011", cc.TYPE_BUYOUT, FPS_B,
                         generation=2, expires=cc.PERM_EXPIRES)
    info = cc.verify_code(code, FPS_B, pub)
    assert info["generation"] == 2 and info["permanent"] is True


def test_fingerprint_mismatch_rejected(keys):
    """一码不通用：A 机指纹的码在 B 机 2/3 不过。"""
    priv, pub = keys
    code = cc.issue_code(priv, "aabbccddeeff0011", cc.TYPE_BUYOUT, FPS_A)
    with pytest.raises(cc.CodeError, match="设备不匹配"):
        cc.verify_code(code, FPS_B, pub)


# ── 客户端锁芯（license_gate）─────────────────────────────────────

def test_client_activates_timed_code_and_shows_days(client):
    priv, _pub = client
    expires = time.strftime("%Y-%m-%d", time.localtime(time.time() + 10 * 86400))
    code = cc.issue_code(priv, "1122334455667788", cc.TYPE_TIMED, FPS_A,
                         expires=expires)
    info = gate.activate(code)
    assert info["type"] == "timed" and info["days_left"] is not None
    assert 9 <= info["days_left"] <= 10
    ok, reason = gate.is_activated()
    assert ok, reason


def test_client_rejects_expired_timed_code(client):
    """时限码到期=绝对日期：原机重输也拒（结构性单次使用）。"""
    priv, _pub = client
    code = cc.issue_code(priv, "1122334455667788", cc.TYPE_TIMED, FPS_A,
                         expires="2020-01-01")
    with pytest.raises(gate.GateError, match="授权已到期"):
        gate.activate(code)


def test_client_rejects_v1_forever_code_after_unbind(client):
    """解绑墓碑：旧码在本机发起解绑后不可再用（防换绑套利旧码复活）。"""
    priv, _pub = client
    code = cc.issue_code(priv, "aabbccddeeff0011", cc.TYPE_BUYOUT, FPS_A)
    gate.activate(code)
    token = gate.unbind()
    assert token and not gate._LICENSE_FILE.exists()
    ok, _reason = gate.is_activated()
    assert not ok
    with pytest.raises(gate.GateError, match="解绑"):
        gate.activate(code)


def test_client_clock_rollback_guard(client):
    """时钟回拨守卫：state 里的最近校验时间在未来超容忍窗 → 拦。"""
    import json
    priv, _pub = client
    code = cc.issue_code(priv, "aabbccddeeff0011", cc.TYPE_BUYOUT, FPS_A)
    gate.activate(code)
    state = json.loads(gate._STATE_FILE.read_text(encoding="utf-8"))
    state["last_check"] = time.time() + 3 * 86400
    gate._STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    ok, reason = gate._check(force=True)
    assert not ok and "回拨" in reason


def test_client_pubkey_xor_crosscheck(client):
    """A6b 公钥双藏：注入正确 X 副本通过；篡改一份即失配锁死。"""
    import secrets as _secrets
    priv, pub = client
    code = cc.issue_code(priv, "aabbccddeeff0011", cc.TYPE_BUYOUT, FPS_A)
    gate.PUBKEY_HEX_X = cc.pubkey_xor_hex(pub.hex())
    assert gate._pubkey_integrity_ok()
    gate.activate(code)  # 注入正确 X 不影响正常激活
    # 篡改一份（模拟二进制 patch）：交叉验证失败 → 激活/校验被锁
    gate.PUBKEY_HEX_X = _secrets.token_hex(32)
    assert not gate._pubkey_integrity_ok()
    ok, reason = gate._check(force=True)
    assert not ok and "完整性" in reason


def test_client_tampered_type_byte_fails_signature(client):
    """时限码冒充永久码：改类型字节 → Ed25519 验签失败（两码型不通用）。"""
    import base64
    priv, pub = client
    expires = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
    code = cc.issue_code(priv, "1122334455667788", cc.TYPE_TIMED, FPS_A,
                         expires=expires)
    compact = code.replace("-", "")
    raw = bytearray(base64.b32decode(compact + "=" * (-len(compact) % 8)))
    raw[1] = cc.TYPE_BUYOUT  # 篡改码型字节
    forged = base64.b32encode(bytes(raw)).decode("ascii").rstrip("=")
    with pytest.raises(gate.GateError, match="签名校验失败"):
        gate.activate(forged)


def test_unbind_token_roundtrip_and_fp_guard(client):
    """解绑令牌：正确令牌过验；指纹对不上台账旧机 → 拒绝换绑。"""
    priv, pub = client
    code = cc.issue_code(priv, "aabbccddeeff0011", cc.TYPE_BUYOUT, FPS_A)
    gate.activate(code)
    token = gate.unbind()
    out = cc.verify_unbind_token(token, pub.hex(), "aabbccddeeff0011",
                                 expect_fps=FPS_A)
    assert out["fp_match"] >= 2
    with pytest.raises(cc.CodeError):
        cc.verify_unbind_token(token, pub.hex(), "aabbccddeeff0011",
                               expect_fps=FPS_B)
    with pytest.raises(cc.CodeError):
        cc.verify_unbind_token(token, pub.hex(), "ffffffffffffffff")
