"""零熵指纹拒收单测（R17「指纹熵弱点一码两机」根修，2026-10-20）。

覆盖：占位指纹（客户端 PowerShell 受限时三件套全替的恒定值）拒收、
三件重复拒收（绑定强度降级特征）、正常三源放行、占位常量与客户端
impl 计算同源。license_console 按台规不入 git——缺失即 skip 不假绿。
"""
from __future__ import annotations

import hashlib
import sys

import pytest

sys.path.insert(0, r"E:\OmniSpace")
sys.path.append(r"E:\OmniSpace\pydeps")

pytest.importorskip("fastapi")
try:
    from license_console import app as lc_app
except ImportError:  # pragma: no cover - 发码台不入库（本机外无此文件）
    pytest.skip("license_console 不在（台规不入库，随本机工作区走）",
                allow_module_level=True)


def _fp_digest(component: str) -> str:
    return hashlib.sha256(component.encode("utf-8")).hexdigest()[:32]


def test_placeholder_constant_matches_client_impl() -> None:
    # 发码台黑名单常量必须与客户端占位指纹同源（impl: "<unreadable>"×3）
    assert lc_app._PLACEHOLDER_FP == _fp_digest("<unreadable>")


def test_placeholder_fp_rejected() -> None:
    p = lc_app._PLACEHOLDER_FP
    with pytest.raises(Exception) as ei:
        lc_app._check_fp_entropy([p, p, p])
    assert "占位熵" in str(ei.value.detail if hasattr(ei.value, "detail")
                           else ei.value)


def test_duplicate_fp_rejected() -> None:
    a, b = _fp_digest("MachineGuid-x"), _fp_digest("UUID-y")
    with pytest.raises(Exception, match="重复"):
        lc_app._check_fp_entropy([a, a, b])
    with pytest.raises(Exception, match="重复"):
        lc_app._check_fp_entropy([a, a, a])


def test_healthy_fp_passes() -> None:
    fps = [_fp_digest("guid-1"), _fp_digest("uuid-2"), _fp_digest("cpu-3")]
    assert len(set(fps)) == 3
    assert all(x != lc_app._PLACEHOLDER_FP for x in fps)
    lc_app._check_fp_entropy(fps)  # 不抛=放行
