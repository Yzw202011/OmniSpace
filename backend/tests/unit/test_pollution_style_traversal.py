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


def test_delete_rejects_traversal(svc, tmp_path) -> None:
    # 2026-09-05 P1-5 收尾：delete_version 是 rollback/rename 之外最后
    # 一个漏网入口（外部参数直拼路径后 rmtree）。非法版本必须被拒且
    # 目标目录毫发无损；对外统一 not_found（不区分格式非法）。
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("x", encoding="utf-8")
    assert svc.delete_version(f"../../{victim.name}") == (False, "not_found")
    assert svc.delete_version("..\\..\\Windows") == (False, "not_found")
    assert svc.delete_version("") == (False, "not_found")
    assert (victim / "keep.txt").exists()


def test_delete_legitimate_version(tmp_path, monkeypatch) -> None:
    # 合法形态不受影响：真实版本目录被删、训练锁链路与 current 清理保持
    from backend.services import style_lora_service as sls
    fake_root = tmp_path / "style_lora"
    fake_root.mkdir()
    (fake_root / "v2").mkdir()
    monkeypatch.setattr(sls, "STYLE_LORA_DIR", fake_root)
    monkeypatch.setattr(sls, "CURRENT_FILE", fake_root / "current.json")
    svc = sls.get_style_lora_service()
    monkeypatch.setattr(svc, "list_tasks", lambda: [])
    assert svc.delete_version("v2") == (True, "deleted")
    assert not (fake_root / "v2").exists()
    # 非法形态在真实目录下同样被拒（不误删根目录自身）
    assert svc.delete_version("v2/../../..") == (False, "not_found")
    assert fake_root.is_dir()


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
# 本项目仅供学习使用，商业授权请+Q 3559331368
