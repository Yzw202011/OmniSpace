"""推理后资产自动绑定单测（2026-10-04 用户令：推理出的无图资产桩自动
绑到分镜行；同名≠同角色二定后仅绑项目域）。

覆盖：
- 仅项目域：跨项目/全局同名资产（样貌可能不同）绝不自动绑；
- 项目内同 (kind,name) 多行（同名不同貌并存）= 歧义跳过；
- 不覆盖：已有 asset_ids 的行原样保留；
- 宽容拆分：scene 单字符串按 顿号/逗号 分隔多场景；
- 顺序稳定：角色→场景→道具、去重；无候选零副作用。
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
            # 仅项目域候选池（autobind 语义）；全局/他项目不进池
            return [a for a in self.assets
                    if a["project_id"] == params[0]
                    and a.get("scope") != "global"]
        if "FROM storyboard_rows" in sql:
            return list(self.rows.values())
        raise AssertionError("unexpected sql: " + sql)

    def update(self, table: str, fields: dict, where: str, params=()):  # noqa: ANN001
        self.updates.append((table, fields, where))
        rid = params[0]
        self.rows[rid].update(fields)


def _proj(aid: str, kind: str, name: str, project: str = "p1") -> dict:
    return {"id": aid, "kind": kind, "name": name,
            "project_id": project, "scope": "project"}


def _glob(aid: str, kind: str, name: str) -> dict:
    return {"id": aid, "kind": kind, "name": name,
            "project_id": "", "scope": "global"}


def test_project_only_and_in_project_ambiguity() -> None:
    """全局同名（样貌可能不同）不绑；项目内同名多行=歧义跳过。"""
    db = _FakeDB(
        assets=[
            _proj("a_proj", "character", "夏沐沐"),
            _glob("a_glob", "character", "夏沐沐"),   # 同名不同貌：不进池
            _glob("g1", "character", "林飞雪"),       # 仅全局存在：不绑
            _proj("m1", "character", "林小曼"),        # 项目内同名×2（不同貌）
            _proj("m2", "character", "林小曼"),
        ],
        rows=[
            {"id": "r1", "characters": json.dumps(["夏沐沐"]),
             "scene": "", "props": "[]", "asset_ids": None},
            {"id": "r2", "characters": json.dumps(["林飞雪"]),
             "scene": "", "props": "[]", "asset_ids": None},
            {"id": "r3", "characters": json.dumps(["林小曼"]),
             "scene": "", "props": "[]", "asset_ids": None},
        ])
    stats = auto_bind_rows(db, "p1", list(db.rows.values()))
    # 夏沐沐：项目唯一候选 → 绑；林飞雪：仅全局 → 不绑不计歧义；
    # 林小曼：项目内×2 同名不同貌 → 歧义跳过
    assert stats == {"bound_rows": 1, "bound_assets": 1,
                     "skipped_ambiguous": 1}
    assert db.updates[0][1]["asset_ids"] == ["a_proj"]
    assert db.rows["r2"].get("asset_ids") is None
    assert db.rows["r3"].get("asset_ids") is None


def test_scene_split_and_order_and_no_manual_overwrite() -> None:
    db = _FakeDB(
        assets=[
            _proj("c1", "character", "夏沐沐"),
            _proj("s1", "scene", "海边"),
            _proj("s2", "scene", "码头"),
            _proj("p1_", "prop", "行李箱"),
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
