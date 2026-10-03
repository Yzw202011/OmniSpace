"""推理后资产自动绑定单测（2026-10-04 用户令：推理出的无图资产桩自动
绑到分镜行）。

覆盖：
- 优先级：项目同名 > 全局唯一；项目内多同名也算歧义跳过；
- 不覆盖：已有 asset_ids 的行原样保留；
- 宽容拆分：scene 单字符串按 顿号/逗号 分隔多场景；
- 顺序稳定：角色→场景→道具、去重；
- 无候选资产时零副作用。
"""
from __future__ import annotations

import json

from src.api.manga.asset_autobind import auto_bind_rows


class _FakeDB:
    """最小 db 桩：query 按 SQL 关键字回 canned 行；update 记录写操作。"""

    def __init__(self, assets: list[dict], rows: list[dict]) -> None:
        self.assets = assets
        self.rows = {r["id"]: dict(r) for r in rows}
        self.updates: list[tuple[str, dict, str]] = []

    def query(self, sql: str, params=()):  # noqa: ANN001
        if "FROM comic_assets" in sql:
            out = []
            if "scope<>'global'" in sql:
                out += [a for a in self.assets
                        if a["project_id"] == params[0]
                        and a.get("scope") != "global"]
            if "scope='global'" in sql or "OR" in sql:
                out += [a for a in self.assets if a.get("scope") == "global"]
            return out
        if "FROM storyboard_rows" in sql:
            return list(self.rows.values())
        raise AssertionError("unexpected sql: " + sql)

    def update(self, table: str, fields: dict, where: str, params=()):  # noqa: ANN001
        self.updates.append((table, fields, where))
        rid = params[0]
        self.rows[rid].update(fields)


def test_priority_project_over_global_and_ambiguity_skip() -> None:
    db = _FakeDB(
        assets=[
            {"id": "a_proj", "kind": "character", "name": "夏沐沐",
             "project_id": "p1", "scope": "project"},
            {"id": "a_glob", "kind": "character", "name": "夏沐沐",
             "project_id": "", "scope": "global"},
            {"id": "g1", "kind": "character", "name": "林飞雪",
             "project_id": "", "scope": "global"},
            {"id": "g2", "kind": "character", "name": "林飞雪",
             "project_id": "", "scope": "global"},
            {"id": "s1", "kind": "scene", "name": "别墅区街角大门",
             "project_id": "p1", "scope": "project"},
        ],
        rows=[
            {"id": "r1", "characters": json.dumps(["夏沐沐"]),
             "scene": "", "props": "[]", "asset_ids": None},
            {"id": "r2", "characters": json.dumps(["林飞雪"]),
             "scene": "", "props": "[]", "asset_ids": None},
        ])
    stats = auto_bind_rows(db, "p1", list(db.rows.values()))
    # 夏沐沐：项目+全局同名 → 项目优先绑定 a_proj
    # 林飞雪：全局×2 同名 → 歧义跳过
    assert stats == {"bound_rows": 1, "bound_assets": 1,
                     "skipped_ambiguous": 1}
    assert db.updates[0][1]["asset_ids"] == ["a_proj"]
    assert db.rows["r2"].get("asset_ids") is None


def test_scene_split_and_order_and_no_manual_overwrite() -> None:
    db = _FakeDB(
        assets=[
            {"id": "c1", "kind": "character", "name": "夏沐沐",
             "project_id": "p1", "scope": "project"},
            {"id": "s1", "kind": "scene", "name": "海边",
             "project_id": "", "scope": "global"},
            {"id": "s2", "kind": "scene", "name": "码头",
             "project_id": "", "scope": "global"},
            {"id": "p1_", "kind": "prop", "name": "行李箱",
             "project_id": "p1", "scope": "project"},
        ],
        rows=[
            {"id": "r1", "characters": json.dumps(["夏沐沐"]),
             "scene": "海边、码头", "props": json.dumps(["行李箱"]),
             "asset_ids": None},
            {"id": "r2", "characters": json.dumps(["夏沐沐"]),
             "scene": "", "props": "[]",
             "asset_ids": json.dumps(["manual_id"])},
        ])
    stats = auto_bind_rows(db, "p1", list(db.rows.values()))
    assert stats["bound_rows"] == 1
    assert stats["bound_assets"] == 4
    # 顺序稳定：角色→场景（按行内顺序）→道具；同名不重复
    assert db.updates[0][1]["asset_ids"] == ["c1", "s1", "s2", "p1_"]
    # r2 已有手动绑定：零写库
    assert db.rows["r2"]["asset_ids"] == json.dumps(["manual_id"])
    assert len(db.updates) == 1


def test_no_candidates_zero_side_effect() -> None:
    db = _FakeDB(assets=[], rows=[
        {"id": "r1", "characters": json.dumps(["谁谁谁"]),
         "scene": "", "props": "[]", "asset_ids": None}])
    stats = auto_bind_rows(db, "p1", list(db.rows.values()))
    assert stats == {"bound_rows": 0, "bound_assets": 0,
                     "skipped_ambiguous": 0}
    assert db.updates == []
