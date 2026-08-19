# -*- coding: utf-8 -*-
"""引擎层纯逻辑测试（TASK-P1-01，审计 P17）。

不碰 GPU、不碰真实模型权重，直测可脱离权重的纯函数与表驱动逻辑：
  - 对话候选 tier 路由（dialog_engine._effective_candidates）
  - 硬件档位表（data/models.detect_hardware_tier，文档B §4.2）
  - 模型路径解析（含 modelscope 嵌套快照布局）
  - 后端类型判定 / 显存估算 / 目录发现（dialog_engine）
  - safetensors dtype 感知的 fp16 加载字节估算（video_engine._dir_load_bytes_fp16）
  - LTX 参数约束校正（video_engine._ltx_align_params，帧数 ≡1 mod 8、宽高 %32）

手法参考 test_knowledge_pipeline.py：tmp_path 构造最小模型目录 +
monkeypatch 引擎模块的 MODELS_DIR 指向临时目录（sparse 文件满足体积
阈值而不占磁盘）。
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from backend.data.models import detect_hardware_tier
from backend.services.inference import dialog_engine, video_engine
from backend.services.inference.paint_engine import PAINT_MODEL_CANDIDATES

MB = 1024 * 1024
MIN_WEIGHT = 100 * MB


def _sparse(path: Path, size: int) -> Path:
    """创建指定逻辑体积的文件（NTFS 稀疏语义，不占实际磁盘）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)
    return path


