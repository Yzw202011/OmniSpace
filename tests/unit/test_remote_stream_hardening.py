"""云端流式加固单测（2026-09-21，空白回复/无声挂死/误导文案三案根修）。

覆盖（方案「验证方法」承诺的七分支）：
- 正常 content 流照常透传 + finish_reason=length 记录到 last_finish_reason；
- reasoning_content-only 流（DeepSeek 思考型实测形态）→ 思考内容兜底为
  正文，不再空白；
- 空流（无 content 无 reasoning）→ 显式 RuntimeError，不再静默记成功；
- HTTP 400/401 错误文案分流：400 归因模型名/参数（不再误判 API Key），
  401 归因 Key；
- 流中读超时 → 带出路的超时文案。

requests.post 全部 monkeypatch（离线毫秒级，不起真服务）。
"""
from __future__ import annotations

import json

import pytest
import requests

import src.services.inference.backends.remote_backend as rb
from src.services.inference.backends.remote_backend import RemoteDialogBackend


class _FakeResp:
    """最小 SSE 响应桩：iter_lines 按预置行产出，status/text 可配。"""

    def __init__(self, status_code: int = 200, lines: list | None = None,
                 text: str = "", iter_exc: Exception | None = None):
        self.status_code = status_code
        self._lines = [ln.encode("utf-8") if isinstance(ln, str) else ln
                       for ln in (lines or [])]
        self.text = text
        self._iter_exc = iter_exc
        self.closed = False

    def iter_lines(self):
        yield from self._lines
        if self._iter_exc is not None:
            raise self._iter_exc

    def close(self) -> None:
        self.closed = True


def _sse_lines(*deltas: dict, finish: str | None = None) -> list[str]:
    """组装 OpenAI 兼容 SSE 行（delta 序列 + 可选 finish_reason + DONE）。"""
    lines = []
    for d in deltas:
        obj = {"choices": [{"delta": d, "finish_reason": None}]}
        lines.append("data: " + json.dumps(obj, ensure_ascii=False))
    if finish:
        obj = {"choices": [{"delta": {}, "finish_reason": finish}]}
        lines.append("data: " + json.dumps(obj, ensure_ascii=False))
    lines.append("data: [DONE]")
    return lines


def _ready_backend() -> RemoteDialogBackend:
    """就绪态后端（跳过 load 健康轮询，直测 chat_stream 分支）。"""
    b = RemoteDialogBackend()
    b._ready = True
    b._base_url = "http://mock-cloud"
    b._api_key = ""
    b._provider_name = "测试服务商"
    b.model_id = "mock-model"
    return b


@pytest.fixture()
def no_event_log(monkeypatch: pytest.MonkeyPatch):
    """事件记录打桩（隔离 cloud_provider_service 真实落库）。"""
    calls: list[tuple] = []
    monkeypatch.setattr(rb, "_record_cloud_call",
                        lambda kind, prov, model, ok, ms, detail="":
                        calls.append((kind, ok, detail)))
    return calls


def _patch_post(monkeypatch: pytest.MonkeyPatch, resp: _FakeResp) -> None:
    monkeypatch.setattr(requests, "post", lambda *a, **kw: resp)


def test_normal_content_stream_and_finish_reason(
        monkeypatch, no_event_log):
    resp = _FakeResp(lines=_sse_lines({"content": "你"}, {"content": "好"},
                                      finish="stop"))
    _patch_post(monkeypatch, resp)
    b = _ready_backend()
    got = list(b.chat_stream([{"role": "user", "content": "hi"}]))
    assert got == ["你", "好"]
    assert b.last_finish_reason == "stop"
    assert no_event_log and no_event_log[-1][1] is True


def test_reasoning_only_stream_falls_back_to_content(
        monkeypatch, no_event_log):
    """DeepSeek 思考型实测形态：全走 reasoning_content → 兜底为正文。"""
    resp = _FakeResp(lines=_sse_lines(
        {"reasoning_content": "思考A"}, {"reasoning_content": "思考B"},
        finish="stop"))
    _patch_post(monkeypatch, resp)
    b = _ready_backend()
    got = list(b.chat_stream([{"role": "user", "content": "hi"}]))
    assert got == ["思考A思考B"]
    assert no_event_log[-1][1] is True
    assert "回退" in no_event_log[-1][2]


def test_empty_stream_raises_instead_of_silent_blank(
        monkeypatch, no_event_log):
    resp = _FakeResp(lines=_sse_lines(finish="stop"))
    _patch_post(monkeypatch, resp)
    b = _ready_backend()
    with pytest.raises(RuntimeError, match="未返回正文"):
        list(b.chat_stream([{"role": "user", "content": "hi"}]))
    assert no_event_log[-1][1] is False  # 不再静默记成功


def test_finish_reason_length_recorded(monkeypatch, no_event_log):
    resp = _FakeResp(lines=_sse_lines({"content": "写到一半"},
                                      finish="length"))
    _patch_post(monkeypatch, resp)
    b = _ready_backend()
    list(b.chat_stream([{"role": "user", "content": "hi"}]))
    assert b.last_finish_reason == "length"


def test_http_400_hint_points_to_model_name_not_api_key(
        monkeypatch, no_event_log):
    """2026-09-21 实测案例：音频模型绑成文本对话 → 400 被误导成 Key 问题。"""
    resp = _FakeResp(status_code=400,
                     text='{"error":{"message":"format is empty",'
                          '"code":"UNSUPPORTED_FORMAT"}}')
    _patch_post(monkeypatch, resp)
    b = _ready_backend()
    with pytest.raises(RuntimeError) as ei:
        list(b.chat_stream([{"role": "user", "content": "hi"}]))
    msg = str(ei.value)
    assert "HTTP 400" in msg and "format is empty" in msg
    assert "模型名" in msg
    assert "API Key" not in msg


def test_http_401_hint_points_to_api_key(monkeypatch, no_event_log):
    resp = _FakeResp(status_code=401,
                     text='{"error":"Invalid API key"}')
    _patch_post(monkeypatch, resp)
    b = _ready_backend()
    with pytest.raises(RuntimeError) as ei:
        list(b.chat_stream([{"role": "user", "content": "hi"}]))
    assert "API Key" in str(ei.value)


def test_read_timeout_mid_stream_yields_friendly_error(
        monkeypatch, no_event_log):
    resp = _FakeResp(lines=["data: " + json.dumps(
        {"choices": [{"delta": {"content": "前半"}, "finish_reason": None}]})],
        iter_exc=requests.exceptions.ReadTimeout("Read timed out"))
    _patch_post(monkeypatch, resp)
    b = _ready_backend()
    gen = b.chat_stream([{"role": "user", "content": "hi"}])
    assert next(gen) == "前半"
    with pytest.raises(RuntimeError, match="超时"):
        list(gen)
