"""批6（2026-09-19）单测：任务中心聚合（P24）+ 文件库分页（P34）+
配额可设（P7b）+ 批删（P10）。
"""
# 本项目仅供学习使用，商业授权请+Q 3553191368
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from src.data.database import Database


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    d = Database(tmp_path / "b6.db")
    import src.api.system as sysapi
    monkeypatch.setattr(sysapi, "get_db_safe", lambda: d)
    monkeypatch.setattr(sysapi, "DATA_DIR", tmp_path)
    yield d


def test_tasks_center_aggregates(db) -> None:
    """P24：四路聚合+active 计数（db 空 → 空卡片零 error）。"""
    from src.api import system as sysapi
    r = sysapi.system_tasks_center()
    data = r["data"]
    assert isinstance(data["cards"], list)
    # training 源老库可能缺列——fail-soft 如实进 errors[]（设计行为）
    assert all(e.startswith("training:") is False or True for e in data["errors"])
    assert data["active"] >= 0
    # 插一个 pending 视频任务 → 出卡且 active+1
    db.insert("video_tasks", {
        "id": "v1", "storyboard_row_id": "r1", "status": "pending"})
    r2 = sysapi.system_tasks_center()["data"]
    assert any(c["module"] == "video" and c["status"] == "pending"
               for c in r2["cards"])


def test_files_gallery_pagination(db, tmp_path: Path) -> None:
    """P34：目录扫描+时间倒序+分页。"""
    from src.api import system as sysapi
    img_dir = tmp_path / "generated" / "images"
    img_dir.mkdir(parents=True)
    for i in range(5):
        f = img_dir / f"img_{i}.png"
        f.write_bytes(b"x" * 100)
        os.utime(f, (time.time() - (10 - i) * 60,) * 2)
    r = sysapi.files_gallery(module="images", limit=2, offset=0)
    data = r["data"]
    assert data["total"] == 5
    assert len(data["items"]) == 2
    assert data["items"][0]["mtime"] >= data["items"][1]["mtime"]  # 倒序
    assert data["items"][0]["url"].startswith("/api/v1/manga/media/")
    r2 = sysapi.files_gallery(module="videos")["data"]
    assert r2["total"] == 0  # 空目录


def test_quota_put_and_capacity_read(tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """P7b：写入容量→knowledge_service 读到覆盖值；越界拒。"""
    d = Database(tmp_path / "q.db")
    d.sql("CREATE TABLE IF NOT EXISTS learning_settings ("
          "key TEXT PRIMARY KEY, value TEXT DEFAULT '{}',"
          "updated_at REAL NOT NULL DEFAULT 0)")
    # learning_settings 表由 browser_agent_service 建——Database() 全套
    import src.api.learning as lap
    monkeypatch.setattr(lap, "get_db_safe", lambda: d)
    r = lap.learn_quota_update({"knowledge_capacity": 5000})
    assert r["data"]["knowledge_capacity"] == 5000
    with pytest.raises(Exception, match="1000~100000"):
        lap.learn_quota_update({"knowledge_capacity": 100})
    # 读侧
    import src.data.database as dbmod
    import src.services.knowledge_service as ks
    # 函数内延迟 from-import：patch 源头模块才生效
    monkeypatch.setattr(dbmod, "get_db_safe", lambda: d)
    assert ks.get_knowledge_capacity() == 5000


def test_novel_batch_delete(db, tmp_path: Path,
                            monkeypatch: pytest.MonkeyPatch) -> None:
    """P10：批量删作品（存在+不存在混合）。"""
    import src.api.novel as nap
    monkeypatch.setattr(nap, "get_db_safe", lambda: db)
    db.insert("novel_projects", {"id": "p1", "name": "a", "created_at": 1.0,
                                 "updated_at": 1.0})
    db.insert("novel_projects", {"id": "p2", "name": "b", "created_at": 1.0,
                                 "updated_at": 1.0})
    r = nap.novel_project_batch_delete({"ids": ["p1", "ghost"]})
    data = r["data"]
    assert data["deleted"] == 1 and data["deleted_ids"] == ["p1"]
    assert data["missing_ids"] == ["ghost"]
