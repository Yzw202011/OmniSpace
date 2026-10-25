"""模型分类器哨兵+行为测（2026-10-25 实弹误分类根修）。

事故：models/paint/qwen-image-2.1（ComfyUI 单文件整包，无
config.json/model_index.json）被 `_NAME_KEYWORDS["qwen"]→DIALOG`
粗词劫持，模型管理页归入「大语言模型」。拍板：修（精确词优先）+
加固（扩散整包布局识别，视频路径排除）。
"""
from __future__ import annotations

from pathlib import Path

from src.data.models import ModelCategory
from src.services.model_manager.classifier import ModelClassifier

REPO = Path(__file__).resolve().parents[2]
CLASSIFIER_PY = (REPO / "src" / "services" / "model_manager"
                 / "classifier.py").read_text(encoding="utf-8")


def _classifier() -> ModelClassifier:
    return ModelClassifier()


def test_qwen_image_name_goes_vision(tmp_path: Path) -> None:
    """精确词修复：qwen-image-* 名字归 VISION，不再被 "qwen" 遮蔽。"""
    c = _classifier()
    assert c.classify(str(tmp_path / "qwen-image-2.1")) is ModelCategory.VISION
    assert c.classify(str(tmp_path / "qwen-image-2512")) is ModelCategory.VISION


def test_qwen_llm_names_still_dialog(tmp_path: Path) -> None:
    """回归防线：Qwen LLM/VLM 家族分类不受影响。"""
    c = _classifier()
    assert c.classify(str(tmp_path / "qwen35-9b-w4a16")) is ModelCategory.DIALOG
    assert c.classify(str(tmp_path / "qwen3-vl-8b-awq")) is ModelCategory.DIALOG


def test_diffusion_bundle_layout_goes_vision(tmp_path: Path) -> None:
    """布局加固：text_encoder/+vae/ 无 config.json 的整包 → VISION
    （防下次新绘画家族名字不含已知精确词时重蹈）。"""
    d = tmp_path / "some-new-image-model"
    (d / "text_encoder").mkdir(parents=True)
    (d / "vae").mkdir()
    (d / "model.safetensors").write_bytes(b"x")
    assert _classifier().classify(str(d)) is ModelCategory.VISION


def test_video_bundle_layout_not_vision(tmp_path: Path) -> None:
    """视频排除：video_gen/h3 同款布局不得归 VISION（防误转绘画组）。"""
    d = tmp_path / "video_gen" / "h3"
    (d / "text_encoders").mkdir(parents=True)
    (d / "vae").mkdir()
    got = _classifier().classify(str(d))
    assert got is not ModelCategory.VISION


def test_classifier_sentinels() -> None:
    assert '"qwen-image": ModelCategory.VISION' in CLASSIFIER_PY, (
        "qwen-image 精确词被删")
    assert CLASSIFIER_PY.index('"qwen-image"') < CLASSIFIER_PY.index(
        '"qwen":'), "精确词必须在粗词 qwen 之前（dict 保序）"
    assert "扩散整包布局加固" in CLASSIFIER_PY, "布局加固被删"
    assert '"video" in part.lower()' in CLASSIFIER_PY, "视频排除被删"
# 本项目仅供学习使用，商业授权请+Q 3559331368
