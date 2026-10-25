"""H3 FaceRefine 服务单测（2026-09-30 方案A）。

零 GPU：模板加载/参数化字段覆盖/开关读取/降级路径（预检不过与
执行失败都返回原片——零阻断铁律）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.services.inference import h3_face_refine as fr


@pytest.fixture()
def template() -> dict:
    return json.loads(fr._WORKFLOW_TEMPLATE.read_text(encoding="utf-8"))


def test_template_frozen_values(template):
    """实弹五坑的固化值锁定（换模板须重验五坑）。"""
    ins2 = template["2"]["inputs"]
    assert ins2["detector"] == "face_yolov8m.pt"          # 平铺名（坑②）
    assert ins2["canvas_mode"] == "manual"                # 钉 768（坑③）
    assert (ins2["canvas_width"], ins2["canvas_height"]) == (768, 768)
    assert ins2["confidence"] == 0.25
    assert "MiniMaxH3NativeAudioLock" in {
        template[n]["class_type"] for n in template}      # 外部依赖节点（坑①）
    assert template["20"]["inputs"]["model_name"] == "sam_vit_b_01ec64.pth"
    assert "turbo_8step" in str(
        template["12"]["inputs"].get("lora_name", ""))    # 8 步 turbo 档


def test_refine_video_parametrization(tmp_path, template, monkeypatch):
    """参数化五处正确覆盖模板（源片/参考图×2/prompt/输出前缀）。"""
    monkeypatch.setattr(fr, "facerefine_ready", lambda base=None: True)
    # 假 ComfyUI：捕获提交的 wf，返回一个带产物的工作流结果
    captured: dict = {}

    class _Img:
        def save(self, path, format=None):  # noqa: A002
            Path(path).write_bytes(b"png")

    # 97c3681（2026-10-24）：_api 签名去 base（端口并 comfy_proc
    # COMFY_PORT 单一真源，防 8189 硬编码脑裂）——假桩同步
    def fake_api(method, path, body=None, timeout=10.0):
        if method == "POST" and path == "/prompt":
            captured["wf"] = body["prompt"]
            return {"prompt_id": "t1"}
        if method == "GET" and path.startswith("/history/"):
            fr_dir = tmp_path / "face_refine"
            fr_dir.mkdir(exist_ok=True)
            out = fr_dir / "fake.mp4"
            out.write_bytes(b"mp4")
            # 产物音轨哨兵：mock _check_audio_or_raise 直通
            monkeypatch.setattr(fr, "_check_audio_or_raise",
                                lambda p: None)
            return {"t1": {"status": {"completed": False},
                           "outputs": {"23": {"gifs": [{
                               "filename": "fake.mp4",
                               "subfolder": "face_refine"}]}}}}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(fr, "_api", fake_api)
    monkeypatch.setattr(fr.time, "sleep", lambda s: None)  # 轮询零等待

    src = tmp_path / "seg.mp4"
    src.write_bytes(b"src")
    out = fr.refine_video(src, prompt="PROMPT",
                          ref_images=[_Img(), _Img()],
                          comfy_input=tmp_path, comfy_output=tmp_path)
    assert out != src and out.name == "seg_fr.mp4"
    wf = captured["wf"]
    assert wf["1"]["inputs"]["video"] == str(src)
    assert wf["9"]["inputs"]["prompt"] == "PROMPT"
    assert wf["6"]["inputs"]["image"].startswith("fr_")
    assert wf["23"]["inputs"]["filename_prefix"].startswith("face_refine/")


def test_refine_video_degrades_on_workflow_error(tmp_path, monkeypatch):
    """工作流校验失败 → 原片返回（零阻断）。"""
    monkeypatch.setattr(fr, "facerefine_ready", lambda base=None: True)

    def bad_api(base, method, path, body=None, timeout=10.0):
        if method == "POST":
            return {"error": {"message": "bad"}}
        raise AssertionError

    monkeypatch.setattr(fr, "_api", bad_api)
    src = tmp_path / "seg.mp4"
    src.write_bytes(b"x")

    class _Img:
        def save(self, path, format=None):  # noqa: A002
            Path(path).write_bytes(b"png")

    assert fr.refine_video(src, prompt="p", ref_images=[_Img()],
                           comfy_input=tmp_path) is src


def test_refine_video_skips_without_refs(tmp_path, monkeypatch):
    """无参考图 → 原片返回（不提交）。"""
    monkeypatch.setattr(fr, "facerefine_ready", lambda base=None: True)
    src = tmp_path / "s.mp4"
    assert fr.refine_video(src, prompt="p", ref_images=[]) is src


def test_facerefine_ready_weight_missing(monkeypatch):
    """权重缺失 → False 且缓存（安装态结论可缓存）。"""
    monkeypatch.setattr(fr, "_READY_CACHE", None)
    monkeypatch.setattr(fr, "_REQUIRED_WEIGHTS",
                        [Path("Z:/nonexistent/definitely_missing.pt")])
    assert fr.facerefine_ready() is False


def test_face_refine_enabled_default_off():
    """总门默认关（config 未配置时）。"""
    from src.config import get_config
    cfg = get_config()
    saved = (cfg.get("comfyui") or {}).get("h3_face_refine")
    if saved is None:  # 仅在未配置时验默认；已配置则验读取通路
        assert fr.face_refine_enabled() is False
    else:
        assert fr.face_refine_enabled() is bool(saved.get("enabled", False))
