"""知识库哨兵测试（知识学习升级方案 批1 验收 / 批4 体检常态化）。

对着**真实知识库**做只读巡检（2026-09-11 批1 存量清洗的固化形态）：
  - 导航/页框架残渣 = 0（跳转到主要内容/数字版权由/剧迷留言/发布留言/随机文章）
  - 图书商品页残渣 = 0（上架时间+出版社 同现）
  - 离题黑名单 = 0（奶粉/眼镜王蛇/ICU/减肥/茶园——编剧技巧库的离题件）
  - 无答案空问题 qa = 0（问？收尾无答段；fact 悬念问句是有效知识不在此列）
  - 测试残留主题 = 0（冒烟/测试/smoke 主题）

真实库缺失（无 data/omnispace.db 的环境）自动跳过——哨兵只服务有库的实机。
判定规则与 knowledge_quality_gate 同源（高精度子集）；历史教训：
检测器必须先在真实语料上验证（2026-09-05 启发式误伤剧情问句、
2026-09-11 英文 Q/A 有效件漏判）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.config import DB_PATH
from backend.services.knowledge_quality_gate import (
    is_answerless_qa,
    is_crawler_noise,
)

_NAV_SIGNS = ("跳转到主要内容", "数字版权由", "剧迷留言", "发布留言", "随机文章")
_OFFTOPIC_SIGNS = ("奶粉", "眼镜王蛇", "ICU", "减肥", "茶园")
_TEST_TOPIC_SIGNS = ("冒烟", "测试", "smoke", "test")

pytestmark = [
    pytest.mark.skipif(
        not Path(str(DB_PATH)).is_file(),
        reason="真实知识库不存在（非实机环境），哨兵仅服务有库环境"),
]


def _all_items() -> list[dict]:
    from backend.services.knowledge_service import get_knowledge_service
    svc = get_knowledge_service()
    items: list[dict] = []
    page = 1
    while True:
        r = svc.list_knowledge(page=page, page_size=200)
        batch = r.get("items") or []
        if not batch:
            break
        items.extend(batch)
        if len(items) >= int(r.get("total") or 0):
            break
        page += 1
    return items


def test_sentinel_no_dirty_entries() -> None:
    offenders: list[str] = []
    for it in _all_items():
        content = str(it.get("content") or "")
        topic = str(it.get("topic") or "")
        ktype = str(it.get("type") or "")
        problems = []
        if any(s in content for s in _NAV_SIGNS) or is_crawler_noise(content):
            problems.append("crawler_noise")
        if any(s in content for s in _OFFTOPIC_SIGNS):
            problems.append("offtopic")
        if is_answerless_qa(ktype, content):
            problems.append("answerless_qa")
        if any(s in topic for s in _TEST_TOPIC_SIGNS):
            problems.append("test_topic")
        if problems:
            offenders.append(f"[{problems}] {str(it.get('id'))[-8:]} {content[:60]}")
    assert offenders == [], f"知识库哨兵发现 {len(offenders)} 条脏数据（先备份再走正规删除）:\n" + "\n".join(offenders[:10])


def test_sentinel_corpus_nontrivial() -> None:
    """库非空健全性：清洗不许误伤到清空。"""
    items = _all_items()
    assert len(items) >= 1000, f"知识库仅 {len(items)} 条，疑似误清"
