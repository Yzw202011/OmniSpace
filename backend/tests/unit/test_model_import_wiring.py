"""模型导入完整接入（2026-09-01 选项 B · 原地登记式）单元测试。

覆盖链路：
  1. ModelImporter 自动分类 + 递归大小 + 大文件跳过 SHA256
  2. POST /models/import 自动归类 / 幂等 / 显存估算落库
  3. _merged_models 外部路径登记条目 downloaded=true（可见性）
  4. ModelManager.resolve_model_path 登记表兜底
  5. imported_dialog_models / imported_paint_models 引擎侧兜底
  6. PaintEngine._pick_model 家族锚点 + 陌生家族明确报错
  7. switch_engine._is_vllm_model 外部路径 AWQ 判定

隔离策略：tmp_path 临时数据库（monkeypatch _db_instance），不触碰
data/omnispace.db；TestClient 不进入 with 块（不触发 lifespan）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.data import database as db_mod
from backend.data.database import Database
from backend.services.model_manager.importer import ModelImporter

pytestmark = pytest.mark.smoke

# 测试权重体积：≥8MB 才能在 GB 三位小数与 min_vram 两位小数舍入后 > 0
_MB = 1024 * 1024
# 对话发现层权重门槛（dialog_engine._MIN_WEIGHT_BYTES 同值）
_MIN_WEIGHT = 100 * _MB + 1024


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离数据库的 TestClient（不触发 lifespan）。"""
    test_db = Database(tmp_path / "import_test.db")
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    from fastapi.testclient import TestClient

    from backend.main import app
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """隔离数据库句柄（不经 HTTP 层的用例用）。"""
    test_db = Database(tmp_path / "unit_test.db")
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    return test_db


def _make_vl_dir(base: Path, name: str = "qwen3-test-vl") -> Path:
    """构造 transformers VL 布局的假模型目录（权重达 100MB 发现门槛）。"""
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps({
        "model_type": "qwen3_vl",
        "architectures": ["Qwen3VLForConditionalGeneration"],
    }), encoding="utf-8")
    (d / "model.safetensors").write_bytes(b"\0" * _MIN_WEIGHT)
    return d


def _make_sdxl_dir(base: Path, name: str = "my-sdxl-checkpoint") -> Path:
    """构造 diffusers SDXL 布局的假模型目录。"""
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "model_index.json").write_text(json.dumps({
        "_class_name": "StableDiffusionXLPipeline",
    }), encoding="utf-8")
    return d


# ── 1. ModelImporter ─────────────────────────────────────────────

def test_importer_classifies_gguf_as_dialog(tmp_path):
    gguf = tmp_path / "MyChat-3B-Q4_K.gguf"
    gguf.write_bytes(b"\0" * (8 * _MB))
    r = ModelImporter().import_model(str(gguf), max_sha_gb=0)
    assert r.success, r.error
    assert r.category.value == "dialog"
    assert r.size_gb > 0
    assert r.sha256 == ""          # max_sha_gb=0 → 一律跳过哈希
    assert r.model_id.startswith("dialog_")


def test_importer_dir_recursive_size(tmp_path):
    d = tmp_path / "some-model-dir"
    d.mkdir()
    (d / "a.bin").write_bytes(b"\0" * (5 * _MB))
    sub = d / "nested"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"\0" * (5 * _MB))
    r = ModelImporter().import_model(str(d), max_sha_gb=0)
    assert r.size_gb > 0            # 目录导入不再恒为 0（旧缺陷回归锁）


def test_importer_small_file_sha_computed(tmp_path):
    f = tmp_path / "tiny.gguf"
    f.write_bytes(b"\0" * 512)
    r = ModelImporter().import_model(str(f))
    assert len(r.sha256) == 64      # 小文件照常算 SHA256


# ── 2. 导入端点：自动归类 / 幂等 ──────────────────────────────────

