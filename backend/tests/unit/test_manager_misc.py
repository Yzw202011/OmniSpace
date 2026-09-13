"""ModelClassifier / ModelSelector / HardwareMonitor._cached 直测
（B9 收尾 2026-09-14）。ScheduleHistory.record 走生产库写入（get_db
单例），其内存降级分支已被 scheduler tick 间接覆盖，本文件不含——
避免为测它而在测试里打生产库连接补丁（诚实取舍，见战报）。
"""
from __future__ import annotations

import json

import backend.services.scheduler.monitor as _mon
from backend.data.models import ModelCategory
from backend.services.model_manager.classifier import ModelClassifier
from backend.services.model_manager.selector import ModelSelector
from backend.services.scheduler.monitor import HardwareMonitor

# ── ModelClassifier：识别顺序契约 ───────────────────────────────────

def test_classify_keyword_name(tmp_path) -> None:
    # 名字关键词（whisper→VOICE）；非目录非文件也命中（只看 name）
    assert ModelClassifier().classify(str(tmp_path / "whisper-tiny")) \
        is ModelCategory.VOICE


def test_classify_dir_signature(tmp_path) -> None:
    d = tmp_path / "some-paint-model"
    d.mkdir()
    (d / "model_index.json").write_text("{}", encoding="utf-8")
    assert ModelClassifier().classify(str(d)) is ModelCategory.VISION


def test_classify_metadata_beats_keyword(tmp_path) -> None:
    # 识别顺序第 1 条：目录 config.json 元数据优先于名字关键词——
    # 名字含 whisper 但 model_type=qwen3 → DIALOG（防语音遮蔽对话模型）
    d = tmp_path / "whisper-named-dialog"
    d.mkdir()
    (d / "config.json").write_text(
        json.dumps({"model_type": "qwen3"}), encoding="utf-8")
    assert ModelClassifier().classify(str(d)) is ModelCategory.DIALOG


def test_classify_extension(tmp_path) -> None:
    f = tmp_path / "weights.gguf"
    f.write_bytes(b"\x00")
    assert ModelClassifier().classify(str(f)) is ModelCategory.DIALOG
    f2 = tmp_path / "ckpt-file.ckpt"
    f2.write_bytes(b"\x00")
    assert ModelClassifier().classify(str(f2)) is ModelCategory.VISION


def test_classify_fallback_auxiliary(tmp_path) -> None:
    assert ModelClassifier().classify(str(tmp_path / "mystery-blob")) \
        is ModelCategory.AUXILIARY


# ── ModelSelector：分派与已下载过滤 ─────────────────────────────────

def test_selector_downloaded_scan_failure_tolerated(monkeypatch) -> None:
    import backend.services.model_manager as _mm
    def _boom():
        raise RuntimeError("scan failed")
    monkeypatch.setattr(_mm, "get_model_manager", _boom)
    assert ModelSelector()._get_downloaded_ids() == set()


def test_selector_dispatch_tables(monkeypatch) -> None:
    from backend.data.models import DIALOG_ROUTING_TABLE, PAINT_ROUTING_TABLE
    s = ModelSelector()
    seen: list = []

    def _fake_select(table: list, vram: float) -> str:
        seen.append(table)
        return "picked"

    monkeypatch.setattr(s, "_select_from_table", _fake_select)
    s.select_for_feature("dialog", 13.4)
    s.select_for_feature("Paint", 13.4)  # 大小写与别名归一
    s.select_for_feature("mystery-feature", 13.4)  # 未知功能→对话兜底
    assert seen[0] is DIALOG_ROUTING_TABLE
    assert seen[1] is PAINT_ROUTING_TABLE
    assert seen[2] is DIALOG_ROUTING_TABLE


def test_selector_video_gen_dispatch(monkeypatch) -> None:
    s = ModelSelector()
    calls: list[float] = []

    class _Stub:
        value = "minimax-h3"

    def _fake_video(vram: float) -> _Stub:
        calls.append(vram)
        return _Stub()

    monkeypatch.setattr(s, "select_video_model", _fake_video)
    out = s.select_for_feature("video_gen", 13.4)
    assert calls == [13.4]
    # 分支契约：返回 .value 字符串（历史注释里的 AttributeError 修复点）
    assert out == "minimax-h3"


def test_selector_skips_not_downloaded_higher_tier(monkeypatch) -> None:
    from backend.data.models import DIALOG_ROUTING_TABLE
    s = ModelSelector()
    monkeypatch.setattr(s, "_get_downloaded_ids", lambda: {"b"})
    table = [
        {"min_vram_gb": 20, "model": "a"},
        {"min_vram_gb": 10, "model": "b"},
    ]
    # a 显存够但未下载 → 跳过选 b（第一遍已下载过滤）
    assert s._select_from_table(table, 20.0) == "b"
    # 全部未下载 → 第二遍回退原行为（选 a）
    monkeypatch.setattr(s, "_get_downloaded_ids", lambda: set())
    assert s._select_from_table(table, 20.0) == "a"
    assert DIALOG_ROUTING_TABLE  # 真表可引用（防 typo）


# ── HardwareMonitor._cached：TTL 语义 ───────────────────────────────

def test_monitor_cached_ttl_hit_and_expire(monkeypatch) -> None:
    class _Clock:
        now = 100.0
    monkeypatch.setattr(_mon.time, "monotonic", lambda: _Clock.now)
    m = HardwareMonitor()
    calls: list[int] = []

    def producer() -> dict:
        calls.append(1)
        return {"v": len(calls)}

    assert m._cached("gpu", ttl_s=2.0, producer=producer) == {"v": 1}
    assert m._cached("gpu", ttl_s=2.0, producer=producer) == {"v": 1}
    assert len(calls) == 1  # TTL 内不重采
    _Clock.now += 3.0
    assert m._cached("gpu", ttl_s=2.0, producer=producer) == {"v": 2}
    assert len(calls) == 2  # 过期重采
