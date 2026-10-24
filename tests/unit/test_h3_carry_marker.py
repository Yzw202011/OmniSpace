"""行级承接（2026-10-24）单测：C 段标记识别 / AddGuide 图注入 / 尾帧画幅护栏 / 生词 prev_context。

对应实现：
- src/api/manga/common.py            _build_abc_prompt(prev_context=)
- src/api/manga/storyboard.py        _load_prev_shot_context
- src/services/inference/h3_chain_engine.py
    detect_carry_marker / _find_prev_row_video / _extract_carry_frame /
    _render_graph(carry_image=) —— 注入 LoadImage+MiniMaxH3AddGuide 并
    改接 ChainContext(1703) 的 conditioning；无 carry_image 时渲染结果
    与模板等价（无新增节点、接线原样）。
"""
from __future__ import annotations

import json
import subprocess  # noqa: S404 - 测试内打桩 ffmpeg 调用
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.api.manga.common import _build_abc_prompt
from src.api.manga.storyboard import _load_prev_shot_context
from src.services.inference.h3_chain_engine import (
    _extract_carry_frame,
    _find_prev_row_video,
    _load_template_text,
    _render_graph,
    detect_carry_marker,
)


# ── detect_carry_marker：词表收紧，只认「动词+上一镜/前一镜」 ─────────
@pytest.mark.parametrize("cseg", [
    "画面：承接上一镜头，少女转身",
    "画面：紧接上一镜头",
    "画面：延续前一镜头的构图",
    "[0.1s-2.0s] 画面：承接上一镜",
    "承接上个镜头的画面继续推进",
])
def test_carry_marker_positive(cseg: str) -> None:
    desc = f"A. 全局风格：网漫风。\nB. 世界观：少女立于街角。\nC. 分镜时间轴：\n{cseg}"
    assert detect_carry_marker(desc) is True


@pytest.mark.parametrize("desc", [
    # B 段散文同形词不触发（只扫 C 段）
    ("A. 全局风格：网漫风。B. 世界观：情绪承接前文，气氛凝重。\n"
     "C. 分镜时间轴：\n[0.1s-2.0s] 画面：少女转身回望"),
    # C 段内普通叙事（动词后无「上一镜/前一镜」宾语）
    "B. 世界观。\nC. 分镜时间轴：\n[0.1s-2.0s] 画面：情绪延续，特写面容",
    "C. 分镜时间轴：\n[0.1s-2.0s] 画面：街角全景",
    "",
    "自由文本描述词，没有 ABC 段标记，也没有任何承接字样",
])
def test_carry_marker_negative(desc: str) -> None:
    assert detect_carry_marker(desc) is False


# ── _render_graph：注入与零回归 ─────────────────────────────────────
_TEMPLATE = json.loads(_load_template_text())
_REFS = [{"kind": "character", "name": "少女", "image": "h3chain_t/p1.png"}]


def _render(**kw) -> dict:
    return _render_graph(json.loads(json.dumps(_TEMPLATE)), "{}", "run",
                         864, 480, _REFS, "task1", **kw)


def test_render_without_carry_unchanged() -> None:
    graph = _render()
    assert "8100" not in graph and "8101" not in graph
    assert (graph["1703"]["inputs"]["conditioning"]
            == _TEMPLATE["1703"]["inputs"]["conditioning"])


def test_render_with_carry_injects_addguide() -> None:
    graph = _render(carry_image="h3chain_task1/carry_tail.png")
    assert graph["8100"]["class_type"] == "LoadImage"
    assert graph["8100"]["inputs"]["image"] == "h3chain_task1/carry_tail.png"
    guide = graph["8101"]
    assert guide["class_type"] == "MiniMaxH3AddGuide"
    assert guide["inputs"]["positive"] == ["110", 0]
    assert guide["inputs"]["latent"] == ["110", 1]
    assert guide["inputs"]["vae"] == ["3", 0]
    assert guide["inputs"]["image"] == ["8100", 0]
    assert guide["inputs"]["frame_idx"] == 0
    assert graph["1703"]["inputs"]["conditioning"] == ["8101", 0]


# ── _find_prev_row_video：上一行最新成功视频解析 ─────────────────────
class _FakeDb:
    """按调用次序弹出 query_one 结果的最小桩。"""

    def __init__(self, results: list) -> None:
        self._results = list(results)

    def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        return self._results.pop(0)


def test_find_prev_row_video_absolute_path(tmp_path: Path) -> None:
    vid = tmp_path / "prev.mp4"
    vid.write_bytes(b"fake")
    db = _FakeDb([
        {"storyboard_id": "sb1", "sort_index": 2},
        {"id": "row1"},
        {"file_path": str(vid)},
    ])
    assert _find_prev_row_video(db, "row2") == str(vid)


