"""批2 P8（2026-09-19）单测：上游变更 → 下游关键帧过期标记。

锁定 mark_keyframes_stale 三种定位模式（行/资产反查/项目全量）+
只标当前版本 + 新关键帧生成天然解除 + _load_rows 透传。
Database() 初始化即建全套真实 schema——直接用真表（含 stale_reason 列）。
"""
# 本项目仅供学习使用，商业授权请+Q 3553191368
from __future__ import annotations

from pathlib import Path

import pytest

from src.api.manga.common import _load_rows, mark_keyframes_stale
from src.data.database import Database


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "stale.db")
    d.insert("projects", {"id": "p1", "name": "测试项目",
                          "created_at": 1.0, "updated_at": 1.0})
    d.insert("storyboards", {"id": "sb1", "project_id": "p1"})
    for i, (rid, aid, aids) in enumerate(
            [("r1", "a1", "[]"), ("r2", "a2", "[]"), ("r3", "", '["a1"]')]):
        d.insert("storyboard_rows", {
            "id": rid, "storyboard_id": "sb1", "shot_number": i + 1,
            "description": f"第{i + 1}镜", "asset_id": aid,
            "asset_ids": aids, "sort_index": i})
    # r1 两版本（v1 历史 v2 当前）；r2/r3 各一版当前
    d.insert("keyframes", {"id": "k1a", "row_id": "r1", "project_id": "p1",
                           "version": 1, "is_current": 0, "status": "done"})
    d.insert("keyframes", {"id": "k1b", "row_id": "r1", "project_id": "p1",
                           "version": 2, "is_current": 1, "status": "done"})
    d.insert("keyframes", {"id": "k2a", "row_id": "r2", "project_id": "p1",
                           "version": 1, "is_current": 1, "status": "done"})
    d.insert("keyframes", {"id": "k3a", "row_id": "r3", "project_id": "p1",
                           "version": 1, "is_current": 1, "status": "done"})
    return d


def _stale(db: Database, kid: str) -> str:
    return str(db.query_one("SELECT stale_reason FROM keyframes WHERE id=?",
                            (kid,))["stale_reason"] or "")


def test_mark_by_row_ids_current_only(db: Database) -> None:
    """行定位：只标当前版本，历史版本不动。"""
    n = mark_keyframes_stale(db, row_ids=["r1"], reason="prompt_changed")
    assert n == 1
    assert _stale(db, "k1b") == "prompt_changed"
    assert _stale(db, "k1a") == ""  # 历史版本保留原状


def test_mark_by_asset_binding(db: Database) -> None:
    """资产反查：asset_id 命中 r1，asset_ids JSON 命中 r3。"""
    n = mark_keyframes_stale(db, asset_id="a1", reason="asset_changed")
    assert n == 2
    assert _stale(db, "k1b") == "asset_changed"
    assert _stale(db, "k3a") == "asset_changed"
    assert _stale(db, "k2a") == ""


def test_mark_by_project(db: Database) -> None:
    """项目全量：画风变更打满（仅当前帧；历史版本不计）。"""
    n = mark_keyframes_stale(db, project_id="p1", reason="style_changed")
    assert n == 3  # 三张当前帧（k1b/k2a/k3a；k1a 非当前不计）
    assert _stale(db, "k1b") == "style_changed"
    assert _stale(db, "k1a") == ""


def test_regenerate_clears_stale_naturally(db: Database) -> None:
    """重新生成=落新当前行（stale_reason 默认空）：无需显式清标记。"""
    mark_keyframes_stale(db, row_ids=["r2"], reason="prompt_changed")
    assert _stale(db, "k2a") == "prompt_changed"
    db.update("keyframes", {"is_current": 0}, "row_id=?", ("r2",))
    db.insert("keyframes", {"id": "k2b", "row_id": "r2", "project_id": "p1",
                            "version": 2, "is_current": 1, "status": "done"})
    assert _stale(db, "k2b") == ""


def test_load_rows_attaches_stale(db: Database) -> None:
    """行序列化透传：只有带标记的行携带 stale_reason 字段。"""
    mark_keyframes_stale(db, row_ids=["r2"], reason="prompt_changed")
    rows = {r["id"]: r for r in _load_rows(db, "sb1")}
    assert rows["r2"].get("stale_reason") == "prompt_changed"
    assert "stale_reason" not in rows["r1"]  # 无标记行不加字段


def test_rejects_unknown_reason(db: Database) -> None:
    with pytest.raises(ValueError, match="未知过期原因"):
        mark_keyframes_stale(db, row_ids=["r1"], reason="nonsense")
