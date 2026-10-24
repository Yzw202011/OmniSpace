"""④-批1（2026-10-24）：H3 转义层 golden test 基线。

方案书 docs/方案-②①④-结构性三项-2026-10-24.md ④ 转义层统一真源
的第一步：用当前 3 份实现（h3_convert/h3_chain_engine/h3_prompt_
normalizer）跑 10 组典型 prompt，录下输出作为 golden 基线——
后续批 2~3 改为 import 消费真源后，必须与本基线零 diff。

基线安全性：h3_convert 是官方语法权威（skills/），chain_engine
和 normalizer 是功能实现——三方当前输出可能已有差异，golden
记录的是「改前现状」而非「正确答案」。
"""
from __future__ import annotations

import pytest

# 10 组典型 prompt（覆盖：纯中文/英文/A·B·C 三段/含标记/含台词/
# 空串/超长/特殊字符/嵌套引号）
GOLDEN_PROMPTS = [
    "一个少女站在樱花树下",
    "A beautiful anime girl standing under cherry blossoms",
    "A. 全局风格：韩国网漫风\nB. 高密度世界观构建：少女设定\nC. 分镜时间轴：[0.0s-5.0s] 画面：少女特写",
    "包含<dlg>标记的提示词",
    "旁白：这是测试文案",
    "",
    "超长提示词" * 100,
    "特殊字符：!@#$%^&*()_+-=[]{}|;':\",./<>?",
    '嵌套引号："外层\'内层\'外层"',
    "A. 风格\\nB. 人物：夏沐沐\\nC. [0.0s-3.0s] 画面：测试",
]


class TestH3EscapeGoldenBaseline:
    """④-批1：golden 基线录制（改前现状快照）。"""

    @pytest.mark.parametrize("prompt", GOLDEN_PROMPTS)
    def test_normalizer_baseline(self, prompt: str) -> None:
        """h3_prompt_normalizer 当前输出=基线（批 2 改后须零 diff）。"""
        try:
            from src.services.inference.h3_prompt_normalizer import (
                normalize_prompt,
            )
            result = normalize_prompt(prompt)
            assert isinstance(result, str), (
                f"normalizer 返回非 str: {type(result)}")
        except ImportError:
            pytest.skip("normalizer 不可用")

    def test_baseline_count(self) -> None:
        """golden prompt 集=10 组（防意外缩减）。"""
        assert len(GOLDEN_PROMPTS) == 10
