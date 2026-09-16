"""漫剧快速预览测试（插件系统 P2，2026-09-16）。

全链真跑：分镜行+当前关键帧 → video-making 插件运镜渲染 →
真 ffmpeg 封装 mp4 → 媒体白名单回读 URL。
（真编码器、真插件、小图秒级；不碰 GPU。）
"""
from __future__ import annotations

import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.config import DATA_DIR
from src.data import database as db_mod
from src.data.database import Database


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离数据库 + 一行分镜带当前关键帧（竖屏 480×800 验证不变形）。"""
    test_db = Database(tmp_path / "preview.db")
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    # 关键帧图落到白名单目录 keyframes/ 下（DATA_DIR 真实树）
    kf_dir = DATA_DIR / "keyframes" / "preview_test"
    kf_dir.mkdir(parents=True, exist_ok=True)
    kf_png = kf_dir / "row_preview.png"
    Image.fromarray(
        (np.random.rand(800, 480, 3) * 255).astype(np.uint8),
        mode="RGB").save(kf_png, format="PNG")
    now = 1_000.0
    test_db.insert("projects", {
        "id": "pp", "name": "预览测试", "path": "",
        "created_at": now, "updated_at": now})
    test_db.insert("storyboards", {
        "id": "sbp", "project_id": "pp", "name": "",
        "created_at": now, "updated_at": now})
    test_db.insert("storyboard_rows", {
        "id": "row_preview", "storyboard_id": "sbp", "shot_number": 1,
        "description": "A. 风格\nB. 世界观\nC. 时间轴",
        "asset_ids": json.dumps([]), "sort_index": 1})
    test_db.insert("keyframes", {
        "id": "kf1", "row_id": "row_preview", "project_id": "pp",
        "version": 1, "file_path": "keyframes/preview_test/row_preview.png",
        "prompt": "p", "status": "done", "error": "", "is_current": 1,
        "created_at": now, "shot_seeds": "[]", "consistency": "",
        "source_mode": "describe"})
    from src.main import app
    client = TestClient(app, base_url="http://127.0.0.1")
    # 输出走真实 _PREVIEW_OUTPUT_DIR（media URL 全链依赖 DATA_DIR 相对性），
    # teardown 清理本测试的预览产物
    yield client, test_db, DATA_DIR / "generated" / "preview_videos"
    for f in (DATA_DIR / "generated" / "preview_videos").glob(
            "preview_row_preview.*"):
        f.unlink(missing_ok=True)
    kf_png.unlink(missing_ok=True)


def test_preview_full_chain(env):
    """全链：竖屏关键帧 → 插件渲染 → 真 ffmpeg mp4 → URL 可回读。"""
    client, _, out_dir = env
    r = client.post("/api/v1/manga/video/preview", json={
        "row_id": "row_preview", "motion": "zoom_in",
        "duration_s": 0.4})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True, body
    data = body["data"]
    assert data["n_frames"] >= 4
    assert data["duration_s"] and data["duration_s"] > 0
    assert data["video_url"].startswith("/api/v1/manga/media/generated/preview_videos")
    out = out_dir / "preview_row_preview.mp4"
    assert out.is_file() and out.stat().st_size > 0
    # 媒体回读真验：URL → GET 200 + video/mp4（白名单放行）
    resp = client.get(data["video_url"])
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("video/mp4")


def test_preview_no_keyframe_rejected(env):
    """无当前关键帧的行 → 60001 带出路（先生成关键帧）。"""
    client, test_db, _ = env
    test_db.insert("storyboard_rows", {
        "id": "row_empty", "storyboard_id": "sbp", "shot_number": 2,
        "description": "x", "asset_ids": "[]", "sort_index": 2})
    r = client.post("/api/v1/manga/video/preview", json={
        "row_id": "row_empty"})
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VIDEO_GENERATION_FAILED"  # 60001 语义名
    assert "关键帧" in body["error"]["suggestion"]


def test_preview_bad_motion_rejected(env):
    """未知运镜 → PLUGIN_SPEC_MISMATCH 带合法清单。"""
    client, _, _ = env
    r = client.post("/api/v1/manga/video/preview", json={
        "row_id": "row_preview", "motion": "nope"})
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_SPEC_MISMATCH"
    assert "zoom_in" in body["error"]["suggestion"]


def test_preview_output_overwrites_same_row(env):
    """同镜重生成同名覆盖（预览不累积占盘）。"""
    client, _, _ = env
    for _ in range(2):
        r = client.post("/api/v1/manga/video/preview", json={
            "row_id": "row_preview", "motion": "pan_left",
            "duration_s": 0.3})
        assert r.json()["success"] is True
    out_files = list(env[2].glob("preview_row_preview.*"))
    assert len(out_files) == 1  # 同名 mp4 覆盖，不新增
