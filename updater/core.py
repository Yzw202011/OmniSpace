"""升级机制共享核心（批1，2026-09-08）：包格式/版本/哈希/路径安全。

本文件被三方共同使用，写法约束由此而来：
- updater/updater.py（升级独立进程）——**只准标准库**，且绝不 import
  src/pydeps：升级落位时会替换 pydeps，运行中导入的 .pyd 会被锁死；
- src/services/upgrade_service.py——经 importlib 文件路径加载本文件
  （先例：boot.py 加载 scripts/comfy_link/comfy_mount.py）；
- tools/make_upgrade_package.py（开发侧打包工具）。

Ed25519 验签需要 cryptography，只在 service 侧做（那边有 pydeps）；
本文件负责不依赖三方库的全部契约：清单格式、版本比较、逐文件 SHA256、
zip-slip 路径安全。

包格式（.upg，本质 zip）：
  upgrade_manifest.json  清单（本文件定义其 schema 与校验规则）
  upgrade_manifest.sig   Ed25519 签名（签名对象=清单的规范化字节）
  README.txt             更新说明（用户可读）
  payload/<相对路径>     待落位文件，镜像安装根相对布局
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

PACKAGE_EXT = ".upg"
MANIFEST_NAME = "upgrade_manifest.json"
SIG_NAME = "upgrade_manifest.sig"
README_NAME = "README.txt"
PAYLOAD_DIR = "payload"
MANIFEST_FORMAT = 1

# payload 永不触碰的安装根子目录：data=用户数据（含激活态/作品），
# updates=升级机制自身的工作区（含备份），logs=取证日志
PROTECTED_ROOTS = ("data", "updates", "logs")

# 升级机制自身状态（断电自愈判据，boot 恢复钩子与 updater 共读）
STATE_DIRNAME = "updates"
STATE_FILENAME = "state.json"

# 中断态=boot 启动时看到这几个 phase 要先从备份还原再启动
INTERRUPTED_PHASES = ("backing_up", "applying", "verifying_start")

_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5",
    "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4",
    "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}
_DRIVE_RE = re.compile(r"(?i)^[a-z]:")
_CHUNK = 1 << 20


class UpgradeCoreError(ValueError):
    """共享核心的错误基类（路径不安全/清单不合法/版本不可解析）。"""


class UnsafePathError(UpgradeCoreError):
    """包内路径试图逃逸目标根（zip-slip / 绝对路径 / 盘符 / 保留名）。"""


class ManifestError(UpgradeCoreError):
    """升级清单缺字段或字段非法。"""


# ── 版本比较 ─────────────────────────────────────────────────────

def parse_version(s: str) -> tuple[int, ...]:
    """把 '3.0.0' / '3.0.0+gabc+20260908' 解析成可比较的整数元组。

    加号后是构建尾缀不参与比较；段内非数字（如 '0b1'）截断处理。
    """
    head = s.split("+", 1)[0].strip()
    parts: list[int] = []
    for seg in head.split("."):
        seg = seg.strip()
        if not seg or not seg.isdigit():
            break
        parts.append(int(seg))
    if not parts:
        raise UpgradeCoreError(f"版本号不可解析: {s!r}")
    return tuple(parts)


def version_cmp(a: str, b: str) -> int:
    """版本比较：-1/0/1。短版本右侧补零对齐（(3,0) == (3,0,0)）。"""
    ta, tb = parse_version(a), parse_version(b)
    n = max(len(ta), len(tb))
    ta += (0,) * (n - len(ta))
    tb += (0,) * (n - len(tb))
    return (ta > tb) - (ta < tb)


def version_in_range(v: str, from_min: str, from_max: str) -> bool:
    """兼容区间判定：from_min <= v < from_max（左闭右开）。"""
    return version_cmp(v, from_min) >= 0 and version_cmp(v, from_max) < 0


# ── 路径安全（zip-slip 防护）────────────────────────────────────

def _rel_parts(rel: str) -> tuple[str, ...]:
    """相对路径的纯词法校验（不绑定根目录）：返回规范化段。

    拒绝：绝对路径（/ 或 \\ 开头）、盘符（C:）、UNC（//、\\\\）、
    任何 .. 段、Windows 保留设备名。
    """
    rel_n = rel.replace("\\", "/")
    if not rel_n or rel_n.startswith(("/", "\\")):
        raise UnsafePathError(f"拒绝绝对路径: {rel!r}")
    if _DRIVE_RE.match(rel_n) or rel_n.startswith("//"):
        raise UnsafePathError(f"拒绝盘符/UNC 路径: {rel!r}")
    parts = tuple(p for p in PurePosixPath(rel_n).parts if p != ".")
    if not parts or any(p == ".." for p in parts):
        raise UnsafePathError(f"拒绝包含 .. 的路径: {rel!r}")
    for p in parts:
        stem = p.split(".")[0].upper()
        if stem in _WIN_RESERVED:
            raise UnsafePathError(f"拒绝 Windows 保留名: {rel!r}")
    return parts


def safe_join(root: Path, rel: str) -> Path:
    """把包内相对路径安全落到 root 下，任何逃逸直接拒绝。

    词法校验（_rel_parts）之外，返回前再 resolve 复核确保仍在 root
    内（大小写与符号链接归一后的最终防线）。
    """
    parts = _rel_parts(rel)
    target = (root.joinpath(*parts)).resolve()
    root_r = root.resolve()
    try:
        target.relative_to(root_r)
    except ValueError as exc:
        raise UnsafePathError(f"路径逃逸目标根: {rel!r}") from exc
    return target


def payload_target(install_root: Path, rel: str) -> Path:
    """payload 条目的落位路径：safe_join 基础上再加保护区禁令。"""
    target = safe_join(install_root, rel)
    top = PurePosixPath(rel.replace("\\", "/")).parts[0].lower()
    if top in PROTECTED_ROOTS:
        raise UnsafePathError(f"payload 禁止触碰受保护区: {rel!r}")
    return target


# ── 哈希 ─────────────────────────────────────────────────────────

def sha256_file(path: Path) -> str:
    """流式全文件 SHA256（1MB 分块，大依赖文件不吃内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


