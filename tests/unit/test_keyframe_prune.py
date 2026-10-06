"""历史版本裁剪测试（2026-10-07 用户令：每行只留最新 10 版）。

真源 = keyframe._prune_keyframes：保留 created_at 最新 keep 条 ∪ 当前
版本；淘汰版本连主图与 v{N}_shot*.png 侧挂图一起删（R7-AG1 同规则）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

from pathlib import Path

import src.api.manga.keyframe as kf_mod
from src.api.manga.keyframe import _KEYFRAME_KEEP_VERSIONS, _prune_keyframes


class _StubDb:
    """query/delete 最小桩（裁剪只依赖这两个接口）。"""

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.deleted: list[str] = []

    def query(self, sql: str, params=()) -> list[dict]:
        assert "ORDER BY created_at DESC, rowid DESC" in sql
        return self.rows

    def delete(self, table: str, where: str, params=()) -> None:
        assert table == "keyframes" and where == "id=?"
        self.deleted.append(params[0])


def _make_files(root: Path, row_id: str, versions: list[int]) -> None:
    # file_path 带库内相对前缀「keyframes/」（与 DB 记录一致）
    d = root / "keyframes" / row_id
    d.mkdir(parents=True, exist_ok=True)
    for v in versions:
        (d / f"v{v}.png").write_bytes(b"png")
        (d / f"v{v}_shot1.png").write_bytes(b"png")
        (d / f"v{v}_shot2.png").write_bytes(b"png")


def test_prune_keeps_newest_ten_and_current(tmp_path, monkeypatch):
    monkeypatch.setattr(kf_mod, "DATA_DIR", tmp_path)
    row_id = "r1"
    versions = list(range(1, 13))  # v1..v12 共 12 版
    _make_files(tmp_path, row_id, versions)
    rows = [
        {"id": f"k{v:02d}", "version": v,
         "file_path": f"keyframes/{row_id}/v{v}.png", "is_current": 0}
        for v in versions
    ]
    # created_at 倒序 = 版本倒序（v12 最新）；当前版本=v5（用户回退到旧版）
    rows.reverse()
    for r in rows:
        if r["version"] == 5:
            r["is_current"] = 1
    db = _StubDb(rows)
    removed = _prune_keyframes(db, row_id, keep=10)
    # 保留 v12..v3（最新 10）∪ v5（当前）→ 删 v1、v2
    assert removed == 2
    assert sorted(db.deleted) == ["k01", "k02"]
    kdir = tmp_path / "keyframes" / row_id
    assert not (kdir / "v1.png").exists()
    assert not (kdir / "v1_shot1.png").exists()
    assert not (kdir / "v1_shot2.png").exists()
    assert not (kdir / "v2.png").exists()
    # 保留版文件完好（含回退位 v5）
    for v in range(3, 13):
        assert (kdir / f"v{v}.png").is_file(), v


def test_prune_noop_under_limit_and_default_keep(tmp_path, monkeypatch):
    monkeypatch.setattr(kf_mod, "DATA_DIR", tmp_path)
    row_id = "r2"
    _make_files(tmp_path, row_id, [1, 2, 3])
    rows = [{"id": f"k{v}", "version": v,
             "file_path": f"keyframes/{row_id}/v{v}.png",
             "is_current": 1 if v == 3 else 0} for v in (1, 2, 3)]
    rows.reverse()
    db = _StubDb(rows)
    assert _prune_keyframes(db, row_id) == 0
    assert db.deleted == []
    assert _KEYFRAME_KEEP_VERSIONS == 10


def test_prune_missing_files_no_error(tmp_path, monkeypatch):
    monkeypatch.setattr(kf_mod, "DATA_DIR", tmp_path)
    row_id = "r3"
    rows = [{"id": f"k{v}", "version": v,
             "file_path": f"keyframes/{row_id}/v{v}.png", "is_current": 0}
            for v in (2, 1)]
    db = _StubDb(rows)
    # 磁盘无文件（孤儿记录）也应安静裁掉
    assert _prune_keyframes(db, row_id, keep=1) == 1
    assert db.deleted == ["k1"]


def test_prune_shared_file_survives_duplicate_path(tmp_path, monkeypatch):
    """撞号双记录共享 file_path：删其一不得连带删共享图（v20 撞号实报）。"""
    monkeypatch.setattr(kf_mod, "DATA_DIR", tmp_path)
    row_id = "r4"
    d = tmp_path / "keyframes" / row_id
    d.mkdir(parents=True)
    (d / "v20.png").write_bytes(b"png")
    (d / "v20_shot1.png").write_bytes(b"png")
    # created_at 倒序：k20b 更新（保留），k20a 撞号旧条（淘汰）——同 file_path
    rows = [
        {"id": "k21", "version": 21, "file_path": f"keyframes/{row_id}/v21.png", "is_current": 1},
        {"id": "k20b", "version": 20, "file_path": f"keyframes/{row_id}/v20.png", "is_current": 0},
        {"id": "k20a", "version": 20, "file_path": f"keyframes/{row_id}/v20.png", "is_current": 0},
    ]
    db = _StubDb(rows)
    removed = _prune_keyframes(db, row_id, keep=2)
    assert removed == 1 and db.deleted == ["k20a"]
    # 共享文件完好（k20b 仍引用）
    assert (d / "v20.png").is_file()
    assert (d / "v20_shot1.png").is_file()
