"""字段级数据加密（要求#36「用户数据隐私：本地加密存储」落地）。

设计：
- 算法：AES-256-GCM（cryptography 库，pydeps 已含 50.0）
- 密钥：随机 32 字节数据密钥，落盘前经 Windows DPAPI（CurrentUser 域）
  保护存储于 data/keys/dbkey.bin——密钥与当前机器用户绑定，拷贝到其他
  机器/账户不可解；非 Windows 或 DPAPI 不可用时回退为「机器指纹 HKDF
  派生」（注册表 MachineGuid + 用户名 + 固定盐），并在状态中如实标注
  保护等级（dpapi / machine-derived）
- 密文格式："enc:v1:" + base64(nonce12 || ciphertext||tag16)
- 迁移安全：decrypt_text 对无 "enc:v1:" 前缀的历史明文原样透传；
  解密失败返回空串并记 warning，绝不使读取路径崩溃

使用边界（诚实声明）：
- 已加密：dialog_messages.content、behavior_logs 的
  content/context/before/after（用户对话与行为细节）
- 不加密：知识库正文（FTS5 全文索引与向量检索依赖明文，且知识源自
  公开网页敏感度低）、会话标题、时间戳等元数据
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wintypes
import logging
import os
import threading

from ..config import DATA_DIR

log = logging.getLogger("omnispace.data.crypto")

_PREFIX = "enc:v1:"
_NONCE_LEN = 12
_KEY_LEN = 32
_KEY_DIR = DATA_DIR / "keys"
_KEY_FILE = _KEY_DIR / "dbkey.bin"
# DPAPI 附加熵：应用级固定盐，绑定到本应用用途
_ENTROPY = b"OmniSpaceAI.dbkey.v1"

_key_cache: bytes | None = None
_key_lock = threading.Lock()
_protection: str = "unavailable"   # dpapi | machine-derived | unavailable
_available: bool | None = None     # 惰性初始化结果缓存


# ── DPAPI（ctypes 直调 crypt32）───────────────────────────────────

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_from(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob._buf = buf  # type: ignore[attr-defined]  # 防 GC
    return blob


def _blob_bytes(blob: _DATA_BLOB) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _dpapi_protect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    in_b = _blob_from(data)
    ent_b = _blob_from(_ENTROPY)
    out_b = _DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(in_b), None,
                                    ctypes.byref(ent_b), None, None, 0,
                                    ctypes.byref(out_b)):
        raise OSError("CryptProtectData 失败")
    try:
        return _blob_bytes(out_b)
    finally:
        kernel32.LocalFree(out_b.pbData)


def _dpapi_unprotect(blob: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    in_b = _blob_from(blob)
    ent_b = _blob_from(_ENTROPY)
    out_b = _DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(in_b), None,
                                      ctypes.byref(ent_b), None, None, 0,
                                      ctypes.byref(out_b)):
        raise OSError("CryptUnprotectData 失败（密钥与本机用户不匹配？）")
    try:
        return _blob_bytes(out_b)
    finally:
        kernel32.LocalFree(out_b.pbData)


# ── 密钥管理 ──────────────────────────────────────────────────────

def _machine_fingerprint() -> bytes:
    """机器指纹：注册表 MachineGuid + 用户名（非 Windows 时退化主机名）。"""
    parts: list[str] = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography") as k:
            parts.append(str(winreg.QueryValueEx(k, "MachineGuid")[0]))
    except Exception:  # noqa: BLE001
        import socket
        parts.append(socket.gethostname())
    parts.append(os.environ.get("USERNAME") or os.environ.get("USER") or "")
    return "|".join(parts).encode("utf-8")


def _derive_key_machine() -> bytes:
    """回退路径：HKDF-SHA256(机器指纹, 固定盐) → 32 字节密钥。"""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA256(), length=_KEY_LEN,
                salt=b"OmniSpaceAI.fieldkey.v1",
                info=b"field-encryption").derive(_machine_fingerprint())


def _load_or_create_key() -> tuple[bytes, str]:
    """加载/创建数据密钥，返回 (key, protection_level)。"""
    # 1) 优先 DPAPI 保护的密钥文件
    if os.name == "nt":
        try:
            if _KEY_FILE.is_file():
                return _dpapi_unprotect(_KEY_FILE.read_bytes()), "dpapi"
            key = os.urandom(_KEY_LEN)
            _KEY_DIR.mkdir(parents=True, exist_ok=True)
            _KEY_FILE.write_bytes(_dpapi_protect(key))
            try:
                os.chmod(_KEY_FILE, 0o600)
            except OSError:
                log.debug("_load_or_create_key: 降级忽略", exc_info=True)
            log.info("字段加密密钥已生成（DPAPI 保护）: %s", _KEY_FILE)
            return key, "dpapi"
        except Exception as exc:  # noqa: BLE001
            log.warning("DPAPI 密钥路径不可用，回退机器指纹派生: %s", exc, exc_info=True)
    # 2) 回退：机器指纹派生（不落盘，随用随算）
    return _derive_key_machine(), "machine-derived"


def _ensure_key() -> bytes | None:
    global _key_cache, _protection, _available
    if _available is not None:
        return _key_cache
    with _key_lock:
        if _available is not None:
            return _key_cache
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
            _key_cache, _protection = _load_or_create_key()
            _available = True
            log.info("字段级加密已启用（保护等级: %s）", _protection)
        except Exception as exc:  # noqa: BLE001
            _key_cache = None
            _protection = "unavailable"
            _available = False
            log.warning("字段级加密不可用（cryptography 缺失？）: %s", exc, exc_info=True)
    return _key_cache


_encrypt_fail_reported = False  # B10：加密失败事件每进程仅报一次

# ── 加解密 API ────────────────────────────────────────────────────

def _report_encrypt_failure(detail: str) -> None:
    """B10：加密失败升格用户时间线告警（每进程去重一次，防刷屏）。"""
    global _encrypt_fail_reported
    if _encrypt_fail_reported:
        return
    _encrypt_fail_reported = True
    try:
        from ..services.event_log import log_event
        log_event("system", "encrypt_failed",
                  "数据加密模块异常，新数据将暂时以明文保存。"
                  "重启应用通常可恢复；如反复出现请联系售后。",
                  level="warning", detail=detail[:200])
    except Exception:  # noqa: BLE001
        log.debug("_report_encrypt_failure: 降级忽略", exc_info=True)


def encrypt_text(plain: str | None) -> str | None:
    """加密文本字段；None/空串原样返回，加密不可用时明文透传。"""
    if not plain:
        return plain
    key = _ensure_key()
    if key is None:
        # B10：密钥不可用（DPAPI 故障/密钥缺失）=生产最常见失败形态，
        # 与 AESGCM 异常路径同告警（每进程去重一次）
        _report_encrypt_failure("encryption key unavailable")
        return plain
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(_NONCE_LEN)
        ct = AESGCM(key).encrypt(nonce, plain.encode("utf-8"), None)
        return _PREFIX + base64.b64encode(nonce + ct).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        # B10（2026-09-14）：加密失败=明文落库（可用性优先），升格为
        # 用户时间线告警（每进程去重一次，防刷屏）。
        global _encrypt_fail_reported
        if not _encrypt_fail_reported:
            _encrypt_fail_reported = True
            try:
                from ..services.event_log import log_event
                log_event("system", "encrypt_failed",
                          "数据加密模块异常，新数据将暂时以明文保存。"
                          "重启应用通常可恢复；如反复出现请联系售后。",
                          level="warning", detail=str(exc)[:200])
            except Exception:  # noqa: BLE001
                log.debug("encrypt_text: 降级忽略", exc_info=True)
        log.warning("字段加密失败（明文落库）: %s", exc, exc_info=True)
        return plain


def decrypt_text(value: str | None) -> str | None:
    """解密文本字段；非密文（历史明文）原样透传，失败返回空串。"""
    if not value or not isinstance(value, str) or not value.startswith(_PREFIX):
        return value
    key = _ensure_key()
    if key is None:
        log.warning("字段解密失败：加密不可用")
        return ""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw = base64.b64decode(value[len(_PREFIX):])
        nonce, ct = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("字段解密失败（返回空串）: %s", exc, exc_info=True)
        return ""


def cache_hmac_key() -> bytes:
    """缓存完整性校验密钥（B8 2026-09-14）：机器指纹派生——同机本应用
    写入的缓存可过验；文件被替换/伪造（他机/手改）验签不过即拒载。"""
    import hashlib
    import hmac as _hmac
    return _hmac.new(b"omnispace.cache-integrity",
                     _derive_key_machine(), hashlib.sha256).digest()


def is_encrypted(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


def crypto_status() -> dict:
    """加密能力状态（如实上报保护等级）。"""
    key = _ensure_key()
    return {
        "enabled": key is not None,
        "algorithm": "AES-256-GCM" if key is not None else "",
        "key_protection": _protection,
        "scope": "dialog_messages.content, behavior_logs.{content,context,before,after}",
    }
# 本项目仅供学习使用，商业授权请+Q 3559331368
