"""P0-3 多角色锚协议纯逻辑测试（2026-08-28）。

AST 沙箱手法（同 test_keyframe_seed_consistency 约定）：从
src/api/manga/keyframe.py 提取 _shot_image_prompt_from_abc 及其
模块内依赖（strip 系函数 + 模块级正则），exec 到隔离命名空间，
detect_style 以桩替换、_SHOT_FRAMING 注入受控景别表——不触发
gen_router/torch/FastAPI 重型导入。

覆盖（P0-3 残留补口：外层锚曾写死「始终只有这一个角色」，与调用
方多角色锚「共 N 位」同句矛盾）：
  - 单角色行：保留 v26「同一角色 + 唯一性禁令」措辞
  - 多角色行：外层为中性「角色锚」措辞，禁止出现单角色唯一性禁令
  - 无角色绑定行：不注入 char 锚
  - 默认参数（char_count=0）与单角色行为一致（向后兼容）
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import ast
import re
from pathlib import Path

KEYFRAME_PY = (Path(__file__).resolve().parents[2]
               / "api" / "manga" / "keyframe.py")

_FUNCS = {"_shot_image_prompt_from_abc", "_strip_prop_colors",
          "_strip_char_appearance", "_strip_hand_flush"}
_CONSTS = {"_PROP_COLOR", "_CHAR_APPEAR_PAT", "_HAND_FLUSH_PAT"}


def _load_shot_prompt_builder() -> dict:
    """AST 提取目标函数 + 依赖常量，exec 到隔离命名空间后返回。"""
    tree = ast.parse(KEYFRAME_PY.read_text(encoding="utf-8"))
    body = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in _FUNCS:
            body.append(n)
        elif isinstance(n, ast.Assign):
            tgt = n.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id in _CONSTS:
                body.append(n)
    picked_names = {n.name for n in body if isinstance(n, ast.FunctionDef)}
    assert _FUNCS <= picked_names, f"目标函数缺失: {_FUNCS - picked_names}"
    ns: dict = {"re": re}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body,
          type_ignores=[])), "<ast-sandbox>", "exec"), ns)
    ns["_SHOT_FRAMING"] = {"1x2": ["Medium shot", "Medium shot"]}

    class _Pack:
        sid = "default"
        post = "realistic"
        style_block = ""
        quality_block = ""

    ns["detect_style"] = lambda text: _Pack()
    return ns


def _build(ns: dict, char_prompt: str, char_count: int = 0) -> str:
    f = ns["_shot_image_prompt_from_abc"]
    grid = {"layout": "1x2", "shots": [
        {"text": "少女站在灯塔下眺望海面"},
        {"text": "海面泛起微光，灯塔亮起"}]}
    return f("A. 全局风格：日系网漫。\nC. 1. 少女站在灯塔下眺望海面。"
             "2. 海面泛起微光。", grid, 0, char_prompt, "", "", "",
             char_count=char_count)


def test_single_char_keeps_uniqueness_anchor():
    ns = _load_shot_prompt_builder()
    out = _build(ns, "小满，外貌、服装与角色设定图严格一致", char_count=1)
    assert "同一角色（各镜严格一致，画面中始终只有这一个角色，" \
           "除该角色外不得出现任何人物）：小满" in out
    assert "角色锚（各镜严格一致）：" not in out


def test_multi_char_anchor_has_no_contradiction():
    ns = _load_shot_prompt_builder()
    cp = ("画面中的角色共2位：小满、阿澈，各自外貌、服装与角色设定图"
          "严格一致；除这些角色外不得出现任何其他人物")
    out = _build(ns, cp, char_count=2)
    # 中性措辞 + 逐名列举完整保留
    assert f"角色锚（各镜严格一致）：{cp}" in out
    # 单角色唯一性禁令不得与「共 N 位」同句出现（P0-3 残留矛盾）
    assert "始终只有这一个角色" not in out
    assert "除这些角色外不得出现任何其他人物" in out


def test_no_char_binding_no_anchor():
    ns = _load_shot_prompt_builder()
    out = _build(ns, "")
    assert "同一角色" not in out
    assert "角色锚" not in out


def test_default_char_count_backcompat():
    """不传 char_count（旧调用方）保持单角色行为。"""
    ns = _load_shot_prompt_builder()
    f = ns["_shot_image_prompt_from_abc"]
    grid = {"layout": "1x2", "shots": [{"text": "少女站在灯塔下"}]}
    out = f("A. 全局风格：日系网漫。", grid, 0,
            "小满，外貌、服装与角色设定图严格一致")
    assert "始终只有这一个角色" in out
