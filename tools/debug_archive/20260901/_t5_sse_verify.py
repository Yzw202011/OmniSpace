"""T5: SSE 链路深度思考双通道验证（POST /dialog/send stream=true）。"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8765/api/v1"

# 1) 创建会话
req = urllib.request.Request(
    f"{BASE}/chat/sessions",
    data=json.dumps({"title": "T5-SSE-深度思考验证"}).encode("utf-8"),
    headers={"Content-Type": "application/json; charset=utf-8"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=15) as r:
    data = json.loads(r.read().decode("utf-8"))
sid = (data.get("data") or {}).get("id") or data.get("id")
print(f"[1] 会话已创建: {sid}")

# 2) SSE 流式发送（thinking=true）
payload = json.dumps({
    "session_id": sid,
    "content": "用一句话解释什么是显存",
    "thinking": True,
    "stream": True,
}).encode("utf-8")
req = urllib.request.Request(
    f"{BASE}/dialog/send",
    data=payload,
    headers={"Content-Type": "application/json; charset=utf-8"},
    method="POST",
)

reasoning_text = ""
content_text = ""
events = []
t0 = time.time()
first_reasoning_ts = None
first_token_ts = None
with urllib.request.urlopen(req, timeout=180) as r:
    for raw in r:
        line = raw.decode("utf-8").strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            ev = json.loads(body)
        except json.JSONDecodeError:
            continue
        if "reasoning" in ev:
            events.append("reasoning")
            if first_reasoning_ts is None:
                first_reasoning_ts = time.time() - t0
            reasoning_text += ev["reasoning"]
        elif "token" in ev:
            events.append("token")
            if first_token_ts is None:
                first_token_ts = time.time() - t0
            content_text += ev["token"]
        elif "session_id" in ev:
            events.append("session")
        elif "error" in ev:
            events.append("error")
            print(f"    [错误事件] {ev['error']}")
        elif "passive_completion" in ev:
            events.append("passive")
        elif "meta" in ev:
            events.append("meta")

dur = time.time() - t0
has_marker = "【问题分析】" in reasoning_text and "【最终回答】" not in content_text
four_step = all(k in reasoning_text for k in ("【问题分析】", "【信息检索】", "【方案评估】", "【决策依据】"))

print(f"[2] SSE 流式耗时 {dur:.1f}s | reasoning {len(reasoning_text)} 字 | content {len(content_text)} 字")
if first_reasoning_ts is not None and first_token_ts is not None:
    print(f"    首个 reasoning 事件: {first_reasoning_ts:.2f}s | 首个 token 事件: {first_token_ts:.2f}s")
print(f"    事件序列: {events[:12]}{'...' if len(events) > 12 else ''}")
print(f"[3] reasoning 非空: {len(reasoning_text) > 0} | content 无【最终回答】污染: {'【最终回答】' not in content_text}")
print(f"[4] 四步框架标记齐全: {four_step}")
print(f"    reasoning 头 150 字: {reasoning_text[:150]}")
print(f"    content: {content_text[:120]}")

ok = len(reasoning_text) > 0 and len(content_text) > 0 and four_step and "【最终回答】" not in content_text
print("=== T5 SSE 验证通过 ===" if ok else "=== T5 失败 ===")
sys.exit(0 if ok else 1)
