"""画质双档重划（2026-11-08 拍板，A/B 战役证据）单测：speed_priority 请求
字段 + quality → 引擎覆盖参映射。质量档=turbo 精修家族（非 turbo 全步
已被 13 条同种子实测推翻：720p 两阶段双输最慢+最软）；速度档=turbo 减步。
缺省 speed_priority=false=质量档。配方证据：data/videos/h3_ab/results.jsonl"""
from __future__ import annotations

from src.api.manga.video import _speed_priority_overrides
from src.data.models import VideoGenerateRequest


def test_quality_tier_480p_maps_to_turbo6_refine() -> None:
    # 质量档 480p = turbo6+低σ精修（A1 配方，锐度 1347 全场最高稳定配方）
    assert _speed_priority_overrides(False, "480p") == {
        "turbo_override": True, "steps_override": 6, "refine_pass": True}


def test_quality_tier_720p_maps_to_turbo2s_8plus2() -> None:
    # 质量档 720p = turbo2s 8+2（U2 配方，粗采最厚；非 turbo 已退役）
    assert _speed_priority_overrides(False, "720p") == {
        "turbo_override": True, "steps_override": 8, "refine_pass": False}


def test_speed_tier_480p_maps_to_turbo4() -> None:
    # 速度档 480p = turbo4 无精修（A2 配方，855s 最快）
    assert _speed_priority_overrides(True, "480p") == {
        "turbo_override": True, "steps_override": 4, "refine_pass": False}


def test_speed_tier_720p_maps_to_turbo2s_6plus2() -> None:
    # 速度档 720p = turbo2s 6+2（U1 配方，978s 抖动最低；=旧速度档不变）
    assert _speed_priority_overrides(True, "720p") == {
        "turbo_override": True, "steps_override": 6, "refine_pass": False}


def test_request_field_defaults_false() -> None:
    r = VideoGenerateRequest(storyboard_row_id="x", description="d",
                             screenshot_4in1="")
    assert r.speed_priority is False
