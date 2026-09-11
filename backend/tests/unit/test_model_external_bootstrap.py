"""外部模型包登记单测（2026-09-03 体验流 2b：跨盘降级的后端侧）。

覆盖：marker 读取健壮性、manifest→注册表行（盘上存在性过滤+类型映射）、
幂等回填（新插/异径更/同径跳过）。bootstrap_external_models 本体是
薄壳（真 DB），不在此触碰开发库。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MOD_PATH = (ROOT / "backend" / "services" / "model_manager"
            / "external_bootstrap.py")

spec = importlib.util.spec_from_file_location("_ext_boot_under_test", MOD_PATH)
eb = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = eb
spec.loader.exec_module(eb)


class _StubDB:
    """get_db_safe 句柄的最小替身（query_one/insert/update 记账）。"""

    def __init__(self, rows: dict | None = None):
        self.rows: dict[str, dict] = rows or {}
        self.inserted: list[str] = []
        self.updated: list[str] = []

    def query_one(self, sql: str, params=()):
        if "FROM models" in sql:
            row = self.rows.get(params[0])
            if row is None:
                return None
            return {"id": params[0], "file_path": row.get("file_path")}
        return None

    def insert(self, table: str, data: dict) -> str:
        self.rows[data["id"]] = dict(data)
        self.inserted.append(data["id"])
        return "1"

    def update(self, table: str, data: dict, where: str, params=()) -> int:
        mid = params[0]
        self.rows[mid].update(data)
        self.updated.append(mid)
        return 1


def _mk_manifest_root(tmp_path: Path) -> Path:
    root = tmp_path / "ext_models"
    (root / "video_gen" / "h3").mkdir(parents=True)
    (root / "qwen3-vl-4b").mkdir()
    root.joinpath("models_manifest.json").write_text(json.dumps({
        "version": "3.0.0",
        "models": {
            "minimax-h3": {"name": "MiniMax H3", "type": "video_gen",
                           "path": "video_gen/h3", "size_gb": 42.7},
            "qwen3-vl-4b": {"name": "Qwen3-VL-4B", "type": "dialog",
                            "path": "qwen3-vl-4b", "size_gb": 8.4},
            "ghost-model": {"name": "幽灵", "type": "dialog",
                            "path": "ghost", "size_gb": 1.0},
        }}, ensure_ascii=False), encoding="utf-8")
    return root


def test_read_marker(tmp_path):
    m = tmp_path / "marker.json"
    m.write_text(json.dumps({"root": str(tmp_path)}), encoding="utf-8")
    assert eb.read_marker(m) == str(tmp_path)
    m.write_text("not-json", encoding="utf-8")
    assert eb.read_marker(m) is None
    assert eb.read_marker(tmp_path / "nope.json") is None


def test_rows_from_manifest_filters_missing_dirs(tmp_path):
    root = _mk_manifest_root(tmp_path)
    rows = eb.external_rows_from_manifest(root)
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {"minimax-h3", "qwen3-vl-4b"}  # ghost 被盘上过滤
    assert by_id["minimax-h3"]["category"] == "video"
    assert by_id["qwen3-vl-4b"]["category"] == "dialog"
    assert by_id["minimax-h3"]["file_path"].endswith(
        ("video_gen\\h3", "video_gen/h3"))
    assert by_id["minimax-h3"]["status"] == "ready"
    assert by_id["qwen3-vl-4b"]["min_vram_gb"] == round(8.4 * 1.2, 2)


def test_sync_idempotent_upsert(tmp_path):
    root = _mk_manifest_root(tmp_path)
    rows = eb.external_rows_from_manifest(root)
    db = _StubDB()
    r1 = eb.sync_external_models(db, rows)
    assert r1 == {"inserted": 2, "updated": 0, "unchanged": 0}
    r2 = eb.sync_external_models(db, rows)       # 同径重跑 → 全部 unchanged
    assert r2["unchanged"] == 2 and not r2["inserted"] and not r2["updated"]
    # 同 id 异径（用户换了外部盘）→ 只回填物理字段
    db.rows["qwen3-vl-4b"]["file_path"] = "D:/old/models/qwen3-vl-4b"
    r3 = eb.sync_external_models(db, rows)
    assert r3["updated"] == 1
    assert db.rows["qwen3-vl-4b"]["file_path"].endswith("qwen3-vl-4b")
    assert db.rows["qwen3-vl-4b"]["purpose"] == "外部模型包（跨盘）"


def test_sync_existing_seed_row_keeps_identity(tmp_path):
    """预置行（路由表播种）撞 id：只回填物理字段，身份字段保持原值。"""
    root = _mk_manifest_root(tmp_path)
    rows = eb.external_rows_from_manifest(root)
    db = _StubDB({"qwen3-vl-4b": {"file_path": "", "category": "dialog",
                                  "purpose": "路由表预置"}})
    r = eb.sync_external_models(db, rows)
    # qwen3-vl-4b 预置行→回填；minimax-h3 无行→新插
    assert r["updated"] == 1 and r["inserted"] == 1
    assert db.updated == ["qwen3-vl-4b"]
    # 身份字段未被外部行覆盖（update 只送物理字段）
    assert db.rows["qwen3-vl-4b"]["purpose"] == "路由表预置"
# 本项目仅供学习使用，商业授权请+Q 3559331368
