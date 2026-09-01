"""F-1/F-2 事实验证：Qwen3-VL 模型 chat template 的 thinking 支持。

只读 tokenizer/chat_template（不加载模型权重）：
1. 默认渲染 vs enable_thinking=True/False 渲染 → diff 判断模板是否真支持
2. add_generation_prompt=True 时思考开标签是否注入 prompt 尾部
"""
import sys
from pathlib import Path

ROOT = Path(r"e:\OmniSpace")
sys.path.insert(0, str(ROOT / "pydeps"))
sys.path.insert(0, str(ROOT))

from transformers import AutoTokenizer  # noqa: E402

MODELS = [
    "qwen3-vl-4b",
    "qwen3-vl-8b-awq",
]

msgs = [{"role": "user", "content": "你好"}]

for mid in MODELS:
    d = ROOT / "models" / mid
    if not d.is_dir():
        print(f"== {mid}: 目录不存在，跳过")
        continue
    print(f"== {mid} ==")
    try:
        tok = AutoTokenizer.from_pretrained(str(d), trust_remote_code=True)
    except Exception as exc:
        print(f"  tokenizer 加载失败: {exc}")
        continue

    # 1. 默认渲染
    try:
        base = tok.apply_chat_template(
            msgs, tokenize=False, add_generation_prompt=True)
        print(f"  默认渲染尾部 120 字符: ...{base[-120:]!r}")
    except Exception as exc:
        print(f"  默认渲染失败: {exc}")
        continue

    # 2. enable_thinking 显式传参渲染
    for flag in (True, False):
        try:
            out = tok.apply_chat_template(
                msgs, tokenize=False, add_generation_prompt=True,
                enable_thinking=flag)
            diff = "SAME" if out == base else "DIFF"
            tail = out[-120:]
            print(f"  enable_thinking={flag}: {diff}  尾部: ...{tail!r}")
        except Exception as exc:
            print(f"  enable_thinking={flag}: 渲染异常 {type(exc).__name__}: {exc}")

    # 3. 模板源码中是否引用 enable_thinking / think 标签
    try:
        src = ""
        cfg = getattr(tok, "chat_template", None)
        if isinstance(cfg, str):
            src = cfg
        elif isinstance(cfg, dict):
            src = " ".join(str(v) for v in cfg.values())
        has_var = "enable_thinking" in src
        has_tag = "<think>" in src
        print(f"  模板源码: enable_thinking 变量={has_var}, <think> 标签={has_tag}")
    except Exception as exc:
        print(f"  模板源码检查失败: {exc}")
    print()
