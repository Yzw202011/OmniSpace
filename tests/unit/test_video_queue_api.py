"""视频队列端点级测试（2026-09-02 B 方案：入队即返回 + 同行去重 + 取消/位次）。

TestClient + tmp 数据库（不触发 lifespan），队列替身只记录 submit/
cancel/position——不触 GPU。双镜场景（用户原始诉求）：第一镜与第二镜
同时提交 → 双双受理、位次 1/2、状态 pending；同行连点仍拒绝。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database

pytestmark = pytest.mark.smoke


class FakeQueue:
    """视频队列替身：记录提交/取消，不启动 worker。"""

    def __init__(self) -> None:
        self.tasks: list[dict] = []
        self.cancels: list[str] = []

    def submit(self, task: dict) -> int:
        self.tasks.append(task)
        return len(self.tasks)

    def cancel(self, task_id: str) -> str:
        self.cancels.append(task_id)
        for i, t in enumerate(self.tasks):
            if t["task_id"] == task_id:
                del self.tasks[i]
                return "queued"
        return "missing"

    def position(self, task_id: str) -> int | None:
        for i, t in enumerate(self.tasks):
            if t["task_id"] == task_id:
                return i + 1
        return None

    def next_kind(self) -> str | None:
        return self.tasks[0]["kind"] if self.tasks else None


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离数据库 + 队列替身的 TestClient 环境。"""
    test_db = Database(tmp_path / "video_queue.db")
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    fake = FakeQueue()
    from src.api.manga import video as video_mod
    monkeypatch.setattr(video_mod, "get_video_queue", lambda: fake)
    from src.main import app
    client = TestClient(app, base_url="http://127.0.0.1")

    # 种子数据：项目 → 分镜表 → 两行分镜（描述词 + 绑定带图资产）
    now = 1_000.0
    test_db.insert("projects", {
        "id": "p1", "name": "队列测试项目", "path": "",
        "created_at": now, "updated_at": now})
    test_db.insert("storyboards", {
        "id": "sb1", "project_id": "p1", "name": "",
        "created_at": now, "updated_at": now})
    test_db.insert("comic_assets", {
        "id": "a1", "project_id": "p1", "kind": "character",
        "name": "主角", "file_path": "comic_assets/p1/characters/主角/x.png",
        "prompt": "测试角色", "meta": "{}", "created_at": now})
    for rid, shot in (("row1", 1), ("row2", 2)):
        test_db.insert("storyboard_rows", {
            "id": rid, "storyboard_id": "sb1", "shot_number": shot,
            "description": "A. 全局风格\nB. 世界观\nC. 时间轴",
            "asset_ids": json.dumps(["a1"]),
            "sort_index": shot})
    return client, test_db, fake


def _generate(client: TestClient, row_id: str):
    return client.post("/api/v1/manga/video/generate", json={
        "storyboard_row_id": row_id,
        "description": "测试描述词",
        "screenshot_4in1": "",
        "duration_seconds": 5,
        "resolution": "1024x576",
    })


def test_two_shots_both_accepted_and_queued(env) -> None:
    """双镜同时提交：双双受理 pending、位次 1/2、单 worker 排队。"""
    client, db, fake = env
    r1 = _generate(client, "row1")
    assert r1.status_code == 200, r1.text
    d1 = r1.json()["data"]
    assert d1["status"] == "pending"
    assert d1["queue_position"] == 1

    r2 = _generate(client, "row2")
    assert r2.status_code == 200, r2.text
    d2 = r2.json()["data"]
    assert d2["status"] == "pending"
    assert d2["queue_position"] == 2

    # 队列替身收到两个 H3 任务（默认选路）
    assert [t["kind"] for t in fake.tasks] == ["h3_chain", "h3_chain"]
    assert fake.tasks[0]["task_id"] == d1["task_id"]
    # DB 双行 pending（排队中，未虚报 generating）
    for t in (d1, d2):
        row = db.query_one("SELECT status FROM video_tasks WHERE id=?",
                           (t["task_id"],))
        assert row["status"] == "pending"


def test_same_row_rapid_click_rejected(env) -> None:
    """同行连点：第二笔直接拒绝（不双跑同行）。"""
    client, _db, fake = env
    assert _generate(client, "row1").status_code == 200
    dup = _generate(client, "row1")
    body = dup.json()
    assert body["success"] is False
    assert "生成中" in body["error"]["message"]
    assert len(fake.tasks) == 1


def test_cancel_queued_task_and_status_position(env) -> None:
    """取消排队任务：出队 + 终态；status 端点附排队位次。"""
    client, db, fake = env
    t1 = _generate(client, "row1").json()["data"]
    t2 = _generate(client, "row2").json()["data"]

    st = client.get(f"/api/v1/manga/video/{t2['task_id']}/status").json()["data"]
    assert st["status"] == "pending"
    assert st["queue_position"] == 2

    cancel = client.post(f"/api/v1/manga/video/{t2['task_id']}/cancel")
    cbody = cancel.json()["data"]
    assert cbody["status"] == "cancelled"
    assert cbody["queue_outcome"] == "queued"
    # 出队后 t1 位次仍为 1，t2 无位次
    st1 = client.get(f"/api/v1/manga/video/{t1['task_id']}/status").json()["data"]
    assert st1.get("queue_position") == 1
    st2 = client.get(f"/api/v1/manga/video/{t2['task_id']}/status").json()["data"]
    assert "queue_position" not in st2
    assert db.query_one("SELECT status FROM video_tasks WHERE id=?",
                        (t2["task_id"],))["status"] == "cancelled"


def test_h3_chain_endpoint_enqueues_pending(env) -> None:
    """H3 直连端点同样入队 pending（与 generate 统一排队）。"""
    client, _db, fake = env
    r = client.post("/api/v1/manga/video/generate_h3_chain", json={
        "storyboard_row_id": "row1", "row_ids": ["row1"],
        "seconds_per_shot": 5, "quality": "480p"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "pending"
    assert data["engine"] == "h3_chain"
    assert fake.tasks and fake.tasks[-1]["kind"] == "h3_chain"
# 本项目仅供学习使用，商业授权请+Q 3559331368
