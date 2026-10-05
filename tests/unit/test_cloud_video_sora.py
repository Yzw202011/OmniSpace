"""sora_video 协议单测（2026-10-03，MagicAI Grok 视频 Sora 任务式接入）。

覆盖：
- 时长档位映射 _sora_seconds（实测 4/8/10/15 四档；≤5 省略防误触未知档）；
- 轮询解析 _sora_parse（终态词=done 非 completed；video_url 直取 +
  video.url 相对路径回退；失败态归一）；
- 提交形态：无首帧=JSON（seconds 只在实锤档位出现）；有首帧=multipart
  input_reference（JPEG 编码）；
- generate_video 双协议闸：sora_video 无首帧放行（平台支持纯文生）、
  未知协议仍拒绝；
- probe_video_provider sora 分支：gin 素 404=路由不存在、401=Key 被拒、
  502=上游不可达（绝不 POST 探测防计费）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

import src.services.cloud_provider_service as cs
from src.services.cloud_provider_service import (
    VALID_PROTOCOLS,
    CloudEndpoint,
    create_provider,
    get_video_endpoint,
    set_binding,
)
from src.services.inference import cloud_video_client as cvc

_ROOT_DIR = Path(__file__).resolve().parents[2]


# ── 纯函数：时长映射与响应解析 ───────────────────────────────────

def test_sora_seconds_mapping() -> None:
    assert cvc._sora_seconds(0) is None
    assert cvc._sora_seconds(4.0) is None
    assert cvc._sora_seconds(5.0) is None
    assert cvc._sora_seconds(5.5) == "8"
    assert cvc._sora_seconds(8.0) == "8"
    assert cvc._sora_seconds(9.9) == "10"
    assert cvc._sora_seconds(10.0) == "10"
    assert cvc._sora_seconds(12.0) == "10"
    assert cvc._sora_seconds(13.0) == "15"
    assert cvc._sora_seconds(30.0) == "15"  # 未实测高档一律钳 15


def test_sora_parse_done_variants() -> None:
    # 实测形态：终态 done + video_url 直链
    st, url, msg = cvc._sora_parse({"status": "done",
                                    "video_url": "https://cdn/x.mp4"})
    assert st == "done" and url == "https://cdn/x.mp4" and msg == ""
    # video.url 相对路径形态（实测同返）
    st2, url2, _ = cvc._sora_parse({
        "status": "done", "video": {"url": "/v1/videos/t1/content"}})
    assert st2 == "done" and url2 == "/v1/videos/t1/content"
    # completed/succeeded 兼容归一
    st3, _, _ = cvc._sora_parse({"status": "completed", "video_url": "u"})
    assert st3 == "done"


def test_sora_parse_running_and_failed() -> None:
    assert cvc._sora_parse({"status": "pending"})[0] == "running"
    assert cvc._sora_parse({})[0] == "running"
    st, url, msg = cvc._sora_parse({"status": "failed", "error": "审核拒绝"})
    assert st == "failed" and url == "" and "审核拒绝" in msg
    st2, _, msg2 = cvc._sora_parse({"status": "error", "message": "boom"})
    assert st2 == "failed" and "boom" in msg2


def test_sora_parse_done_without_url_is_failure() -> None:
    st, url, msg = cvc._sora_parse({"status": "done"})
    assert st == "failed" and url == "" and "视频地址" in msg


# ── 提交形态与双协议闸 ───────────────────────────────────────────

def _ep(protocol: str = "sora_video") -> CloudEndpoint:
    return CloudEndpoint(base_url="https://vendor.example", api_key="sk-k",
                         model="grok-imagine-video", provider_id="prov_s1",
                         provider_name="MagicAI 视频", protocol=protocol)


def test_sora_submit_t2v_json_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """无首帧=JSON 提交；seconds 只带实锤档位（5s 省略）。"""
    captured: dict = {}

    class _Resp:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"id": "task_abc", "status": "pending"}

    def _fake_post(url, json=None, data=None, files=None, headers=None,
                   timeout=None):  # noqa: ANN001
        captured.update(url=url, json=json, data=data, files=files)
        return _Resp()

    import requests as _requests
    monkeypatch.setattr(_requests, "post", _fake_post)
    ep = _ep()
    assert cvc._sora_submit(ep, "一只猫", None, 5.0) == "task_abc"
    assert captured["url"] == "https://vendor.example/v1/videos"
    assert captured["json"]["model"] == "grok-imagine-video"
    assert "seconds" not in captured["json"]  # 5s 档省略
    assert not captured["files"]
    # 8s 档带 seconds
    cvc._sora_submit(ep, "一只猫", None, 8.0)
    assert captured["json"]["seconds"] == "8"


def test_sora_submit_i2v_multipart(monkeypatch: pytest.MonkeyPatch) -> None:
    """有首帧=multipart input_reference（JPEG 编码，无 dataurl 前缀）。"""
    captured: dict = {}

    class _Resp:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"id": "task_i2v"}

    def _fake_post(url, json=None, data=None, files=None, headers=None,
                   timeout=None):  # noqa: ANN001
        captured.update(json=json, data=data, files=files)
        return _Resp()

    import requests as _requests
    monkeypatch.setattr(_requests, "post", _fake_post)
    frame = Image.new("RGB", (32, 18), (200, 30, 30))
    cvc._sora_submit(_ep(), "少女转身", frame, 8.0)
    assert captured["json"] is None  # multipart 不走 JSON
    assert captured["data"]["seconds"] == "8"
    part = captured["files"]["input_reference"]
    assert part[0] == "first_frame.jpg" and part[2] == "image/jpeg"
    raw = part[1]
    assert not raw.startswith(b"data:") and raw[:2] == b"\xff\xd8"  # JPEG 魔数


def test_generate_video_protocol_gates() -> None:
    # sora_video 无首帧放行（平台支持纯文生）
    with pytest.raises(RuntimeError) as ei:
        cvc.generate_video(_ep(), "x", None, check_cancel=lambda: (_ for _ in ()).throw(
            RuntimeError("cancel-probe")))
    assert "cancel-probe" in str(ei.value)  # 走到轮询前的协作取消=闸已放行
    # 未知协议仍拒绝
    with pytest.raises(RuntimeError, match="不支持视频"):
        cvc.generate_video(_ep("openai_image"), "x", None)


def test_generate_video_sora_end_to_end(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """提交→轮询（pending→done）→下载 全链（requests 全桩）。"""
    calls = {"n": 0}

    class _Resp:
        def __init__(self, payload: dict, status: int = 200,
                     content: bytes = b"") -> None:
            self._p = payload
            self.status_code = status
            self.text = ""
            self.content = content

        def json(self):
            return self._p

    def _fake_post(url, json=None, data=None, files=None, headers=None,
                   timeout=None):  # noqa: ANN001
        return _Resp({"id": "task_e2e"})

    def _fake_get(url, headers=None, timeout=None):  # noqa: ANN001
        calls["n"] += 1
        if "/v1/videos/task_e2e" in url:
            if calls["n"] == 1:
                return _Resp({"status": "pending"})
            return _Resp({"status": "done",
                          "video_url": "https://cdn.example/v.mp4"})
        return _Resp({}, content=b"\x00\x00\x00\x18ftypmp42-mp4-bytes")

    import requests as _requests
    monkeypatch.setattr(_requests, "post", _fake_post)
    monkeypatch.setattr(_requests, "get", _fake_get)
    monkeypatch.setattr(cvc, "SORA_POLL_INTERVAL_S", 0.01)
    data = cvc.generate_video(_ep(), "一只猫", None, duration_seconds=10.0)
    assert data.startswith(b"\x00\x00\x00\x18ftypmp42")


# ── 协议闸与槽位路由（服务层）────────────────────────────────────

@pytest.fixture()
def mem_kv(monkeypatch: pytest.MonkeyPatch):
    store: dict = {}

    def _read(key, default=None):
        return store.get(key, default)

    def _write(key, value):
        store[key] = value
        return True

    monkeypatch.setattr(cs, "_read_kv", _read)
    monkeypatch.setattr(cs, "_write_kv", _write)
    cs._invalidate_route_cache()
    yield store
    cs._invalidate_route_cache()


def test_sora_protocol_in_valid_gate(mem_kv) -> None:
    item = create_provider("MagicAI 视频", "sora_video",
                           "https://magicai.click", "sk-k",
                           ["grok-imagine-video"])
    assert item["protocol"] == "sora_video"
    assert "sora_video" in VALID_PROTOCOLS


def test_sora_video_slot_routing(mem_kv) -> None:
    vid = create_provider("MagicAI 视频", "sora_video",
                          "https://magicai.click", "sk-v",
                          ["grok-imagine-video"])
    set_binding("manga.video", vid["id"], "grok-imagine-video")
    ep = get_video_endpoint()
    assert ep is not None and ep.protocol == "sora_video"
    assert ep.model == "grok-imagine-video"


# ── probe 甄别 ───────────────────────────────────────────────────

def test_probe_sora_discrimination(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests as _requests
    ep = _ep()

    def _fake(status: int, text: str):
        return lambda *a, **k: type("R", (), {
            "status_code": status, "text": text})()

    # gin 素 404「page not found」=Sora 路由不存在 → 不可达
    monkeypatch.setattr(_requests, "get", _fake(
        404, "404 page not found"))
    ok, detail = cvc.probe_video_provider(ep)
    assert not ok and "端点不存在" in detail
    # 401=Key 被拒
    monkeypatch.setattr(_requests, "get", _fake(401, "denied"))
    ok2, detail2 = cvc.probe_video_provider(ep)
    assert not ok2 and "Key" in detail2
    # 502=上游暂不可达（2026-10-03 实测形态）
    monkeypatch.setattr(_requests, "get", _fake(
        502, '{"error":{"message":"Upstream service temporarily '
             'unavailable"}}'))
    ok3, detail3 = cvc.probe_video_provider(ep)
    assert not ok3 and "上游" in detail3
    # 结构化 404（任务不存在语义）=可达且鉴权通过
    monkeypatch.setattr(_requests, "get", _fake(
        404, '{"error":{"message":"task not found","type":"not_found_error"}}'))
    ok4, detail4 = cvc.probe_video_provider(ep)
    assert ok4 and detail4 == ""
# 本项目仅供学习使用，商业授权请+Q 3559331368
