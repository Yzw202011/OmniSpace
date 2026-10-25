"""默认画质双链并存（2026-10-25 拍板）单测：speed_priority 请求字段 →
引擎覆盖参映射。速度优先=turbo 双采（A/B 实测 -48%~-55% 画质持平）；
质量优先=现行链全步数；缺省=false=存量行为零变化。"""
from __future__ import annotations

from src.api.manga.video import _speed_priority_overrides
from src.data.models import VideoGenerateRequest


def test_speed_priority_maps_to_turbo_two_pass() -> None:
    assert _speed_priority_overrides(True) == {
        "turbo_override": True, "steps_override": 6, "refine_pass": True}


def test_quality_priority_pins_current_chain() -> None:
    # 质量优先显式钉死非 turbo——用户将来 flip config h3_turbo 也不被改道
    assert _speed_priority_overrides(False) == {
        "turbo_override": False, "steps_override": None, "refine_pass": False}


def test_request_field_defaults_false() -> None:
    r = VideoGenerateRequest(storyboard_row_id="x", description="d",
                             screenshot_4in1="")
    assert r.speed_priority is False
