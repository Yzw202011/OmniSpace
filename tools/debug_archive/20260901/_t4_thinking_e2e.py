"""T4：深度思考全链路 e2e。

验证链：
1. WS thinking=true → reasoning 帧流式产出（与 token 帧分离）
2. token 帧不含 <think> 标签（编排层解析正确）
3. meta.thinking_used / first_content_ms 透出
4. 落库回放：GET session detail → assistant 消息 reasoning 非空
5. 对照组：thinking=false → 无 reasoning 帧
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, r"e:\OmniSpace")
import requests  # noqa: E402
import websockets  # noqa: E402

BASE = "http://127.0.0.1:8765/api/v1"
WS_BASE = "ws://127.0.0.1:8765/api/v1/dialog/stream"
CHAT = f"{BASE}/chat/sessions"

QUESTION = "我想给团队选一个协作工具，10人小团队，主要写文档和看板，预算有限"


async def send_with_thinking(sid: str, content: str, thinking: bool):
    """发送一条消息，返回 (reasoning_full, content_full, meta, had_error)."""
    reasoning_parts: list[str] = []
    token_parts: list[str] = []
    meta = None
    err = None
    async with websockets.connect(
            f"{WS_BASE}/{sid}", max_size=10 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "type": "message",
            "data": {"content": content, "thinking": thinking},
        }, ensure_ascii=False))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=300)
            frame = json.loads(raw)
            t = frame.get("type")
            if t == "reasoning":
                reasoning_parts.append(frame["data"]["text"])
            elif t == "token":
                token_parts.append(frame["data"].get("token", ""))
            elif t == "status":
                print(f"  [status] {frame['data'].get('message', '')}")
            elif t == "meta":
                meta = frame["data"]
            elif t == "error":
                err = frame["data"]
                break
            elif t == "done":
                break
    return "".join(reasoning_parts), "".join(token_parts), meta, err


def main() -> int:
    # 1. 创建会话（chat 路由：data 即 session 对象）
    r = requests.post(CHAT, json={"title": "T4-thinking-e2e"}, timeout=15)
    r.raise_for_status()
    data = r.json()["data"]
    sid = data.get("id") or data.get("session", {}).get("id")
    print(f"[1] 会话已创建: {sid}")

    # 2. thinking=true 流式
    t0 = time.time()
    print("[2] thinking=true 发送中…")
    reasoning, content, meta, err = asyncio.run(
        send_with_thinking(sid, QUESTION, True))
    dt = time.time() - t0
    if err:
        print(f"  [FAIL] error 帧: {err}")
        return 1
    print(f"  耗时 {dt:.1f}s | reasoning {len(reasoning)} 字 | content {len(content)} 字")
    ok_reasoning = len(reasoning) > 20
    ok_clean = ("<think>" not in content and "</think>" not in content
                and "【最终回答】" not in content)
    ok_meta = bool(meta and meta.get("thinking_used")
                   and "first_content_ms" in (meta or {}))
    has_steps = any(k in reasoning for k in
                    ("【问题分析】", "【信息检索】", "【方案评估】", "【决策依据】"))
    print(f"  reasoning 非空: {ok_reasoning} | content 无标签污染: {ok_clean} "
          f"| meta.thinking_used: {ok_meta} | 四步框架标记: {has_steps}")
    print(f"  meta: {meta}")
    print(f"  reasoning 头 300 字:\n  {reasoning[:300]}")
    print(f"  content 头 200 字:\n  {content[:200]}")
    if not (ok_reasoning and ok_clean and ok_meta):
        print("[FAIL] thinking=true 链路断言失败")
        return 1

    # 3. 落库回放（chat 路由：data 即 session + messages）
    r = requests.get(f"{CHAT}/{sid}", timeout=15)
    r.raise_for_status()
    msgs = r.json()["data"]["messages"]
    assistant = [m for m in msgs if m.get("role") == "assistant"]
    ok_persist = bool(assistant and assistant[-1].get("reasoning"))
    print(f"[3] 落库回放 assistant.reasoning 非空: {ok_persist}"
          f"（长度 {len(assistant[-1].get('reasoning') or '') if assistant else 0}）")
    if not ok_persist:
        print("[FAIL] reasoning 未持久化")
        return 1

    # 4. 对照组 thinking=false
    print("[4] thinking=false 对照发送中…")
    r2, c2, meta2, err2 = asyncio.run(
        send_with_thinking(sid, "一句话介绍 SQLite 的 WAL 模式", False))
    if err2:
        print(f"  [FAIL] error 帧: {err2}")
        return 1
    ok_no_reasoning = len(r2) == 0
    print(f"  reasoning 长度 {len(r2)}（应为 0）: {ok_no_reasoning} "
          f"| content {len(c2)} 字 | meta2: {meta2}")
    if not ok_no_reasoning:
        print("[FAIL] thinking=false 不应产出 reasoning")
        return 1

    print("\n=== T4 全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
