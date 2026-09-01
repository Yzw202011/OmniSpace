"""实测：qwen3-vl-8b-awq 对【最终回答】标记协议的遵守度。
流式请求（与后端 chat_stream 同款路径）+ 标记解析验证。"""
import json
import sys

sys.path.insert(0, r"e:\OmniSpace")
import requests  # noqa: E402

from backend.engines.vllm_service import CHAT_URL  # noqa: E402
from backend.services.inference.dialog_engine import (  # noqa: E402
    DEFAULT_SYSTEM_PROMPT,
    THINKING_SYSTEM_SUFFIX,
)

SYS = DEFAULT_SYSTEM_PROMPT + THINKING_SYSTEM_SUFFIX

payload = {
    "model": "qwen3-vl-8b-awq",
    "messages": [
        {"role": "system", "content": SYS},
        {"role": "user", "content": "我想给团队选一个协作工具，10人小团队，主要写文档和看板，预算有限"},
    ],
    "temperature": 0.6,
    "max_tokens": 1536,
    "stream": True,
}

chunks = []
finish = None
with requests.post(CHAT_URL, json=payload, stream=True,
                   timeout=(10, 180),
                   proxies={"http": None, "https": None}) as resp:
    resp.raise_for_status()
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        data = raw[5:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except ValueError:
            continue
        ch = (obj.get("choices") or [{}])[0]
        delta = ch.get("delta") or {}
        if delta.get("content"):
            chunks.append(delta["content"])
        if ch.get("finish_reason"):
            finish = ch["finish_reason"]

content = "".join(chunks)
print(f"finish_reason={finish}")
print(f"content 长度: {len(content)}")
marker = "【最终回答】"
n = content.count(marker)
print(f"【最终回答】标记出现次数: {n}")
steps = [k for k in ("【问题分析】", "【信息检索】", "【方案评估】", "【决策依据】")
         if k in content]
print(f"四步框架标记命中: {steps}")
if n >= 1:
    reasoning = content.split(marker, 1)[0]
    answer = content.split(marker, 1)[1]
    print(f"\n== 思考段（{len(reasoning)} 字）前 500 字 ==\n{reasoning[:500]}")
    print(f"\n== 回答段（{len(answer)} 字）前 400 字 ==\n{answer[:400]}")
else:
    print("\n[未遵守标记协议] content 前 400 字：")
    print(content[:400])
