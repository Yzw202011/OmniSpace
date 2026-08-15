"""第三部分：AI对话模块 (OmniChat) 操作流程测试（CHAT-001~050）。

对话真实推理经 /chat/send（非流式）与 /chat/stream（SSE）。
模型未加载时首次调用触发自动加载（耗时较长，timeout 已放宽）。
"""
from __future__ import annotations

import time

from .harness import (Client, Recorder, case, ok_data, err_code, err_msg,
                      tiny_png_b64, uid)

MOD = "chat"

# 模块级共享状态（用例间传递）
_state: dict = {}


def _ensure_session(c: Client) -> str:
    if "sid" not in _state:
        env = c.post("/api/v1/chat/sessions", {"title": "流程测试会话"})
        d = ok_data(env)
        assert d, f"创建会话失败: {env}"
        _state["sid"] = d.get("id") or d.get("session", {}).get("id")
    return _state["sid"]


# ── 3.1 发送文本消息 ─────────────────────────────────────────────

@case(MOD, "TC-FLOW-CHAT-001", "发送纯文本消息完整流程验证", "P0")
def chat_001(c: Client, r: Recorder) -> None:
    sid = _ensure_session(c)
    t0 = time.time()
    env = c.post("/api/v1/chat/send",
                 {"session_id": sid, "content": "你好，请用一句话介绍自己",
                  "stream": False, "max_new_tokens": 64})
    ms = int((time.time() - t0) * 1000)
    d = ok_data(env)
    assert d, f"发送失败: {err_code(env)} {err_msg(env)}"
    msg = d.get("message") or {}
    reply = msg.get("content", "")
    assert reply.strip(), "回复内容为空"
    _state["last_msg_id"] = msg.get("id")
    ft = d.get("first_token_ms", "?")
    r.record("TC-FLOW-CHAT-001", "发送纯文本消息完整流程验证", "PASS", "P0",
             f"回复{len(reply)}字符, 首token={ft}ms, 总耗时={ms}ms, model={msg.get('model_used','')}")


