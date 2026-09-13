"""VideoRouter / ModelCache / FeaturePredictor 直测（B9 补测 2026-09-14）。

VideoRouter 部分兼作「VideoModel 死枚举收敛」的手术回归锁：
路由表仅 minimax-h3、哨兵兜底、params_map 无死键。
FeaturePredictor 用独立 tmp db（不触 data/model_usage.db）。
"""
from __future__ import annotations

import pytest

from backend.data.models import VideoModel
from backend.middleware.error_handler import ApiError
from backend.services.model_manager.cache import ModelCache
from backend.services.model_manager.predictor import FeaturePredictor
from backend.services.scheduler.video_router import VideoRouter

# ── VideoRouter：死枚举收敛手术回归锁 ───────────────────────────────

def test_select_model_routes_h3_on_16g() -> None:
    assert VideoRouter().select_model(13.4) is VideoModel.MINIMAX_H3


def test_select_model_cpu_sentinel_on_no_vram() -> None:
    assert VideoRouter().select_model(0.0) is VideoModel.COGVIDEOX_2B_CPU


def test_select_model_override_and_invalid() -> None:
    r = VideoRouter()
    assert r.select_model(13.4, user_override="minimax-h3") is VideoModel.MINIMAX_H3
    with pytest.raises(ApiError):
        r.select_model(13.4, user_override="ltx-2")  # 已收敛死成员→无效


def test_generation_params_no_dead_keys() -> None:
    """params_map 键集必须与现存枚举严格一致（死键回潮即红）。"""
    r = VideoRouter()
    for member in VideoModel:
        params = r.get_generation_params(member)
        assert params and "max_fps" in params, member


# ── ModelCache：LRU 行为 ────────────────────────────────────────────

def test_cache_put_get_roundtrip_and_remove() -> None:
    c = ModelCache(max_entries=4, max_size_gb=1.0)
    c.put("a", {"w": 1}, size_bytes=10)
    assert c.get("a") == {"w": 1}
    assert c.remove("a") is True
    assert c.get("a") is None
    assert c.remove("a") is False  # 二次删除=缺失


def test_cache_lru_eviction_order() -> None:
    c = ModelCache(max_entries=2, max_size_gb=1.0)
    c.put("a", 1, size_bytes=1)
    c.put("b", 2, size_bytes=1)
    c.get("a")  # 触碰 a → b 变 LRU
    c.put("c", 3, size_bytes=1)  # 应逐出 b
    assert c.get("a") == 1 and c.get("c") == 3
    assert c.get("b") is None


def test_cache_put_same_key_replaces() -> None:
    c = ModelCache(max_entries=4, max_size_gb=1.0)
    c.put("a", "v1", size_bytes=10)
    c.put("a", "v2", size_bytes=10)
    assert c.get("a") == "v2"


# ── FeaturePredictor：切换事件记录（独立 tmp db）────────────────────

def test_predictor_records_and_tracks_last(tmp_path) -> None:
    p = FeaturePredictor(db_path=str(tmp_path / "usage.db"))
    p.record_event("", "dialog", ts=1700000000.0)
    p.record_event("dialog", "paint", ts=1700000600.0)
    assert p.last_feature == "paint"
    assert p.event_count >= 2


def test_predictor_ignores_empty_target(tmp_path) -> None:
    p = FeaturePredictor(db_path=str(tmp_path / "usage.db"))
    before = p.event_count
    p.record_event("dialog", "  ", ts=1700000000.0)
    assert p.event_count == before  # 空 to 不记账
