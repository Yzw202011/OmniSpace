"""模型就绪总检单测（2026-09-03 体验流 #3：绿灯/缺件指路）。

沙盘 manifest + tmp 目录验证：模块归并、权重文件判据（≥100MB 才算
在位，防空壳目录）、内置件缺件不翻灰只记 notes、非内置缺件计数。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MOD_PATH = ROOT / "backend" / "services" / "model_manager" / "readiness.py"

spec = importlib.util.spec_from_file_location("_readiness_under_test", MOD_PATH)
readiness = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = readiness
spec.loader.exec_module(readiness)

BIG = 120 * 1024 * 1024          # ≥100MB 判据之上
SMALL = 1024


def _mk_model(root: Path, rel: str, *, big: bool = True) -> None:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "weight.safetensors").write_bytes(b"x" * (BIG if big else SMALL))


def _manifest(tmp_path: Path, entries: dict) -> Path:
    mf = tmp_path / "models_manifest.json"
    mf.write_text(json.dumps(
        {"version": "3.0.0", "models": entries}, ensure_ascii=False),
        encoding="utf-8")
    return mf


def test_module_merge_and_missing(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    _mk_model(root, "qwen3-vl-4b")                      # dialog 在位
    mf = _manifest(tmp_path, {
        "qwen3-vl-4b": {"type": "dialog", "path": "qwen3-vl-4b"},
        "flux2-klein-9b": {"type": "image_gen", "path": "paint/flux2-klein-9b"},
        "minimax-h3": {"type": "video_gen", "path": "video_gen/h3"},
    })
    r = readiness.compute_readiness(root, mf)
    by_key = {m["key"]: m for m in r["modules"]}
    assert by_key["dialog"]["ready"] is True
    assert by_key["paint"]["ready"] is False            # image_gen 缺
    assert by_key["manga_keyframe"]["ready"] is False   # 同底座联动
    assert by_key["manga_video"]["ready"] is False
    assert r["missing_count"] == 2 and r["all_ready"] is False
    assert by_key["paint"]["missing"] == ["flux2-klein-9b"]


def test_empty_dir_without_weight_is_missing(tmp_path):
    """目录在但文件是空壳（<100MB）＝不在位（16.5G 空壳旧案防线）。"""
    root = tmp_path / "models"
    root.mkdir()
    _mk_model(root, "sdxl", big=False)                  # 只有小文件
    mf = _manifest(tmp_path, {
        "sdxl": {"type": "image_gen", "path": "sdxl"}})
    r = readiness.compute_readiness(root, mf)
    by_key = {m["key"]: m for m in r["modules"]}
    assert by_key["paint"]["ready"] is False
    assert r["missing_count"] == 1


def test_builtin_missing_keeps_green_with_note(tmp_path):
    """内置件（embedding/asr/…）缺件不翻灰，只进 notes（包损坏≠用户没放）。"""
    root = tmp_path / "models"
    root.mkdir()
    _mk_model(root, "qwen3-vl-4b")
    _mk_model(root, "sdxl")                              # 补齐非内置类型
    _mk_model(root, "video_gen/h3")
    mf = _manifest(tmp_path, {
        "qwen3-vl-4b": {"type": "dialog", "path": "qwen3-vl-4b"},
        "sdxl": {"type": "image_gen", "path": "sdxl"},
        "minimax-h3": {"type": "video_gen", "path": "video_gen/h3"},
        "bge-large-zh": {"type": "embedding", "path": "embed/bge"},
    })
    r = readiness.compute_readiness(root, mf)
    by_key = {m["key"]: m for m in r["modules"]}
    assert by_key["knowledge"]["ready"] is True          # 内置缺件不翻灰
    assert by_key["knowledge"]["builtin"] is True
    assert any("内置件缺失" in n for n in r["notes"])
    assert r["missing_count"] == 0 and r["all_ready"] is True


def test_small_builtin_model_counts_present(tmp_path):
    """小内置件回归（dinov2 翻案）：单文件 60MB<100MB 但 ≥50MB ＝在位。"""
    root = tmp_path / "models"
    d = root / "face" / "dinov2-small"
    d.mkdir(parents=True)
    (d / "model.safetensors").write_bytes(b"x" * (60 * 1024 * 1024))
    mf = _manifest(tmp_path, {
        "dinov2-small": {"type": "auxiliary", "path": "face/dinov2-small",
                         "size_gb": 0.1}})
    r = readiness.compute_readiness(root, mf)
    by_key = {m["key"]: m for m in r["modules"]}
    assert by_key["consistency"]["present"] == ["dinov2-small"]
    assert not any("内置件缺失" in n for n in r["notes"])


def test_big_model_hollow_by_declared_size(tmp_path):
    """空壳防线：碎文件攒 200MB 但 manifest 声称 30G ＝不在位。"""
    root = tmp_path / "models"
    d = root / "paint" / "klein-9b"
    d.mkdir(parents=True)
    for i in range(4):                                   # 4×50MB 碎片
        (d / f"shard_{i}.bin").write_bytes(b"x" * (50 * 1024 * 1024 - 1))
    mf = _manifest(tmp_path, {
        "flux2-klein-9b": {"type": "image_gen",
                           "path": "paint/klein-9b", "size_gb": 30.0}})
    r = readiness.compute_readiness(root, mf)
    assert r["missing_count"] == 1


def test_no_manifest_reports_all_pending(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    r = readiness.compute_readiness(root, tmp_path / "nope.json")
    assert r["manifest_found"] is False
    assert all(m["ready"] is False for m in r["modules"])
    assert r["modules"] and r["modules"][0]["key"] == "dialog"


def test_scan_entry_cap_prevents_hang(tmp_path):
    """病态目录（海量小文件）触保险丝＝按不在位返回，不死循环。"""
    root = tmp_path / "models"
    d = root / "junk"
    d.mkdir(parents=True)
    for i in range(50):                                  # 每目录小文件批量
        for j in range(100):
            (d / f"s{i}" ).mkdir(exist_ok=True)
            (d / f"s{i}" / f"f{j}.bin").write_bytes(b"x" * SMALL)
    mf = _manifest(tmp_path, {"junk": {"type": "image_gen", "path": "junk"}})
    r = readiness.compute_readiness(root, mf)            # 应在封顶后快速返回
    assert r["missing_count"] == 1