def test_find_prev_row_video_missing_file(tmp_path: Path) -> None:
    db = _FakeDb([
        {"storyboard_id": "sb1", "sort_index": 2},
        {"id": "row1"},
        {"file_path": str(tmp_path / "gone.mp4")},
    ])
    assert _find_prev_row_video(db, "row2") == ""


def test_find_prev_row_video_no_prev_row() -> None:
    db = _FakeDb([{"storyboard_id": "sb1", "sort_index": 1}, None])
    assert _find_prev_row_video(db, "row1") == ""


# ── _extract_carry_frame：画幅比护栏（打桩 ffmpeg，不依赖真机） ───────
@pytest.fixture()
def _fake_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.services.encoder_service as enc_mod
    monkeypatch.setattr(
        enc_mod, "get_encoder_service",
        lambda: SimpleNamespace(available=True, ffmpeg_path="ffmpeg"))


def _stub_ffmpeg_writing(size: tuple[int, int], monkeypatch) -> None:
    def _run(cmd: list, **kwargs):
        from PIL import Image
        Image.new("RGB", size, (10, 10, 10)).save(Path(cmd[-1]), format="PNG")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", _run)


def test_extract_carry_frame_matching_aspect(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        _fake_encoder: None) -> None:
    _stub_ffmpeg_writing((1344, 768), monkeypatch)
    assert _extract_carry_frame("x.mp4", tmp_path / "t.png", 1344, 768) is True


def test_extract_carry_frame_face_refine_padding_passes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        _fake_encoder: None) -> None:
    # 2026-10-24 段2 实弹修正：脸修复链把 1344 宽垫到 1376（偏差 2.38%），
    # 2% 护栏误杀真承接——5% 容差下必须放行
    _stub_ffmpeg_writing((1376, 768), monkeypatch)
    assert _extract_carry_frame("x.mp4", tmp_path / "t.png", 1344, 768) is True


def test_extract_carry_frame_aspect_mismatch_falls_back(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        _fake_encoder: None) -> None:
    _stub_ffmpeg_writing((480, 854), monkeypatch)  # 竖屏 → 横屏目标
    assert _extract_carry_frame("x.mp4", tmp_path / "t.png",
                                1344, 768) is False


def test_extract_carry_frame_encoder_unavailable(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.services.encoder_service as enc_mod
    monkeypatch.setattr(
        enc_mod, "get_encoder_service",
        lambda: SimpleNamespace(available=False, ffmpeg_path=""))
    assert _extract_carry_frame("x.mp4", tmp_path / "t.png",
                                1344, 768) is False


# ── 生词侧：prev_context 注入与零回归 ────────────────────────────────
def test_build_abc_prompt_prev_context_injection() -> None:
    row = {"original_dialogue": "嗯，到底在哪儿呢？", "duration": 10}
    base = _build_abc_prompt(row, [])
    prev = "[8.0s-10s] 画面：[夜风] 青年驻足栏杆前远眺 | 运镜：平视中景"
    with_prev = _build_abc_prompt(row, [], prev_context=prev)
    assert base == _build_abc_prompt(row, [], prev_context="")
    assert "承接上一镜头" not in base
    assert "承接上一镜头" in with_prev
    assert prev in with_prev
    assert "严禁使用该前缀" in with_prev  # 非连续场景的双向约束


def test_load_prev_shot_context_returns_last_shot() -> None:
    desc = ("A. 全局风格：日系动漫。\n"
            "B. 高密度世界观构建：青年立于天台。\n"
            "C. 分镜时间轴（分镜网格 1×2）：\n"
            "[0.1s-4.0s] 画面：[天台夜风] 青年推门上天台 | 运镜：平视中景\n"
            "[4.0s-8.0s] 画面：[拾起收音机] 青年蹲下拾起收音机 | 运镜：俯视近景")
    db = _FakeDb([
        {"storyboard_id": "sb1", "sort_index": 2},
        {"description": desc},
    ])
    out = _load_prev_shot_context(db, "row2")
    assert out.startswith("[4.0s-8.0s]")
    assert "拾起收音机" in out


def test_load_prev_shot_context_empty_cases() -> None:
    assert _load_prev_shot_context(None, "r") == ""
    db = _FakeDb([None])
    assert _load_prev_shot_context(db, "r") == ""
    db2 = _FakeDb([{"storyboard_id": "", "sort_index": 0}])
    assert _load_prev_shot_context(db2, "r") == ""
