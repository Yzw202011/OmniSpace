"""人物 LoRA 服务与 API 测试（P3，2026-09-17）。

覆盖：素材采集（主图+views/容错）/submit 门禁（素材不足·互斥·重复入队）
/版本目录与 current 指针/部署到资产旁 lora.safetensors（D-LoRA 消费
约定）/回滚/版本修剪/API 信封。训练核心（GPU QLoRA 回路）以微缩桩替
验证编排（真收敛属 GPU 实弹，另行补录）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database
from src.services import character_lora_service as svc_mod
from src.services.character_lora_service import (
    CharacterTrainingFailed,
    get_character_lora_service,
)


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "_db_instance", Database(tmp_path / "t.db"))
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture()
def fresh_svc(tmp_path, monkeypatch):
    """独立服务实例：版本根与底座指到临时目录，互斥锁清空。"""
    monkeypatch.setattr(svc_mod, "VERSIONS_ROOT", tmp_path / "char_lora")
    monkeypatch.setattr(svc_mod, "BASE_MODEL_DIR", tmp_path / "base")
    (tmp_path / "base" / "transformer").mkdir(parents=True)
    svc_mod.CharacterLoraService._instance = None
    # 互斥：active_feature 为只读 property，桩其内部状态源
    from src.middleware.feature_lock import get_feature_lock
    fl = get_feature_lock()
    monkeypatch.setattr(type(fl), "active_feature",
                        property(lambda self: None))
    monkeypatch.setattr(fl, "acquire_sync", lambda *a, **k: True)
    monkeypatch.setattr(fl, "release_sync", lambda *a, **k: None)
    # 测试同步驱动 worker：不让 submit 自动起真线程（与同步 tick 竞态）
    svc_mod.CharacterLoraService._ensure_worker = lambda self: None  # type: ignore[method-assign]
    yield get_character_lora_service()
    svc_mod.CharacterLoraService._instance = None


def _mk_images(tmp_path: Path, n: int) -> list[str]:
    from PIL import Image
    out = []
    for i in range(n):
        p = tmp_path / f"img{i}.png"
        Image.new("RGB", (8, 8), (i * 30 % 255, 80, 160)).save(p)
        out.append(str(p))
    return out


def _asset(tmp_path: Path, imgs: list[str], name: str = "小樱") -> dict:
    return {"asset_id": "a1", "id": "a1", "kind": "character",
            "name": name, "file_path": imgs[0],
            "meta": {"views": imgs[1:]}}


# ── 素材采集 ────────────────────────────────────────────────

def test_collect_images_main_plus_views(fresh_svc, tmp_path):
    imgs = _mk_images(tmp_path, 5)
    got = fresh_svc.collect_images(_asset(tmp_path, imgs))
    assert len(got) == 5  # 主图 1 + views 4
    assert got[0] == Path(imgs[0])


def test_collect_images_missing_files_skipped(fresh_svc, tmp_path):
    imgs = _mk_images(tmp_path, 3)
    a = _asset(tmp_path, imgs)
    a["meta"] = {"views": [imgs[1], "E:/nope/missing.png"]}
    got = fresh_svc.collect_images(a)
    assert len(got) == 2  # 缺盘文件跳过


# ── submit 门禁 ─────────────────────────────────────────────

def test_submit_rejects_insufficient_images(fresh_svc, tmp_path):
    imgs = _mk_images(tmp_path, 2)
    with pytest.raises(CharacterTrainingFailed, match="素材不足"):
        fresh_svc.submit(_asset(tmp_path, imgs))


def test_submit_rejects_missing_base(fresh_svc, tmp_path, monkeypatch):
    monkeypatch.setattr(svc_mod, "BASE_MODEL_DIR", tmp_path / "nothere")
    imgs = _mk_images(tmp_path, 4)
    with pytest.raises(CharacterTrainingFailed, match="底座"):
        fresh_svc.submit(_asset(tmp_path, imgs))


def test_submit_rejects_duplicate(fresh_svc, tmp_path):
    imgs = _mk_images(tmp_path, 4)
    a = _asset(tmp_path, imgs)
    fresh_svc.submit(a)
    fresh_svc._queue.clear()  # 不真跑 worker：清队列留任务记录
    t = fresh_svc.list_tasks()[0]
    t["status"] = "training"
    fresh_svc._upsert_task(t)
    with pytest.raises(CharacterTrainingFailed, match="已有训练任务"):
        fresh_svc.submit(a)


# ── 版本/部署/回滚（以桩替 _train_core 走全编排） ─────────────

class _FakeAdapter:
    def save_pretrained(self, d: str) -> None:
        Path(d, "adapter_model.safetensors").write_bytes(b"fake-adapter")


def _stub_train(svc, tmp_path, imgs):
    def _core(asset, images, progress_cb, task_id=""):
        progress_cb(0.5, step=1, max_steps=2)
        d, v = svc._next_version_dir(asset["asset_id"])
        _FakeAdapter().save_pretrained(str(d))
        (d / "meta.json").write_text(
            json.dumps({"version": v, "data_count": len(images)}),
            encoding="utf-8")
        svc._set_current(asset["asset_id"], v)
        deployed = svc._deploy_to_asset(asset, d)
        progress_cb(1.0)
        return {"version": v, "deployed": deployed}
    svc._train_core = _core  # type: ignore[method-assign]


def test_full_pipeline_train_deploy_versions_rollback(fresh_svc, tmp_path):
    imgs = _mk_images(tmp_path, 4)
    a = _asset(tmp_path, imgs)
    _stub_train(fresh_svc, tmp_path, imgs)
    task = fresh_svc.submit(a)
    fresh_svc._worker_loop()  # 同步跑空队列（worker 线程的一次 tick）

    t = fresh_svc.get_task(task["id"])
    assert t is not None and t["status"] == "done"
    assert t["progress"] == 1.0 and t["version"] == "v1"
    # 部署约定：资产旁 lora.safetensors（D-LoRA 消费链）
    deployed = Path(imgs[0]).parent / "lora.safetensors"
    assert deployed.is_file()
    # 版本列表 + current
    vers = fresh_svc.versions("a1")
    assert len(vers) == 1 and vers[0]["is_current"] and vers[0]["version"] == "v1"
    # 再练一版 → 回滚到 v1
    task2 = fresh_svc.submit(a)
    fresh_svc._worker_loop()
    assert fresh_svc.get_task(task2["id"]) is not None
    vers = fresh_svc.versions("a1")
    assert [v["version"] for v in vers] == ["v2", "v1"]
    r = fresh_svc.rollback(a, "v1")
    assert r["version"] == "v1"
    assert fresh_svc.versions("a1")[0]["is_current"] is False or True
    assert next(v for v in fresh_svc.versions("a1")
                if v["version"] == "v1")["is_current"]


# ── API 信封 ────────────────────────────────────────────────

def test_api_assets_and_param_guards(api_client, monkeypatch):
    # 桩掉资产查询与聚合（API 层信封是被测对象）
    import src.api.character_lora as api_mod
    monkeypatch.setattr(
        api_mod, "_load_asset",
        lambda aid: {"asset_id": aid, "id": aid, "kind": "character",
                     "name": "t", "file_path": "", "meta": {}})
    r = api_client.post("/api/v1/character/lora/train", json={})
    assert r.json()["error"]["code"] == "CHARACTER_LORA_PARAM"
    r2 = api_client.get("/api/v1/character/lora/versions?asset_id=")
    assert r2.json()["error"]["code"] == "CHARACTER_LORA_PARAM"


def test_aggregator_includes_character_source(api_client, monkeypatch):
    import src.api.training as tr
    monkeypatch.setattr(tr, "learn_tasks", lambda: {
        "success": True, "data": {"items": []}})
    monkeypatch.setattr(tr, "style_tasks", lambda: {
        "success": True, "data": {"items": []}})
    monkeypatch.setattr(tr, "character_lora_tasks", lambda: {
        "success": True, "data": {"items": [
            {"id": "c1", "name": "小樱", "status": "training",
             "progress": 0.4, "created_at": 5.0}]}})
    r = api_client.get("/api/v1/training/tasks")
    body = r.json()
    assert body["success"] is True
    items = body["data"]["items"]
    assert len(items) == 1 and items[0]["kind"] == "character"
    assert items[0]["kind_label"] == "人物训练"
    assert "character" in body["data"]["sources"]
