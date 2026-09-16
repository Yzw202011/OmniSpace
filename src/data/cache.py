"""OmniSpace AI v2.1 缓存管理（规格 §7 model_cache / §5.1 调度缓存）。

优先使用 Redis；不可用时降级为内存 LRU 缓存。
支持 LZ4 压缩（如可用）以减少大对象内存占用。
提供 get / set / delete / clear 方法。

规格引用：
  - §7 model_cache.compression = lz4 / eviction_policy = lru
  - §5.1 智能调度：模型缓存 L2 管理
  - §14 约束2：仅本地，Redis 绑定 127.0.0.1
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import hashlib
import hmac as _hmac_mod
import logging
import pickle
import threading
import time
from collections import OrderedDict
from typing import Any

from ..config import CACHE_COMPRESSION

log = logging.getLogger("omnispace.cache")

# ── LZ4 可用性探测 ──────────────────────────────────────────
_LZ4_AVAILABLE = False
try:
    import lz4.frame as lz4_frame  # type: ignore
    _LZ4_AVAILABLE = True
except Exception:  # pragma: no cover - 降级路径
    pass

# ── B8 缓存完整性（HMAC 签名，2026-09-14）──────────────────
# L2 落盘数据统一签名：非本机本应用写入（替换/伪造/他机拷贝）
# 验签不过即拒载。密钥=机器指纹派生（crypto.cache_hmac_key）。
_CACHE_MAGIC = b"OMNI1"
_HMAC_SIG_LEN = 32  # sha256 hex 前 32 字节二进制（digest 前 32B）


def _cache_hmac_key() -> bytes:
    try:
        from ..data.crypto import cache_hmac_key
        return cache_hmac_key()
    except Exception:  # noqa: BLE001 - 密钥不可用=跳过签名（同机自写自读）
        return b""


def _sign(payload: bytes) -> bytes:
    key = _cache_hmac_key()
    if not key:
        return payload
    return _hmac_mod.new(key, payload, hashlib.sha256).digest()[:16]


def _wrap_signed(payload: bytes) -> bytes:
    sig = _sign(payload)
    return _CACHE_MAGIC + sig + payload if sig else payload


def _verify_and_strip(data: bytes) -> bytes | None:
    if not data.startswith(_CACHE_MAGIC):
        return data  # 旧版无签名数据（兼容读一轮后自然淘汰）
    key = _cache_hmac_key()
    if not key:
        return None
    sig = data[len(_CACHE_MAGIC):len(_CACHE_MAGIC) + 16]
    body = data[len(_CACHE_MAGIC) + 16:]
    expected = _hmac_mod.new(key, body, hashlib.sha256).digest()[:16]
    return body if _hmac_mod.compare_digest(sig, expected) else None


# ── Redis 可用性探测 ────────────────────────────────────────
_REDIS_AVAILABLE = False
try:
    import redis  # type: ignore
    _REDIS_AVAILABLE = True
except Exception:  # pragma: no cover - 降级路径
    pass


# ═══════════════════════════════════════════════════════════════════
#  内存 LRU 缓存
# ═══════════════════════════════════════════════════════════════════

class MemoryCache:
    """线程安全的 LRU 内存缓存，支持 TTL 与可选压缩。"""

    def __init__(self, max_size: int = 512, default_ttl: int = 0) -> None:
        self._max_size = max_size
        self._default_ttl = default_ttl      # 0 = 永不过期
        self._store: OrderedDict[str, tuple[bytes, float]] = OrderedDict()
        self._lock = threading.RLock()
        self._compress = _LZ4_AVAILABLE and CACHE_COMPRESSION == "lz4"
        self._hits = 0
        self._misses = 0

    def _serialize(self, value: Any) -> bytes:
        """序列化 + 可选压缩。

        信任边界（审计 09-10 P2-4）：pickle 仅用于**进程内自写自读**的
        缓存值（任意 Python 对象，含 bytes/对象，故不用 JSON）；缓存
        数据源全部是本机服务自身产出，不接受外部输入作为缓存键值，
        不存在反序列化不可信数据的通路。
        """
        raw = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        if self._compress:
            try:
                return lz4_frame.compress(raw)
            except Exception:
                return raw
        return raw

    def _deserialize(self, data: bytes) -> Any:
        """反序列化 + 可选解压（信任边界见 _serialize，审计 09-10 P2-4）。"""
        if self._compress:
            try:
                data = lz4_frame.decompress(data)
            except Exception:
                pass
        return pickle.loads(data)

    def get(self, key: str) -> Any | None:
        """读取缓存，未命中返回 None。"""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            data, expire_at = entry
            if expire_at > 0 and time.time() > expire_at:
                del self._store[key]
                self._misses += 1
                return None
            # LRU：移到末尾
            self._store.move_to_end(key)
            self._hits += 1
            try:
                return self._deserialize(data)
            except Exception:
                self._misses += 1
                return None

    def set(self, key: str, value: Any, ttl: int = 0) -> bool:
        """写入缓存，ttl=0 使用默认值，负数=永不过期。"""
        ttl = ttl or self._default_ttl
        expire_at = (time.time() + ttl) if ttl > 0 else 0.0
        with self._lock:
            # 驱逐
            while len(self._store) >= self._max_size and key not in self._store:
                self._store.popitem(last=False)
            self._store[key] = (self._serialize(value), expire_at)
            self._store.move_to_end(key)
        return True

    def delete(self, key: str) -> bool:
        """删除指定键，返回是否曾存在。"""
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    def clear(self) -> None:
        """清空所有缓存。"""
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> dict:
        """返回缓存统计信息。"""
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._store),
                "max_size": self._max_size,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 4) if total else 0.0,
                "compression": "lz4" if self._compress else "none",
                "backend": "memory",
            }


# ═══════════════════════════════════════════════════════════════════
#  Redis 缓存（降级时回退到内存）
# ═══════════════════════════════════════════════════════════════════

class RedisCache:
    """Redis 缓存封装，连接失败时自动降级到内存缓存。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 6379,
                 max_size: int = 512, default_ttl: int = 0) -> None:
        self._default_ttl = default_ttl
        self._fallback = MemoryCache(max_size, default_ttl)
        self._client = None
        self._connected = False
        self._compress = _LZ4_AVAILABLE and CACHE_COMPRESSION == "lz4"
        try:
            self._client = redis.Redis(
                host=host, port=port, decode_responses=False,
                socket_connect_timeout=1, socket_timeout=1,
            )
            self._client.ping()
            self._connected = True
            log.info("Redis 缓存已连接: %s:%d", host, port)
        except Exception:
            log.info("Redis 不可用，缓存降级为内存 LRU")
            self._connected = False
            self._client = None

    def _wrap(self, value: Any) -> bytes:
        raw = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        if self._compress:
            try:
                raw = lz4_frame.compress(raw)
            except Exception:
                pass
        return _wrap_signed(raw)

    def _unwrap(self, data: bytes, key: str = "") -> Any:
        """信任边界（审计 09-10 P2-4）：pickle 只解**本机 Redis 自写**
        的缓存值（本机回环、单用户），不接受外部输入作为缓存内容。"""
        # B8（2026-09-14）：L2 数据 HMAC 验签——非本机本应用写入的
        # 数据（替换/伪造/他机拷贝）拒载并清键，杜绝不可信反序列化。
        body = _verify_and_strip(data)
        if body is None:
            log.warning("L2 缓存验签失败，拒绝反序列化（key=%s）", key)
            try:
                self._client.delete(key)
            except Exception:  # noqa: BLE001
                pass
            return None
        if self._compress:
            try:
                body = lz4_frame.decompress(body)
            except Exception:
                pass
        return pickle.loads(body)

    def get(self, key: str) -> Any | None:
        if not self._connected:
            return self._fallback.get(key)
        try:
            data = self._client.get(key)
            if data is None:
                return None
            return self._unwrap(data, key)
        except Exception:
            return self._fallback.get(key)

    def set(self, key: str, value: Any, ttl: int = 0) -> bool:
        if not self._connected:
            return self._fallback.set(key, value, ttl)
        try:
            ttl = ttl or self._default_ttl
            data = self._wrap(value)
            if ttl > 0:
                self._client.setex(key, ttl, data)
            else:
                self._client.set(key, data)
            return True
        except Exception:
            return self._fallback.set(key, value, ttl)

    def delete(self, key: str) -> bool:
        if not self._connected:
            return self._fallback.delete(key)
        try:
            return bool(self._client.delete(key))
        except Exception:
            return self._fallback.delete(key)

    def clear(self) -> None:
        if not self._connected:
            self._fallback.clear()
            return
        try:
            self._client.flushdb()
        except Exception:
            self._fallback.clear()

    def stats(self) -> dict:
        if self._connected:
            return {"backend": "redis", "compression": "lz4" if self._compress else "none"}
        return self._fallback.stats()


