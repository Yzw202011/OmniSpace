"""四视图生成终态广播测试（2026-09-02 幽灵任务修复）。

用户实报：通知中心挂「漫剧生成」任务、后端显存早已释放——根因 =
_generate_turnaround_sync 只广播进度、从不广播 status=done，前端
任务条等不到终态（useTaskStore 以 task_progress 驱动，done 事件
收敛任务）。本测试锁定：成功路径必发 done、失败路径必发 error。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import pytest

from backend.api.manga import comic_gen


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, kind: str, ctx_id: str, **kw) -> None:
        self.calls.append({"kind": kind, "ctx_id": ctx_id, **kw})

    def has(self, status: str) -> bool:
        return any(c.get("status") == status for c in self.calls)


def _fake_req():
    from backend.data.models import AssetTurnaroundRequest
    return AssetTurnaroundRequest(project_id="p1", name="广播测试角色",
                                  prompt="测试", seed=1)


@pytest.fixture()
def patched(monkeypatch: pytest.MonkeyPatch):
    rec = _Recorder()
    monkeypatch.setattr(comic_gen, "broadcast_gen_progress", rec)
    # 假管线产物（不触引擎/文件系统重活）
    monkeypatch.setattr(
        comic_gen, "_run_turnaround_pipeline",
        lambda engine, out_dir, **kw: {
            "pipeline": "onepass", "seed": 1, "model": "flux",
            "views": {"front": "f", "side": "s", "back": "b",
                      "closeup": "c"},
            "canvas": "canvas.png", "consistency": {},
            "view_errors": [], "ref_used": False, "prompt_zh": "x"})
    monkeypatch.setattr(comic_gen, "_sync_portrait_from_views",
                        lambda out_dir: None)
    monkeypatch.setattr(comic_gen, "_apply_turnaround_meta",
                        lambda meta, gen: None)
    monkeypatch.setattr(comic_gen, "_append_asset_history",
                        lambda meta, action, old, rel: None)
    db = type("_DB", (), {
        "query_one": staticmethod(lambda *a, **k: None),
        "query": staticmethod(lambda *a, **k: []),
        "insert": staticmethod(lambda *a, **k: None),
        "update": staticmethod(lambda *a, **k: None),
    })()
    monkeypatch.setattr(comic_gen, "get_db_safe", lambda: db)
    # 落盘目录重定向到 tmp（out_dir.mkdir 会真实建目录）
    return rec


def test_turnaround_broadcasts_done_on_success(
        patched, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(comic_gen, "_COMIC_ASSET_DIR", tmp_path)
    monkeypatch.setattr(comic_gen, "DATA_DIR", tmp_path)  # relative_to 基准
    result = comic_gen._generate_turnaround_sync(_fake_req())
    assert patched.has("done"), "成功路径必须广播 status=done（幽灵根因）"
    assert patched.has("error") is False
    assert result["pipeline"] == "onepass"
    done_evt = [c for c in patched.calls if c.get("status") == "done"][0]
    assert done_evt["percent"] == 100 and done_evt["kind"] == "asset"


def test_turnaround_broadcasts_error_on_failure(
        patched, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(comic_gen, "_COMIC_ASSET_DIR", tmp_path)
    monkeypatch.setattr(comic_gen, "DATA_DIR", tmp_path)

    def _boom(engine, out_dir, **kw):
        raise RuntimeError("engine down")

    monkeypatch.setattr(comic_gen, "_run_turnaround_pipeline", _boom)
    with pytest.raises(RuntimeError):
        comic_gen._generate_turnaround_sync(_fake_req())
    assert patched.has("error"), "失败路径必须广播 status=error"
    assert patched.has("done") is False
