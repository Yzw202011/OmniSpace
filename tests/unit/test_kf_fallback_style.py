"""直文本兜底画风治理回归（2026-09-02 P6d 实测缺陷）。

缺陷：关键帧生成在「行无描述词」时走剧本原文兜底路径，旧实现缀
_STYLE_PHOTO（08-14 写真人设参考图时代的写实助推遗留）——网漫项目
被推成照片风、角色锚外观被文本压漂（实测：白背心→黑背心、短寸→
长发、网漫风→照片风，场景/动作仍对）。

修复契约演进（静态钉，AST 沙箱约定同 test_keyframe_char_anchor）：
  - 2026-09-02 P6d：兜底分支前置项目风格行；
  - **2026-10-05 用户二定（dbc1b78，现行契约）**：分镜图以描述词
    为主——单帧回退路径亦不前缀项目风格行（描述词 A 段自带风格
    词），兜底直出 prompt 原文 + 角色锚仲裁句；
  - 恒定：分支内不得出现 `prompt + _STYLE_PHOTO` 拼接；keyframe
    模块不导入 _STYLE_PHOTO；_project_style_line 空 db / 空风格
    回退 _INFER_STYLE_LINE（非空，供主路径 A 段替换用）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import ast
from pathlib import Path

KEYFRAME_PY = (Path(__file__).resolve().parents[2] / "src" / "api" / "manga" / "keyframe.py")
ASSET_PY = (Path(__file__).resolve().parents[2] / "src" / "api" / "manga" / "comic_asset.py")


def test_fallback_branch_keeps_prompt_sovereignty() -> None:
    src = KEYFRAME_PY.read_text(encoding="utf-8")
    # 2026-10-05 用户二定：兜底直出原文（描述词为主），不前缀风格行。
    # ④批4（2026-10-25）后形态：外包版式指令剥除（剥空回退原文=主权
    # 语义不变，仅滤「四视图」类版式短语）
    assert "_strip_layout_instruction(prompt) or prompt" in src, (
        "兜底分支应直出 prompt 原文（④批4 后=剥版式指令，剥空回退）")
    assert 'gen_prompt = f"{style_line}，{prompt}"' not in src, (
        "2026-10-05 二定后兜底不得前缀项目风格行（描述词 A 段自带风格）")
    # 旧写实助推拼接不得复活（P6d 恒定红线）
    assert "prompt + _STYLE_PHOTO" not in src


def test_keyframe_module_no_style_photo_ref() -> None:
    tree = ast.parse(KEYFRAME_PY.read_text(encoding="utf-8"))
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            imported.update(a.name for a in n.names)
    assert "_STYLE_PHOTO" not in imported, (
        "_STYLE_PHOTO 不应再被 keyframe 导入（写实助推遗留）")


def _load_style_line_fn() -> tuple[dict, str]:
    """B9：AST 沙箱改真 import（真模块 log/常量自动就位）。
    前两个测试的「源码静态钉」保留读源码形态——它们测的就是文本契约。"""
    import src.api.manga.comic_asset as _asset_mod
    ns: dict = {
        "_project_style_line": _asset_mod._project_style_line,
        "_INFER_STYLE_LINE": _asset_mod._INFER_STYLE_LINE,
        "_ART_STYLE_ZH": _asset_mod._ART_STYLE_ZH,
    }
    return ns, ns["_INFER_STYLE_LINE"]


def test_project_style_line_fallback_never_empty() -> None:
    ns, infer = _load_style_line_fn()
    assert infer and infer.strip(), "_INFER_STYLE_LINE 回退行不得为空"
    # 空 db / 空 project_id → 直接回退（兜底拼接的非空前提）
    assert ns["_project_style_line"](None, "") == infer