# ═══════════════════════════════════════════════════════════════════
#  统一缓存接口
# ═══════════════════════════════════════════════════════════════════

class CacheManager:
    """缓存管理器：自动选择 Redis 或内存 LRU。"""

    def __init__(self, max_size: int = 512, default_ttl: int = 0) -> None:
        if _REDIS_AVAILABLE:
            self._impl: Any = RedisCache(max_size=max_size, default_ttl=default_ttl)
        else:
            self._impl = MemoryCache(max_size=max_size, default_ttl=default_ttl)

    def get(self, key: str) -> Any | None:
        return self._impl.get(key)

    def set(self, key: str, value: Any, ttl: int = 0) -> bool:
        return self._impl.set(key, value, ttl)

    def delete(self, key: str) -> bool:
        return self._impl.delete(key)

    def clear(self) -> None:
        self._impl.clear()

    def stats(self) -> dict:
        return self._impl.stats()

    @property
    def backend(self) -> str:
        return self._impl.stats().get("backend", "unknown")


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

_cache_instance: CacheManager | None = None
_cache_lock = threading.Lock()


def get_cache() -> CacheManager:
    """获取全局缓存管理器单例。"""
    global _cache_instance
    if _cache_instance is None:
        with _cache_lock:
            if _cache_instance is None:
                _cache_instance = CacheManager()
    return _cache_instance
