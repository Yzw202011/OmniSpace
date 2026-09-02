"""B0/B4 污染防护回归：style 版本号/数据集 id 路径穿越（P1-5 当代形态）。

2026-09-02 B0 静态审计发现：rollback/rename_version/dataset_stats 三处
把用户可控字符串直接拼接路径（STYLE_LORA_DIR / version、
DATASET_DIR / dataset_id）——"../xxx" 可穿越（rename 任意目录写
meta.json / stats 穿越读 / rollback 污染 current 指针）。修复=
v\\d+ / 32位hex 格式白名单。本文件锁定修复不回退。
"""
from __future__ import annotations

import pytest

from backend.services.style_lora_service import get_style_lora_service


@pytest.fixture()
def svc() -> object:
    return get_style_lora_service()


def test_rollback_rejects_traversal(svc) -> None:
    assert svc.rollback("../../OmniSpace") is False
    assert svc.rollback("..\\..\\Windows") is False
    assert svc.rollback("/abs/path") is False
    assert svc.rollback("v1/../../..") is False
    assert svc.rollback("") is False


def test_rename_rejects_traversal(tmp_path, svc) -> None:
    # 任意目录写防护：非法版本号必须被拒（不落任何文件）
    target = tmp_path / "pwned"
    target.mkdir()
    assert svc.rename_version(f"../../{target.name}", "x") is False
    assert not (target / "meta.json").exists()


def test_dataset_stats_rejects_traversal(svc) -> None:
    out = svc.dataset_stats("../../backend")
    assert out["total"] == 0 and out["sufficient"] is False


def test_legitimate_ids_pass(svc, tmp_path, monkeypatch) -> None:
    # 合法形态不受影响：v3 回滚（构造真实版本目录）
    from backend.services import style_lora_service as sls
    fake_root = tmp_path / "style_lora"
    fake_root.mkdir()
    (fake_root / "v3").mkdir()
    monkeypatch.setattr(sls, "STYLE_LORA_DIR", fake_root)
    monkeypatch.setattr(
        sls, "CURRENT_FILE",
        fake_root / "current.json")
    assert svc.rollback("v3") is True
    assert svc.get_current() == "v3"
    # 合法 dataset id 形态放行（目录不存在=0 样本，非拒绝）
    stats = svc.dataset_stats("a" * 32)
    assert stats["total"] == 0
