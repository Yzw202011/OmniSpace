"""模型删除正规通道·manifest 同步哨兵（UAT 2026-09-10 缺陷⑨根修）。

背景：当日三次删除（deepseek/GGUF-9B/qwen-edit+wan×2）都不同步
models_manifest.json → 幽灵条目（登记有磁盘无）靠提交闸兜底。
remove_manifest_entry 让删除正规通道具备登记簿同步能力：
- 命中移除 → True 且文件更新（保留其余条目与顶层结构）；
- 条目不存在 → False 且不写文件；
- 清单损坏 → False 且**不覆写原文件**（对齐模块「损坏按缺失降级」边界）。
"""
from __future__ import annotations

import json
from pathlib import Path

from backend.data import model_registry as reg


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
