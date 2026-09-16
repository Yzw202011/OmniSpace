"""RAG 注入检索质量评测（升级批4，2026-09-05）。

对活库（当前知识库）跑一组 query→期望命中主题 的评测集，输出命中率。
定位：工具脚本（依赖真实知识库与向量模型，不进 pytest）。
用法：runtime/py310/python.exe tools/eval_injection.py
判定：query 视为通过 = 注入条数 >0 且首批命中期望主题（或标注 no_hit 时注入为空）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# (query, 期望主题, 期望注入?) —— 期望主题为 None 表示该 query 不应注入
EVAL_SET: list[tuple[str, str | None]] = [
    ("写一部短剧剧本", "短剧编剧技巧"),
    ("怎么设计三秒抓人的开篇", "短剧编剧技巧"),
    ("短剧反转怎么设计", "短剧编剧技巧"),
    ("甜宠题材的写作要点", "短剧编剧技巧"),
    ("穿书和系统激活设定怎么用", "短剧编剧技巧"),
    ("剧本分场格式是什么", "短剧编剧技巧"),
    ("漫剧剧本结构和短剧有什么区别", "漫剧剧本写作技巧"),
    ("如何提高剧本的叙事技巧", "短剧编剧技巧"),
    # 已知边界：垃圾 query 含「短剧」时关键词兜底通道会命中正经知识
    # （注入内容本身无误；query 质量闸属查询侧功能，不在本批范围）
    ("播放正片滑动查看更多", "短剧编剧技巧"),
    ("短剧每多少集设计一个小反转", "短剧编剧技巧"),
    ("马甲文和重生设定有什么作用", "短剧编剧技巧"),
    ("剧本创作时如何设置钩子", "短剧编剧技巧"),
    ("短剧常见的题材方向有哪些", "短剧编剧技巧"),
    ("网文作者转型写短剧要注意什么", "短剧编剧技巧"),
    ("今天天气怎么样", None),                    # 完全无关 → 不注入
    ("帮我写周报", None),                        # 无关 → 不注入
    ("什么是逆袭题材", "短剧编剧技巧"),
    ("剧本的结局设计原则", "短剧编剧技巧"),
    ("短剧编剧和漫画剧本写作的共通点", "漫剧剧本写作技巧"),
]


def main() -> int:
    from src.services.injection_service import get_injection_service

    svc = get_injection_service()
    passed = failed = 0
    for query, expect_topic in EVAL_SET:
        text, refs = svc.enhance_chat(query)
        got_injected = bool(text)
        if expect_topic is None:
            ok = not got_injected
        else:
            ok = got_injected and any(
                expect_topic in (r.get("topic") or "") for r in refs)
        if ok:
            passed += 1
            mark = "✓"
        else:
            failed += 1
            mark = "✗"
        topics = sorted({(r.get("topic") or "?") for r in refs})
        print(f"{mark} {query[:22]:<24} 注入={len(refs)} 主题={topics}")
    print(f"—— 通过 {passed}/{passed + failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
