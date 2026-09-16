"""升级机制批1（2026-09-08）：共享核心与验签服务契约单测。

覆盖：版本比较/区间（左闭右开）、zip-slip 路径安全（绝对/盘符/../保留名）、
清单 schema 校验、规范化字节稳定性、Ed25519 验签往返与三类拒签、解包
深验（篡改/缺件/保护区）、兼容判定四规则（开发形态/同构建/降级/区间）、
updates 目录扫描与升级状态读写。

隔离纪律：临时密钥每次现生成，绝不触碰 keys/ 真实密钥与 data/ 用户库。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from src.services import upgrade_service as svc

core = svc.core()


# ── 造物辅助 ─────────────────────────────────────────────────────

def _replace_entry(path: str, content: bytes = b"hello") -> dict:
    return {"path": path, "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "action": "replace"}


def _manifest(files: list[dict], **over: object) -> dict:
    m: dict = {
        "format": 1, "from_min": "2.3.1", "from_max": "3.0.0",
        "to_version": "3.0.0", "to_build_id": "3.0.0+gbbb+20260908",
        "created": "2026-09-08 00:00:00", "contains_migration": False,
        "payload_bytes": 5, "files": files,
    }
    m.update(over)
    return m


def _ephemeral_key() -> tuple[ed25519.Ed25519PrivateKey, str]:
    sk = ed25519.Ed25519PrivateKey.generate()
    pub_hex = sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    return sk, pub_hex


def _write_package(path: Path, manifest: dict, sig: bytes,
                   payload: dict[str, bytes] | None = None) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("upgrade_manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=1))
        zf.writestr("upgrade_manifest.sig", sig)
        zf.writestr("README.txt", "测试包")
        for rel, content in (payload or {}).items():
            zf.writestr(f"payload/{rel}", content)


# ── 版本 ─────────────────────────────────────────────────────────

def test_version_parse_and_cmp() -> None:
    assert core.parse_version("3.0.0") == (3, 0, 0)
    assert core.parse_version("2.3.1+gabc+20260908") == (2, 3, 1)
    assert core.parse_version("3.0") == (3, 0)
    assert core.version_cmp("3.0", "3.0.0") == 0          # 短版补零对齐
    assert core.version_cmp("2.9.9", "3.0.0") < 0
    assert core.version_cmp("3.0.1", "3.0.0") > 0
    assert core.version_in_range("2.5", "2.3.1", "3.0.0")
    assert core.version_in_range("2.3.1", "2.3.1", "3.0.0")   # 左闭
    assert not core.version_in_range("3.0.0", "2.3.1", "3.0.0")  # 右开
    with pytest.raises(core.UpgradeCoreError):
        core.parse_version("abc")


# ── 路径安全 ─────────────────────────────────────────────────────

def test_safe_join_rejects_escapes(tmp_path: Path) -> None:
    for evil in ("../x", "a/../../b", "C:/x", "C:\\x", "/abs", "\\abs",
                 "//srv/share", "..\\x", "CON", "a/NUL.bin"):
        with pytest.raises(core.UnsafePathError):
            core.safe_join(tmp_path, evil)


def test_safe_join_accepts_normal(tmp_path: Path) -> None:
    target = core.safe_join(tmp_path, "src/services/a.py")
    assert target == (tmp_path / "backend" / "services" / "a.py").resolve()


def test_payload_target_protected(tmp_path: Path) -> None:
    for rel in ("data/evil.db", "updates/evil.upg", "logs/evil.log"):
        with pytest.raises(core.UnsafePathError):
            core.payload_target(tmp_path, rel)
    assert core.payload_target(tmp_path, "src/a.py").is_absolute()


# ── 清单 schema ──────────────────────────────────────────────────

def test_validate_manifest() -> None:
    core.validate_manifest(_manifest([_replace_entry("src/a.py")]))
    bad_cases = [
        _manifest([_replace_entry("a")], format=2),                 # 版本不符
        _manifest([_replace_entry("a")], to_version=""),            # 缺字段
        _manifest([_replace_entry("a")], contains_migration="no"),  # 类型错
        _manifest([]),                                              # 空 files
        _manifest([_replace_entry("a"), _replace_entry("a")]),      # 重复路径
        _manifest([{"path": "a", "action": "rm"}]),                 # 动作非法
        _manifest([_replace_entry("a")], from_min="3.0.0",
                  from_max="2.3.1"),                                # 区间倒挂
        _manifest([_replace_entry("a")], from_min="x.y"),           # 版本脏值
    ]
    for bad in bad_cases:
        with pytest.raises(core.ManifestError):
            core.validate_manifest(bad)


def test_canonical_bytes_stable() -> None:
    m1 = _manifest([_replace_entry("a")])
    m2 = {"files": m1["files"], **{k: v for k, v in m1.items() if k != "files"}}
    m3 = {k: m1[k] for k in reversed(list(m1.keys()))}
    assert core.canonical_manifest_bytes(m2) == core.canonical_manifest_bytes(m1)
    assert core.canonical_manifest_bytes(m3) == core.canonical_manifest_bytes(m1)


# ── 验签 ─────────────────────────────────────────────────────────

def test_signature_roundtrip_and_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    sk, pub_hex = _ephemeral_key()
    monkeypatch.setattr(svc, "UPGRADE_PUBKEY_HEX", pub_hex)
    m = _manifest([_replace_entry("src/a.py")])
    sig = sk.sign(core.canonical_manifest_bytes(m))
    svc.verify_signature(m, sig)                       # 正包通过（不抛即过）

    tampered = dict(m)
    tampered["to_version"] = "9.9.9"
    with pytest.raises(svc.UpgradeError) as ei:
        svc.verify_signature(tampered, sig)
    assert ei.value.code == "UPGRADE_SIGNATURE_INVALID"

    monkeypatch.setattr(svc, "UPGRADE_PUBKEY_HEX", "")  # 未配置公钥=诚实拒绝
    with pytest.raises(svc.UpgradeError) as ei2:
        svc.verify_signature(m, sig)
    assert ei2.value.code == "UPGRADE_KEY_NOT_CONFIGURED"


# ── 解包深验 ─────────────────────────────────────────────────────

def test_read_package_and_extract(tmp_path: Path,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    sk, pub_hex = _ephemeral_key()
    monkeypatch.setattr(svc, "UPGRADE_PUBKEY_HEX", pub_hex)
    content = b"print(1)"
    m = _manifest([_replace_entry("src/a.py", content)],
                  payload_bytes=len(content))
    sig = sk.sign(core.canonical_manifest_bytes(m))
    pkg = tmp_path / "demo.upg"
    _write_package(pkg, m, sig, {"src/a.py": content})

    m2, sig2 = svc.read_package(pkg)
    svc.verify_signature(m2, sig2)
    summary = svc.extract_verified(pkg, tmp_path / "work")
    assert summary["to_version"] == "3.0.0"
    assert summary["files_replace"] == 1
    assert summary["files_delete"] == 0

    # 篡改 payload：内容变了 → SHA256/大小不符 → 深验拒绝
    tam = tmp_path / "tampered.upg"
    _write_package(tam, m, sig, {"src/a.py": b"print(2)"})
    with pytest.raises(svc.UpgradeError) as ei:
        svc.extract_verified(tam, tmp_path / "w2")
    assert ei.value.code == "UPGRADE_CONTENT_MISMATCH"


def test_read_package_rejects_zip_slip(tmp_path: Path) -> None:
    sk, _pub = _ephemeral_key()
    m = _manifest([_replace_entry("src/a.py")])
    sig = sk.sign(core.canonical_manifest_bytes(m))
    evil = tmp_path / "evil.upg"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("upgrade_manifest.json", json.dumps(m))
        zf.writestr("upgrade_manifest.sig", sig)
        zf.writestr("payload/../evil.py", b"x")        # 逃逸条目
    with pytest.raises(svc.UpgradeError) as ei:
        svc.read_package(evil)
    assert ei.value.code == "UPGRADE_PACKAGE_BAD"


def test_read_package_rejects_garbage(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.upg"
    garbage.write_bytes(b"this is not a zip at all")
    with pytest.raises(svc.UpgradeError) as ei:
        svc.read_package(garbage)
    assert ei.value.code == "UPGRADE_PACKAGE_BAD"


def test_verify_extracted_problems(tmp_path: Path) -> None:
    work = tmp_path / "work"
    (work / "payload" / "backend").mkdir(parents=True)
    (work / "payload" / "backend" / "a.py").write_bytes(b"hello")
    m = _manifest([_replace_entry("src/a.py")])
    assert core.verify_extracted(work, m) == []

    (work / "payload" / "backend" / "a.py").write_bytes(b"hellO")
    assert any("SHA256" in p for p in core.verify_extracted(work, m))

    (work / "payload" / "backend" / "a.py").unlink()
    assert any("缺文件" in p for p in core.verify_extracted(work, m))

    m2 = _manifest([_replace_entry("data/evil.db")])
    assert any("受保护" in p for p in core.verify_extracted(work, m2))


# ── 兼容判定 ─────────────────────────────────────────────────────

def test_check_compatibility_rules() -> None:
    m = _manifest([_replace_entry("src/a.py")])
    dev = {"version": "2.3.1", "build": "2.3.1+dev", "edition": "dev"}
    ok, why = svc.check_compatibility(m, dev)
    assert not ok and "开发" in why

    same = {"version": "3.0.0", "build": "3.0.0+gbbb+20260908",
            "edition": "release"}
    ok, why = svc.check_compatibility(m, same)
    assert not ok and "已是" in why

    newer = {"version": "3.1.0", "build": "3.1.0+z", "edition": "release"}
    ok, why = svc.check_compatibility(m, newer)
    assert not ok and "降级" in why

    below = {"version": "2.2.0", "build": "2.2.0+z", "edition": "release"}
    ok, why = svc.check_compatibility(m, below)
    assert not ok and "区间" in why

    fit = {"version": "2.9.9", "build": "2.9.9+z", "edition": "release"}
    ok, why = svc.check_compatibility(m, fit)
    assert ok and why == ""


# ── 扫描与状态 ───────────────────────────────────────────────────

def test_scan_updates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "install_root", lambda: tmp_path)
    (tmp_path / "updates").mkdir()
    # 让扫描器认出 release 形态 + 当前版本 2.9.9
    (tmp_path / "dist_manifest.json").write_text(
        json.dumps({"build_id": "2.9.9+gtest+20260908"}), encoding="utf-8")
    monkeypatch.setattr(svc.config, "APP_VERSION", "2.9.9")
    monkeypatch.setattr(svc.config, "BUILD_ID", "2.9.9+gtest+20260908")

    sk, pub_hex = _ephemeral_key()
    monkeypatch.setattr(svc, "UPGRADE_PUBKEY_HEX", pub_hex)
    m = _manifest([_replace_entry("src/a.py")])
    sig = sk.sign(core.canonical_manifest_bytes(m))
    good = tmp_path / "updates" / "OmniSpace-upgrade-2.3.1-to-3.0.0.upg"
    _write_package(good, m, sig, {"src/a.py": b"hello"})
    (tmp_path / "updates" / "broken.upg").write_bytes(b"not a zip")

    results = svc.scan_updates()
    by_name = {r["file"]: r for r in results}
    assert set(by_name) == {"OmniSpace-upgrade-2.3.1-to-3.0.0.upg", "broken.upg"}
    good_entry = by_name["OmniSpace-upgrade-2.3.1-to-3.0.0.upg"]
    assert good_entry["signature_ok"] and good_entry["compatible"]
    assert good_entry["valid"] and good_entry["reason"] == ""
    assert by_name["broken.upg"]["valid"] is False
    assert by_name["broken.upg"]["reason"]


def test_state_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "install_root", lambda: tmp_path)
    assert svc.read_state() == {}                    # 无状态文件=空闲
    svc.write_state({"phase": "applying", "package": "x.upg"})
    assert svc.read_state()["phase"] == "applying"
    # 损坏状态文件按空闲处理（绝不抛）
    core.state_path(tmp_path).write_text("{broken json", encoding="utf-8")
    assert svc.read_state() == {}