# ── 清单 schema 与校验 ──────────────────────────────────────────

def canonical_manifest_bytes(m: dict[str, Any]) -> bytes:
    """清单规范化字节：签名的唯一对象（排序键+紧凑分隔+UTF-8）。

    验签方对 json.load 的结果重新规范化后比对，因此清单文件本身
    允许带缩进，只要内容一致签名即有效。
    """
    return json.dumps(m, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def validate_manifest(m: dict[str, Any]) -> None:
    """清单结构校验：字段齐全、类型正确、动作合法。不合规抛 ManifestError。"""
    if not isinstance(m, dict):
        raise ManifestError("清单必须是 JSON 对象")
    if m.get("format") != MANIFEST_FORMAT:
        raise ManifestError(f"清单 format 不支持: {m.get('format')!r}")
    for key in ("from_min", "from_max", "to_version", "to_build_id", "created"):
        if not isinstance(m.get(key), str) or not m[key]:
            raise ManifestError(f"清单缺字符串字段: {key}")
    if not isinstance(m.get("contains_migration"), bool):
        raise ManifestError("contains_migration 必须是布尔值")
    if not isinstance(m.get("payload_bytes"), int) or m["payload_bytes"] < 0:
        raise ManifestError("payload_bytes 必须是非负整数")
    if not isinstance(m.get("files"), list) or not m["files"]:
        raise ManifestError("files 必须是非空列表")
    seen: set[str] = set()
    for i, ent in enumerate(m["files"]):
        if not isinstance(ent, dict):
            raise ManifestError(f"files[{i}] 必须是对象")
        path = ent.get("path")
        if not isinstance(path, str) or not path:
            raise ManifestError(f"files[{i}].path 非法")
        if path in seen:
            raise ManifestError(f"files 中路径重复: {path}")
        seen.add(path)
        action = ent.get("action")
        if action not in ("replace", "delete"):
            raise ManifestError(f"files[{i}].action 非法: {action!r}")
        if action == "replace":
            if not isinstance(ent.get("sha256"), str) or len(ent["sha256"]) != 64:
                raise ManifestError(f"files[{i}]（replace）缺合法 sha256")
            if not isinstance(ent.get("size"), int) or ent["size"] < 0:
                raise ManifestError(f"files[{i}]（replace）缺合法 size")
    # 版本字段本身必须可解析（防脏值在安装期才爆）
    for key in ("from_min", "from_max", "to_version"):
        try:
            parse_version(m[key])
        except UpgradeCoreError as exc:
            raise ManifestError(f"{key} 不可解析: {exc}") from exc
    if version_cmp(m["from_min"], m["from_max"]) > 0:
        raise ManifestError("from_min 大于 from_max")


def verify_extracted(work_dir: Path, m: dict[str, Any]) -> list[str]:
    """对已解压到 work_dir/payload 的包做逐文件核验。

    返回问题清单（空列表=全部通过）。校验：replace 条目的 payload
    文件存在、大小一致、SHA256 一致；任何条目不得落入受保护区。
    """
    problems: list[str] = []
    payload_root = work_dir / PAYLOAD_DIR
    for ent in m["files"]:
        rel = ent["path"]
        try:
            _rel_parts(rel)              # 词法合法性（绝对路径/../保留名）
            _assert_not_protected(rel)
            if ent["action"] == "replace":
                src = safe_join(payload_root, rel)
                if not src.is_file():
                    problems.append(f"payload 缺文件: {rel}")
                    continue
                if src.stat().st_size != ent["size"]:
                    problems.append(f"大小不符: {rel}")
                    continue
                if sha256_file(src) != ent["sha256"]:
                    problems.append(f"SHA256 不符（文件被改动）: {rel}")
        except UnsafePathError as exc:
            problems.append(str(exc))
    return problems


def _assert_not_protected(rel: str) -> None:
    top = PurePosixPath(rel.replace("\\", "/")).parts[0].lower()
    if top in PROTECTED_ROOTS:
        raise UnsafePathError(f"清单条目触碰受保护区: {rel!r}")


# ── 升级状态（断电自愈判据）─────────────────────────────────────

def state_path(install_root: Path) -> Path:
    return install_root / STATE_DIRNAME / STATE_FILENAME


def read_state(install_root: Path) -> dict[str, Any]:
    """读升级状态；无文件/损坏一律返回空 dict（视为空闲，绝不抛）。"""
    try:
        data = json.loads(state_path(install_root).read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(install_root: Path, state: dict[str, Any]) -> None:
    """原子写升级状态（临时文件+os.replace，断电不留半截 json）。"""
    p = state_path(install_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, p)
