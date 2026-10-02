"""云端四视图整图直出管线单测（2026-10-03，四视图/重生上云）。

覆盖：
- 模板拼装：用户模板导语（视图清单/中文标注/禁外语字符/美术风格）
  + 角色名注入 + 角色设定原文尾拼；
- 白沟切分：四色块纯白底整图 → 按白沟智能切回四条（不均宽也能切准）；
- 等分兜底：无白沟（满幅噪声）回退等宽四切，恒有四条产出。
"""
from __future__ import annotations

from PIL import Image

from src.services.inference.cloud_turnaround import (
    build_turnaround_cloud_prompt,
    slice_views_by_white_gutters,
)


def test_build_prompt_contains_user_template_and_settings() -> None:
    p = build_turnaround_cloud_prompt("夏沐沐", "时代背景：当代中国都市。角色设定：22岁女性。")
    # 用户模板要素逐项在场（2026-10-03 原文）
    for piece in ("【角色】：夏沐沐", "生成角色4视图", "正面全身、侧面全身、背面全身、上半身特写",
                  "纯白色背景", "图片左上角标注中文角色名：夏沐沐",
                  "禁止出现任何其他文字", "禁止外语字符",
                  "3D CG 游戏风格", "PBR 物理渲染"):
        assert piece in p
    # 角色设定原文尾拼在场
    assert "时代背景：当代中国都市。角色设定：22岁女性。" in p
    assert p.rstrip().endswith("22岁女性。")


def test_slice_by_white_gutters_uneven_blocks() -> None:
    """四色块不等宽（各带 60px 白沟）→ 白沟切分还原四条，无跨块剪切。"""
    w, h = 1520, 1000
    img = Image.new("RGB", (w, h), "white")
    colors = ((200, 30, 30), (30, 200, 30), (30, 30, 200), (200, 200, 30))
    spans = [(0, 320), (380, 760), (820, 1140), (1200, 1520)]
    for (lo, hi), c in zip(spans, colors, strict=True):
        for x in range(lo, hi):
            for y in range(80, 900):
                img.putpixel((x, y), c)
    views = slice_views_by_white_gutters(img, 4)
    assert len(views) == 4
    # 每条的中心样本应落在对应色块内（切界在白沟里，不切进人物区）
    for view, (_lo, _hi), c in zip(views, spans, colors, strict=True):
        cx = view.width // 2
        cy = view.height // 2
        assert view.getpixel((cx, cy)) == c, f"视图切歪：中心色 {view.getpixel((cx, cy))} != {c}"


def test_slice_falls_back_to_equal_quarters_without_gutters() -> None:
    """满幅非白（无可用白沟）→ 等宽四切兜底，恒出四条。"""
    w, h = 1600, 1000
    img = Image.new("RGB", (w, h), (128, 128, 128))
    for x in range(0, w, 7):
        for y in range(0, h, 7):
            img.putpixel((x, y), (100, 100, 100))
    views = slice_views_by_white_gutters(img, 4)
    assert len(views) == 4
    assert all(v.width == w // 4 for v in views)
