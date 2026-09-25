"""分格链模型配置重接测试（2026-09-23 方案2 大修，切换诚实性延伸）。

背景：漫画页「模型设置」保存的 manga-paint 槽 default 此前与分格
出图断链——gen_router 白名单提权只认 klein 偏好序、分格 comfy 分支
硬编码 klein-9b，前端选 z/qwen21 实际全部降级 SDXL（前端四连实弹
Exposed）。重接后：显式选择 ∈ comfy 真源 → 直通对应底座。

覆盖（纯逻辑面，生成行为由前端 UI 实弹验收）：
  ① _explicit_comfy_model 真源内模型直通（mock 槽位读取）
  ② 空串/auto 智能路由/名单外模型/读取异常 → 空串（行为零变化）
  ③ 真源集合契约：四件套在列（与切换诚实性测试互为映照）
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import pytest

import src.api.manga.keyframe as _kf_mod
from src.services.inference import comfy_paint_engine as cpe


@pytest.fixture()
def _slot(monkeypatch: pytest.MonkeyPatch):
    """槽位读取桩：module_default_model 经闭包受控。"""
    holder: dict = {"value": ""}

    def _fake(slot: str) -> str:
        return str(holder["value"] or "")

    monkeypatch.setattr(
        "src.api.models.module_default_model", _fake)
    return holder


def test_explicit_model_in_truth_set_passes(_slot: dict) -> None:
    for mid in ("flux2-klein-9b", "z-image-turbo", "qwen-image-2.1"):
        _slot["value"] = mid
        assert _kf_mod._explicit_comfy_model() == mid
    # 4b 二次止血期间不在真源 → 空串（显式点名将由引擎守卫拒绝）
    if cpe._KLEIN_4B_BROKEN:
        _slot["value"] = "flux2-klein-4b"
        assert _kf_mod._explicit_comfy_model() == ""


def test_auto_and_unknown_return_empty(_slot: dict) -> None:
    # 智能路由（空）→ auto 旧行为
    _slot["value"] = ""
    assert _kf_mod._explicit_comfy_model() == ""
    # 名单外（legacy diffusers 栈模型）→ 不劫持选路
    for bad in ("sdxl-base-1.0", "qwen-image-2512", "minimax-h3"):
        _slot["value"] = bad
        assert _kf_mod._explicit_comfy_model() == ""


def test_config_read_failure_returns_empty(
        _slot: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(slot: str) -> str:
        raise RuntimeError("kv down")

    monkeypatch.setattr("src.api.models.module_default_model", _boom)
    assert _kf_mod._explicit_comfy_model() == ""


def test_truth_set_contract_four_models() -> None:
    # 方案2 验收契约：分格链可选 = comfy 真源（2026-09-25 D 线
    # 收编 z-image-base → 五件套）
    ids = {m["id"] for m in cpe.COMFY_PAINT_MODELS}
    if cpe._KLEIN_4B_BROKEN:
        assert ids == {"flux2-klein-9b", "z-image-turbo",
                       "qwen-image-2.1"}
    else:
        assert ids == {"flux2-klein-9b", "flux2-klein-4b",
                       "z-image-turbo", "z-image-base",
                       "qwen-image-2.1"}


def test_no_pulid_models_identified() -> None:
    # 软锁判定口径：klein 家族支持 PuLID，z/qwen21 不支持
    for m in cpe.COMFY_PAINT_MODELS:
        expects_pulid = m["id"].startswith("flux2-klein")
        assert m["id"].startswith("flux2-klein") == expects_pulid
        if m["id"] in ("z-image-turbo", "qwen-image-2.1"):
            assert not m["id"].startswith("flux2-klein")