def _make_transformers_dir(base: Path, name: str, model_type: str,
                           weight_mb: int = 120,
                           extra_config: dict | None = None,
                           architectures: list | None = None,
                           tokenizer: bool = False) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    cfg = {"model_type": model_type}
    if architectures:
        cfg["architectures"] = architectures
    if extra_config:
        cfg.update(extra_config)
    (d / "config.json").write_text(
        json.dumps(cfg), encoding="utf-8")
    if tokenizer:
        (d / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    if weight_mb > 0:
        _sparse(d / "model.safetensors", weight_mb * MB)
    return d


# ── 对话候选 tier 路由 ────────────────────────────────────────────

def test_dialog_low_tier_excludes_8b(monkeypatch):
    """低档位（探测失败/无 CUDA 按 0）：仅 4b/2b 保守候选。"""
    monkeypatch.setattr(dialog_engine, "_gpu_tier_min_vram_gb", lambda: 0.0)
    cands = dialog_engine._effective_candidates()
    assert cands == dialog_engine.DIALOG_MODEL_CANDIDATES
    assert "qwen3-vl-8b" not in [c[0] for c in cands]


def test_dialog_high_tier_prepends_8b(monkeypatch):
    """高档位（RTX 4090/5090 档 min_vram≥20）：8b 成为首选候选。"""
    monkeypatch.setattr(dialog_engine, "_gpu_tier_min_vram_gb", lambda: 20.0)
    cands = dialog_engine._effective_candidates()
    assert cands[0] == ("qwen3-vl-8b", "qwen3-vl-8b", 12.0)
    assert cands[1:] == dialog_engine.DIALOG_MODEL_CANDIDATES


def test_dialog_tier_boundary_is_inclusive(monkeypatch):
    """min_vram 恰为 12.0 时触发高档路由（>= 比较，边界含）。"""
    monkeypatch.setattr(dialog_engine, "_gpu_tier_min_vram_gb", lambda: 12.0)
    cands = dialog_engine._effective_candidates()
    assert cands[0][0] == "qwen3-vl-8b"


def test_dialog_tier_just_below_boundary(monkeypatch):
    """min_vram=11.9（如 RTX 5070 12GB 实际 11.x 可用）不触发 8b 路由。"""
    monkeypatch.setattr(dialog_engine, "_gpu_tier_min_vram_gb", lambda: 11.9)
    cands = dialog_engine._effective_candidates()
    assert "qwen3-vl-8b" not in [c[0] for c in cands]


# ── 硬件档位表（纯表驱动，文档B §4.2）──────────────────────────────

def test_tier_name_match_rtx5070ti():
    tier = detect_hardware_tier("NVIDIA GeForce RTX 5070 Ti", 16384)
    assert tier["tier"] == "rtx5070ti"
    assert tier["matched_by"] == "name"
    assert tier["min_vram_gb"] == 14
    assert tier["models"]["dialog"] == "qwen3-vl-8b"


def test_tier_name_match_rtx4090():
    tier = detect_hardware_tier("NVIDIA GeForce RTX 4090", 24576)
    assert tier["tier"] == "rtx4090"
    assert tier["min_vram_gb"] == 20


def test_tier_ti_variant_outranks_base():
    """表序依赖回归：5070 Ti 必须先于 5070 匹配（"5070 ti" 含 "5070"）。"""
    ti = detect_hardware_tier("NVIDIA GeForce RTX 5070 Ti", 16384)
    base = detect_hardware_tier("NVIDIA GeForce RTX 5070", 12288)
    assert ti["tier"] == "rtx5070ti"
    assert base["tier"] == "rtx5070"


def test_tier_fallback_by_vram_12gb():
    """未登记型号按显存保守降档：12GB → rtx4070（10GB 档）而非 4070Ti。"""
    tier = detect_hardware_tier("Mystery GPU Model X", 12288)
    assert tier["matched_by"] == "vram"
    assert tier["tier"] == "rtx4070"
    assert tier["min_vram_gb"] == 10


def test_tier_fallback_by_vram_16gb():
    tier = detect_hardware_tier("Mystery GPU Model X", 16384)
    assert tier["matched_by"] == "vram"
    assert tier["tier"] == "rtx4080"


def test_tier_no_gpu_falls_to_cpu():
    """无 GPU（空名 + 0 显存）：回退表尾 CPU 档。"""
    tier = detect_hardware_tier("", 0)
    assert tier["matched_by"] == "none"
    assert tier is not None
    from backend.data.models import HARDWARE_TIER_TABLE
    assert tier["tier"] == HARDWARE_TIER_TABLE[-1]["tier"]


# ── 模型路径解析（扁平 + modelscope 嵌套布局）──────────────────────

def test_resolve_flat_layout(monkeypatch, tmp_path):
    monkeypatch.setattr(dialog_engine, "MODELS_DIR", tmp_path)
    _make_transformers_dir(tmp_path, "qwen3-vl-4b", "qwen3_vl")
    resolved = dialog_engine._resolve_candidate_dir("qwen3-vl-4b")
    assert resolved == tmp_path / "qwen3-vl-4b"


def test_resolve_modelscope_nested_snapshot(monkeypatch, tmp_path):
    """modelscope 嵌套布局：models/<id>/models/<Org--Name>/snapshots/<rev>/。"""
    monkeypatch.setattr(dialog_engine, "MODELS_DIR", tmp_path)
    snap_parent = (tmp_path / "qwen3-vl-8b" / "models"
                   / "Qwen--Qwen3-VL-8B-Instruct" / "snapshots")
    snap = _make_transformers_dir(snap_parent, "master", "qwen3_vl")
    resolved = dialog_engine._resolve_candidate_dir("qwen3-vl-8b")
    assert resolved == snap


def test_resolve_incomplete_download_returns_none(monkeypatch, tmp_path):
    """下载残留（.safetensors.incomplete）不算就绪权重。"""
    monkeypatch.setattr(dialog_engine, "MODELS_DIR", tmp_path)
    d = tmp_path / "half"
    d.mkdir()
    (d / "config.json").write_text('{"model_type": "qwen3_vl"}',
                                   encoding="utf-8")
    _sparse(d / "model.safetensors.incomplete", 2 * MB)
    assert dialog_engine._resolve_candidate_dir("half") is None


def test_resolve_missing_dir_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(dialog_engine, "MODELS_DIR", tmp_path)
    assert dialog_engine._resolve_candidate_dir("nope") is None


# ── 后端类型判定 / 显存估算 / 目录发现 ─────────────────────────────

def test_detect_backend_vl(tmp_path):
    d = _make_transformers_dir(tmp_path, "m", "qwen3_vl")
    assert dialog_engine._detect_backend(d) == "vl"


def test_detect_backend_text_causallm(tmp_path):
    d = _make_transformers_dir(tmp_path, "m", "llama",
                               architectures=["LlamaForCausalLM"],
                               tokenizer=True)
    assert dialog_engine._detect_backend(d) == "text"


def test_detect_backend_rejects_whisper(tmp_path):
    d = _make_transformers_dir(tmp_path, "m", "whisper")
    assert dialog_engine._detect_backend(d) == ""


def test_detect_backend_causallm_with_vision_is_vl(tmp_path):
    """Phi-4 风格：CausalLM 注册但 config 含 vision_config → 走 VL 加载。"""
    d = _make_transformers_dir(tmp_path, "m", "phi3_v",
                               architectures=["Phi3VForCausalLM"],
                               extra_config={"vision_config": {}})
    assert dialog_engine._detect_backend(d) == "vl"


def test_estimate_vram_gguf(tmp_path):
    g = _sparse(tmp_path / "model.gguf", 1024 * MB)
    assert dialog_engine._estimate_vram_gb(g, "gguf") == 1.10


def test_estimate_vram_fp32_disk_compressed(tmp_path):
    """fp32 存盘加载 bf16：2GiB 权重估 ~1.21GB（×0.55×1.10）。"""
    d = _make_transformers_dir(tmp_path, "m", "llama", weight_mb=2048,
                               extra_config={"torch_dtype": "float32"})
    assert dialog_engine._estimate_vram_gb(d, "text") == round(
        2 * 0.55 * 1.10, 2)


def test_estimate_vram_fp16_full_bytes(tmp_path):
    d = _make_transformers_dir(tmp_path, "m", "llama", weight_mb=2048,
                               extra_config={"torch_dtype": "bfloat16"})
    assert dialog_engine._estimate_vram_gb(d, "text") == round(
        2 * 1.15 * 1.10, 2)


def test_discover_dialog_models_full_flow(monkeypatch, tmp_path):
    """目录发现：transformers 目录 / GGUF 目录取最大文件 / 顶层 GGUF /
    小于 100MB 跳过 / 下划线目录跳过 / 两层扫描。"""
    monkeypatch.setattr(dialog_engine, "MODELS_DIR", tmp_path)
    _make_transformers_dir(tmp_path, "vl-model", "qwen3_vl")

    gguf_dir = tmp_path / "gguf-model"
    gguf_dir.mkdir()
    _sparse(gguf_dir / "q4_k_m.gguf", 150 * MB)
    _sparse(gguf_dir / "q8.gguf", 300 * MB)

    _sparse(tmp_path / "top.gguf", 200 * MB)
    _make_transformers_dir(tmp_path, "tiny", "qwen3_vl", weight_mb=1)
    _make_transformers_dir(tmp_path, "_hidden", "qwen3_vl")
    _make_transformers_dir(tmp_path / "outer", "inner", "llama",
                           architectures=["LlamaForCausalLM"])

    found = dialog_engine.discover_dialog_models()

    assert found["vl-model"]["backend"] == "vl"
    assert found["vl-model"]["path"].endswith("vl-model")

    assert found["gguf-model"]["backend"] == "gguf"
    assert found["gguf-model"]["path"].endswith("q8.gguf")  # 取最大文件

    assert found["top"]["backend"] == "gguf"
    assert found["top"]["path"].endswith("top.gguf")

    assert "tiny" not in found          # 权重 <100MB
    assert "_hidden" not in found       # 下划线开头跳过
    assert found["inner"]["backend"] == "text"  # 两层扫描命中


# ── safetensors dtype 感知的 fp16 加载字节估算 ─────────────────────

def _make_safetensors(path: Path, tensors: dict[str, dict],
                      data_len: int = 4096) -> Path:
    header = json.dumps(tensors).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(struct.pack("<Q", len(header)))
        f.write(header)
        f.write(b"\x00" * data_len)
    return path


def _st(dtype: str) -> dict:
    return {"dtype": dtype, "shape": [1], "data_offsets": [0, 4]}


def test_dir_bytes_f32_safetensors_halved(tmp_path):
    f = _make_safetensors(tmp_path / "m.safetensors", {"w": _st("F32")})
    size = f.stat().st_size
    assert video_engine._dir_load_bytes_fp16(tmp_path) == size // 2


def test_dir_bytes_f16_bf16_full(tmp_path):
    f16 = _make_safetensors(tmp_path / "a.safetensors", {"w": _st("F16")})
    total = f16.stat().st_size
    assert video_engine._dir_load_bytes_fp16(tmp_path) == total

    tmp2 = tmp_path / "bf16"
    bf = _make_safetensors(tmp2 / "m.safetensors", {"w": _st("BF16")})
    assert video_engine._dir_load_bytes_fp16(tmp2) == bf.stat().st_size


def test_dir_bytes_mixed_dtype_full(tmp_path):
    """F32+F16 混存头：非全 F32，按原字节保守计。"""
    f = _make_safetensors(tmp_path / "m.safetensors",
                          {"a": _st("F32"), "b": _st("F16")})
    assert video_engine._dir_load_bytes_fp16(tmp_path) == f.stat().st_size


def test_dir_bytes_bin_skipped_when_safetensors_twin(tmp_path):
    """同名 .bin 与 .safetensors 并存：跳过 .bin 防重复计数。"""
    st = _make_safetensors(tmp_path / "m.safetensors", {"w": _st("F32")})
    _sparse(tmp_path / "m.bin", 500 * MB)
    assert video_engine._dir_load_bytes_fp16(tmp_path) == st.stat().st_size // 2


def test_dir_bytes_bin_alone_counted(tmp_path):
    b = _sparse(tmp_path / "w.bin", 100 * MB)
    assert video_engine._dir_load_bytes_fp16(tmp_path) == b.stat().st_size


def test_dir_bytes_corrupt_header_conservative(tmp_path):
    """头部损坏（hlen 垃圾值）：按原字节保守估计，不抛异常。"""
    f = tmp_path / "bad.safetensors"
    f.write_bytes(struct.pack("<Q", 10 ** 12) + b"garbage" * 100)
    assert video_engine._dir_load_bytes_fp16(tmp_path) == f.stat().st_size


def test_dir_bytes_recursive_and_ignores_other_exts(tmp_path):
    """子目录权重计入（LTX 多组件布局）；.txt/.json 不计。"""
    _make_safetensors(tmp_path / "text_encoder" / "te.safetensors",
                      {"w": _st("F16")})
    (tmp_path / "notes.txt").write_text("x" * 1000, encoding="utf-8")
    te = (tmp_path / "text_encoder" / "te.safetensors").stat().st_size
    assert video_engine._dir_load_bytes_fp16(tmp_path) == te


# ── LTX 参数约束校正 ──────────────────────────────────────────────

def test_ltx_720p_preset_alignment():
    """1080p/720p 预设 1280x720：720 对齐 736，1280 不变。"""
    frames, w, h = video_engine._ltx_align_params(65, 1280, 720)
    assert (frames, w, h) == (65, 1280, 736)


def test_ltx_frames_round_up_to_mod8():
    frames, _, _ = video_engine._ltx_align_params(60, 1280, 720)
    assert frames == 65
    assert frames % 8 == 1


def test_ltx_frames_cap_257():
    frames, _, _ = video_engine._ltx_align_params(300, 1280, 720)
    assert frames == 257
    assert 257 % 8 == 1


def test_ltx_frames_floor_9():
    frames, _, _ = video_engine._ltx_align_params(4, 1280, 720)
    assert frames == 9
    assert 9 % 8 == 1


def test_ltx_resolution_floor_32():
    frames, w, h = video_engine._ltx_align_params(9, 16, 16)
    assert (w, h) == (32, 32)


@pytest.mark.parametrize("nf,w,h", [
    (1, 1, 1), (9, 33, 33), (60, 720, 1280), (100, 100, 100),
    (256, 736, 736), (1000, 4096, 4096), (17, 63, 95),
])
def test_ltx_invariants(nf, w, h):
    """不变式：输出帧数 ∈ [9,257] 且 ≡1 (mod 8)；宽高 ≥32 且 %32==0。"""
    frames, ow, oh = video_engine._ltx_align_params(nf, w, h)
    assert 9 <= frames <= 257
    assert frames % 8 == 1
    assert ow >= 32 and ow % 32 == 0
    assert oh >= 32 and oh % 32 == 0


# ── 候选表自身一致性 ──────────────────────────────────────────────

def test_dialog_candidate_table_wellformed():
    ids = [c[0] for c in dialog_engine.DIALOG_MODEL_CANDIDATES]
    assert len(ids) == len(set(ids)), "候选 model_id 重复"
    for _mid, rel, vram in dialog_engine.DIALOG_MODEL_CANDIDATES:
        assert rel, f"{_mid} 缺目录映射"
        assert vram > 0, f"{_mid} 显存需求必须为正"


def test_paint_candidate_table_wellformed():
    ids = [c[0] for c in PAINT_MODEL_CANDIDATES]
    assert len(ids) == len(set(ids)), "候选 model_id 重复"
    for _mid, rel, vram in PAINT_MODEL_CANDIDATES:
        assert rel, f"{_mid} 缺目录映射"
        assert vram > 0, f"{_mid} 显存需求必须为正"
