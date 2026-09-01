"""资产金库（P6 锁4）：工作流/风格库等 IP 资产的加密装载。

大白话：值钱的提示词资产不再以明文进发行包——出包时用「公钥派生密钥」
加密成 assets_enc/*.enc；运行时本模块解密装载。密钥由发行公钥派生，
而公钥只存在于被 Cython 编译过的 license_gate 里——拔钥匙=逆向二进制。

- 开发模式（未注入公钥）：金库关闭，一切走明文原路径（E 盘开发无感）。
- AES-256-GCM，nonce 前置；密文格式 v1。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

ASSET_DIR = Path(__file__).resolve().parent / "assets_enc"


def asset_key_from_pubkey(pubkey_hex: str) -> bytes:
    """资产密钥 = SHA256("asset|v1|" + 发行公钥)。公钥本身保密于编译模块。"""
    return hashlib.sha256(("asset|v1|" + pubkey_hex).encode("ascii")).digest()


def _key() -> bytes | None:
    from .license_gate import PUBKEY_HEX
    return asset_key_from_pubkey(PUBKEY_HEX) if PUBKEY_HEX else None


def encrypt_bytes(data: bytes, pubkey_hex: str) -> bytes:
    """加密（打包侧使用：显式传公钥，不依赖仓库内占位空值）。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    return b"OMNIENC1" + nonce + AESGCM(
        asset_key_from_pubkey(pubkey_hex)).encrypt(nonce, data, None)


def decrypt_bytes(blob: bytes, pubkey_hex: str | None = None) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not blob.startswith(b"OMNIENC1"):
        raise ValueError("密文格式不正确")
    key = asset_key_from_pubkey(pubkey_hex) if pubkey_hex else _key()
    if key is None:
        raise RuntimeError("资产金库未启用（无发行公钥）")
    nonce, body = blob[8:20], blob[20:]
    return AESGCM(key).decrypt(nonce, body, None)


def read_asset(filename: str) -> bytes | None:
    """读取 assets_enc 下加密资产；金库关闭/文件缺失返回 None（调用方回退明文）。"""
    path = ASSET_DIR / filename
    if not path.is_file() or _key() is None:
        return None
    try:
        return decrypt_bytes(path.read_bytes())
    except Exception:  # noqa: BLE001 - 密钥不符/损坏：按缺失处理走明文回退
        return None
