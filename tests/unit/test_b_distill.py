"""批7 混合路线（2026-11-09）：B 段蒸馏器单测（mock，不真起 llama-server）。

覆盖：构造性防朗读（散文原文永不进合同）/ [世界画面] 注入 / 降级回退 /
缓存命中 / SSRF 防线（_guard_url 拒绝非环回目标）。"""
from __future__ import annotations

import pytest

from src.config import _cfg
from src.services import b_section_distiller as bd
from src.services.inference.h3_chain_engine import _six_section

_INN_B = ("明末乱世，北方边镇的一座老旧客栈是三教九流的藏身之所；梁上悬着"
          "褪色的红灯笼，柜台后摆着一坛坛烧酒，江湖恩怨在这里一夜爆发。")
_DISTILLED = "柜面列着数坛粗陶烧酒，红灯笼轻晃，窗外风雪拍打糊纸窗棂。"


@pytest.fixture
def distill_on(monkeypatch):
    monkeypatch.setitem(_cfg.setdefault("manga", {}), "h3_b_distill", True)


def _make_six(desc: str, monkeypatch, distilled: str | None, calls: list):
    monkeypatch.setattr(bd, "distill_b_section",
                        lambda t: (calls.append(t), distilled)[1])
    return _six_section(desc, [{"kind": "character", "name": "测试角色"}], 5.0)


def test_guard_url_rejects_non_loopback() -> None:
    pytest.raises(ValueError, bd._guard_url, "http://192.168.1.1:8131/x")
    pytest.raises(ValueError, bd._guard_url, "https://127.0.0.1:8131/x")
    assert bd._guard_url("http://127.0.0.1:8131/health")  # 环回放行


def _fake_proc_cls():
    class _P:
        def terminate(self) -> None:
            return None

        def wait(self, timeout: int = 0) -> int:
            return 0

        def kill(self) -> None:
            return None
    return _P


def test_distill_cache_hits_once(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(bd, "_wait_health", lambda u, d: True)
    monkeypatch.setattr(bd, "_http_compile",
                        lambda u, t, s: (calls.append(t), _DISTILLED)[1])
    monkeypatch.setattr(bd.subprocess, "Popen", lambda *a, **k: _fake_proc_cls()())
    bd._cache.clear()
    assert bd.distill_b_section(_INN_B) == _DISTILLED
    assert bd.distill_b_section(_INN_B) == _DISTILLED
    assert len(calls) == 1  # 第二次走缓存


def test_distill_fallback_on_compile_failure(monkeypatch) -> None:
    bd._cache.clear()
    monkeypatch.setattr(bd, "_wait_health", lambda u, d: True)
    monkeypatch.setattr(bd, "_http_compile", lambda u, t, s: None)
    monkeypatch.setattr(bd.subprocess, "Popen", lambda *a, **k: _fake_proc_cls()())
    assert bd.distill_b_section(_INN_B) is None  # 降级链返回 None


def test_six_section_injects_world_block(distill_on, monkeypatch) -> None:
    calls: list = []
    out = _make_six(f"A.画风：写实。\nB.世界观：{_INN_B}\n"
                    "C.分镜：\n[0s-5s]\n画面：客栈大堂一景。\n", monkeypatch,
                    _DISTILLED, calls)
    assert "[世界画面]" in out
    assert "柜面列着数坛粗陶烧酒" in out      # 蒸馏画面句入合同
    # 蒸馏输入=B 段原文（含「世界观：」栏目标签——真实描述词同格式）
    assert calls == [f"世界观：{_INN_B}"]
    # 构造性防朗读：B 段散文原文（含泄漏词恩怨）不进合同正文
    assert "江湖恩怨在这里一夜爆发" not in out


def test_six_section_fallback_swallow(distill_on, monkeypatch) -> None:
    calls: list = []
    out = _make_six(f"A.画风：写实。\nB.世界观：{_INN_B}\n"
                    "C.分镜：\n[0s-5s]\n画面：客栈大堂一景。\n", monkeypatch,
                    None, calls)
    assert "[世界画面]" not in out             # 降级=回退吞除
    assert "江湖恩怨在这里一夜爆发" not in out  # 散文原文仍不进合同


def test_six_section_switch_off_unchanged(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setitem(_cfg.setdefault("manga", {}), "h3_b_distill", False)
    out = _make_six(f"A.画风：写实。\nB.世界观：{_INN_B}\n"
                    "C.分镜：\n[0s-5s]\n画面：客栈大堂一景。\n", monkeypatch,
                    _DISTILLED, calls)
    assert "[世界画面]" not in out and calls == []  # 开关关=蒸馏不触发
