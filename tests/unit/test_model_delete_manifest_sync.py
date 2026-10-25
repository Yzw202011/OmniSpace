"""模型删除正规通道·manifest 同步哨兵（UAT 2026-09-10 缺陷⑨根修）。

背景：当日三次删除（deepseek/GGUF-9B/qwen-edit+wan×2）都不同步
models_manifest.json → 幽灵条目（登记有磁盘无）靠提交闸兜底。
remove_manifest_entry 让删除正规通道具备登记簿同步能力：
- 命中移除 → True 且文件更新（保留其余条目与顶层结构）；
- 条目不存在 → False 且不写文件；
- 清单损坏 → False 且**不覆写原文件**（对齐模块「损坏按缺失降级」边界）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import json
from pathlib import Path

from src.data import model_registry as reg


def _setup(tmp_path: Path, monkeypatch, entries: dict, raw: str | None = None) -> Path:
    mp = tmp_path / "models_manifest.json"
    content = raw if raw is not None else json.dumps(
        {"version": "3.0.0", "manifest_version": 3,
         "models": entries}, ensure_ascii=False, indent=1) + "\n"
    mp.write_text(content, encoding="utf-8", newline="\n")
    monkeypatch.setattr(reg, "MANIFEST_PATH", mp)
    return mp


def test_remove_hits_and_preserves_others(tmp_path, monkeypatch) -> None:
    mp = _setup(tmp_path, monkeypatch, {
        "model_a": {"name": "A", "type": "dialog", "path": "a",
                    "capabilities": ["text"], "lifecycle": "inproc"},
        "model_b": {"name": "B", "type": "dialog", "path": "b",
                    "capabilities": ["text"], "lifecycle": "inproc"},
    })
    assert reg.remove_manifest_entry("model_a") is True
    data = json.loads(mp.read_text(encoding="utf-8"))
    assert "model_a" not in data["models"] and "model_b" in data["models"]
    assert data["manifest_version"] == 3


def test_remove_missing_returns_false_without_write(tmp_path, monkeypatch) -> None:
    mp = _setup(tmp_path, monkeypatch, {
        "model_b": {"name": "B", "type": "dialog", "path": "b",
                    "capabilities": ["text"], "lifecycle": "inproc"},
    })
    before = mp.read_text(encoding="utf-8")
    assert reg.remove_manifest_entry("ghost") is False
    assert mp.read_text(encoding="utf-8") == before


def test_remove_corrupt_manifest_keeps_file(tmp_path, monkeypatch) -> None:
    """清单损坏：拒绝移除且**不覆写**（诚实边界，损坏按缺失降级）。"""
    raw = "{ this is not json !!!"
    mp = _setup(tmp_path, monkeypatch, {}, raw=raw)
    assert reg.remove_manifest_entry("anything") is False
    assert mp.read_text(encoding="utf-8") == raw


def test_remove_missing_file_returns_false(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(reg, "MANIFEST_PATH", tmp_path / "nope.json")
    assert reg.remove_manifest_entry("x") is False


# ── _sync_manifest 委托接线（2026-10-25 止血回归锁）──────────────────
# 事故：R2-H10 首版自带 JSON 读写按 list 处理 models 字段，而 v3 真源
# 是 dict——purge/delete 端点任一调用都把真注册表清成空 list 落盘
# （models/models_manifest.json 22 条→0 实锤）。根修后 api 层委托
# 本模块单写者；以下锁住「dict 格式永不被清空」契约。

def test_sync_remove_preserves_dict_format(tmp_path, monkeypatch) -> None:
    """api._sync_manifest(remove)：摘一条，其余 dict 条目分毫无损。"""
    from src.api import models as models_api
    mp = _setup(tmp_path, monkeypatch, {
        "model_a": {"name": "A", "type": "dialog", "path": "a",
                    "capabilities": ["text"], "lifecycle": "inproc"},
        "model_b": {"name": "B", "type": "dialog", "path": "b",
                    "capabilities": ["text"], "lifecycle": "inproc"},
    })
    models_api._sync_manifest("model_a", "remove")
    data = json.loads(mp.read_text(encoding="utf-8"))
    assert isinstance(data["models"], dict), "models 必须保持 dict（v3 契约）"
    assert set(data["models"]) == {"model_b"}


def test_registry_add_entry_contract(tmp_path, monkeypatch) -> None:
    """add_manifest_entry：完整条目写入；薄条目拒写不动文件。"""
    mp = _setup(tmp_path, monkeypatch, {})
    full = {"name": "N", "type": "paint", "path": "p",
            "capabilities": ["image"], "lifecycle": "inproc"}
    assert reg.add_manifest_entry("m1", full) is True
    assert json.loads(mp.read_text(encoding="utf-8"))["models"]["m1"] == full
    before = mp.read_text(encoding="utf-8")
    assert reg.add_manifest_entry("m2", {"name": "薄条目"}) is False
    assert mp.read_text(encoding="utf-8") == before


def test_sync_add_skips_thin_entries(tmp_path, monkeypatch) -> None:
    """api._sync_manifest(add)：合并视图供不齐 v3 必填字段→跳过不写。

    导入链的合并记录只有 id/name/category 等薄字段——薄条目一旦落盘
    会击穿 test_manifest_v3_schema，必须拒收（出包全量重写补全）。
    """
    from src.api import models as models_api
    mp = _setup(tmp_path, monkeypatch, {})
    monkeypatch.setattr(
        models_api, "_merged_models",
        lambda: [{"id": "thin", "name": "薄", "category": "llm"}])
    before = mp.read_text(encoding="utf-8")
    models_api._sync_manifest("thin", "add")
    assert mp.read_text(encoding="utf-8") == before
