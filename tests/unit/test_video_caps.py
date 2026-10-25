"""视频模型档位表单测（2026-10-03 分档具体化：时长↔分辨率精确配对，
用户令禁止模糊大小比较——档位=数据表，校验=精确配对查表）。

覆盖：
- 档位表形状：每档 duration + qualities 非空清单、720p 档仅 5/10 秒（2026-10-12 解锁）；
- resolve_quality：显式档精确配对（合法通过/非法带允许清单拒绝）；
- 默认档：未指定=该档清单首位（5s/10s→720p、15s→480p，数据表驱动）；
- 非档时长（如链式内部窗口 12.0s）落「不小于它的最近档」（→15s 档）；
- 云端端点（cloud::）tiers 为空表（档位未知不造假）。
"""
from __future__ import annotations

import pytest

from src.api.manga.video_caps import (
    DEFAULT_VIDEO_MODEL,
    VIDEO_MODEL_TIERS,
    resolve_quality,
    tiers_for,
)
from src.middleware.error_handler import ApiError


def test_tier_table_shape_exact_pairs() -> None:
    # 2026-10-12 用户令解锁 5/10/15s 档（720p×10s 经 16G 探针实弹通过，
    # 峰值 15.6G；720p×15s 撞墙维持 480p）——旧 [4,8,11,15] 口径作废
    tiers = VIDEO_MODEL_TIERS[DEFAULT_VIDEO_MODEL]["tiers"]
    assert [t["duration"] for t in tiers] == [5, 10, 15]
    # 720p 档仅 5/10 秒（16G 显存实测约束，具体到档不比较）
    for t in tiers:
        assert t["qualities"], "每档必须有非空可选清单"
        if "720p" in t["qualities"]:
            assert t["duration"] in (5, 10)


def test_resolve_quality_exact_pair_validation() -> None:
    # 合法配对逐一通过
    assert resolve_quality("", 4, "720p") == "720p"
    assert resolve_quality("minimax-h3", 8, "480p") == "480p"
    assert resolve_quality("minimax-h3", 15, "480p") == "480p"
    # 非法配对：11 秒档点 720p → 精确拒绝并带该档允许清单
    with pytest.raises(ApiError) as ei:
        resolve_quality("minimax-h3", 11, "720p")
    assert "480p" in str(ei.value.message)
    assert "VIDEO_QUALITY_TIER_INVALID" in str(ei.value.code)


def test_resolve_quality_default_per_tier() -> None:
    """未指定=该档默认（清单首位）：4s→720p、15s→480p（与旧推导等值，
    但由档位表数据驱动，无散落比较式）。"""
    assert resolve_quality("", 4) == "720p"
    assert resolve_quality("", 8) == "720p"
    assert resolve_quality("", 11) == "480p"
    assert resolve_quality("", 15) == "480p"


def test_resolve_quality_non_tier_seconds_falls_to_declared_tier() -> None:
    # 2026-10-12 起 10.0s 是精确档（默认 720p）；真实非档窗口 12.0s
    # → 落「不小于它的最近档」= 15s 档（480p）
    assert resolve_quality("minimax-h3", 10.0, "") == "720p"
    assert resolve_quality("minimax-h3", 12.0, "") == "480p"
    # 3.5s（下界钳后）→ 5s 档
    assert resolve_quality("minimax-h3", 3.5, "") == "720p"


def test_cloud_endpoint_tiers_empty() -> None:
    """云端视频模型档位未知 → 空表（不借 h3 档造假）。"""
    assert tiers_for("cloud::prov_x::some-video") == []
    # 空/未知模型 → 默认模型档位（前端系统默认选项同源）
    assert tiers_for("") == VIDEO_MODEL_TIERS[DEFAULT_VIDEO_MODEL]["tiers"]
