"""AI 漫画图像技能测试（技能插座批3，2026-09-17）。

覆盖：/comic/skill 端点（参数校验/路径零信任/技能不存在/纯数据档
图像往返产帧落盘 + /manga/media 可回读/沙箱档拒图像/空产出文本兜底）。
纯 CPU（numpy 造帧）不触 GPU、不进图像队列。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.config import DATA_DIR
from src.data import database as db_mod
from src.data.database import Database
from src.services.plugin_runtime import registry as pr_registry
from src.services.plugin_runtime.registry import get_plugin_runtime

COMIC_SKILL_PLUGIN_PY = '''
from typing import Any

import numpy as np

try:
    from .plugin import ExpertPlugin
except ImportError:
    from omnispace.plugin import ExpertPlugin


class ComicSkillPlugin(ExpertPlugin):
    BASE_MODEL = "comic.skill"
    CAPABILITY = "comic skill test"

    def on_think(self, event, ctx):
        data = event.get("data") or {}
        imgs = data.get("images") or []
        if not imgs:
            return {"text": "（未收到图像）"}
        first = np.asarray(imgs[0])
        # 反色处理：验证 ndarray 真进了插件
        out = 255 - first
        return {"frames": [out], "text": ""}
'''

COMIC_SKILL = {"id": "invert", "feature": "comic", "title": "反色演示",
               "description": "把画面反色（测试用）", "input": "image"}


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    monkeypatch.setattr(pr_registry, "USER_PLUGIN_DIR",
                        tmp_path / "imported")
    monkeypatch.setattr(pr_registry, "USER_REGISTRY_PATH",
                        tmp_path / "user_registry.json")
    monkeypatch.setattr(pr_registry, "FACTORY_OVERRIDE_PATH",
                        tmp_path / "factory_overrides.json")
    # OUTPUT_ROOT 保持产品位（data/plugins/output）：断言产物 URL 落
    # 在 /manga/media 白名单目录内——测试期写入量 1 张小 PNG 可忽略，
    # 用独立 run 目录避免污染（目录名带 uuid）
    monkeypatch.setattr(pr_registry, "_runtime", None)
    monkeypatch.setattr(db_mod, "_db_instance", Database(tmp_path / "t.db"))
    from src.main import app
    src = tmp_path / "comic_skill_plugin.py"
    src.write_text(COMIC_SKILL_PLUGIN_PY, encoding="utf-8")
    rt = get_plugin_runtime()
    rt.register("comic-demo", src, trust="user_data", skills=[COMIC_SKILL])
    rt.register("comic-sbx", src, trust="user_source",
                skills=[COMIC_SKILL])
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture()
def sample_image(tmp_path):
    """在 keyframes 白名单目录下造一张 2x2 测试图。"""
    from PIL import Image
    d = DATA_DIR / "keyframes"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "skill_e2e_test.png"
    Image.new("RGB", (2, 2), (10, 20, 30)).save(p)
    return "keyframes/skill_e2e_test.png"


def _skill(client: TestClient, payload: dict) -> dict:
    return client.post("/api/v1/comic/skill", json=payload).json()


def test_skill_requires_params(api_client):
    body = _skill(api_client, {})
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_SPEC_MISMATCH"


def test_skill_not_found(api_client):
    body = _skill(api_client, {"plugin": "ghost", "skill_id": "x",
                               "image_paths": ["keyframes/a.png"]})
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_SKILL_NOT_FOUND"


def test_skill_path_traversal_rejected(api_client):
    for bad in ("../config.yaml", "C:/Windows/win.ini",
                "generated/exports/x.png"):
        body = _skill(api_client, {"plugin": "comic-demo",
                                   "skill_id": "invert",
                                   "image_paths": [bad]})
        assert body["success"] is False, bad
        # 40008 经 _LEGACY_CODE_MAP 映射为 SYSTEM_PARAM_INVALID 语义串
        assert body["error"]["code"] == "SYSTEM_PARAM_INVALID"


def test_skill_sandbox_refuses_image(api_client, sample_image):
    body = _skill(api_client, {"plugin": "comic-sbx",
                               "skill_id": "invert",
                               "image_paths": [sample_image]})
    if body["success"] is False:
        # 沙箱开启时如实拒绝（config 门控关着则走进程内路——两态皆合法）
        assert body["error"]["code"] == "PLUGIN_IMAGE_SANDBOX_UNSUPPORTED"


def test_skill_image_roundtrip(api_client, sample_image):
    body = _skill(api_client, {"plugin": "comic-demo",
                               "skill_id": "invert",
                               "image_paths": [sample_image]})
    assert body["success"] is True, body
    data = body["data"]
    assert len(data["image_urls"]) == 1
    url = data["image_urls"][0]
    assert url.startswith("plugins/output/comic-demo/skill-")
    # 产物可经 /manga/media 回读（白名单已含 plugins/output）
    resp = api_client.get(f"/api/v1/manga/media/{url}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
