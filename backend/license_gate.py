"""客户端激活门禁（P5-客户端侧）：产品内的「锁芯」。

大白话：这个模块装在卖出去的产品里（会被 Cython 编译成二进制），
内嵌发行公钥；启动和定期都拿本机指纹对授权文件里的激活码做本地验签
（离线、不联网）。没激活 → 后端业务 API 全部 403，只放行健康检查、
激活接口和前端页面。

设计要点：
- PUBKEY_HEX 由 make_dist --pubkey 在出包时注入；空值 = 开发模式
  （门禁完全旁路，E 盘开发环境与金母版基线不受影响）。
- 授权文件 = 激活码原文（data/license.bin）。「码即凭证」：每次验签
  重新跑 Ed25519 + 指纹比对，改一个字都过不了，无需额外哈希树。
- 指纹采集（主板/CPUID/系统盘卷序列号）结果缓存 5 分钟，
  验证状态缓存 10 秒——中间件每请求查询走缓存，不拖慢业务。
- 验证逻辑与授权管理台 license_console.crypto_core 同源同构
  （含 2/3 指纹容错），此处为进包的独立副本。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import struct
import subprocess
import threading
import time

from .config import DATA_DIR

logger = logging.getLogger("omnispace.license")

# make_dist --pubkey 注入（占位空 = 开发模式不启用门禁）
PUBKEY_HEX = ""

_LICENSE_FILE = DATA_DIR / "license.bin"
_FP_TTL_S = 300
_CHECK_TTL_S = 10

_lock = threading.RLock()  # 重入锁：is_activated 持锁时会调 collect_fingerprints（同样加锁）
_cache: dict = {"fp": None, "fp_at": 0.0, "ok": None, "checked_at": 0.0,
                "info": None, "reason": ""}

_TYPE_NAMES = {1: "buyout", 2: "dev"}


class GateError(Exception):
    """激活失败（message 面向客户说人话）。"""


# ── 指纹采集（客户端独立实现，与管理台同规范）──────────────────
# 三件套（2026-09-03 熵修复）= MachineGuid + 主板UUID + CPUID：
# 同配置设备最多对上 CPU 一项（1/3 < 2 拒绝）；换/加硬盘三件全不动，
# 换系统盘重装也只丢 MachineGuid 一项（2/3 仍过）。旧三件套（主板
# 序列号+卷序列号+CPUID）已废：占位串与型号级值可致同配置设备 2/3
# 通配（实测本机主板序列号即 "Default string"）。

_PS_FINGERPRINT = r"""
$g = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid
$u = (Get-CimInstance Win32_ComputerSystemProduct).UUID
$c = (Get-CimInstance Win32_Processor).ProcessorId
Write-Output "$g"
Write-Output "$u"
Write-Output "$c"
"""

# 占位串黑名单（采集遇到=无效，绝不参与匹配——激活将以人话失败）
_PLACEHOLDER = {
    "", "default string", "none", "null", "n/a",
    "to be filled by o.e.m.", "system serial number",
    "0123456789", "1234567890", "123456789",
}
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _fp_component_ok(kind: str, val: str) -> bool:
    v = (val or "").strip()
    if v.lower() in _PLACEHOLDER:
        return False
    if kind in ("guid", "uuid"):
        if not _GUID_RE.match(v):
            return False
        hexes = v.replace("-", "")
        return hexes not in ("0" * 32, "f" * 32)
    if kind == "cpu":
        return len(v) >= 8 and all(ch in "0123456789ABCDEFabcdef" for ch in v)
    return True


def _fp_digest(component: str) -> str:
    return hashlib.sha256(component.encode("utf-8")).hexdigest()[:32]


def collect_fingerprints(force: bool = False) -> list[str]:
    """本机三件套指纹（32 位 hex ×3），缓存 5 分钟。"""
    with _lock:
        if (not force and _cache["fp"] is not None
                and time.time() - _cache["fp_at"] < _FP_TTL_S):
            return _cache["fp"]
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", _PS_FINGERPRINT],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        parts = [x.strip() for x in r.stdout.splitlines() if x.strip()][:3]
        kinds = ["guid", "uuid", "cpu"]
        # 读不全 / 任一项为占位串：给确定性占位指纹（激活必然以
        # 「设备不匹配」人话失败，绝不静默放行，也绝不拿占位串匹配）
        if (r.returncode != 0 or len(parts) < 3
                or not all(_fp_component_ok(k, v)
                           for k, v in zip(kinds, parts, strict=False))):
            parts = ["<unreadable>"] * 3
        _cache["fp"] = [_fp_digest(p) for p in parts]
        _cache["fp_at"] = time.time()
        return _cache["fp"]


# ── 验码（与管理台 crypto_core.verify_code 同构的进包副本）──────

def _verify_code(code: str, local_fps: list[str], pub_raw: bytes,
                 min_match: int = 2) -> dict:
    compact = code.replace("-", "").replace(" ", "").strip().upper()
    try:
        pad = "=" * (-len(compact) % 8)
        raw = base64.b32decode(compact + pad)
        payload, sig = raw[:-64], raw[-64:]
    except Exception as exc:  # noqa: BLE001
        raise GateError("激活码格式不正确，请完整复制后重试") from exc
    if len(payload) != 62:
        raise GateError("激活码长度不正确，请核对是否复制完整")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(pub_raw).verify(sig, payload)
    except Exception as exc:  # noqa: BLE001
        raise GateError("激活码签名校验失败（可能被篡改或非官方发放）") from exc
    version, lic_type = payload[0], payload[1]
    if version != 1:
        raise GateError(f"激活码版本不兼容（v{version}），请升级软件")
    fp_payload = [payload[10 + i * 16:26 + i * 16].hex() for i in range(3)]
    y, m, d = struct.unpack(">HBB", payload[58:62])
    match = len(set(fp_payload) & set(local_fps))
    if match < min_match:
        raise GateError(
            f"激活码与当前设备不匹配（匹配 {match}/3，需 ≥{min_match}）。"
            "换机请联系卖家换绑")
    return {"serial": payload[2:10].hex(),
            "type": _TYPE_NAMES.get(lic_type, "?"),
            "issued": f"{y:04d}-{m:02d}-{d:02d}", "fp_match": match}


# ── 门禁状态 ───────────────────────────────────────────────────

def gate_enabled() -> bool:
    return bool(PUBKEY_HEX)


def is_activated() -> tuple[bool, str]:
    """（门禁启用时）当前是否持有效授权。返回 (ok, 失败原因人话)。"""
    if not gate_enabled():
        return True, ""
    with _lock:
        if (_cache["ok"] is not None
                and time.time() - _cache["checked_at"] < _CHECK_TTL_S):
            return _cache["ok"], _cache["reason"]
        ok, reason = False, "尚未激活"
        try:
            code = _LICENSE_FILE.read_text(encoding="utf-8").strip()
            info = _verify_code(code, collect_fingerprints(),
                                bytes.fromhex(PUBKEY_HEX))
            ok, reason = True, ""
            _cache["info"] = info
        except FileNotFoundError:
            reason = "尚未激活"
        except GateError as exc:
            reason = str(exc)
        except Exception as exc:  # noqa: BLE001 - 授权文件损坏等
            reason = f"授权状态异常：{exc}"
            logger.warning("授权校验异常：%s", exc)
        _cache.update(ok=ok, reason=reason, checked_at=time.time())
        return ok, reason


def activate(code: str) -> dict:
    """激活：验码通过后把激活码落为授权文件（码即凭证）。"""
    if not gate_enabled():
        raise GateError("当前构建未启用激活门禁")
    code = (code or "").strip()
    info = _verify_code(code, collect_fingerprints(force=True),
                        bytes.fromhex(PUBKEY_HEX))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _LICENSE_FILE.write_text(code, encoding="utf-8")
    with _lock:
        _cache.update(ok=True, reason="", info=info, checked_at=time.time())
    logger.info("产品已激活：serial=%s type=%s", info["serial"], info["type"])
    return info


def deactivate() -> None:
    """（调试/售后）摘除授权文件并失效缓存。"""
    _LICENSE_FILE.unlink(missing_ok=True)
    with _lock:
        _cache.update(ok=None, reason="尚未激活", info=None,
                      checked_at=time.time())


def status() -> dict:
    ok, reason = is_activated()
    return {"gate_enabled": gate_enabled(), "activated": ok,
            "reason": reason if not ok else "",
            "fingerprints": collect_fingerprints(),
            "license": _cache.get("info"),
            "license_file": str(_LICENSE_FILE)}
# 本项目仅供学习使用，商业授权请+Q 3559331368
