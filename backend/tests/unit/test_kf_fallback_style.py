"""直文本兜底画风治理回归（2026-09-02 P6d 实测缺陷）。

缺陷：关键帧生成在「行无描述词」时走剧本原文兜底路径，旧实现缀
_STYLE_PHOTO（08-14 写真人设参考图时代的写实助推遗留）——网漫项目
被推成照片风、角色锚外观被文本压漂（实测：白背心→黑背心、短寸→
长发、网漫风→照片风，场景/动作仍对）。

修复契约（静态钉，AST 沙箱约定同 test_keyframe_char_anchor）：
  1. 兜底分支前置项目风格行（gen_prompt = f"{style_line}，{prompt}"），
     空风格由 _project_style_line 回退网漫，分支内不再出现
     `prompt + _STYLE_PHOTO` 拼接；
  2. keyframe 模块不再导入/引用 _STYLE_PHOTO（防复发）；
  3. _project_style_line 空 db / 空风格均回退 _INFER_STYLE_LINE（非空）。
"""
from __future__ import annotations

import ast
from pathlib import Path

KEYFRAME_PY = (Path(__file__).resolve().parents[2]
               / "api" / "manga" / "keyframe.py")
ASSET_PY = (Path(__file__).resolve().parents[2]
            / "api" / "manga" / "comic_asset.py")


def test_fallback_branch_prefixes_project_style() -> None:
    src = KEYFRAME_PY.read_text(encoding="utf-8")
    # 修复后的兜底拼接形态（风格行前置，全角逗号连接）
    assert 'gen_prompt = f"{style_line}，{prompt}"' in src, (
        "兜底分支应前置项目风格行（2026-09-02 P6d 修复被回退）")
    # 旧写实助推拼接不得复活
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
    tree = ast.parse(ASSET_PY.read_text(encoding="utf-8"))
    body = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == "_project_style_line":
            body.append(n)
        elif isinstance(n, ast.Assign):
            tgt = n.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id in (
                    "_INFER_STYLE_LINE", "_ART_STYLE_ZH"):
                body.append(n)
    assert body, "comic_asset.py 中未找到 _project_style_line 及其依赖"
    ns: dict = {"log": type("L", (), {
        "warning": staticmethod(lambda *a, **k: None)})()}
    mod = ast.Module(body=body, type_ignores=[])
    exec(compile(mod, "<extract>", "exec"), ns)  # noqa: S102 - 测试沙箱
    return ns, ns["_INFER_STYLE_LINE"]


def test_project_style_line_fallback_never_empty() -> None:
    ns, infer = _load_style_line_fn()
    assert infer and infer.strip(), "_INFER_STYLE_LINE 回退行不得为空"
    # 空 db / 空 project_id → 直接回退（兜底拼接的非空前提）
    assert ns["_project_style_line"](None, "") == infer
