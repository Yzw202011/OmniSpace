"""验证新版 _unique_text：多 salt 多轮模拟，检查 extracted 与 dedup 决策。"""
import sys

sys.path.insert(0, r"e:\OmniSpace")

from backend.services.knowledge_service import get_knowledge_service

svc = get_knowledge_service()
_TOPIC = "短剧编剧技巧"

# 复刻 cases_learn 的生成器（避免导入 tests.flow 触发 requests 会话）
import random  # noqa: E402 - 探针脚本，bootstrap 后按需导入

_RUN_TAGS = ["deadbeef", "cafe1234", "00ff8899"]  # 模拟 3 轮

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "cl", r"e:\OmniSpace\tests\flow\cases_learn.py")
# 不执行模块（其顶层仅定义），直接拷模板池
_FACT = ("短剧开场要在{a}秒内抛出核心冲突，编剧技巧要求第一屏就有张力",
         "短剧每{b}秒需要一次小反转，这是编剧保持留存的节拍技巧",
         "付费卡点通常设在第{a}到第{b}集，卡点前铺垫决定转化率",
         "短剧单集台词不超过{a}句，编剧要把它压缩到{b}分钟以内",
         "短剧主角目标要在{a}秒内交代清楚，致命弱点最迟第{b}集揭示",
         "竖屏短剧单集时长{a}分钟左右，编剧技巧上每{b}秒给一次刺激")
_DEF = ("钩子前置是指编剧把最强悬念提到第一屏的短剧开场技巧",
        "反转结构是短剧编剧维持观众留存的核心叙事引擎",
        "人物标签化是指编剧用极致单一特质降低记忆成本的短剧手法",
        "付费卡点是指短剧中引导解锁下一集的剧情张力峰值设计",
        "信息不对称是短剧编剧制造爽点最常用的戏剧技巧")
_METH = ("短剧编剧流程建议先写付费卡点再倒推开场，步骤上先定结局情绪再铺中段反转",
         "短剧改稿方法上先检查每{a}秒节拍表，再逐句压缩书面语台词",
         "编剧技巧训练第一步拆解爆款短剧，第二步复刻其反转节奏，第三步替换人物标签",
         "短剧大纲阶段的流程是先列人物目标清单，再排布每集钩子位置")
_CASE = ("例如某爆款短剧在第{a}集卡点前置反派登场，转化率显著提升",
         "比如开场{a}秒内主角直接被退婚，这就是钩子前置的典型案例",
         "例如把误会集中在第{b}集爆发，短剧弹幕讨论度会明显升高")

def gen(run_tag: str, salt: str) -> str:
    rng = random.Random(f"{run_tag}:{salt}")
    a, b = rng.randint(2, 9), rng.randint(10, 49)
    sents = [s.format(a=a, b=b) for s in rng.sample(_FACT, 2)]
    sents.append(rng.choice(_DEF).format(a=a, b=b))
    sents.append(rng.choice(_METH).format(a=a, b=b))
    sents.append(rng.choice(_CASE).format(a=a, b=b))
    marker = (run_tag + salt.encode().hex())[:8]
    return f"短剧编剧技巧学习材料（{marker}）：" + "。".join(sents) + "。"

for tag in _RUN_TAGS:
    for salt in ("extract", "doc", "b1", "b2", "dedup"):
        text = gen(tag, salt)
        clean = svc.filter_content(text)
        segs = svc.segment_content(clean)
        n_ext, n_pass, decisions = 0, 0, []
        for seg in segs:
            for k in svc.extract_knowledge(seg, _TOPIC):
                n_ext += 1
                q = svc.evaluate_quality(k, _TOPIC)
                if q.passed:
                    n_pass += 1
                    decisions.append(svc.deduplicate(k))
        print(f"tag={tag} salt={salt:8s} 提取={n_ext} 过质量门={n_pass} dedup={decisions}")