def test_import_endpoint_auto_category_and_idempotent(client, tmp_path):
    gguf = tmp_path / "test-chat-3b.gguf"
    gguf.write_bytes(b"\0" * (8 * _MB))
    r1 = client.post("/api/v1/models/import", json={"path": str(gguf)})
    assert r1.status_code == 200, r1.text
    body1 = r1.json()["data"]
    assert body1["category"] == "dialog"
    assert body1["purpose"] == "用户导入"
    assert body1["min_vram_gb"] > 0
    assert body1["file_path"] == str(gguf)

    r2 = client.post("/api/v1/models/import", json={"path": str(gguf)})
    assert r2.status_code == 200
    assert r2.json()["data"]["id"] == body1["id"]   # 幂等：同路径不重复登记


def test_import_endpoint_missing_path_error(client, tmp_path):
    r = client.post("/api/v1/models/import",
                    json={"path": str(tmp_path / "nope.gguf")})
    # 本项目业务错误约定：HTTP 200 + error 体（error_handler.py:317）
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] in (30001, "MODEL_FILE_NOT_FOUND")


# ── 3. 合并视图可见性（外部路径 downloaded=true）──────────────────

def test_merged_models_external_import_visible(client, tmp_path):
    gguf = tmp_path / "visible-chat.gguf"
    gguf.write_bytes(b"\0" * 4096)
    resp = client.post("/api/v1/models/import", json={"path": str(gguf)})
    mid = resp.json()["data"]["id"]

    from backend.api.models import _merged_models
    hit = next((m for m in _merged_models() if m["id"] == mid), None)
    assert hit is not None, "导入记录未进入合并视图"
    assert hit["downloaded"] is True, "外部路径在盘应判 downloaded（可见性闸门）"
    assert hit["status"] == "ready"


# ── 4. ModelManager 登记表路径兜底 ─────────────────────────────────

def test_resolve_model_path_registry_fallback(client, tmp_path):
    gguf = tmp_path / "resolvable-chat.gguf"
    gguf.write_bytes(b"\0" * 4096)
    mid = client.post("/api/v1/models/import",
                      json={"path": str(gguf)}).json()["data"]["id"]

    from backend.services.model_manager import get_model_manager
    mgr = get_model_manager()
    assert mgr.resolve_model_path(mid) == str(gguf)

    row = mgr.registry_entry(mid)
    assert row is not None and row["category"] == "dialog"
    assert mgr.registry_entry("不存在的id") is None


def test_estimate_vram_registry_branch(client, tmp_path):
    gguf = tmp_path / "vram-est.gguf"
    gguf.write_bytes(b"\0" * 4096)
    mid = client.post("/api/v1/models/import",
                      json={"path": str(gguf)}).json()["data"]["id"]
    from backend.services.model_manager import get_model_manager
    est = get_model_manager().estimate_vram_gb(mid, "dialog")
    assert est > 0        # 登记表 min_vram_gb 分支（导入落库 size×1.2）


# ── 5. 引擎侧登记表兜底 ───────────────────────────────────────────

def test_imported_dialog_models_vl_dir(isolated_db, tmp_path):
    d = _make_vl_dir(tmp_path)
    isolated_db.insert("models", {
        "id": "dialog_import_x", "name": "x", "category": "dialog",
        "purpose": "用户导入", "size_gb": 1.0, "min_vram_gb": 1.2,
        "status": "ready", "file_path": str(d), "sha256": "",
    })
    from backend.services.inference.dialog_engine import imported_dialog_models
    hits = imported_dialog_models()
    assert "dialog_import_x" in hits
    assert hits["dialog_import_x"]["backend"] == "vl"
    assert hits["dialog_import_x"]["path"] == str(d)


def test_imported_dialog_models_excludes_non_dialog(isolated_db, tmp_path):
    d = _make_vl_dir(tmp_path, "whisper-fake")
    (d / "config.json").write_text(json.dumps({
        "model_type": "whisper",
        "architectures": ["WhisperForConditionalGeneration"],
    }), encoding="utf-8")
    isolated_db.insert("models", {
        "id": "voice_import_y", "name": "y", "category": "voice",
        "purpose": "用户导入", "size_gb": 1.0, "min_vram_gb": 1.0,
        "status": "ready", "file_path": str(d), "sha256": "",
    })
    from backend.services.inference.dialog_engine import imported_dialog_models
    assert "voice_import_y" not in imported_dialog_models()


