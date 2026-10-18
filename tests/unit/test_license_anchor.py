"""H4 补强（2026-10-19 防逆向 P2 拍板项）单测：回拨守卫注册表交叉锚。

覆盖：_anchor_write/_anchor_read 回环、_effective_last_check 取两锚
最大值、_anchor_clear 清除。锁芯真身 license_gate_impl.py 出仓不入
git——缺失（他机新克隆）则整文件 skip，不假绿。
"""
from __future__ import annotations

import sys
import time

import pytest

sys.path.insert(0, r"E:\OmniSpace")

pytest.importorskip("winreg", reason="注册表交叉锚为 Windows 语义")
try:
    from src import license_gate_impl as gi
except ImportError:  # pragma: no cover - 锁芯出仓（本机外无此文件）
    pytest.skip("license_gate_impl 不在（出仓文件，随本机工作区走）",
                allow_module_level=True)


@pytest.fixture(autouse=True)
def _clean_anchor():
    gi._anchor_clear()
    yield
    gi._anchor_clear()


@pytest.mark.skipif(sys.platform != "win32", reason="winreg 语义")
def test_anchor_roundtrip() -> None:
    ts = time.time()
    assert gi._anchor_read() == 0.0          # 初始无锚
    gi._anchor_write(ts)
    got = gi._anchor_read()
    assert abs(got - ts) < 1.0               # 毫秒精度回环
    gi._anchor_write(ts + 3600)
    assert gi._anchor_read() > got           # 后写覆盖（取新值）


@pytest.mark.skipif(sys.platform != "win32", reason="winreg 语义")
def test_effective_last_check_takes_max() -> None:
    past, future = time.time() - 100, time.time() + 100
    # state 新、锚旧 → 取 state
    assert gi._effective_last_check(
        {"last_check": future}) >= future - 1
    # state 被「删/伪造为旧」、锚还在 → 取锚（堵删 state 续命路线）
    gi._anchor_write(past + 50)
    assert gi._effective_last_check({"last_check": past}) == pytest.approx(
        past + 50, abs=1.0)
    # 双空 → 0（新装机语义）
    gi._anchor_clear()
    assert gi._effective_last_check({}) == 0.0


@pytest.mark.skipif(sys.platform != "win32", reason="winreg 语义")
def test_anchor_clear() -> None:
    gi._anchor_write(time.time())
    assert gi._anchor_read() > 0
    gi._anchor_clear()
    assert gi._anchor_read() == 0.0
