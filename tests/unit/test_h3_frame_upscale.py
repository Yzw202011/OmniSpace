"""H3 逐帧放大服务单测（2026-10-01 清晰度线接链）。

零 GPU：工作流构造/参数化/预检降级/分辨率自验哨兵/开关默认关/
批量入口零阻断。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.services.inference import h3_frame_upscale as fu


def test_workflow_construction(tmp_path, monkeypatch):
    """工作流六节点构造与参数化（源片/音轨/目标分辨率/前缀）。"""
    monkeypatch.setattr(fu, "frame_upscale_ready", lambda base=None: True)
    captured: dict = {}

    def fake_api(base, method, path, body=None, timeout=10.0):
        if method == "POST" and path == "/prompt":
            captured["wf"] = body["prompt"]
            return {"prompt_id": "t1"}
        if method == "GET" and path.startswith("/history/"):
            d = tmp_path / "frame_upscale"
            d.mkdir(exist_ok=True)
            (d / "fake.mp4").write_bytes(b"mp4")
            monkeypatch.setattr(fu, "_check_output_or_raise",
                                lambda p, w=0, h=0: None)
            return {"t1": {"status": {"completed": False},
                           "outputs": {"6": {"gifs": [{
                               "filename": "fake.mp4",
                               "subfolder": "frame_upscale"}]}}}}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(fu, "_api", fake_api)
    monkeypatch.setattr(fu.time, "sleep", lambda s: None)

    src = tmp_path / "seg.mp4"
    src.write_bytes(b"src")
    out = fu.upscale_video(src, comfy_input=tmp_path, comfy_output=tmp_path)
    assert out.name == "seg_fu.mp4"
    wf = captured["wf"]
    assert wf["2"]["inputs"]["model_name"] == "4x-UltraSharp.pth"
    # 双路调和：3a=模型锐利版 3b=lanczos 柔和版 4=ImageBlend
    assert wf["3a"]["inputs"]["width"] == 1344  # 默认交付档 1K（2026-10-12 用户令 2K→1K）
    assert wf["3b"]["inputs"]["upscale_method"] == "lanczos"
    assert wf["4"]["class_type"] == "ImageBlend"
    assert wf["4"]["inputs"]["image1"] == ["3a", 0]
    assert wf["4"]["inputs"]["image2"] == ["3b", 0]
    # 定档 0.55 柔和（2026-10-02 盲调定案）→ blend_factor=0.45
    assert wf["4"]["inputs"]["blend_factor"] == 0.45
    assert wf["6"]["inputs"]["filename_prefix"].startswith("frame_upscale/")
    # 音轨拼回：VideoCombine.audio ← LoadAudioUpload
    assert wf["6"]["inputs"]["audio"] == ["5", 0]


def test_upscale_degrades_on_workflow_error(tmp_path, monkeypatch):
    """校验失败 → 原片返回（零阻断）。"""
    monkeypatch.setattr(fu, "frame_upscale_ready", lambda base=None: True)

    def bad_api(base, method, path, body=None, timeout=10.0):
        if method == "POST":
            return {"error": {"message": "bad"}}
        raise AssertionError

    monkeypatch.setattr(fu, "_api", bad_api)
    src = tmp_path / "seg.mp4"
    src.write_bytes(b"x")
    assert fu.upscale_video(src, comfy_input=tmp_path) is src


def test_resolution_sentinel(tmp_path):
    """分辨率哨兵：非交付档产物=失败。"""
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="分辨率"):
        fu._check_output_or_raise(bad, 2688, 1536)  # 空文件 → ffprobe 空 → 不等目标档


def test_ready_weight_missing(monkeypatch):
    """权重缺失 → False 且缓存。"""
    monkeypatch.setattr(fu, "_READY_CACHE", None)
    monkeypatch.setattr(fu, "_ESRGAN_WEIGHT",
                        Path("Z:/nonexistent/missing.pth"))
    assert fu.frame_upscale_ready() is False


def test_frame_upscale_enabled_default_off():
    """总门默认关（config 未配置时）。"""
    from src.config import get_config
    cfg = get_config()
    saved = (cfg.get("comfyui") or {}).get("h3_frame_upscale")
    if saved is None:
        assert fu.frame_upscale_enabled() is False
    else:
        assert fu.frame_upscale_enabled() is bool(saved.get("enabled", False))


def test_upscale_segments_zero_blocking(tmp_path, monkeypatch):
    """批量入口：开关关/失败路径均原样返回（零阻断铁律）。"""
    segs = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    for s in segs:
        s.write_bytes(b"x")
    monkeypatch.setattr(fu, "frame_upscale_enabled", lambda: False)
    assert fu.upscale_segments(segs) is segs
    monkeypatch.setattr(fu, "frame_upscale_enabled", lambda: True)
    monkeypatch.setattr(fu, "frame_upscale_ready", lambda base=None: False)
    # ready=False 走逐个 upscale_video 原片回退：值相等（未处理）即可
    assert fu.upscale_segments(segs) == segs


def test_2k_failure_falls_back_to_1k(tmp_path, monkeypatch):
    """2K 档失败 → 自动回退 1K 档重试（2026-10-07 用户令）。"""
    monkeypatch.setattr(fu, "frame_upscale_ready", lambda base=None: True)
    posts: list[dict] = []

    def fake_api(base, method, path, body=None, timeout=10.0):
        if method == "POST" and path == "/prompt":
            posts.append(body["prompt"])
            if len(posts) == 1:
                return {"error": {"message": "2k oom"}}  # 首档（2K）失败
            return {"prompt_id": "t2"}
        if method == "GET" and path.startswith("/history/"):
            d = tmp_path / "frame_upscale"
            d.mkdir(exist_ok=True)
            (d / "fake1k.mp4").write_bytes(b"mp4")
            monkeypatch.setattr(fu, "_check_output_or_raise",
                                lambda p, w=0, h=0: None)
            return {"t2": {"status": {"completed": False},
                           "outputs": {"6": {"gifs": [{
                               "filename": "fake1k.mp4",
                               "subfolder": "frame_upscale"}]}}}}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(fu, "_api", fake_api)
    monkeypatch.setattr(fu.time, "sleep", lambda s: None)
    src = tmp_path / "seg.mp4"
    src.write_bytes(b"src")
    # 显式 2K 目标：本测试专验「2K 失败→1K 回退」链（默认档已改 1K，
    # 2026-10-12 用户令——缺省调用只发一档）
    out = fu.upscale_video(src, comfy_input=tmp_path, comfy_output=tmp_path,
                           target=fu._DELIVERY_2K)
    assert out.name == "seg_fu.mp4"
    assert len(posts) == 2
    assert posts[0]["3a"]["inputs"]["width"] == 2688  # 首档 2K
    assert posts[1]["3a"]["inputs"]["width"] == 1344  # 回退 1K


def test_chunked_streaming_2k(tmp_path, monkeypatch):
    """流式分块（Video2X 式）：探测帧数→分块循环→合段；块 wf 无声+保真 fps。"""
    monkeypatch.setattr(fu, "frame_upscale_ready", lambda base=None: True)
    monkeypatch.setattr(fu, "_probe_video_meta", lambda s: (240, 24))
    posts: list[dict] = []
    muxed: list = []

    def fake_api(base, method, path, body=None, timeout=10.0):
        if method == "POST" and path == "/prompt":
            posts.append(body["prompt"])
            idx = len(posts)
            d = tmp_path / "frame_upscale"
            d.mkdir(exist_ok=True)
            (d / f"chunk{idx}.mp4").write_bytes(b"mp4")
            monkeypatch.setattr(fu, "_check_output_or_raise",
                                lambda p, w=0, h=0: None)
            return {"prompt_id": f"t{idx}"}
        if method == "GET" and path.startswith("/history/"):
            idx = int(path.rsplit("t", 1)[-1])
            return {f"t{idx}": {"status": {"completed": False},
                                "outputs": {"6": {"gifs": [{
                                    "filename": f"chunk{idx}.mp4",
                                    "subfolder": "frame_upscale"}]}}}}
        raise AssertionError(f"unexpected {method} {path}")

    def fake_mux(chunks, src, out, fps, progress_cb=None,
                 target_w=None, target_h=None):
        muxed.append((len(chunks), fps))
        out.write_bytes(b"final")

    monkeypatch.setattr(fu, "_concat_and_mux", fake_mux)
    monkeypatch.setattr(fu, "_api", fake_api)
    monkeypatch.setattr(fu.time, "sleep", lambda s: None)
    src = tmp_path / "seg.mp4"
    src.write_bytes(b"src")
    out = fu.upscale_video(src, comfy_input=tmp_path, comfy_output=tmp_path)
    # 240 帧 / 48 帧块 = 5 块
    assert len(posts) == 5
    assert [p["1"]["inputs"]["skip_first_frames"] for p in posts] == \
        [0, 48, 96, 144, 192]
    assert all(p["1"]["inputs"]["frame_load_cap"] == 48 for p in posts)
    # 块 wf 无声（无 5/音轨节点），帧率保真 24
    assert all("5" not in p for p in posts)
    assert all(p["6"]["inputs"]["frame_rate"] == 24 for p in posts)
    # 合段：5 块 + 源音轨拼回
    assert muxed == [(5, 24)]
    assert out.name == "seg_fu.mp4"