def test_imported_paint_models_family_gate(isolated_db, tmp_path):
    ok_dir = _make_sdxl_dir(tmp_path)
    bad_dir = tmp_path / "unknown-family"
    bad_dir.mkdir()
    (bad_dir / "model_index.json").write_text(json.dumps({
        "_class_name": "SomeUnknownPipeline",
    }), encoding="utf-8")
    isolated_db.insert("models", {
        "id": "vision_ok", "name": "ok", "category": "vision",
        "purpose": "用户导入", "size_gb": 5.0, "min_vram_gb": 6.0,
        "status": "ready", "file_path": str(ok_dir), "sha256": "",
    })
    isolated_db.insert("models", {
        "id": "vision_bad", "name": "bad", "category": "vision",
        "purpose": "用户导入", "size_gb": 5.0, "min_vram_gb": 6.0,
        "status": "ready", "file_path": str(bad_dir), "sha256": "",
    })
    from backend.services.inference.paint_engine import imported_paint_models
    hits = imported_paint_models()
    assert "vision_ok" in hits and hits["vision_ok"]["anchor"] == "sdxl-base-1.0"
    assert "vision_bad" not in hits    # 陌生家族不进清单


# ── 6. PaintEngine._pick_model 家族锚点 / 明确报错 ────────────────

def test_paint_pick_model_imported_anchor_and_error(isolated_db, tmp_path):
    from backend.services.inference.paint_engine import get_paint_engine
    ok_dir = _make_sdxl_dir(tmp_path)
    bad_dir = tmp_path / "stranger"
    bad_dir.mkdir()
    (bad_dir / "model_index.json").write_text(json.dumps({
        "_class_name": "MysteryPipeline",
    }), encoding="utf-8")
    isolated_db.insert("models", {
        "id": "vision_pick_ok", "name": "ok", "category": "vision",
        "purpose": "用户导入", "size_gb": 5.0, "min_vram_gb": 6.0,
        "status": "ready", "file_path": str(ok_dir), "sha256": "",
    })
    isolated_db.insert("models", {
        "id": "vision_pick_bad", "name": "bad", "category": "vision",
        "purpose": "用户导入", "size_gb": 5.0, "min_vram_gb": 6.0,
        "status": "ready", "file_path": str(bad_dir), "sha256": "",
    })
    engine = get_paint_engine()
    pick = engine._pick_model("vision_pick_ok")
    assert pick is not None
    assert pick[0] == "sdxl-base-1.0"      # 家族锚点（复用家族管线）
    assert pick[1] == ok_dir               # 导入路径透传

    assert engine._pick_model("vision_pick_bad") is None
    assert "暂不支持" in engine._last_error  # 明确报错不装死


# ── 7. switch_engine 外部路径 AWQ 判定 ────────────────────────────

def test_is_vllm_model_external_awq(isolated_db, tmp_path):
    d = tmp_path / "ext-awq-model"
    d.mkdir()
    (d / "config.json").write_text(json.dumps({
        "model_type": "qwen3_vl",
        "architectures": ["Qwen3VLForConditionalGeneration"],
        "quantization_config": {"quant_method": "awq"},
    }), encoding="utf-8")
    isolated_db.insert("models", {
        "id": "dialog_ext_awq", "name": "awq", "category": "dialog",
        "purpose": "用户导入", "size_gb": 7.0, "min_vram_gb": 8.0,
        "status": "ready", "file_path": str(d), "sha256": "",
    })
    from backend.services.switch_engine import _is_vllm_model
    assert _is_vllm_model("dialog_ext_awq") is True
    assert _is_vllm_model("plain-nonexistent-id") is False
# 本项目仅供学习使用，商业授权请+Q 3559331368
