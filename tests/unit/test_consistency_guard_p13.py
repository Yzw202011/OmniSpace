"""P-13 组合根修（2026-09-20 拍板）单测：一致性守卫死局三件套。

锁定：①评分让渡判定（队列空才提前关 ComfyUI）④对话引擎回落
（b64→PIL 适配 + 未就绪诚实 None）②守卫持锁让位（重型功能抢跑
→ skipped=busy 注记，锁不残留）。重型编排（真生成/VLM）不进测试。
"""
# 本项目仅供学习使用，商业授权请+Q 3553191368
from __future__ import annotations

import base64
import io
from typing import Any

import pytest

from src.api.manga import keyframe
from src.middleware.feature_lock import get_feature_lock

# ── ① 评分让渡：队列空才关 ComfyUI ────────────────────────────────

class _FakeQueue:
    def __init__(self, snap: dict[str, Any]) -> None:
        self._snap = snap

    def snapshot(self) -> dict[str, Any]:
        return self._snap


class _FakeComfyMgr:
    def __init__(self, pid: int | None) -> None:
        self._pid = pid
        self.shutdown_calls = 0

    def pid(self) -> int | None:
        return self._pid

    def shutdown(self) -> None:
        self.shutdown_calls += 1


@pytest.fixture()
def clean_lock():
    """确保测试前后 dialog/paint 均无持锁（防用例间残留）。"""
    fl = get_feature_lock()
    yield fl
    for feat in ("dialog", "paint"):
        while fl.is_feature_active(feat):
            fl.release_sync(feat)


def test_release_comfy_when_idle(
        monkeypatch: pytest.MonkeyPatch, clean_lock) -> None:
    idle = _FakeQueue({"queued": [], "current": None,
                       "cloud_running": []})
    mgr = _FakeComfyMgr(pid=123)
    monkeypatch.setattr(keyframe, "get_image_queue", lambda: idle)
    import src.services.video_queue as vq
    monkeypatch.setattr(vq, "get_video_queue", lambda: idle)
    import src.services.inference.comfy_proc as cp
    monkeypatch.setattr(cp, "get_comfy_proc", lambda: mgr)
    keyframe._release_idle_comfy_for_scoring()
    assert mgr.shutdown_calls == 1


def test_no_release_when_queue_busy(
        monkeypatch: pytest.MonkeyPatch, clean_lock) -> None:
    busy = _FakeQueue({"queued": [{"task_id": "t"}], "current": None,
                       "cloud_running": []})
    mgr = _FakeComfyMgr(pid=123)
    monkeypatch.setattr(keyframe, "get_image_queue", lambda: busy)
    import src.services.video_queue as vq
    monkeypatch.setattr(vq, "get_video_queue", lambda: busy)
    import src.services.inference.comfy_proc as cp
    monkeypatch.setattr(cp, "get_comfy_proc", lambda: mgr)
    keyframe._release_idle_comfy_for_scoring()
    assert mgr.shutdown_calls == 0


def test_no_release_when_comfy_not_running(
        monkeypatch: pytest.MonkeyPatch, clean_lock) -> None:
    idle = _FakeQueue({"queued": [], "current": None,
                       "cloud_running": []})
    mgr = _FakeComfyMgr(pid=None)
    monkeypatch.setattr(keyframe, "get_image_queue", lambda: idle)
    import src.services.video_queue as vq
    monkeypatch.setattr(vq, "get_video_queue", lambda: idle)
    import src.services.inference.comfy_proc as cp
    monkeypatch.setattr(cp, "get_comfy_proc", lambda: mgr)
    keyframe._release_idle_comfy_for_scoring()
    assert mgr.shutdown_calls == 0


# ── ④ 对话引擎回落 ────────────────────────────────────────────────

class _FakeEngine:
    def __init__(self) -> None:
        self.seen_images: list = []
        self.seen_messages: list = []
        self.ready = True

    def ensure_loaded(self) -> bool:
        return self.ready

    def chat_stream(self, messages, images=None, temperature=0.7,
                    max_new_tokens=1024):
        self.seen_images = list(images or [])
        self.seen_messages = messages
        yield "42"
        yield " 分"


def test_dialog_fallback_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """引擎就绪 → 返回适配器 + dialog_fallback(后端名) 透明标签。"""
    import src.services.inference.dialog_engine as de

    monkeypatch.setattr(de, "get_dialog_engine", lambda: _FakeEngine())
    chat, tag = keyframe._dialog_fallback_backend()
    assert chat is not None
    assert tag.startswith("dialog_fallback(")


def test_scorer_adapts_b64_to_pil() -> None:
    from PIL import Image

    eng = _FakeEngine()
    scorer = keyframe._DialogEngineScorer(eng)
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 100, 50)).save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    chunks = list(scorer.chat_stream(
        [{"role": "user", "content": "评分这张图"}], images_b64=[b64]))
    assert "".join(chunks) == "42 分"
    assert len(eng.seen_images) == 1
    assert eng.seen_images[0].size == (4, 4)
    # Qwen-VL content 列表格式（纯文本 content 不注图像占位符，
    # generate 报 tokens:0/features:N 空产出——2026-09-20 实测）
    content = eng.seen_messages[-1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "image"}
    assert content[-1] == {"type": "text", "text": "评分这张图"}


def test_fallback_honest_when_engine_not_ready(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import src.services.inference.dialog_engine as de

    class _Dead:
        def ensure_loaded(self) -> bool:
            return False

    monkeypatch.setattr(de, "get_dialog_engine", lambda: _Dead())
    chat, tag = keyframe._dialog_fallback_backend()
    assert chat is None
    assert tag == "dialog_engine_not_ready"


# ── ② 守卫持锁让位 ────────────────────────────────────────────────

class _StubDb:
    """_annotate_consistency 落库捕捉（update("keyframes", …)）。"""

    def __init__(self) -> None:
        self.updates: list[dict] = []

    def update(self, table: str, fields: dict, where: str,
               params: tuple) -> None:
        self.updates.append(fields)


@pytest.mark.asyncio
async def test_guard_yields_when_heavy_feature_holds_lock(
        monkeypatch: pytest.MonkeyPatch, clean_lock) -> None:
    db = _StubDb()
    monkeypatch.setattr(keyframe, "get_db_safe", lambda: db)
    monkeypatch.setattr(keyframe, "broadcast_gen_progress",
                        lambda *a, **k: None)
    monkeypatch.setattr(keyframe, "_GUARD_LOCK_WAIT_S", 0.0)  # 测试免等
    fl = clean_lock
    assert await fl.acquire("paint", task_id="user-gen") is True
    await keyframe._consistency_guard_inner(
        "row_x", "p1", "kf_x", 1, 1280, 720)
    assert db.updates, "让位必须落 skipped 注记"
    import json
    payload = json.loads(db.updates[0]["consistency"])
    assert payload["skipped"] == "busy"
    # 守卫不得残留 dialog 持锁
    assert not fl.is_feature_active("dialog")
