#!/usr/bin/env python3
r"""OmniSpace 升级包打包器（升级机制批1，2026-09-08）。

大白话：拿「旧版本构建目录」和「新版本构建目录」各一份（make_dist/
make_release 的产物，两边都有全量哈希清单 dist_manifest.json），逐文件
比对出差异，把新增/变更的文件打进升级包、消失的记成删除清单，最后用
开发者的升级私钥给清单签名——用户软件里的验签公钥只认这个签名。

用法：
  runtime/py310/python.exe tools/make_upgrade_package.py --init-key
      # 一次性：生成 Ed25519 升级密钥对（keys/upgrade_signing.key，
      # gitignore+打包黑名单双保险绝不外流；公钥印到终端，固化进
      # src/services/upgrade_service.py 的 UPGRADE_PUBKEY_HEX）

  runtime/py310/python.exe tools/make_upgrade_package.py \
      --old D:/omnispacefz/OmniSpace-2.3.1-g3d810ff \
      --new D:/omnispacefz/OmniSpace-3.0.0-gXXXXXX \
      [--out D:/omnispacefz] [--notes 更新说明.txt] \
      [--from-min 2.3.1] [--from-max 3.0.0] \
      [--include runtime,tools,models] [--key keys/upgrade_signing.key]

默认装什么：代码区全量差分（src/frontend/launcher/skills/
modelxiazai/根文件）+ pydeps 自动差分；runtime/tools(ComfyUI)/models
默认不带（--include 显式开启）；runtime/py310 永不带（updater 自己就
跑在这个解释器上，v1 换它=自断手脚，硬拒绝）。

产出：OmniSpace-upgrade-<旧版本>-to-<新版本>.upg（zip 容器），出包后
当场自验（签名+逐文件哈希+保护区+zip-slip），零问题才算成功。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent

# 默认不进升级包的大目录（--include 显式开启）；runtime/py310 见 HARD_EXCLUDE
DEFAULT_EXCLUDED_TOPS = ("runtime", "tools", "models")
# 永不进包：updater 运行所在的解释器；后三个是用户侧目录（构建里本就不该有）
HARD_EXCLUDES = ("runtime/py310", "data", "updates", "logs")
MANIFEST_NAME = "upgrade_manifest.json"
SIG_NAME = "upgrade_manifest.sig"
README_NAME = "README.txt"
PAYLOAD_DIR = "payload"
_CHUNK = 1 << 20


class ToolError(RuntimeError):
    """打包工具错误（人话消息直接给操作者）。"""


def _load_core() -> Any:
    """加载 updater/core.py（共享校验核心，importlib 文件路径加载）。"""
    spec = importlib.util.spec_from_file_location(
        "_omnispace_upgrade_core", REPO / "updater" / "core.py")
    if spec is None or spec.loader is None:
        raise ToolError("找不到 updater/core.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


core = _load_core()


def _load_crypto() -> tuple[Any, Any]:
    """加载 (ed25519 模块, serialization 模块)。开发侧依赖 pydeps，
    首次 import 失败自动把仓库 pydeps 挂到 sys.path 尾部再试。"""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
        return ed25519, serialization
    except ImportError:
        sys.path.append(str(REPO / "pydeps"))
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ed25519
            return ed25519, serialization
        except ImportError as exc:
            raise ToolError(
                f"cryptography 不可用（{exc}）；请用 runtime/py310 解释器运行"
                "（pydeps 已随仓库就位）") from exc


@dataclass
class BuildArgs:
    old: Path
    new: Path
    out: Path
    key_path: Path
    notes: str = ""
    from_min: str = ""
    from_max: str = ""
    include: set[str] = field(default_factory=set)


def load_build_manifest(build_dir: Path) -> tuple[str, dict[str, str]]:
    """读构建目录的 dist_manifest.json → (build_id, {路径: sha256})。"""
    mf = build_dir / "dist_manifest.json"
    if not mf.is_file():
        raise ToolError(f"{build_dir} 缺 dist_manifest.json——只接受 make_dist/"
                        "make_release 的构建产物做差分基准")
    data = json.loads(mf.read_text("utf-8"))
    build_id = str(data.get("build_id") or "")
    if not build_id:
        raise ToolError(f"{mf} 缺 build_id")
    files = {f["path"]: f["sha256"] for f in data.get("files", [])}
    if not files:
        raise ToolError(f"{mf} 的 files 为空")
    return build_id, files


def _build_version(build_id: str) -> str:
    """build_id（版本+g哈希+日期）→ 裸版本号。"""
    return build_id.split("+", 1)[0]


def _keep(path: str, include: set[str]) -> bool:
    """差分条目是否进升级包（含硬排除与默认排除）。"""
    parts = path.split("/")
    second = "/".join(parts[:2]) if len(parts) > 1 else path
    for hard in HARD_EXCLUDES:
        if second == hard or parts[0] == hard:
            return False
    return parts[0] not in DEFAULT_EXCLUDED_TOPS or parts[0] in include


def _lp(path: Path) -> str:
    """Windows 长路径前缀（对齐 make_dist._lp，ComfyUI 树>260 字符）。"""
    s = str(path.resolve())
    if sys.platform == "win32" and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + s
    return s


def _zip_add(zf: zipfile.ZipFile, src: Path, arcname: str) -> None:
    """流式入包（长路径安全 + 1MB 分块，大依赖文件不吃内存）。"""
    core._rel_parts(arcname)                    # 构建期就拦 zip-slip 形态
    info = zipfile.ZipInfo(arcname, date_time=time.localtime()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    with open(_lp(src), "rb") as fsrc, zf.open(info, "w") as fdst:
        shutil.copyfileobj(fsrc, fdst, _CHUNK)


def read_private_key(path: Path) -> bytes:
    txt = path.read_text("utf-8").strip()
    try:
        raw = bytes.fromhex(txt)
    except ValueError as exc:
        raise ToolError(f"私钥文件不是 hex 格式: {path}") from exc
    if len(raw) != 32:
        raise ToolError(f"私钥长度异常（应 32 字节 hex）: {path}")
    return raw


def init_key(keys_dir: Path, force: bool) -> int:
    """生成升级签名密钥对；打印公钥供固化进后端/updater。"""
    ed, serialization = _load_crypto()
    keys_dir.mkdir(parents=True, exist_ok=True)
    priv_path = keys_dir / "upgrade_signing.key"
    pub_path = keys_dir / "upgrade_signing.pub.hex"
    if priv_path.exists() and not force:
        raise ToolError(f"已存在 {priv_path}（覆写请 --force；旧公钥一旦出厂"
                        "不可换，除非配套换绑流程）")
    sk = ed.Ed25519PrivateKey.generate()
    priv_hex = sk.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption()).hex()
    pub_hex = sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    priv_path.write_text(priv_hex + "\n", encoding="utf-8")
    pub_path.write_text(pub_hex + "\n", encoding="utf-8")
    print(f"✅ 私钥：{priv_path}（gitignore keys/ + 打包黑名单双保险，严禁外发）")
    print(f"✅ 公钥：{pub_path}")
    print("\n公钥 hex（固化到 src/services/upgrade_service.py 的"
          " UPGRADE_PUBKEY_HEX 与 updater/updater.py 的同名常量）：")
    print(f"  {pub_hex}")
    return 0


def build_package(a: BuildArgs) -> Path:
    """产出一个 .upg 升级包并当场自验；返回包路径。"""
    old_id, old_files = load_build_manifest(a.old)
    new_id, new_files = load_build_manifest(a.new)
    old_ver, new_ver = _build_version(old_id), _build_version(new_id)
    if core.version_cmp(new_ver, old_ver) <= 0:
        raise ToolError(f"新版本({new_ver})必须大于旧版本({old_ver})")

    raw_changed = [p for p in new_files
                   if p not in old_files or old_files[p] != new_files[p]]
    raw_deleted = set(old_files) - set(new_files)
    py310_touched = sorted(
        p for p in list(raw_changed) + list(raw_deleted)
        if p.startswith("runtime/py310/"))
    if py310_touched:
        raise ToolError(f"runtime/py310 有差异但 v1 硬不支持替换解释器"
                        f"（updater 自身运行所在）: {py310_touched[:3]}…")
    changed = sorted(p for p in raw_changed if _keep(p, a.include))
    deleted = sorted(p for p in raw_deleted if _keep(p, a.include))
    if not changed and not deleted:
        raise ToolError("两构建零差异（连 build_info 都没变），没有可升级的内容")
    # 新版全量身份清单必须随包走（升级落位后成为新副本的 build_id 真源）
    entries: list[dict[str, Any]] = []
    payload_bytes = 0

    def _add_replace(rel: str) -> None:
        nonlocal payload_bytes
        src = a.new / rel
        size = src.stat().st_size
        entries.append({"path": rel, "size": size,
                        "sha256": core.sha256_file(src),
                        "action": "replace"})
        payload_bytes += size

    for rel in changed:
        _add_replace(rel)
    _add_replace("dist_manifest.json")       # 幂等：构建清单可能已在 changed 里
    for rel in deleted:
        entries.append({"path": rel, "action": "delete"})

    # 去重（dist_manifest.json 二次加入时保留先到的 replace 记录）
    seen: dict[str, dict[str, Any]] = {}
    for e in entries:
        seen.setdefault(e["path"], e)
    entries = [seen[p] for p in sorted(seen)]

    manifest = {
        "format": core.MANIFEST_FORMAT,
        "from_min": a.from_min or old_ver,
        "from_max": a.from_max or new_ver,
        "to_version": new_ver,
        "to_build_id": new_id,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        # 保守启发：数据库模块有变动就按"可能带迁移"对待（升级器会快照 DB）
        "contains_migration": any(
            e["path"].startswith("src/data/database") for e in entries),
        "payload_bytes": payload_bytes,
        "files": entries,
    }
    core.validate_manifest(manifest)

    ed, _serialization = _load_crypto()
    sk = ed.Ed25519PrivateKey.from_private_bytes(read_private_key(a.key_path))
    sig = sk.sign(core.canonical_manifest_bytes(manifest))

    a.out.mkdir(parents=True, exist_ok=True)
    out_path = a.out / f"OmniSpace-upgrade-{old_ver}-to-{new_ver}.upg"
    if out_path.exists():
        out_path.unlink()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_NAME,
                    json.dumps(manifest, ensure_ascii=False, indent=1))
        zf.writestr(SIG_NAME, sig)
        notes = a.notes or (f"OmniSpace {old_ver} → {new_ver} 升级包\n"
                            f"构建号：{new_id}\n生成：{manifest['created']}\n")
        zf.writestr(README_NAME, notes)
        for e in entries:
            if e["action"] == "replace":
                _zip_add(zf, a.new / e["path"], f"{PAYLOAD_DIR}/{e['path']}")

    problems = self_verify(out_path, sk.public_key())
    if problems:
        out_path.unlink(missing_ok=True)
        raise ToolError("自验失败（包已删除）：" + "; ".join(problems[:5]))
    n_rep = sum(1 for e in entries if e["action"] == "replace")
    n_del = len(entries) - n_rep
    print(f"✅ 升级包已产出：{out_path}")
    print(f"   {old_ver}({old_id}) → {new_ver}({new_id})；"
          f"替换 {n_rep} 件 / 删除 {n_del} 件 / 未压缩 {payload_bytes / 1e6:.1f} MB"
          f" / 包体 {out_path.stat().st_size / 1e6:.1f} MB")
    print(f"   兼容区间 [{manifest['from_min']}, {manifest['from_max']})；"
          f"contains_migration={manifest['contains_migration']}")
    return out_path


def self_verify(path: Path, pubkey: Any) -> list[str]:
    """开包自验：结构/签名/逐文件哈希/保护区/zip-slip，返回问题清单。"""
    problems: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad:
                problems.append(f"CRC 损坏: {bad}")
            names = zf.namelist()
            if MANIFEST_NAME not in names or SIG_NAME not in names:
                problems.append("包缺清单/签名文件")
                return problems
            manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
            sig = zf.read(SIG_NAME)
            for n in names:
                try:
                    core._rel_parts(n)
                except core.UnsafePathError as exc:
                    problems.append(f"包内路径不安全: {exc}")
            try:
                core.validate_manifest(manifest)
            except core.ManifestError as exc:
                problems.append(f"清单不合法: {exc}")
                return problems
            from cryptography.exceptions import InvalidSignature
            try:
                pubkey.verify(sig, core.canonical_manifest_bytes(manifest))
            except InvalidSignature:
                problems.append("签名验证失败")
            for e in manifest["files"]:
                if e["action"] != "replace":
                    continue
                arc = f"{PAYLOAD_DIR}/{e['path']}"
                if arc not in names:
                    problems.append(f"包内缺 payload: {e['path']}")
                    continue
                h = hashlib.sha256()
                with zf.open(arc) as f:
                    for chunk in iter(lambda: f.read(_CHUNK), b""):
                        h.update(chunk)
                if h.hexdigest() != e["sha256"]:
                    problems.append(f"哈希不符: {e['path']}")
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        problems.append(f"包无法读取: {exc}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="OmniSpace 升级包打包器")
    ap.add_argument("--init-key", action="store_true",
                    help="生成 Ed25519 升级签名密钥对（一次性）")
    ap.add_argument("--force", action="store_true", help="init-key 覆盖已有私钥")
    ap.add_argument("--old", help="旧版本构建目录（含 dist_manifest.json）")
    ap.add_argument("--new", help="新版本构建目录（含 dist_manifest.json）")
    ap.add_argument("--out", default="D:/omnispacefz", help="输出目录")
    ap.add_argument("--notes", help="更新说明文本文件（进包 README.txt）")
    ap.add_argument("--from-min", help="兼容下界（默认=旧构建版本号）")
    ap.add_argument("--from-max", help="兼容上界（默认=新版本号，开区间）")
    ap.add_argument("--include", default="",
                    help="额外包含的大目录（逗号分隔：runtime,tools,models）")
    ap.add_argument("--key", default=str(REPO / "keys" / "upgrade_signing.key"),
                    help="签名私钥路径")
    args = ap.parse_args(argv)

    try:
        if args.init_key:
            return init_key(REPO / "keys", force=args.force)
        if not (args.old and args.new):
            ap.error("--old 与 --new 必填（或用 --init-key 生成密钥）")
        include = {s.strip() for s in args.include.split(",") if s.strip()}
        bad_inc = include - set(DEFAULT_EXCLUDED_TOPS)
        if bad_inc:
            ap.error(f"--include 只接受 {DEFAULT_EXCLUDED_TOPS}，不认识: {bad_inc}")
        notes = ""
        if args.notes:
            notes = Path(args.notes).read_text("utf-8")
        a = BuildArgs(
            old=Path(args.old), new=Path(args.new), out=Path(args.out),
            key_path=Path(args.key), notes=notes,
            from_min=args.from_min or "", from_max=args.from_max or "",
            include=include)
        build_package(a)
        return 0
    except ToolError as exc:
        print(f"❌ {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
