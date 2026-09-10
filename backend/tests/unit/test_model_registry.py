"""ADR-003 P1 验收测试：模型注册表真源（manifest v3）锁定。

锁定四类不变量（任何一条失败 = 注册表/引用面失真，禁止合入）：
  1. manifest v3 schema（REQUIRED_FIELDS / lifecycle 枚举 / capabilities 形状）
  2. required 模型磁盘在位 + validate_against_disk 全绿（幽灵/孤儿/缺失清零）
  3. HARDWARE_TIER_TABLE（除 cpu 虚拟变体档）models 全部 ∈ 注册表
  4. gen_router 风格包底座偏好链全部 ∈ 注册表
"""
from __future__ import annotations

import pytest

from backend.config import MODELS_DIR
from backend.data import model_registry
from backend.data.model_registry import (
    LIFECYCLE_VALUES,
    REQUIRED_FIELDS,
    load_manifest,
    registry_ids,
    validate_against_disk,
)

pytestmark = pytest.mark.smoke

# cpu 档 models 已随 2026-08-29 模型裁剪置空（纯 CPU 档对话/绘画/
# 视频均不可用，诚实降级），豁免注册表归属校验
_TIER_VIRTUAL_EXCEPTIONS = {"cpu"}


@pytest.fixture(scope="module")
def manifest() -> dict:
    data = load_manifest()
    assert data, "models_manifest.json 不可用（缺失/损坏/结构非法）"
    return data


def test_manifest_v3_schema(manifest: dict) -> None:
    """schema 锁定：manifest_version=3，每条目必填字段齐全且合法。"""
    assert manifest.get("manifest_version") == 3
    models = manifest["models"]
    assert len(models) >= 18, (
        "注册表覆盖磁盘模型族（09-02 实测 20；09-10 体积清理经用户拍板删"
        " deepseek/GGUF-9B/qwen-edit/wan×2 共 4 条后实测 18，回补权重须重新登记）")
    for mid, e in models.items():
        for field in REQUIRED_FIELDS:
            assert field in e, f"{mid} 缺必填字段 {field}"
        assert e["lifecycle"] in LIFECYCLE_VALUES, f"{mid} lifecycle 非法"
        caps = e["capabilities"]
        assert isinstance(caps, list) and caps, f"{mid} capabilities 空"
        assert all(isinstance(c, str) for c in caps), f"{mid} capabilities 非字符串"
        assert isinstance(e["path"], str) and e["path"], f"{mid} path 空"
        assert isinstance(e.get("wired"), bool), f"{mid} wired 缺失/非法"
        assert float(e.get("min_vram_gb", -1)) >= 0, f"{mid} min_vram_gb 非法"


@pytest.mark.skipif(not MODELS_DIR.is_dir(), reason="无 models/ 目录的环境跳过")
def test_required_entries_on_disk() -> None:
    """required=true 条目磁盘必须在位（bge-large-zh / 门禁小模型）。"""
    models = load_manifest().get("models") or {}
    missing = [
        mid for mid, e in models.items()
        if e.get("required") and not (MODELS_DIR / e["path"]).exists()
    ]
    assert missing == [], f"必需模型磁盘缺失: {missing}"


@pytest.mark.skipif(not MODELS_DIR.is_dir(), reason="无 models/ 目录的环境跳过")
def test_validate_against_disk_clean() -> None:
    """一致性校验全绿：幽灵条目 / 孤儿目录 / 必需缺失三清零。"""
    report = validate_against_disk()
    assert report["required_missing"] == [], "required 缺失（新登记模型须真实在位）"
    assert report["ghost_entries"] == [], "幽灵条目（登记但磁盘无权重，禁止回潮）"
    assert report["orphan_dirs"] == [], (
        "磁盘存在未登记模型目录——新模型落盘后须同步登记 manifest v3"
    )


def test_tier_table_models_registered() -> None:
    """HARDWARE_TIER_TABLE（除 cpu 虚拟档）models 全部 ∈ 注册表。"""
    from backend.data.models import HARDWARE_TIER_TABLE

    known = registry_ids()
    assert known, "注册表为空，无法裁决 tier 表"
    for entry in HARDWARE_TIER_TABLE:
        if entry["tier"] in _TIER_VIRTUAL_EXCEPTIONS:
            continue
        for slot, mid in entry["models"].items():
            if not mid:
                # 空串 = 该档位此功能不可用（2026-08-29 裁剪后 8GB 档
                # 无 H3 视频 / CPU 档全空），诚实降级语义，跳过归属校验
                continue
            assert mid in known, (
                f"档位 {entry['tier']} 的 {slot} 引用未注册模型 {mid}"
                "（幽灵型号禁止回潮）")


def test_stylepack_base_prefs_registered() -> None:
    """gen_router 风格包底座偏好链全部 ∈ 注册表。"""
    from backend.services.inference.gen_router import DEFAULT_PACK, STYLE_PACKS

    known = registry_ids()
    assert known, "注册表为空，无法裁决偏好链"
    packs = [*STYLE_PACKS, DEFAULT_PACK]
    for pack in packs:
        for mid in pack.base_prefs:
            assert mid in known, (
                f"风格包 {pack.sid} 偏好链引用未注册模型 {mid}")


def test_registry_entry_contract() -> None:
    """entry() 契约：命中返回 dict，未命中返回空 dict（_manifest_entry 依赖）。"""
    assert isinstance(model_registry.entry("qwen3-vl-4b"), dict)
    assert model_registry.entry("qwen3-vl-4b").get("type") == "dialog"
    assert model_registry.entry("不存在的模型-id") == {}
