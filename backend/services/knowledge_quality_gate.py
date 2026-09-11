"""知识入库质检闸（知识学习模块升级方案 2026-09-05 批 2）。

四条规则（纯函数、可单测、可解释；被拒条目不入库）：
  R1 crawler_noise   爬虫导航残渣：播放正片/滑动查看/提示：N 号/纯页码行
  R2 answerless_qa   无答案 QA：type=qa 且内容以问号结尾且没有「答：」段
                     （或答段本身仍是问句——营销 teaser）
  R3 too_short       超短无信息：清洗后 < 8 字
  R4 answer_teatimer 已并入 R2（答段为问句）

判定教训（2026-09-05 实测）：「以？结尾」不能单独作为垃圾判据——短剧
剧情简介常以悬念问句收尾（type=fact，含类型/主题/剧情，是有效知识）；
无答案判定必须同时看 type 与「答：」段。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import re

_NAV_RESIDUE_RE = re.compile(
    r"播放正片|滑动查看更多|提示：\s*\d+\s*号|红果短剧官方|全\d+集",
    re.IGNORECASE)
_PAGE_NUMBER_LINE_RE = re.compile(r"^(?:\d{1,3}\s*号?[\s,，、]*){3,}$")
_QUESTION_TAIL = ("？", "?")
_QUESTION_WORDS = ("是什么", "为什么", "如何", "哪些", "什么样", "谁", "吗",
                   "多少", "怎么")


def is_crawler_noise(content: str) -> bool:
    """爬虫导航残渣/页码列表。"""
    c = (content or "").strip()
    if not c:
        return False
    if _NAV_RESIDUE_RE.search(c):
        return True
    return bool(_PAGE_NUMBER_LINE_RE.match(c))


def is_answerless_qa(k_type: str, content: str) -> bool:
    """无答案 QA：仅 qa 类型参与判定（fact 悬念问句是有效知识）。"""
    c = (content or "").strip()
    if (k_type or "").lower() != "qa":
        return False
    if not c.endswith(_QUESTION_TAIL):
        return False
    if "答：" not in c and "答:" not in c:
        return True
    answer = re.split("答[：:]", c, maxsplit=1)[1].strip()
    return answer.endswith(_QUESTION_TAIL)


def is_too_short(content: str, min_len: int = 8) -> bool:
    return len((content or "").strip()) < min_len


def check(content: str, k_type: str = "fact",
          min_len: int = 8) -> tuple[bool, str]:
    """入库质检：返回 (通过, 拒收原因)；通过时原因 ="";
    事实类悬念问句（type=fact 以？收尾）不在拒收之列。"""
    if is_crawler_noise(content):
        return False, "crawler_noise"
    if is_answerless_qa(k_type, content):
        return False, "answerless_qa"
    if is_too_short(content, min_len):
        return False, "too_short"
    return True, ""
