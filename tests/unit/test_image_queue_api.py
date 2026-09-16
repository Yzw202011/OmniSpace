"""图像队列端点级测试（draw 入队即返回 + 位次 + 取消/优先级重绑）。

TestClient + 队列替身（不触 GPU/引擎）：验证绘画页任务受理语义
（一律入队 pending，不再被其他功能 40007 拒绝）与位次/取消接口。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database

pytestmark = pytest.mark.smoke


class FakeImageQueue:
    def __init__(self) -> None:
        self.tasks: list[dict] = []
        self.cancels: list[str] = []
        self.priorities: list[tuple[str, int]] = []

    def submit(self, task: dict) -> int:
        self.tasks.append(task)
        return len(self.tasks)

    async def submit_and_wait(self, task: dict, timeout_s: float = 3600.0):
        """假实现：立即执行 runner（测试无排队语义，只验接线）。"""
        self.tasks.append(task)
        try:
            return task["runner"](task, lambda: None)
        except BaseException as exc:  # noqa: BLE001 - 与真队列语义一致
            raise exc

    def cancel(self, task_id: str) -> str:
        self.cancels.append(task_id)
        for i, t in enumerate(self.tasks):
            if t["task_id"] == task_id:
                del self.tasks[i]
                return "queued"
        return "missing"

    def set_priority(self, task_id: str, priority: int) -> bool:
        self.priorities.append((task_id, priority))
        return any(t["task_id"] == task_id for t in self.tasks)

    def position(self, task_id: str) -> int | None:
        for i, t in enumerate(self.tasks):
            if t["task_id"] == task_id:
                return i + 1
        return None

    def snapshot(self) -> dict:
        return {"queued": [{"task_id": t["task_id"], "kind": t.get("kind"),
                            "priority": t.get("priority", 5)}
                           for t in self.tasks],
                "current": None, "lock_held": False}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    test_db = Database(tmp_path / "image_queue.db")
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    fake = FakeImageQueue()
    from src.api import draw as draw_mod
    monkeypatch.setattr(draw_mod, "get_image_queue", lambda: fake)
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1"), fake


def test_draw_generate_enqueues_pending_with_position(env) -> None:
    """两笔文生图：双双受理 pending、位次 1/2（不拒绝、不直跑）。"""
    client, fake = env
    r1 = client.post("/api/v1/draw/generate", json={
        "prompt": "测试文生图一", "width": 512, "height": 512, "steps": 4})
    assert r1.status_code == 200, r1.text
    t1 = r1.json()["data"]["task_id"]
    assert (client.get(f"/api/v1/draw/result/{t1}").json()["data"]
            ["status"]) == "pending"

    r2 = client.post("/api/v1/draw/generate", json={
        "prompt": "测试文生图二", "width": 512, "height": 512, "steps": 4})
    t2 = r2.json()["data"]["task_id"]
    s1 = client.get(f"/api/v1/draw/result/{t1}").json()["data"]
    s2 = client.get(f"/api/v1/draw/result/{t2}").json()["data"]
    assert s1["status"] == "pending" and s2["status"] == "pending"
    assert s1["queue_position"] == 1 and s2["queue_position"] == 2
    assert len(fake.tasks) == 2


def test_draw_cancel_queued_and_priority(env) -> None:
    """排队取消=出队终态；优先级接口重绑到统一队列。"""
    client, fake = env
    ids = [client.post("/api/v1/draw/generate", json={
        "prompt": f"取消测试 {i}", "width": 512, "height": 512,
        "steps": 4}).json()["data"]["task_id"] for i in range(2)]
    cancel = client.post(f"/api/v1/draw/task/{ids[1]}/cancel")
    cbody = cancel.json()["data"]
    assert cbody["status"] == "cancelled" and cbody["was_pending"] is True
    s2 = client.get(f"/api/v1/draw/result/{ids[1]}").json()["data"]
    assert s2["status"] == "cancelled"

    pri = client.post(f"/api/v1/paint/task/{ids[0]}/priority",
                      json={"priority": 8})
    assert pri.json()["data"]["effective"] is True
    assert fake.priorities == [(ids[0], 8)]


def test_draw_queue_snapshot_from_unified_queue(env) -> None:
    """/draw/queue 快照真源=统一图像队列。"""
    client, fake = env
    client.post("/api/v1/draw/generate", json={
        "prompt": "快照测试", "width": 512, "height": 512, "steps": 4})
    snap = client.get("/api/v1/draw/queue").json()["data"]
    assert snap["pending_count"] == 1
    assert snap["pending"][0]["status"] == "pending"


@pytest.fixture()
def env2(tmp_path, monkeypatch):
    """隔离 DB + 假图像队列 + 假生成核心（四视图/单视图重生接线验证）。"""
    test_db = Database(tmp_path / "image_queue2.db")
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    fake = FakeImageQueue()

    class _Res:
        marker = {"runs": 0}

        def turnaround_sync(self, req):  # noqa: ANN001
            self.marker["runs"] += 1
            return {"asset_id": "a1", "pipeline": "onepass",
                    "file_path": "x.png"}

    res = _Res()
    import src.api.manga.comic_asset as ca
    monkeypatch.setattr(ca, "get_image_queue", lambda: fake)
    monkeypatch.setattr(ca, "_generate_turnaround_sync", res.turnaround_sync)
    monkeypatch.setattr(ca, "_regenerate_view_sync",
                        lambda asset, view, p: {"file_path": "v.png"})
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1"), fake, res, test_db


def test_turnaround_enqueued_and_run(env2) -> None:
    """四视图生成：入队受理 + runner 在队内执行（kind=comic_asset）。"""
    client, fake, res, db = env2
    db.insert("projects", {"id": "p1", "name": "t", "path": "",
                           "created_at": 1, "updated_at": 1})
    r = client.post("/api/v1/comic/asset/generate-turnaround", json={
        "project_id": "p1", "name": "测试角色", "prompt": "女孩",
        "seed": 42})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["pipeline"] == "onepass"
    assert res.marker["runs"] == 1
    # 任务确实经过统一图像队列（不是直跑）
    assert len(fake.tasks) == 1
    assert fake.tasks[0]["kind"] == "comic_asset"


def test_regenerate_view_enqueued(env2) -> None:
    """单视图重生：四视图资产校验通过后入队执行。"""
    client, fake, _res, db = env2
    db.insert("projects", {"id": "p1", "name": "t", "path": "",
                           "created_at": 1, "updated_at": 1})
    db.insert("comic_assets", {
        "id": "a1", "project_id": "p1", "kind": "character",
        "name": "四视图角色", "file_path": "comic_assets/x.png",
        "prompt": "女孩", "meta": '{"turnaround": true}',
        "created_at": 1})
    r = client.post("/api/v1/comic/asset/a1/regenerate-view", json={
        "view": "side"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["file_path"] == "v.png"
    assert len(fake.tasks) == 1
# 本项目仅供学习使用，商业授权请+Q 3559331368