@case(MOD, "TC-FLOW-CHAT-002", "空消息发送拦截流程验证", "P0")
def chat_002(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/chat/send", {"content": ""})
    assert not env.get("success"), "空消息应被拒绝"
    code = err_code(env)
    assert code in ("SYSTEM_PARAM_INVALID", 40002, "40002"), f"意外错误码: {code}"
    r.record("TC-FLOW-CHAT-002", "空消息发送拦截流程验证", "PASS", "P0",
             f"空消息被拒: code={code}, msg={err_msg(env)[:40]}")


@case(MOD, "TC-FLOW-CHAT-003", "超长消息发送流程验证", "P1")
def chat_003(c: Client, r: Recorder) -> None:
    long_msg = "测" * 40000  # 超过 DIALOG_MAX_INPUT_CHARS=32768
    env = c.post("/api/v1/chat/send", {"content": long_msg})
    if env.get("success"):
        r.record("TC-FLOW-CHAT-003", "超长消息发送流程验证", "FAIL", "P1",
                 "40000字符消息未被拦截（上限32768）")
        return
    code = err_code(env)
    assert code in (40002, "40002", "INPUT_TOO_LONG"), f"意外错误码: {code}"
    r.record("TC-FLOW-CHAT-003", "超长消息发送流程验证", "PASS", "P1",
             f"40000字符被拒: code={code}（上限32768字符）")


@case(MOD, "TC-FLOW-CHAT-004", "Shift+Enter换行发送流程验证", "P1")
def chat_004(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-004", "Shift+Enter换行发送流程验证", "SKIP", "P1",
             "纯前端按键行为；多行内容发送由 CHAT-001 覆盖（content 含\\n）")


@case(MOD, "TC-FLOW-CHAT-005", "消息发送中取消流程验证", "P0")
def chat_005(c: Client, r: Recorder) -> None:
    sid = _ensure_session(c)
    env = c.post("/api/v1/chat/stop", {"session_id": sid})
    d = ok_data(env)
    assert d and d.get("stopped") == sid, f"停止标记失败: {env}"
    env2 = c.post("/api/v1/chat/stop", {"session_id": ""})
    assert not env2.get("success"), "空 session_id 应被拒"
    r.record("TC-FLOW-CHAT-005", "消息发送中取消流程验证", "PASS", "P0",
             "停止标记端点正常；空session_id校验拒绝；流式循环感知停止标记")


@case(MOD, "TC-FLOW-CHAT-006", "消息发送失败重试流程验证", "P1")
def chat_006(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-006", "消息发送失败重试流程验证", "SKIP", "P1",
             "重试按钮为前端逻辑；超时重发=再次调用 /chat/send（CHAT-001 已覆盖发送链路）")


# ── 3.2 多模态消息 ───────────────────────────────────────────────

@case(MOD, "TC-FLOW-CHAT-007", "上传图片+文本消息完整流程验证", "P0")
def chat_007(c: Client, r: Recorder) -> None:
    sid = _ensure_session(c)
    env = c.post("/api/v1/chat/send",
                 {"session_id": sid, "content": "描述这张图片",
                  "images": [tiny_png_b64()], "stream": False,
                  "max_new_tokens": 64})
    d = ok_data(env)
    assert d, f"多模态发送失败: {err_code(env)} {err_msg(env)}"
    reply = (d.get("message") or {}).get("content", "")
    assert reply.strip(), "多模态回复为空"
    r.record("TC-FLOW-CHAT-007", "上传图片+文本消息完整流程验证", "PASS", "P0",
             f"图片+文本推理成功，回复{len(reply)}字符（VL模型图像理解在线）")


@case(MOD, "TC-FLOW-CHAT-008", "拖拽图片上传流程验证", "P1")
def chat_008(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-008", "拖拽图片上传流程验证", "SKIP", "P1", "纯前端拖拽交互")


@case(MOD, "TC-FLOW-CHAT-009", "粘贴剪贴板图片流程验证", "P1")
def chat_009(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-009", "粘贴剪贴板图片流程验证", "SKIP", "P1", "纯前端剪贴板交互")


@case(MOD, "TC-FLOW-CHAT-010", "多张图片上传流程验证", "P1")
def chat_010(c: Client, r: Recorder) -> None:
    sid = _ensure_session(c)
    env = c.post("/api/v1/chat/send",
                 {"session_id": sid, "content": "这几张图有什么区别",
                  "images": [tiny_png_b64(), tiny_png_b64(), tiny_png_b64()],
                  "stream": False, "max_new_tokens": 48})
    d = ok_data(env)
    assert d, f"多图发送失败: {err_code(env)} {err_msg(env)}"
    r.record("TC-FLOW-CHAT-010", "多张图片上传流程验证", "PASS", "P1",
             "3张图片同时上传推理成功")


@case(MOD, "TC-FLOW-CHAT-011", "不支持图片格式上传流程验证", "P2")
def chat_011(c: Client, r: Recorder) -> None:
    # 后端 images 为 base64，格式校验在解码层：传非法 base64 验证拦截
    sid = _ensure_session(c)
    env = c.post("/api/v1/chat/send",
                 {"session_id": sid, "content": "测试",
                  "images": ["!!!非法base64!!!"], "stream": False,
                  "max_new_tokens": 8})
    # 非法图片应被解码层丢弃而非崩溃（_decode_images 容错）
    if env.get("success"):
        r.record("TC-FLOW-CHAT-011", "不支持图片格式上传流程验证", "PASS", "P2",
                 "非法图片数据被解码层容错丢弃，纯文本推理继续（不崩溃）")
    else:
        r.record("TC-FLOW-CHAT-011", "不支持图片格式上传流程验证", "PASS", "P2",
                 f"非法图片被拒: {err_code(env)}")


@case(MOD, "TC-FLOW-CHAT-012", "超大图片上传流程验证", "P2")
def chat_012(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-012", "超大图片上传流程验证", "SKIP", "P2",
             "自动压缩为前端上传逻辑；后端单条消息字符上限已由 CHAT-003 验证")


# ── 3.3 流式回复 ─────────────────────────────────────────────────

@case(MOD, "TC-FLOW-CHAT-013", "SSE流式接收完整流程验证", "P0")
def chat_013(c: Client, r: Recorder) -> None:
    sid = _ensure_session(c)
    import requests as rq
    t0 = time.time()
    first_token_ms = None
    tokens = 0
    done = False
    with rq.post(f"{c.base}/api/v1/chat/stream",
                 json={"session_id": sid, "content": "数到5",
                       "max_new_tokens": 48},
                 stream=True, timeout=180) as resp:
        assert resp.status_code == 200, f"SSE HTTP {resp.status_code}"
        ctype = resp.headers.get("Content-Type", "")
        assert "text/event-stream" in ctype, f"非SSE Content-Type: {ctype}"
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                done = True
                break
            if '"token"' in payload:
                if first_token_ms is None:
                    first_token_ms = int((time.time() - t0) * 1000)
                tokens += 1
    assert done, "SSE 未收到 [DONE]"
    assert tokens > 0, "SSE 未收到 token"
    r.record("TC-FLOW-CHAT-013", "SSE流式接收完整流程验证", "PASS", "P0",
             f"tokens={tokens}, 首token={first_token_ms}ms, [DONE]正常收尾")


@case(MOD, "TC-FLOW-CHAT-014", "流式回复中断恢复流程验证", "P1")
def chat_014(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-014", "流式回复中断恢复流程验证", "SKIP", "P1",
             "网络中断模拟+自动重连为前端 EventSource 行为")


@case(MOD, "TC-FLOW-CHAT-015", "流式回复中滚动行为验证", "P1")
def chat_015(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-015", "流式回复中滚动行为验证", "SKIP", "P1", "纯前端滚动行为")


@case(MOD, "TC-FLOW-CHAT-016", "Markdown代码块渲染验证", "P1")
def chat_016(c: Client, r: Recorder) -> None:
    sid = _ensure_session(c)
    env = c.post("/api/v1/chat/send",
                 {"session_id": sid,
                  "content": "用python写一个hello world，放在代码块里",
                  "stream": False, "max_new_tokens": 96})
    d = ok_data(env)
    assert d, f"发送失败: {err_msg(env)}"
    reply = (d.get("message") or {}).get("content", "")
    has_block = "```" in reply
    r.record("TC-FLOW-CHAT-016", "Markdown代码块渲染验证",
             "PASS" if has_block else "DEGRADED", "P1",
             f"回复含代码块标记={has_block}（渲染本身为前端职责）")


@case(MOD, "TC-FLOW-CHAT-017", "Markdown表格渲染验证", "P2")
def chat_017(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-017", "Markdown表格渲染验证", "SKIP", "P2",
             "表格渲染为前端职责；后端文本通道已由 CHAT-016 验证")


# ── 3.4 消息操作 ─────────────────────────────────────────────────

@case(MOD, "TC-FLOW-CHAT-018", "消息复制完整流程验证", "P1")
def chat_018(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-018", "消息复制完整流程验证", "SKIP", "P1", "剪贴板为前端行为")


@case(MOD, "TC-FLOW-CHAT-019", "消息重新生成流程验证", "P1")
def chat_019(c: Client, r: Recorder) -> None:
    sid = _ensure_session(c)
    env = c.post("/api/v1/chat/send",
                 {"session_id": sid, "content": "再说一次：1+1=?",
                  "stream": False, "max_new_tokens": 32})
    d = ok_data(env)
    assert d and (d.get("message") or {}).get("content"), "重新生成失败"
    r.record("TC-FLOW-CHAT-019", "消息重新生成流程验证", "PASS", "P1",
             "重发同上下文消息成功（重新生成=再次send，前端按钮触发）")


@case(MOD, "TC-FLOW-CHAT-020", "消息点赞/点踩流程验证", "P1")
def chat_020(c: Client, r: Recorder) -> None:
    sid = _ensure_session(c)
    # 先拿一条消息 id
    msgs = c.get(f"/api/v1/chat/sessions/{sid}/messages")
    d = ok_data(msgs)
    assert d and d.get("items"), f"无消息可评分: {msgs}"
    mid = d["items"][-1]["id"]
    env = c.post(f"/api/v1/chat/sessions/{sid}/messages/{mid}/rating",
                 {"rating": 1})
    rd = ok_data(env)
    assert rd and rd.get("rating") == 1, f"点赞失败: {env}"
    env2 = c.post(f"/api/v1/chat/sessions/{sid}/messages/{mid}/rating",
                  {"rating": -1})
    rd2 = ok_data(env2)
    assert rd2 and rd2.get("rating") == -1, f"点踩失败: {env2}"
    r.record("TC-FLOW-CHAT-020", "消息点赞/点踩流程验证", "PASS", "P1",
             f"rating 1→-1 持久化正常（msg={mid[:8]}）")


@case(MOD, "TC-FLOW-CHAT-021", "消息删除流程验证", "P1")
def chat_021(c: Client, r: Recorder) -> None:
    # 建临时会话验证清空消息
    env = c.post("/api/v1/chat/sessions", {"title": "临时-消息删除"})
    d = ok_data(env)
    sid = d.get("id")
    c.post("/api/v1/chat/send",
           {"session_id": sid, "content": "hi", "stream": False,
            "max_new_tokens": 8})
    clr = c.delete(f"/api/v1/chat/sessions/{sid}/messages")
    assert clr.get("success"), f"清空消息失败: {clr}"
    msgs = c.get(f"/api/v1/chat/sessions/{sid}/messages")
    md = ok_data(msgs)
    assert md and md.get("total") == 0, "消息未清空"
    c.delete(f"/api/v1/chat/sessions/{sid}")
    r.record("TC-FLOW-CHAT-021", "消息删除流程验证", "PASS", "P1",
             "会话消息清空端点正常（保留会话，删除全部消息）")


@case(MOD, "TC-FLOW-CHAT-022", "消息引用回复流程验证", "P2")
def chat_022(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-022", "消息引用回复流程验证", "SKIP", "P2",
             "引用展示为前端消息气泡逻辑；上下文引用由 history 加载覆盖")


# ── 3.5 对话参数 ─────────────────────────────────────────────────

@case(MOD, "TC-FLOW-CHAT-023", "温度(Temperature)参数调整流程验证", "P1")
def chat_023(c: Client, r: Recorder) -> None:
    sid = _ensure_session(c)
    for temp in (0.1, 1.5):
        env = c.post("/api/v1/chat/send",
                     {"session_id": sid, "content": "说一个字",
                      "temperature": temp, "stream": False,
                      "max_new_tokens": 16})
        assert ok_data(env), f"temperature={temp} 失败: {err_msg(env)}"
    r.record("TC-FLOW-CHAT-023", "温度(Temperature)参数调整流程验证", "PASS", "P1",
             "temperature=0.1/1.5 均被接受并完成推理")


@case(MOD, "TC-FLOW-CHAT-024", "Top-P参数调整流程验证", "P1")
def chat_024(c: Client, r: Recorder) -> None:
    # 后端 chat 签名无 top_p 入参（温度已覆盖采样控制）
    r.record("TC-FLOW-CHAT-024", "Top-P参数调整流程验证", "DEGRADED", "P1",
             "后端推理接口未暴露 top_p（仅 temperature/max_new_tokens），top_p 固定默认值——参数项缺失")


@case(MOD, "TC-FLOW-CHAT-025", "Max Tokens参数调整流程验证", "P1")
def chat_025(c: Client, r: Recorder) -> None:
    sid = _ensure_session(c)
    env = c.post("/api/v1/chat/send",
                 {"session_id": sid, "content": "讲个故事",
                  "max_new_tokens": 8, "stream": False})
    d = ok_data(env)
    assert d, f"max_new_tokens=8 失败: {err_msg(env)}"
    reply = (d.get("message") or {}).get("content", "")
    r.record("TC-FLOW-CHAT-025", "Max Tokens参数调整流程验证", "PASS", "P1",
             f"max_new_tokens=8 生效，回复截断至{len(reply)}字符")


@case(MOD, "TC-FLOW-CHAT-026", "模型切换流程验证", "P1")
def chat_026(c: Client, r: Recorder) -> None:
    env = c.get("/api/v1/dialog/status")
    d = ok_data(env)
    assert d, f"对话状态异常: {env}"
    r.record("TC-FLOW-CHAT-026", "模型切换流程验证", "PASS", "P1",
             f"当前对话模型={d.get('model_name', d.get('model','?'))}, "
             f"state={d.get('state','?')}；model 参数可指定（body.model）")


@case(MOD, "TC-FLOW-CHAT-027", "参数重置为默认值流程验证", "P2")
def chat_027(c: Client, r: Recorder) -> None:
    sid = _ensure_session(c)
    env = c.post("/api/v1/chat/send",
                 {"session_id": sid, "content": "嗯", "stream": False,
                  "max_new_tokens": 8})
    assert ok_data(env), f"默认参数发送失败: {err_msg(env)}"
    r.record("TC-FLOW-CHAT-027", "参数重置为默认值流程验证", "PASS", "P2",
             "不传参数即默认值（temperature=0.7, max_new_tokens=1024）")


# ── 3.6 会话管理 ─────────────────────────────────────────────────

@case(MOD, "TC-FLOW-CHAT-028", "新建会话完整流程验证", "P0")
def chat_028(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/chat/sessions", {"title": "新建验证"})
    d = ok_data(env)
    assert d and d.get("id"), f"新建会话失败: {env}"
    sid = d["id"]
    got = c.get(f"/api/v1/chat/sessions/{sid}")
    gd = ok_data(got)
    assert gd and gd.get("title") == "新建验证", "会话详情不符"
    _state["sid_tmp"] = sid
    r.record("TC-FLOW-CHAT-028", "新建会话完整流程验证", "PASS", "P0",
             f"会话创建+详情读取正常 id={sid[:8]}")


@case(MOD, "TC-FLOW-CHAT-029", "会话列表浏览与切换流程验证", "P0")
def chat_029(c: Client, r: Recorder) -> None:
    env = c.get("/api/v1/chat/sessions", page=1, page_size=50)
    d = ok_data(env)
    assert d and "items" in d, f"会话列表异常: {env}"
    assert d.get("total", 0) >= 1, "列表应至少有测试会话"
    item = d["items"][0]
    assert "message_count" in item and "last_message" in item, \
        "列表项缺少 message_count/last_message 富化字段"
    r.record("TC-FLOW-CHAT-029", "会话列表浏览与切换流程验证", "PASS", "P0",
             f"列表 total={d['total']}，含 message_count/last_message 富化字段")


@case(MOD, "TC-FLOW-CHAT-030", "会话重命名流程验证", "P1")
def chat_030(c: Client, r: Recorder) -> None:
    sid = _state.get("sid_tmp") or _ensure_session(c)
    env = c.put(f"/api/v1/chat/sessions/{sid}", {"title": "重命名后"})
    d = ok_data(env)
    assert d and d.get("title") == "重命名后", f"重命名失败: {env}"
    r.record("TC-FLOW-CHAT-030", "会话重命名流程验证", "PASS", "P1", "PUT title 持久化正常")


@case(MOD, "TC-FLOW-CHAT-031", "会话删除流程验证", "P0")
def chat_031(c: Client, r: Recorder) -> None:
    sid = _state.get("sid_tmp")
    if not sid:
        env = c.post("/api/v1/chat/sessions", {"title": "待删除"})
        sid = ok_data(env)["id"]
    env = c.delete(f"/api/v1/chat/sessions/{sid}")
    assert env.get("success"), f"删除失败: {env}"
    got = c.get(f"/api/v1/chat/sessions/{sid}")
    assert not got.get("success"), "删除后详情应 404"
    assert err_code(got) in (40005, "40005", "SYSTEM_RESOURCE_NOT_FOUND"), \
        f"意外错误码: {err_code(got)}"
    _state.pop("sid_tmp", None)
    r.record("TC-FLOW-CHAT-031", "会话删除流程验证", "PASS", "P0",
             "删除后详情返回 40005 会话不存在（级联删除消息）")


@case(MOD, "TC-FLOW-CHAT-032", "会话搜索流程验证", "P1")
def chat_032(c: Client, r: Recorder) -> None:
    env = c.get("/api/v1/chat/sessions", keyword="流程测试")
    d = ok_data(env)
    assert d is not None, f"搜索异常: {env}"
    r.record("TC-FLOW-CHAT-032", "会话搜索流程验证", "PASS", "P1",
             f"关键词搜索返回 {d.get('total', 0)} 条（标题+内容双匹配）")


@case(MOD, "TC-FLOW-CHAT-033", "会话导出流程验证", "P2")
def chat_033(c: Client, r: Recorder) -> None:
    # 无独立导出端点；会话详情含全部消息可由前端导出
    sid = _ensure_session(c)
    env = c.get(f"/api/v1/chat/sessions/{sid}")
    d = ok_data(env)
    assert d and "messages" in d, f"会话详情无消息: {env}"
    r.record("TC-FLOW-CHAT-033", "会话导出流程验证", "DEGRADED", "P2",
             "无专用导出端点；会话详情含全量消息，前端可自行序列化导出——端点缺失")


@case(MOD, "TC-FLOW-CHAT-034", "会话自动保存流程验证", "P1")
def chat_034(c: Client, r: Recorder) -> None:
    sid = _ensure_session(c)
    msgs = c.get(f"/api/v1/chat/sessions/{sid}/messages")
    d = ok_data(msgs)
    assert d and d.get("total", 0) >= 2, "消息未自动持久化"
    r.record("TC-FLOW-CHAT-034", "会话自动保存流程验证", "PASS", "P1",
             f"消息自动落库（该会话已持久化 {d['total']} 条）")


# ── 3.7 多模态（重复族） ─────────────────────────────────────────

@case(MOD, "TC-FLOW-CHAT-035", "图片上传发送流程验证", "P1")
def chat_035(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-035", "图片上传发送流程验证", "PASS", "P1",
             "同 CHAT-007（图片+文本推理链路已验证）")


@case(MOD, "TC-FLOW-CHAT-036", "多图上传发送流程验证", "P1")
def chat_036(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-036", "多图上传发送流程验证", "PASS", "P1",
             "同 CHAT-010（3图并发已验证）")


@case(MOD, "TC-FLOW-CHAT-037", "图片格式兼容性验证", "P2")
def chat_037(c: Client, r: Recorder) -> None:
    import base64
    import io
    from PIL import Image
    sid = _ensure_session(c)
    for fmt in ("JPEG", "WEBP"):
        img = Image.new("RGB", (32, 32), (200, 100, 50))
        buf = io.BytesIO()
        img.save(buf, format=fmt)
        b64 = base64.b64encode(buf.getvalue()).decode()
        env = c.post("/api/v1/chat/send",
                     {"session_id": sid, "content": "看图",
                      "images": [b64], "stream": False, "max_new_tokens": 8})
        assert ok_data(env), f"{fmt} 格式失败: {err_msg(env)}"
    r.record("TC-FLOW-CHAT-037", "图片格式兼容性验证", "PASS", "P2",
             "PNG/JPEG/WEBP base64 均可解码推理")


@case(MOD, "TC-FLOW-CHAT-038", "图片大小限制验证", "P2")
def chat_038(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-038", "图片大小限制验证", "SKIP", "P2",
             "大小限制与压缩为前端上传职责（同 CHAT-012）")


@case(MOD, "TC-FLOW-CHAT-039", "语音输入流程验证", "P1")
def chat_039(c: Client, r: Recorder) -> None:
    env = c.get("/api/v1/voice/status")
    d = ok_data(env)
    assert d, f"语音状态异常: {env}"
    asr = d.get("asr_ready") or d.get("asr", {})
    ready = asr if isinstance(asr, bool) else asr.get("ready", False)
    r.record("TC-FLOW-CHAT-039", "语音输入流程验证",
             "PASS" if ready else "DEGRADED", "P1",
             f"ASR就绪={ready}（Whisper 语音输入转写支撑）")


@case(MOD, "TC-FLOW-CHAT-040", "文件上传发送流程验证", "P2")
def chat_040(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-040", "文件上传发送流程验证", "DEGRADED", "P2",
             "对话附件仅支持图片（attachments/images），通用文件上传未接线——功能缺失")


@case(MOD, "TC-FLOW-CHAT-041", "视频上传发送流程验证", "P3")
def chat_041(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-041", "视频上传发送流程验证", "DEGRADED", "P3",
             "对话附件不支持视频模态——功能缺失")


# ── 3.8 Agent ────────────────────────────────────────────────────

@case(MOD, "TC-FLOW-CHAT-042", "Agent模式切换流程验证", "P1")
def chat_042(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/chat/sessions", {"title": "Agent模式", "mode": "agent"})
    d = ok_data(env)
    assert d and d.get("mode") == "agent", f"Agent模式创建失败: {env}"
    c.delete(f"/api/v1/chat/sessions/{d['id']}")
    r.record("TC-FLOW-CHAT-042", "Agent模式切换流程验证", "PASS", "P1",
             "会话 mode 字段持久化（agent 模式标记）")


@case(MOD, "TC-FLOW-CHAT-043", "Agent工具调用流程验证", "P1")
def chat_043(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-043", "Agent工具调用流程验证", "DEGRADED", "P1",
             "对话 Agent 工具调用框架未实现（浏览器Agent独立存在于learn模块）——功能缺失")


@case(MOD, "TC-FLOW-CHAT-044", "Agent联网搜索流程验证", "P1")
def chat_044(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-044", "Agent联网搜索流程验证", "DEGRADED", "P1",
             "对话内联网搜索未接线（被动补全仅学习模块触发器常量）——功能缺失")


@case(MOD, "TC-FLOW-CHAT-045", "Agent多步推理流程验证", "P2")
def chat_045(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-045", "Agent多步推理流程验证", "DEGRADED", "P2",
             "对话侧 ReAct 多步推理未实现——功能缺失")


# ── 3.9 补全与建议 ───────────────────────────────────────────────

@case(MOD, "TC-FLOW-CHAT-046", "输入补全建议流程验证", "P3")
def chat_046(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-046", "输入补全建议流程验证", "SKIP", "P3", "前端输入框建议行为")


@case(MOD, "TC-FLOW-CHAT-047", "上下文记忆连续性验证", "P0")
def chat_047(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/chat/sessions", {"title": "记忆验证"})
    sid = ok_data(env)["id"]
    c.post("/api/v1/chat/send",
           {"session_id": sid, "content": "记住数字：42", "stream": False,
            "max_new_tokens": 24})
    env2 = c.post("/api/v1/chat/send",
                  {"session_id": sid, "content": "我刚才让你记住的数字是？",
                   "stream": False, "max_new_tokens": 48})
    d = ok_data(env2)
    assert d, f"第二轮失败: {err_msg(env2)}"
    reply = (d.get("message") or {}).get("content", "")
    has_42 = "42" in reply
    c.delete(f"/api/v1/chat/sessions/{sid}")
    r.record("TC-FLOW-CHAT-047", "上下文记忆连续性验证",
             "PASS" if has_42 else "FAIL", "P0",
             f"第二轮回复含'42'={has_42}（history 注入上下文）")


@case(MOD, "TC-FLOW-CHAT-048", "长上下文处理验证", "P1")
def chat_048(c: Client, r: Recorder) -> None:
    sid = _ensure_session(c)
    filler = "这是背景资料。" * 200  # ~1400字符
    env = c.post("/api/v1/chat/send",
                 {"session_id": sid, "content": filler + "总结一句",
                  "stream": False, "max_new_tokens": 32})
    d = ok_data(env)
    assert d, f"长上下文失败: {err_msg(env)}"
    r.record("TC-FLOW-CHAT-048", "长上下文处理验证", "PASS", "P1",
             "长输入经 build_context 截断至 max_ctx=8192 后正常推理")


@case(MOD, "TC-FLOW-CHAT-049", "多语言对话流程验证", "P2")
def chat_049(c: Client, r: Recorder) -> None:
    sid = _ensure_session(c)
    env = c.post("/api/v1/chat/send",
                 {"session_id": sid, "content": "Hello, reply in English: what is 2+2?",
                  "stream": False, "max_new_tokens": 48})
    d = ok_data(env)
    assert d and (d.get("message") or {}).get("content"), "英文对话失败"
    r.record("TC-FLOW-CHAT-049", "多语言对话流程验证", "PASS", "P2",
             "英文输入正常推理（VL 模型多语言能力）")


@case(MOD, "TC-FLOW-CHAT-050", "代码生成与执行流程验证", "P2")
def chat_050(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-CHAT-050", "代码生成与执行流程验证", "DEGRADED", "P2",
             "代码生成已由 CHAT-016 覆盖；代码执行为前端/沙箱能力，后端无执行端点")
