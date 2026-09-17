"""
分类式 token 编解码层 (64 比特 class-typed token)

一个 token 占 64 比特:
  - 高 32 比特 = token 组 (group): 定义该 token 的语义类别
  - 低 32 比特 = 组内载荷 (low32): 由分组决定其含义

纯文本场景下采用硬性分组:
  - 0x00000000 (CHAR_GROUP): utf8-mb4 字符 token 组 (保留)
        低 32 位直接存放 Unicode 码点 (UTF-8 最长 4 字节,
        码点 ≤ 0x10FFFF < 2^32, 32 位足够容纳, 天然支持
        astral / emoji 等 4 字节字符)

其余分组 (RESERVED_GROUPS) 在后续版本扩展 (如子词/词 token、
控制端等), 本版仅注册文档化保留, 不实现具体语义。

桥接: embed_tokens() 把 64 比特 token 序列确定性折叠为
dim 维脉冲信号, 使 CubeGPT 文本通路可直接消费 class-token
(逐比特确定, 与 spike_codec.encode_text 的 crc32 确定性一致)。
"""

import zlib

import numpy as np

# ── 64 比特 token 位域常量 ─────────────────────────────────────
TOKEN_BITS = 64
MAX_TOK = (1 << TOKEN_BITS) - 1                  # 0xFFFFFFFFFFFFFFFF
GROUP_MASK = 0xFFFFFFFF00000000                  # 高 32 位
LOW32_MASK = 0x00000000FFFFFFFF                  # 低 32 位
GROUP_ID_MASK = 0xFFFFFFFF                        # 高 32 位的 32 位分组 id
UNICODE_MAX = 0x10FFFF                           # utf8-mb4 / Unicode 上限

# ── 硬性分组 (保留): 分组 id 为 32 比特 ────────────────────
# utf8-mb4 字符 token 组: 分组 id 0x00000000 保留
# 低 32 位 = Unicode 码点 (token 形如 0x00000000xxxxxxxx)
CHAR_GROUP = 0x00000000
CHAR_GROUP_NAME = "utf8-mb4-character"

# 其余分组 id: 文档化保留, 语义留待后续版本
RESERVED_GROUPS: dict[int, str] = {
    0x00000001: "subword/word token (保留)",
    0x00000002: "special/control token (保留)",
    0x0000007E: "reserved block (保留)",
}
ALL_GROUPS = {CHAR_GROUP: CHAR_GROUP_NAME, **RESERVED_GROUPS}


def make_token(group: int, low: int) -> int:
    """拼接 64 比特 token: = (group_id << 32) | (low & LOW32_MASK)"""
    return (int(group) & GROUP_ID_MASK) << 32 | (int(low) & LOW32_MASK)


def group_of(tok: int) -> int:
    """取 token 的分组 id (高 32 位, 返回 0~0xFFFFFFFF 的 32 位 id)"""
    return (int(tok) >> 32) & GROUP_ID_MASK


def low_of(tok: int) -> int:
    """取 token 的低 32 位 (组内载荷)"""
    return int(tok) & LOW32_MASK


def is_char_token(tok: int) -> bool:
    """该 token 是否属于 utf8-mb4 字符组"""
    return group_of(tok) == CHAR_GROUP


def char_token(cp: int) -> int:
    """Unicode 码点 → 64 比特字符 token (CHAR_GROUP | cp)"""
    cp = int(cp)
    if not (0 <= cp <= UNICODE_MAX):
        raise ValueError(
            f"码点 {cp:#x} 超出 utf8-mb4 范围 (0 ~ {UNICODE_MAX:#x})")
    return make_token(CHAR_GROUP, cp)


def char_group_token(ch: str) -> int:
    """单个字符 → 64 比特字符 token (空串非法)"""
    if not isinstance(ch, str) or len(ch) != 1:
        raise ValueError(f"char_group_token 需单个字符, 收到 {ch!r}")
    return char_token(ord(ch))


def encode(text: str) -> list[int]:
    """文本 → 64 比特 class-token 序列 (每字符一个字符 token)

    Python str 迭代即按 Unicode 码点, astral / emoji (4 字节
    utf8-mb4) 与 BMP/CJK 统一映射, 天然保留多字节字符。
    换行等控制字符亦可存储 (低 32 位为码点)。
    """
    return [char_token(ord(ch)) for ch in str(text)]


def tokenize(text: str) -> list[int]:
    """encode 的别名"""
    return encode(text)


def decode(tokens: list[int]) -> str:
    """64 比特 class-token 序列 → 文本 (仅接受 utf8-mb4 字符组 token)

    遇到非字符组 token 抛明确异常, 杜绝静默乱码。
    """
    out = []
    for tok in tokens:
        if not is_char_token(tok):
            cp = low_of(tok)
            raise ValueError(
                f"非 utf8-mb4 字符组 token: {tok:#018x} "
                f"(group={group_of(tok):#010x}, low={cp:#010x})")
        cp = int(low_of(tok))
        if cp > UNICODE_MAX:
            raise ValueError(f"码点 {cp:#x} 超出 utf8-mb4 范围, 无法解码")
        out.append(chr(cp))
    return "".join(out)


def embed_tokens(tokens: list[int], dim: int) -> np.ndarray:
    """64 比特 token 序列 → dim 维脉冲信号 (确定性, 供网络消费)

    对每个 token 做确定性折叠:
      - 取 64 比特原始值混入分组位 (zlib.crc32 于 8 字节小端),
      - 再以低 32 位码点参与 crc32, 得到稳定且区分组的维度哈希,
      - 累加后 L2 归一化。

    与 spike_codec.encode_text 同用 crc32, 跨进程逐比特确定。
    空序列返回零向量。
    """
    signal = np.zeros(dim, dtype=float)
    for tok in tokens:
        tok = int(tok) & MAX_TOK
        # token 全 64 位 → 折叠 (分组位也参与, 区分字符组/未来组)
        base = zlib.crc32(tok.to_bytes(8, byteorder="little")) % dim
        # 低 32 位码点额外折叠, 增强同分组内多字符区分度
        low = low_of(tok)
        mix = zlib.crc32(low.to_bytes(4, byteorder="little")) % dim
        signal[base] += 1.0
        signal[mix] += 0.5
    norm = np.linalg.norm(signal)
    if norm > 0:
        signal = signal / norm
    return signal
