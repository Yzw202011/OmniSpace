"""角色四视图设定图质量回归测试（2026-09-02 小林实拍问题）。

三个缺陷的回归锁定：
  1. regenerate 后 portrait 主图 = front 正面切片（此前被 master
     整图覆盖 → 表格头像显示整张四视图拼版）；
  2. 剧情态场景句剥离（「雨夜便利店场景中，衣角微湿」混进角色
     设定 → 设定图后两格画成剧情场景）；
  3. 纯白背景校验按格判定（整图边框采样对「部分格有背景」的
     四视图拼图失效——右两格背景在图内部，边框被白区拉白误判）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

from backend.api.manga.comic_gen import (
    _strip_scene_state_clauses,
    _verify_background_white,
)


def test_strip_scene_state_keeps_permanent_look() -> None:
    raw = ("20岁，纤瘦，短发微卷至耳下。日常穿着浅灰连帽卫衣，"
           "深蓝工装裤，腰间挂便利店工牌。常低头整理货架，嘴角微抿。"
           "雨夜便利店场景中，衣角微湿，无伞，动作自然。"
           "空手，常态平静表情，眼睛平视镜头。")
    out = _strip_scene_state_clauses(raw)
    assert "雨夜" not in out and "微湿" not in out
    # 恒定外观与恒定随身物保留
    assert "卫衣" in out and "工牌" in out and "常态平静表情" in out


def test_strip_is_noop_on_clean_prompt() -> None:
    raw = "20岁，纤瘦，短发微卷至耳下。日常穿着浅灰连帽卫衣，常态平静表情。"
    assert _strip_scene_state_clauses(raw) == raw


def test_verify_background_white_per_cell() -> None:
    """按格判定：右半格有深色场景时必须不通过（整图边框采样会误判）。"""
    import numpy as np
    from PIL import Image
    # 模拟 4 格拼图 256×144：左两格纯白，右两格深色场景（带浅色顶部）
    img = Image.new("RGB", (256, 144), (255, 255, 255))
    arr = np.asarray(img).copy()
    arr[:, 128:] = (60, 70, 90)          # 右半全部深色
    arr[:12, 128:] = (200, 205, 215)     # 右半顶部浅色（模拟天花板）
    img = Image.fromarray(arr)
    assert _verify_background_white(img) is False


def test_verify_background_white_all_white_passes() -> None:
    import numpy as np
    from PIL import Image
    img = Image.new("RGB", (256, 144), (255, 255, 255))
    arr = np.asarray(img).copy()
    arr[20:120, 30:60] = (0, 0, 0)       # 中央人物不触发边框判定
    assert _verify_background_white(Image.fromarray(arr)) is True
