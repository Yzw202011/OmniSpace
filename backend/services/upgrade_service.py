"""应用内升级服务（批1，2026-09-08）：包扫描/验签/兼容判定核心。

方案真源：docs/升级机制方案-2026-09-08.md（D1~D4 已用户拍板）。
批1 范围=纯校验核心（无路由、无副作用）；批2 接 API 与前端，批3 交付
升级执行器 updater.py 与 boot 恢复钩子。

三方分工（为什么长这样）：
- 包格式/版本比较/逐文件 SHA256/zip-slip 路径安全的**唯一定义**在
  updater/core.py——它 stdlib-only（升级独立进程绝不 import pydeps，
  否则替换 pydeps 时文件被锁），本模块经 importlib 文件路径加载它
  （先例：boot.py 加载 scripts/comfy_link/comfy_mount.py）；
- Ed25519 验签在本模块做：后端进程有 pydeps（cryptography），
  先验签后放行；updater 落位时再做逐文件哈希复核兜底。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import zipfile
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .. import config

# 升级签名公钥（Ed25519 raw 32 字节 hex）。私钥=开发者本机
# keys/upgrade_signing.key（gitignore keys/ + make_dist 打包黑名单双保险，
# 绝不入库入包）。2026-09-08 由 tools/make_upgrade_package.py --init-key
# 生成；轮换需配套换绑流程，勿随手改。
UPGRADE_PUBKEY_HEX = (
    "47cf4254362ceb13d9703bfb61002c736881c1d96f31860c1952f512c1b3ade7")

_UPDATES_DIRNAME = "updates"

_core_mod: Any = None
_core_lock = threading.Lock()


class UpgradeError(RuntimeError):
    """升级业务错误：code 走语义串（API 层批2 转 ApiError）。"""

    def __init__(self, code: str, message: str,
                 detail: dict[str, Any] | None = None,
                 suggestion: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail or {}
        self.suggestion = suggestion


def core() -> Any:
    """加载（并缓存）updater/core.py 共享核心。"""
    global _core_mod
    if _core_mod is not None:
        return _core_mod
    with _core_lock:
        if _core_mod is None:
            path = install_root() / "updater" / "core.py"
            spec = importlib.util.spec_from_file_location(
                "_omnispace_upgrade_core", path)
            if spec is None or spec.loader is None:
                raise UpgradeError("UPGRADE_CORE_MISSING",
                                   f"共享核心缺失: {path}")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod      # 先注册再 exec（dataclass 注解查名）
            spec.loader.exec_module(mod)
            _core_mod = mod
    return _core_mod


def install_root() -> Path:
    """安装根（开发环境=仓库根；产品包=backend 上一层）。"""
    return Path(__file__).resolve().parents[2]


def updates_dir() -> Path:
    return install_root() / _UPDATES_DIRNAME


def current_info() -> dict[str, Any]:
    """当前副本身份：版本/构建号/发行形态（升级只认 release 形态）。"""
    root = install_root()
    return {
        "version": config.APP_VERSION,
        "build": config.BUILD_ID,
        "edition": "release" if (root / "dist_manifest.json").is_file()
        else "dev",
        "updates_dir": str(updates_dir()),
    }


def read_package(zip_path: Path) -> tuple[dict[str, Any], bytes]:
    """读包内清单与签名；结构问题抛 UpgradeError（UPGRADE_PACKAGE_BAD）。"""
    c = core()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            if bad:
                raise UpgradeError(
                    "UPGRADE_PACKAGE_CORRUPT",
                    f"包体损坏（CRC 校验失败: {bad}）",
                    suggestion="请重新下载升级包再试")
            names = zf.namelist()
            if c.MANIFEST_NAME not in names or c.SIG_NAME not in names:
                raise UpgradeError(
                    "UPGRADE_PACKAGE_BAD", "包内缺清单或签名文件",
                    suggestion="这不是 OmniSpace 升级包，请从正规渠道下载")
            for n in names:
                try:
                    c._rel_parts(n)
                except c.UnsafePathError as exc:
                    raise UpgradeError(
                        "UPGRADE_PACKAGE_BAD", f"包内路径不安全: {exc}") from exc
            try:
                manifest = json.loads(zf.read(c.MANIFEST_NAME).decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise UpgradeError(
                    "UPGRADE_PACKAGE_BAD", f"清单不是合法 JSON: {exc}") from exc
            sig = zf.read(c.SIG_NAME)
    except zipfile.BadZipFile as exc:
        raise UpgradeError(
            "UPGRADE_PACKAGE_BAD", f"不是有效的升级包（zip）: {exc}",
            suggestion="请确认下载完整（.upg 文件），或重新下载") from exc
    try:
        c.validate_manifest(manifest)
    except c.ManifestError as exc:
        raise UpgradeError("UPGRADE_MANIFEST_BAD", f"清单不合法: {exc}") from exc
    return manifest, sig


def verify_signature(manifest: dict[str, Any], sig: bytes) -> None:
    """Ed25519 验签（签名对象=清单规范化字节）。不通过抛 UpgradeError。"""
    if not UPGRADE_PUBKEY_HEX:
        raise UpgradeError(
            "UPGRADE_KEY_NOT_CONFIGURED",
            "本构建未固化升级验签公钥（开发环境常态），拒绝放行",
            detail={"hint": "出包前由 make_dist 注入或随 backend 代码固化"})
    c = core()
    pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(UPGRADE_PUBKEY_HEX))
    try:
        pub.verify(sig, c.canonical_manifest_bytes(manifest))
    except InvalidSignature as exc:
        raise UpgradeError(
            "UPGRADE_SIGNATURE_INVALID", "升级包签名验证失败",
            suggestion="包可能被篡改或不是官方出品，请从正规渠道重新下载"
        ) from exc


def check_compatibility(manifest: dict[str, Any],
                        cur: dict[str, Any] | None = None) -> tuple[bool, str]:
    """包与本副本的兼容判定：(可否, 人话原因；可兼容时原因为空)。

    规则（按序）：开发形态拒绝 → 同构建号拒绝 → 目标版本不高于当前拒绝
    → from 区间（左闭右开）校验。
    """
    c = core()
    cur = cur or current_info()
    if cur["edition"] != "release":
        return False, "开发环境请用 git 更新代码，升级包仅用于正式安装"
    if manifest.get("to_build_id") == cur["build"]:
        return False, "当前已是该构建（无需升级）"
    if c.version_cmp(manifest.get("to_version", ""), cur["version"]) <= 0:
        return False, (f"目标版本 {manifest.get('to_version')} 不高于当前 "
                       f"{cur['version']}（禁止降级）")
    try:
        in_range = c.version_in_range(
            cur["version"], manifest.get("from_min", ""),
            manifest.get("from_max", ""))
    except c.UpgradeCoreError:
        return False, "清单版本字段不可解析"
    if not in_range:
        return False, (f"当前 {cur['version']} 不在包兼容区间 "
                       f"[{manifest.get('from_min')}, {manifest.get('from_max')})")
    return True, ""


def extract_verified(zip_path: Path, work_dir: Path) -> dict[str, Any]:
    """安全解包到 work_dir 并逐文件核验（导入/开始升级时的深验）。

    zip-slip 防护：逐条目 safe_join；受保护区（data/updates/logs）禁令
    由 core.verify_extracted 复核。全部通过返回清单摘要，否则抛
    UpgradeError(UPGRADE_CONTENT_MISMATCH)。
    """
    c = core()
    manifest, _sig = read_package(zip_path)
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                # 符号链接条目直接拒绝（Windows 下 zip 造不出合法场景）
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise UpgradeError(
                        "UPGRADE_PACKAGE_BAD",
                        f"包内含符号链接条目: {info.filename}")
                target = c.safe_join(work_dir, info.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as fsrc, open(target, "wb") as fdst:
                    while True:
                        chunk = fsrc.read(1 << 20)
                        if not chunk:
                            break
                        fdst.write(chunk)
    except c.UnsafePathError as exc:
        raise UpgradeError("UPGRADE_PACKAGE_BAD", f"包内路径不安全: {exc}") from exc
    problems = c.verify_extracted(work_dir, manifest)
    if problems:
        raise UpgradeError(
            "UPGRADE_CONTENT_MISMATCH",
            "包内容与清单不符（文件被改动或下载不完整）",
            detail={"problems": problems[:10]},
            suggestion="请重新下载升级包")
    return summarize(manifest, zip_path)


def summarize(manifest: dict[str, Any], zip_path: Path | None = None) -> dict[str, Any]:
    """清单摘要（扫描/前端展示用，不含全量 files）。"""
    files = manifest.get("files", [])
    n_replace = sum(1 for e in files if e.get("action") == "replace")
    out: dict[str, Any] = {
        "to_version": manifest.get("to_version"),
        "to_build_id": manifest.get("to_build_id"),
        "from_min": manifest.get("from_min"),
        "from_max": manifest.get("from_max"),
        "created": manifest.get("created"),
        "contains_migration": manifest.get("contains_migration"),
        "payload_bytes": manifest.get("payload_bytes"),
        "files_replace": n_replace,
        "files_delete": len(files) - n_replace,
    }
    if zip_path is not None:
        try:
            out["package_bytes"] = zip_path.stat().st_size
            out["mtime"] = zip_path.stat().st_mtime
        except OSError:
            pass
    return out


def scan_updates() -> list[dict[str, Any]]:
    """扫描 updates/ 下全部 .upg：逐包验签+兼容判定，单包异常不拖垮整体。

    轻量模式：只验清单签名（快）；逐文件哈希深验在导入/开始升级时做
    （大包全量哈希耗时，不该每次扫描都烧）。
    """
    results: list[dict[str, Any]] = []
    udir = updates_dir()
    if not udir.is_dir():
        return results
    for pkg in sorted(udir.glob("*" + core().PACKAGE_EXT)):
        entry: dict[str, Any] = {
            "file": pkg.name, "path": str(pkg),
            "valid": False, "reason": "",
            "signature_ok": False,
            "compatible": False, "compat_reason": "",
            "manifest": None,
        }
        try:
            manifest, sig = read_package(pkg)
            entry["manifest"] = summarize(manifest, pkg)
            verify_signature(manifest, sig)
            entry["signature_ok"] = True
            ok, reason = check_compatibility(manifest)
            entry["compatible"] = ok
            entry["compat_reason"] = reason
            entry["valid"] = ok
            entry["reason"] = reason
        except UpgradeError as exc:
            entry["reason"] = str(exc)
        except OSError as exc:
            entry["reason"] = f"包文件读取失败: {exc}"
        results.append(entry)
    return results


def read_state() -> dict[str, Any]:
    """升级状态（断电自愈判据）；委托共享核心，损坏按空闲处理。"""
    return core().read_state(install_root())


def write_state(state: dict[str, Any]) -> None:
    core().write_state(install_root(), state)
# 本项目仅供学习使用，商业授权请+Q 3559331368
