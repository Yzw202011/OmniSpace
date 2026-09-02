"""绘画批量生成端点回归（P2 复核修复 2026-09-02）。

病根：batch_size 前端有控件、后端 /draw/generate 静默丢弃——
「批量=2 实生 1 张」不是点击丢失，是参数从未被消费（死控件）。
修复 = 后端钳 1~4 派发 N 个队列任务（定种子按序派生防同图、
随机种子各任务独立），返回 task_ids 全量。

TestClient + 队列替身（不触 GPU），约定同 test_image_queue_api。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.data import database as db_mod
from backend.data.database import Database

pytestmark = pytest.mark.smoke


class FakeImageQueue:
    def __init__(self) -> None:
        self.tasks: list[dict] = []

    def submit(self, task: dict) -> int:
        self.tasks.append(task)
        return len(self.tasks)

    def cancel(self, task_id: str) -> str:
        return "missing"

    def set_priority(self, task_id: str, priority: int) -> bool:
        return False

    def position(self, task_id: str) -> int | None:
        return None

    def snapshot(self) -> dict:
        return {"queued": [], "current": None, "lock_held": False}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    test_db = Database(tmp_path / "batch.db")
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    fake = FakeImageQueue()
    from backend.api import draw as draw_mod
    monkeypatch.setattr(draw_mod, "get_image_queue", lambda: fake)
    from backend.main import app
    return TestClient(app, base_url="http://127.0.0.1"), fake


def test_batch_two_submits_two_tasks(env) -> None:
    """batch_size=2 → 两个独立队列任务 + task_ids 全量返回。"""
    client, fake = env
    r = client.post("/api/v1/draw/generate", json={
        "prompt": "批量测试", "width": 512, "height": 512, "steps": 4,
        "batch_size": 2, "seed": 42})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["batch"] == 2
    assert len(data["task_ids"]) == 2
    assert data["task_id"] == data["task_ids"][0]
    assert len(fake.tasks) == 2, "批量=2 必须实派 2 个任务"


def test_batch_clamped_to_4(env) -> None:
    """越界钳制：0 → 1、9 → 4。"""
    client, fake = env
    r0 = client.post("/api/v1/draw/generate", json={
        "prompt": "钳0", "batch_size": 0, "steps": 4})
    assert r0.json()["data"]["batch"] == 1
    r9 = client.post("/api/v1/draw/generate", json={
        "prompt": "钳9", "batch_size": 9, "steps": 4})
    assert r9.json()["data"]["batch"] == 4
    assert len(fake.tasks) == 5  # 1 + 4


def test_fixed_seed_derived_per_task(env) -> None:
    """固定种子按序派生（seed=42 → 42/43），防批量同图。

    种子存于 task["params"]["seed"]（生成时消费；顶层 seed 字段是
    运行后回写实际值，pending 期恒 -1），从任务注册表直读。
    """
    client, fake = env
    r = client.post("/api/v1/draw/generate", json={
        "prompt": "种子派生", "batch_size": 2, "seed": 42, "steps": 4})
    from backend.api import draw as draw_mod
    ids = r.json()["data"]["task_ids"]
    with draw_mod._tasks_lock:
        seeds = [draw_mod._tasks[i]["params"].get("seed") for i in ids]
    assert seeds == [42, 43], f"定种子批量必须按序派生，got {seeds}"
