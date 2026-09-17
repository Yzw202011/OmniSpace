"""64 比特分类式 token 测试（DF v0.11.0 跟版 2026-09-17）。

一个 token 占 64 比特：高 32 比特为 token 组（0x00000000 保留为
utf8-mb4 字符组）、低 32 比特直接承载 Unicode 码点（≤0x10FFFF 覆盖
4 字节/astral/emoji）。移植自上游 test_class_token.py（12 项），
zip 补 strict 以过 B905 闸。
"""
from __future__ import annotations

import numpy as np
import pytest

from src.codec.class_token import (
    CHAR_GROUP,
    LOW32_MASK,
    UNICODE_MAX,
    char_group_token,
    char_token,
    decode,
    embed_tokens,
    encode,
    group_of,
    is_char_token,
    low_of,
    make_token,
    tokenize,
)


def test_ascii_roundtrip():
    text = "fn main() -> u32 { let x = 5; x }"
    toks = encode(text)
    assert all(is_char_token(t) for t in toks)
    assert decode(toks) == text


def test_cjk_multibyte_roundtrip():
    # 中文 UTF-8 3 字节, utf8-mb4 (≤4 字节) 覆盖
    text = "所有权移动借用冲突生命周期类型不匹配"
    assert decode(encode(text)) == text


def test_emoji_astral_roundtrip():
    # emoji 为 UTF-8 4 字节 (utf8-mb4), 需 32 位可容纳的码点 (astral)
    text = "🚀 脉冲模型 🔬 测试 ✓"
    toks = encode(text)
    assert decode(toks) == text
    # 验证 4 字节字符的码点确实落在低位且 ≤ 0x10FFFF
    for tok, ch in zip(toks, text, strict=False):
        assert low_of(tok) == ord(ch)
        assert low_of(tok) <= UNICODE_MAX


def test_char_group_zero_always():
    for text in ("ab", "中文", "🎉", "fn x(){}"):
        for tok in encode(text):
            assert group_of(tok) == CHAR_GROUP == 0x00000000


def test_low32_is_codepoint():
    text = "H 汉 🔥 #"
    for tok, ch in zip(encode(text), text, strict=False):
        assert low_of(tok) == ord(ch)
        assert (low_of(tok) & LOW32_MASK) == ord(ch)


def test_make_group_of_lowof_inverse():
    tok = make_token(0x00000001, 0x12345678)
    assert group_of(tok) == 0x00000001
    assert low_of(tok) == 0x12345678
    tok2 = make_token(0x00000000, 0x1F600)  # 😀 码点
    assert is_char_token(tok2)
    assert decode([tok2]) == "😀"


def test_char_token_validation():
    assert char_token(65) == make_token(CHAR_GROUP, 65)
    assert char_group_token("A") == make_token(CHAR_GROUP, 65)
    with pytest.raises(ValueError):
        char_token(UNICODE_MAX + 1)  # 超 utf8-mb4 上限
    with pytest.raises(ValueError):
        char_group_token("ab")  # 非单字符


def test_decode_rejects_non_char_group():
    with pytest.raises(ValueError):
        decode([make_token(0x00000001, 123)])  # 非字符组


def test_tokenize_alias():
    assert tokenize("rust") == encode("rust")


def test_embed_deterministic_normalized():
    toks = encode('fn main() { let n = 5; println!("{} ", n); }')
    a = embed_tokens(toks, dim=16)
    b = embed_tokens(toks, dim=16)
    assert np.array_equal(a, b)                     # 逐比特确定
    assert np.count_nonzero(a) > 0                   # 有激活
    assert abs(np.linalg.norm(a) - 1.0) < 1e-6       # 归一化
    assert a.shape == (16,)


def test_embed_empty_is_zero():
    assert np.count_nonzero(embed_tokens([], dim=16)) == 0


def test_embed_differs_across_text():
    a = embed_tokens(encode("move value"), dim=16)
    b = embed_tokens(encode("type mismatch"), dim=16)
    assert not np.array_equal(a, b)
