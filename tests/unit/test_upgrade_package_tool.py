"""升级包打包器（批1，2026-09-08）：差分规则与往返契约单测。

覆盖：默认包含范围（代码+pydeps 差分；runtime/tools/models 默认不带）、
--include 显式开启、runtime/py310 硬拒绝（updater 自身运行的解释器）、
零差异拒绝、产物经 upgrade_service 全链验签、payload 篡改与清单篡改
双双被拒、self_verify 兜底。

隔离纪律：临时构建目录+临时密钥；不触碰真实 keys/ 与任何发行产物。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from src.services import upgrade_service as svc

REPO = Path(__file__).resolve().parents[2]


def _load_tool() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_omni_make_upg_tool", REPO / "tools" / "make_upgrade_package.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("打包器加载失败")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


tool = _load_tool()


def _sha(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _make_build(root: Path, build_id: str, files: dict[str, bytes]) -> Path:
    d = root / build_id.replace("+", "_")
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    entries = [{"path": rel.replace("\\", "/"), "size": len(c),
                "sha256": _sha(c)} for rel, c in sorted(files.items())]
    (d / "dist_manifest.json").write_text(
        json.dumps({"build_id": build_id, "edition": "release",
                    "created": "t", "generator": "test", "files": entries},
                   ensure_ascii=False), encoding="utf-8")
    return d


def _key_files(tmp_path: Path) -> tuple[ed25519.Ed25519PrivateKey, Path, str]:
    sk = ed25519.Ed25519PrivateKey.generate()
    priv_hex = sk.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption()).hex()
    pub_hex = sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    key_path = tmp_path / "ephemeral.key"
    key_path.write_text(priv_hex, encoding="utf-8")
    return sk, key_path, pub_hex


def _build(tmp_path: Path, old_files: dict[str, bytes],
           new_files: dict[str, bytes], include: set[str] | None = None
           ) -> tuple[Path, ed25519.Ed25519PrivateKey, str]:
    old = _make_build(tmp_path, "2.3.1+ga+20260101", old_files)
    new = _make_build(tmp_path, "3.0.0+gb+20260102", new_files)
    sk, key_path, pub_hex = _key_files(tmp_path)
    args = tool.BuildArgs(
        old=old, new=new, out=tmp_path / "out", key_path=key_path,
        include=include or set())
    pkg = tool.build_package(args)
    return pkg, sk, pub_hex


def _read_pkg_manifest(pkg: Path) -> dict:
    with zipfile.ZipFile(pkg) as zf:
        return json.loads(zf.read("upgrade_manifest.json").decode("utf-8"))


OLD_BASE = {
    "src/a.py": b"A",
    "src/b.py": b"B1",
    "src/gone.py": b"G",
    "pydeps/pkg1/chg.py": b"C1",
    "runtime/py310/core.dll": b"P1",
    "runtime/py313/x.dll": b"X1",
}
NEW_BASE = {
    "src/a.py": b"A",
    "src/b.py": b"B2",
    "pydeps/pkg1/chg.py": b"C2",
    "pydeps/pkg1/new.py": b"N",
    "runtime/py310/core.dll": b"P1",
    "runtime/py313/x.dll": b"X2",
    "models/big.bin": b"MB",
}


def test_diff_default_rules(tmp_path: Path) -> None:
    """默认=代码+pydeps 差分：变更换新、消失记删；runtime/tools/models 不带。"""
    pkg, _sk, _pub = _build(tmp_path, OLD_BASE, NEW_BASE)
    m = _read_pkg_manifest(pkg)
    replaces = {e["path"] for e in m["files"] if e["action"] == "replace"}
    deletes = {e["path"] for e in m["files"] if e["action"] == "delete"}
    assert {"src/b.py", "pydeps/pkg1/chg.py", "pydeps/pkg1/new.py",
            "dist_manifest.json"} <= replaces
    assert "src/a.py" not in replaces            # 未变不带
    assert "runtime/py313/x.dll" not in replaces     # 默认排除 runtime
    assert "models/big.bin" not in replaces          # 默认排除 models
    assert deletes == {"src/gone.py"}
    assert m["from_min"] == "2.3.1" and m["from_max"] == "3.0.0"
    assert m["to_build_id"] == "3.0.0+gb+20260102"
    assert m["contains_migration"] is False


def test_include_overrides(tmp_path: Path) -> None:
    pkg, _sk, _pub = _build(tmp_path, OLD_BASE, NEW_BASE, include={"models"})
    m = _read_pkg_manifest(pkg)
    replaces = {e["path"] for e in m["files"] if e["action"] == "replace"}
    assert "models/big.bin" in replaces
    assert "runtime/py313/x.dll" not in replaces     # 没开的仍不带


def test_py310_hard_refused(tmp_path: Path) -> None:
    old = dict(OLD_BASE)
    new = dict(NEW_BASE)
    new["runtime/py310/core.dll"] = b"P2"            # 动了 updater 的解释器
    with pytest.raises(tool.ToolError, match="py310"):
        _build(tmp_path, old, new)


def test_zero_diff_refused(tmp_path: Path) -> None:
    files = {"src/a.py": b"A"}
    with pytest.raises(tool.ToolError, match="零差异"):
        _build(tmp_path, files, dict(files))


def test_migration_flag_heuristic(tmp_path: Path) -> None:
    old = {"src/a.py": b"A", "src/data/database.py": b"D1"}
    new = {"src/a.py": b"A", "src/data/database.py": b"D2"}
    pkg, _sk, _pub = _build(tmp_path, old, new)
    m = _read_pkg_manifest(pkg)
    assert m["contains_migration"] is True           # 数据库模块有变=按带迁移对待


def test_roundtrip_with_service(tmp_path: Path,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    """打包器产物 → 服务端验签/兼容/深验全通过；两类篡改双双被拒。"""
    pkg, _sk, pub_hex = _build(tmp_path, OLD_BASE, NEW_BASE)
    monkeypatch.setattr(svc, "UPGRADE_PUBKEY_HEX", pub_hex)

    m, sig = svc.read_package(pkg)
    svc.verify_signature(m, sig)
    cur = {"version": "2.9.9", "build": "2.9.9+z", "edition": "release"}
    ok, why = svc.check_compatibility(m, cur)
    assert ok, why
    summary = svc.extract_verified(pkg, tmp_path / "work")
    assert summary["files_replace"] >= 1

    # 篡改 payload 内容 → 深验（哈希/大小）拒绝
    tam = tmp_path / "tampered.upg"
    with zipfile.ZipFile(pkg) as src, zipfile.ZipFile(tam, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "payload/src/b.py":
                data = data + b"x"
            dst.writestr(info, data)
    with pytest.raises(svc.UpgradeError) as ei:
        svc.extract_verified(tam, tmp_path / "w2")
    assert ei.value.code == "UPGRADE_CONTENT_MISMATCH"

    # 篡改清单（换目标版本，签名随之失效）
    tam2 = tmp_path / "tampered2.upg"
    with zipfile.ZipFile(pkg) as src, zipfile.ZipFile(tam2, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "upgrade_manifest.json":
                mm = json.loads(data)
                mm["to_version"] = "9.9.9"
                data = json.dumps(mm).encode("utf-8")
            dst.writestr(info, data)
    m2, sig2 = svc.read_package(tam2)
    with pytest.raises(svc.UpgradeError) as ei2:
        svc.verify_signature(m2, sig2)
    assert ei2.value.code == "UPGRADE_SIGNATURE_INVALID"


def test_self_verify_catches_tamper(tmp_path: Path) -> None:
    pkg, sk, _pub = _build(tmp_path, OLD_BASE, NEW_BASE)
    assert tool.self_verify(pkg, sk.public_key()) == []

    tam = tmp_path / "t.upg"
    with zipfile.ZipFile(pkg) as src, zipfile.ZipFile(tam, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "payload/src/b.py":
                data = data + b"x"
            dst.writestr(info, data)
    problems = tool.self_verify(tam, sk.public_key())
    assert problems and any("哈希" in p or "大小" in p for p in problems)
# 本项目仅供学习使用，商业授权请+Q 3559331368
