"""知识入库质检闸单测（升级批2，2026-09-05）。

纯函数直测（模块零重依赖）。判定教训锁定：
「以？结尾」不能单独判垃圾——短剧剧情悬念问句（type=fact）是有效知识。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

from backend.services.knowledge_quality_gate import (
    check,
    is_answerless_qa,
    is_crawler_noise,
    is_too_short,
)


def test_crawler_noise_detected() -> None:
    assert is_crawler_noise("播放正片滑动查看更多短剧热门短剧全72集总裁夫人")
    assert is_crawler_noise("（提示：25 号）24 号 25 号 26 号提交相关内容红果短剧官方")
    assert is_crawler_noise("24 号 25 号 26 号 27 号")
    # 2026-09-11 批1 存量清洗实证反哺（14 条实锤脏数据特征）
    assert is_crawler_noise("问：跳转到主要内容妙笔生花首页专题博客登录返回")
    assert is_crawler_noise("本书数字版权由果麦文化提供，并由其授权制作发行")
    assert is_crawler_noise("剧迷留言↑ 发布留言刚看完最新一集")
    assert is_crawler_noise("| 微短剧剧本征稿随机文章")


def test_product_metadata_pair_detected() -> None:
    # 图书商品页：上架时间+出版社 同现判残渣（单现不判防误杀）
    assert is_crawler_noise(
        "问：品牌：果麦文化上架时间：2025-03-01出版社：太白文艺出版社最新章节")
    assert not is_crawler_noise("这本书的出版社是太白文艺出版社")
    assert not is_crawler_noise("新剧上架时间定了，全网等待官宣")


def test_valid_entries_survive_gate_regression() -> None:
    """误杀防护回归（2026-09-11 批1 人工终审留存件锁定）。"""
    # 英文 Q/A 格式的有效知识（启发式只认中文标记曾漏判有效）
    ok, reason = check(
        "Q：短剧一集要写多少字？A：1-3分钟的短剧，每集约 300-800字的剧本。", "qa")
    assert ok, reason
    # 答案超短但真实的 qa（字数阈值不得误杀事实型答案）
    ok, reason = check(
        "问：短剧《虚颜不藏巾帼》的主角是谁？答：祝青云、倾城公主、陆闻雪。", "qa")
    assert ok, reason
    # 含播放页残留片段的有效剧情概要（立即播放不入册的原因）
    ok, reason = check(
        "短剧标题：慕家寻回走失五年的小千金糯糯，性子软糯懂事，收获满心疼爱。立即播放",
        "fact")
    assert ok, reason


def test_normal_fact_not_noise() -> None:
    assert not is_crawler_noise("短剧《谢衡舟南汐谢让夜色诱我》类型：古装爱情宫廷，主题为女扮男装")
    assert not is_crawler_noise("短剧剧本写法关键步骤包括：确定题材类型、设计三秒抓人开篇")


def test_answerless_qa_detected() -> None:
    assert is_answerless_qa("qa", "《这部漫剧》的制作团队是谁？")
    assert is_answerless_qa("qa", "如何提高剧本的叙事技巧？")
    # 答：段本身是问句（营销 teaser）同样算无答案
    assert is_answerless_qa(
        "qa", "问：短剧《大宋嫡公主》的结局是什么？答：红墙之内，是沉沦还是双向救赎？")


def test_answered_qa_and_fact_question_tail_not_flagged() -> None:
    # 有真实答案的 qa 放行
    assert not is_answerless_qa(
        "qa", "问：短剧的开篇要点是什么？答：三秒抓人，前3集必须抛出核心冲突。")
    # 误伤教训锁定：fact 类型以悬念问句收尾是有效知识，绝不判垃圾
    assert not is_answerless_qa(
        "fact", "短剧《谢衡舟南汐谢让夜色诱我》主题为女扮男装，欺君之罪如何逆转成帝后？")


def test_too_short() -> None:
    assert is_too_short("好的")
    assert not is_too_short("短剧《漫漫时光听见你》已完结，主题为爱情都市甜宠")


def test_check_composite() -> None:
    ok, reason = check("短剧剧本写法关键步骤包括：确定题材类型、设计三秒抓人开篇", "fact")
    assert ok and reason == ""
    ok, reason = check("《这部漫剧》的制作团队是谁？", "qa")
    assert not ok and reason == "answerless_qa"
    ok, reason = check("播放正片滑动查看更多", "fact")
    assert not ok and reason == "crawler_noise"
    ok, reason = check("好的", "fact")
    assert not ok and reason == "too_short"
