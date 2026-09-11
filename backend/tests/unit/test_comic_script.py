"""漫画模块 C2「AI 写分格」单测（2026-09-08，本地路）。

覆盖：
- 提示词拼装（纯函数锁定：格数/梗概注入）；
- 输出解析容错链：裸数组 / 代码围栏 / 截断抢救 / 混入非字符串 / 坏输出；
- API 冒烟（tmp 库隔离 + mock 引擎，不触 GPU）：追加/清空重填双语义、
  锁释放、ensure_loaded 自动装载路径。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.api.manga import comic_script as cs
from backend.data import database as db_mod
from backend.data.database import Database

# ── 纯函数 ───────────────────────────────────────────────────────

def test_build_prompt_contains_story_and_panels():
    p = cs.build_script_prompt("少女与猫的雨天", 6)
    assert "6 格" in p and "少女与猫的雨天" in p
    assert "JSON 数组" in p


def test_parse_bare_array():
    out = cs.parse_panel_descriptions('["格一", "格二", "格三"]', 4)
    assert out == ["格一", "格二", "格三"], "少于期望格数=诚实返回实得"


def test_parse_code_fence():
    raw = '```json\n["围栏一", "围栏二"]\n```'
    assert cs.parse_panel_descriptions(raw, 2) == ["围栏一", "围栏二"]


def test_parse_truncated_salvage():
    # 撞 token 上限腰斩：截断抢救应保住前两个完整值
    raw = '["完整一", "完整二", "被拦腰截断的第三'
    out = cs.parse_panel_descriptions(raw, 3)
    assert out == ["完整一", "完整二"]


def test_parse_over_expect_clamps():
    out = cs.parse_panel_descriptions('["1","2","3","4","5"]', 3)
    assert out == ["1", "2", "3"], "超过期望格数截断"


def test_parse_garbage_returns_empty():
    assert cs.parse_panel_descriptions("模型今天不在线", 4) == []
    assert cs.parse_panel_descriptions("", 4) == []
    assert cs.parse_panel_descriptions('{"不是":"数组"}', 4) == []


# ── API 冒烟（mock 引擎，不触 GPU）─────────────────────────────

class _FakeLock:
    def __init__(self) -> None:
        self.released: list[str] = []

    async def release(self, feature: str) -> None:
        self.released.append(feature)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    test_db = Database(tmp_path / "comic_script.db")
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    lock = _FakeLock()

    async def _fake_acquire(feature, task_id=None):  # noqa: ARG001
        return lock

    monkeypatch.setattr(cs, "acquire_or_raise", _fake_acquire)
    from backend.main import app
    return SimpleNamespace(client=TestClient(app, base_url="http://127.0.0.1"),
                           test_db=test_db, lock=lock)


def _mock_engine(monkeypatch, reply: str, ready: bool = True):
    calls: dict = {}

    def _chat(messages, temperature=0.7, max_new_tokens=1024):  # noqa: ARG001
        calls["prompt"] = messages[0]["content"]
        return reply

    def _ensure(target):  # noqa: ARG001
        calls["ensure"] = target
        return True

    async def _run_blocking(fn, *a, **kw):
        return fn(*a, **kw)

    fake = SimpleNamespace(
        is_ready=ready, chat=_chat, ensure_loaded=_ensure,
        get_status=lambda: {"state": "loaded", "last_error": ""})
    monkeypatch.setattr(cs, "get_dialog_engine", lambda: fake)
    monkeypatch.setattr(cs, "run_blocking", _run_blocking)
    return calls


def test_script_generate_append_and_replace(client, monkeypatch):
    # 建项目
    r = client.client.post("/api/v1/comic/project/create",
                           json={"name": "C2测试", "project_type": "comic"})
    assert r.json()["success"]
    pid = r.json()["data"]["project_id"]

    calls = _mock_engine(monkeypatch, '["格一描述", "格二描述", "格三描述", "格四描述"]')

    # 追加语义：默认 replace=False
    r1 = client.client.post("/api/v1/manga/comic/script-generate",
                            json={"project_id": pid, "story": "测试梗概", "panels": 4})
    assert r1.status_code == 200 and r1.json()["success"], r1.text
    data = r1.json()["data"]
    assert data["generated"] == 4 and len(data["rows"]) == 4
    assert data["rows"][0]["description"] == "格一描述"
    assert "测试梗概" in calls["prompt"], "梗概须进提示词"
    assert client.lock.released == ["dialog"], "功能锁必须释放"

    # 再来一次追加 → 8 行
    r2 = client.client.post("/api/v1/manga/comic/script-generate",
                            json={"project_id": pid, "story": "续写", "panels": 2})
    assert len(r2.json()["data"]["rows"]) == 6, "追加语义：4+2=6 行"

    # replace=True 清空重填
    r3 = client.client.post("/api/v1/manga/comic/script-generate",
                            json={"project_id": pid, "story": "推倒重来",
                                  "panels": 2, "replace": True})
    rows3 = r3.json()["data"]["rows"]
    assert len(rows3) == 2 and rows3[0]["description"] == "格一描述"


def test_script_generate_bad_output_honest_error(client, monkeypatch):
    r = client.client.post("/api/v1/comic/project/create",
                           json={"name": "C2坏输出", "project_type": "comic"})
    pid = r.json()["data"]["project_id"]
    _mock_engine(monkeypatch, "今天模型罢工了，不输出 JSON")
    r1 = client.client.post("/api/v1/manga/comic/script-generate",
                            json={"project_id": pid, "story": "梗概", "panels": 4})
    body = r1.json()
    assert body["success"] is False and "未返回可用的分格描述" in str(body.get("error"))


def test_script_generate_triggers_auto_load(client, monkeypatch):
    """未就绪必须全自动装载（UX 铁律）：is_ready=False 时 ensure 被调。"""
    r = client.client.post("/api/v1/comic/project/create",
                           json={"name": "C2冷启", "project_type": "comic"})
    pid = r.json()["data"]["project_id"]
    calls = _mock_engine(monkeypatch, '["一格"]', ready=False)
    r1 = client.client.post("/api/v1/manga/comic/script-generate",
                            json={"project_id": pid, "story": "冷启动梗概", "panels": 1})
    assert r1.json()["success"]
    assert "ensure" in calls, "引擎未就绪时必须自动装载"
