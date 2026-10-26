"""R1 死资产绑定白名单过滤单测（2026-10-26 用户拍板）。

事故：04:47 删除角色后，前端旧 state 保存把死 id 写回 storyboard_rows
（6 秒复活）→ 分格说话人「未知资产」+「undefined的台词」。
契约：行保存写入 asset_ids / bubbles 时按现存资产 id 白名单过滤——
死 id 写不回库；speaker 死亡的气泡置空转旁白（文本保留）；活 id 全保留。
"""
from __future__ import annotations

from typing import Any

from src.api.manga.storyboard import _sanitize_row_asset_bindings


class FakeDB:
    """query 返回预设存活 id 集合（id IN (...) 查询）。"""

    def __init__(self, live_ids: list[str]) -> None:
        self.live = set(live_ids)
        self.queries = 0

    def query(self, sql: str, params: Any = ()) -> list[dict]:
        self.queries += 1
        return [{"id": p} for p in params if p in self.live]


DEAD = "613f7799deadbeef613f7799deadbeef"
LIVE_A = "aaaa0000000000000000000000000000"
LIVE_B = "bbbb0000000000000000000000000000"


def test_dead_ids_filtered_from_asset_ids() -> None:
    db = FakeDB([LIVE_A])
    fields: dict[str, Any] = {"asset_ids": [DEAD, LIVE_A]}
    _sanitize_row_asset_bindings(db, fields)
    assert fields["asset_ids"] == [LIVE_A]


def test_live_ids_all_kept() -> None:
    db = FakeDB([LIVE_A, LIVE_B])
    fields: dict[str, Any] = {"asset_ids": [LIVE_A, LIVE_B]}
    _sanitize_row_asset_bindings(db, fields)
    assert fields["asset_ids"] == [LIVE_A, LIVE_B]


def test_dead_bubble_speaker_becomes_narration() -> None:
    """speaker 死亡 → asset_id 置空转旁白，text 保留不丢。"""
    db = FakeDB([LIVE_A])
    fields: dict[str, Any] = {"bubbles": [
        {"asset_id": DEAD, "text": "这个题目好难啊", "shape": "dialogue"},
        {"asset_id": LIVE_A, "text": "李小曼的台词"},
        {"asset_id": "", "text": "旁白"},
    ]}
    _sanitize_row_asset_bindings(db, fields)
    b = fields["bubbles"]
    assert b[0]["asset_id"] == "" and b[0]["text"] == "这个题目好难啊"
    assert b[1]["asset_id"] == LIVE_A
    assert b[2]["asset_id"] == ""


def test_unrelated_fields_skip_query() -> None:
    """不涉及绑定字段时零查库（微开销护栏）。"""
    db = FakeDB([])
    fields: dict[str, Any] = {"description": "新描述"}
    _sanitize_row_asset_bindings(db, fields)
    assert db.queries == 0


def test_empty_bindings_skip_query() -> None:
    db = FakeDB([])
    fields: dict[str, Any] = {"asset_ids": [], "bubbles": []}
    _sanitize_row_asset_bindings(db, fields)
    assert db.queries == 0


def test_sanitize_wired_into_row_update_sentinel() -> None:
    """哨兵：单行保存端点必须接入过滤（防回退）。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "src" / "api"
           / "manga" / "storyboard.py").read_text(encoding="utf-8")
    assert "_sanitize_row_asset_bindings(db, update_fields)" in src, (
        "单行保存端点未接入死绑定过滤")
    assert src.index("def _sanitize_row_asset_bindings") < src.index(
        "_sanitize_row_asset_bindings(db, update_fields)")
# 本项目仅供学习使用，商业授权请+Q 3559331368
