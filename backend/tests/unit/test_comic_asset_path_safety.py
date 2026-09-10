"""资产链路路径安全回归（2026-09-10 审计 P1-A/B/C + P2-2 修复）。

背景：上轮 P1-6 立下的「路径名必须消毒」内部不变量在三个端点执行
不一致——upload 的 pid（P1-A）、AssetTurnaroundRequest 全字段（P1-B）、
AssetAdoptRequest.project_id（P1-C）此前零校验，`../` 可把文件写出
comic_assets/ 或被删除流程 rmtree 触达。P2-2 二阶污染：改名端点与
LLM 实体名直接落库，事后经 _asset_dir_for/图替换/copytree 拼进路径。

校验发生在文件系统触碰之前：拒绝类用例零落盘、零建行。
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.api.manga.comic_asset import _COMIC_ASSET_DIR, _safe_entity_name
from backend.data import database as db_mod
from backend.data.database import Database
from backend.data.models import (
    AssetAdoptRequest,
    AssetTurnaroundRequest,
    AssetUpdateRequest,
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    test_db = Database(tmp_path / "asset_path_safety.db")
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    from backend.main import app
    return TestClient(app, base_url="http://127.0.0.1")


def _png_bytes() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(buf, "PNG")
    return buf.getvalue()


# ── 模型层：与姊妹 AssetGenerateRequest 同规矩 ─────────────────────────

def test_turnaround_rejects_traversal_pid_and_name() -> None:
    """P1-B：project_id/name 直拼落盘目录，拒路径元字符。"""
    with pytest.raises(ValidationError):
        AssetTurnaroundRequest(project_id=r"..\..\pwn", name="角色",
                               prompt="x")
    with pytest.raises(ValidationError):
        AssetTurnaroundRequest(project_id="p1", name="a/../b", prompt="x")
    req = AssetTurnaroundRequest(project_id="p1", name="角色", prompt="x")
    assert req.project_id == "p1"


def test_adopt_rejects_traversal_pid() -> None:
    """P1-C：project_id 拼目标目录且后续 rmtree 可触达。"""
    with pytest.raises(ValidationError):
        AssetAdoptRequest(asset_id="a1", project_id="../outside")
    req = AssetAdoptRequest(asset_id="a1", project_id="p1")
    assert req.project_id == "p1"


def test_asset_update_rejects_traversal_name_none_ok() -> None:
    """P2-2：改名后的 name 会拼进磁盘路径；None=不改名必须放行。"""
    with pytest.raises(ValidationError):
        AssetUpdateRequest(name="a/../b")
    req = AssetUpdateRequest(name=None, prompt="只改描述词")
    assert req.name is None


# ── 端点层：校验先于任何落盘/建行 ─────────────────────────────────────

def test_upload_traversal_pid_rejected_before_write(client) -> None:
    """P1-A：恶意 pid 拒绝，且穿越目标路径零落盘。"""
    evil_pid = r"..\..\pwn_ax"
    r = client.post(
        "/api/v1/comic/asset/upload",
        data={"project_id": evil_pid, "kind": "character", "name": "夏沐沐"},
        files={"file": ("t.png", _png_bytes(), "image/png")})
    body = r.json()
    assert body.get("success") is False, f"恶意 pid 应被拒绝: {body}"
    would_be = (_COMIC_ASSET_DIR / evil_pid).resolve()
    assert not would_be.exists(), f"穿越目录不应存在: {would_be}"


def test_turnaround_traversal_pid_rejected(client) -> None:
    """P1-B：pydantic 校验层拒绝（本项目 HTTP 恒 200，错误走信封）。"""
    r = client.post("/api/v1/comic/asset/generate-turnaround",
                    json={"project_id": r"..\..\pwn", "name": "角色",
                          "prompt": "x"})
    body = r.json()
    assert body.get("success") is False, f"恶意 pid 应被拒绝: {body}"
    assert body["error"]["code"] == "SYSTEM_PARAM_INVALID"


def test_adopt_traversal_pid_rejected(client) -> None:
    """P1-C：校验层拒绝（先于 _ensure_project 建行/copytree）。"""
    r = client.post("/api/v1/comic/asset/adopt",
                    json={"asset_id": "a1", "project_id": "../outside"})
    body = r.json()
    assert body.get("success") is False, f"恶意 pid 应被拒绝: {body}"
    assert body["error"]["code"] == "SYSTEM_PARAM_INVALID"


# ── 实体名消毒助手（P2-2：LLM/分镜列实体名落库前消毒）─────────────────

def test_safe_entity_name_strips_metachars_keeps_text() -> None:
    assert _safe_entity_name("孙悟空/大圣") == "孙悟空大圣"
    assert _safe_entity_name(" 夏沐沐 ") == "夏沐沐"


def test_safe_entity_name_drops_unsalvageable() -> None:
    assert _safe_entity_name("..") == ""
    assert _safe_entity_name("a..b") == ""      # 同 _check_safe_name 保守口径
    assert _safe_entity_name(r"\\/:*?") == ""
    assert _safe_entity_name("") == ""
    assert len(_safe_entity_name("名" * 300)) == 100
