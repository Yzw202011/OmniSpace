"""资产金库（P6 锁4 / H6 方案 A 2026-10-24）：IP 资产的双格式加密装载。

大白话：值钱的提示词资产不再以明文进发行包——出包时加密成
assets_enc/*.enc；运行时本模块解密装载。

- **v1（历史格式，永久兼容）**：密钥 = SHA256("asset|v1|" + 发行公钥)。
  公钥随包分发做验签=公开材料，密钥可零成本推导——防护实为混淆
  （R3 高危定谳）。存量包与未升级出包链路继续走此路径。
- **v2（2026-10-24 方案 A）**：独立真随机 32B 资产密钥（发码台 vault
  生成、make_dist 注入编译模块，与公钥无关）——攻击者必须逆向二进制
  才能取钥，不再是公开材料哈希。白盒无完美解，目标是「不可从公开
  材料推导」。
- 开发模式（未注入）：金库关闭，一切走明文原路径（E 盘开发无感）。
- AES-256-GCM，nonce 前置；v1 前缀 OMNIENC1，v2 前缀 OMNIENC2+8B 密钥 id。
- v2 开关=锁芯是否注入 ASSET_KEY_HEX（presence 语义，不设 config 键——
  出包时密钥在则 v2 生效、不在则纯 v1，与注入链单一真源一致）。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

ASSET_DIR = Path(__file__).resolve().parent / "assets_enc"

_MAGIC_V1 = b"OMNIENC1"
MAGIC_V2 = b"OMNIENC2"
KEY_ID_LEN = 8


def asset_key_from_pubkey(pubkey_hex: str) -> bytes:
    """v1 资产密钥 = SHA256("asset|v1|" + 发行公钥)（历史格式永久保留）。"""
    return hashlib.sha256(("asset|v1|" + pubkey_hex).encode("ascii")).digest()


def _key() -> bytes | None:
    from .license_gate import PUBKEY_HEX
    return asset_key_from_pubkey(PUBKEY_HEX) if PUBKEY_HEX else None


def _v2_key() -> tuple[bytes, bytes] | None:
    """v2 密钥（key, key_id)：来自锁芯注入；未注入返回 None（v2 关闭）。"""
    from .license_gate import ASSET_KEY_HEX, ASSET_KEY_ID
    if not ASSET_KEY_HEX or not ASSET_KEY_ID:
        return None
    try:
        key = bytes.fromhex(ASSET_KEY_HEX)
        kid = bytes.fromhex(ASSET_KEY_ID)[:KEY_ID_LEN]
    except ValueError:
        return None
    if len(key) != 32:
        return None
    return key, kid


def encrypt_bytes(data: bytes, pubkey_hex: str) -> bytes:
    """v1 加密（打包侧使用：显式传公钥，不依赖仓库内占位空值）。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    return _MAGIC_V1 + nonce + AESGCM(
        asset_key_from_pubkey(pubkey_hex)).encrypt(nonce, data, None)


def encrypt_bytes_v2(data: bytes, key: bytes, key_id: bytes) -> bytes:
    """v2 加密（打包侧）：独立随机密钥 + 8B 密钥 id（轮换位预留）。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(key) != 32:
        raise ValueError("v2 资产密钥须为 32 字节")
    nonce = os.urandom(12)
    kid = key_id[:KEY_ID_LEN].ljust(KEY_ID_LEN, b"\x00")
    return MAGIC_V2 + kid + nonce + AESGCM(key).encrypt(nonce, data, None)


def decrypt_bytes(blob: bytes, pubkey_hex: str | None = None) -> bytes:
    """解密：按密文前缀分发 v2/v1（v1 永久兼容，v2 需锁芯注入密钥）。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if blob.startswith(MAGIC_V2):
        pair = _v2_key()
        if pair is None:
            raise ValueError("资产金库 v2 密钥未注入（无法解密 v2 资产）")
        key, kid = pair
        blob_kid = blob[len(MAGIC_V2):len(MAGIC_V2) + KEY_ID_LEN]
        if blob_kid != kid:
            raise ValueError(
                f"v2 资产密钥 id 不匹配（密文 {blob_kid.hex()} ≠ 注入 "
                f"{kid.hex()}）——包与密钥非同批")
        nonce_start = len(MAGIC_V2) + KEY_ID_LEN
        nonce, body = blob[nonce_start:nonce_start + 12], blob[nonce_start + 12:]
        return AESGCM(key).decrypt(nonce, body, None)
    if not blob.startswith(_MAGIC_V1):
        raise ValueError("密文格式不正确")
    key = asset_key_from_pubkey(pubkey_hex) if pubkey_hex else _key()
    if key is None:
        raise RuntimeError("资产金库未启用（无发行公钥）")
    nonce, body = blob[8:20], blob[20:]
    return AESGCM(key).decrypt(nonce, body, None)


def read_asset(filename: str) -> bytes | None:
    """读取 assets_enc 下加密资产（v2/v1 自动分发）；金库关闭/文件缺失/
    解密失败一律返回 None（调用方回退明文——v2 密钥未注入时的降级面）。"""
    path = ASSET_DIR / filename
    if not path.is_file():
        return None
    try:
        return decrypt_bytes(path.read_bytes())
    except Exception:  # noqa: BLE001 - 密钥不符/损坏：按缺失处理走明文回退
        return None
# 本项目仅供学习使用，商业授权请+Q 3559331368
