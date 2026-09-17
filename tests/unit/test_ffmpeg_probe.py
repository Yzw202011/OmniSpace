"""FFmpeg 探测同源回归（2026-09-17 审计批2）。

病灶：startup_check._check_ffmpeg 旧版只查 PATH+C 盘猜测，对随包
runtime/ffmpeg/bin 误报「未安装」——深度体检与实际能出片自相矛盾
（诊断与产品逻辑不同源）。修复后必须复用 EncoderService.discover_ffmpeg
同源发现链（runtime → tools/downloads → PATH）。
"""
from __future__ import annotations

from src.services.encoder_service import EncoderService
from src.startup_check import _check_ffmpeg


def test_ffmpeg_probe_uses_product_discovery_chain():
    """随包 FFmpeg 在位 → 探测必须通过并指向真实二进制。"""
    r = _check_ffmpeg()
    assert r.passed is True
    assert "ffmpeg" in r.detail.lower()
    # 同源断言：探测结果与产品发现链返回同一路径
    assert r.data.get("path") == EncoderService.discover_ffmpeg()


def test_ffmpeg_probe_absent_reports_warning(monkeypatch):
    """三处都找不到 → 如实 warn（不得谎报 pass）。"""
    monkeypatch.setattr(
        EncoderService, "discover_ffmpeg",
        classmethod(lambda cls: None))
    r = _check_ffmpeg()
    assert r.passed is False
    assert "未找到" in r.detail
