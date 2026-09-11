"""P1 多参考拆分纯逻辑测试（2026-08-28「参考图拆分加权」）。

AST 沙箱手法（同 test_keyframe_seed_consistency 约定）：从源文件提
取目标函数及模块级依赖，exec 到隔离命名空间——不触发 torch/FastAPI/
gen_router 重型导入。

覆盖：
  - keyframe._select_shot_references：链帧整帧替换 / 面部前置 /
    同类内点名优先 / 8 张上限淘汰
  - keyframe._shot_char_protocol：逐镜点名过滤 / 全员回退 / 单多角色措辞
  - comfy_paint_engine._build_workflow：多图 ReferenceLatent 链式
    级联 / pos 模式负侧不注入 / off 无 ref 节点 / 分档分辨率预算
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
KEYFRAME_PY = BACKEND / "api" / "manga" / "keyframe.py"
COMMON_PY = BACKEND / "api" / "manga" / "common.py"
COMFY_PY = BACKEND / "services" / "inference" / "comfy_paint_engine.py"


def _extract(source: Path, funcs: set[str], consts: set[str],
             class_name: str | None = None) -> dict:
    """AST 提取函数（可指定类内方法）与模块级常量赋值，exec 隔离。"""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    body: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id in consts:
                body.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == class_name:
            body.extend(sub for sub in node.body
                        if isinstance(sub, ast.FunctionDef)
                        and sub.name in funcs)
        elif isinstance(node, ast.FunctionDef) and node.name in funcs:
            body.append(node)
    got = {n.name for n in body if isinstance(n, ast.FunctionDef)}
    assert funcs <= got, f"目标函数缺失: {funcs - got}"
    ns: dict = {}
    exec(compile(ast.fix_missing_locations(
        ast.Module(body=body, type_ignores=[])), "<ast-sandbox>", "exec"),
        ns)
    return ns


def _img() -> object:
    """PIL 占位（选择逻辑不触碰像素）。"""
    return object()


# ── _select_shot_references ───────────────────────────────────────

def _refs():
    return [
        {"kind": "character", "name": "小满", "image": _img()},
        {"kind": "character", "name": "阿澈", "image": _img()},
        {"kind": "scene", "name": "灯塔", "image": _img()},
        {"kind": "prop", "name": "行李箱", "image": _img()},
    ]


def test_select_named_char_first():
    ns = _extract(KEYFRAME_PY, {"_select_shot_references"},
                  {"_MAX_REF_IMAGES"})
    f = ns["_select_shot_references"]
    out = f(_refs(), "阿澈在灯塔下 waits")
    kinds = [(e["kind"], e["name"]) for e in out]
    # 角色优先于场景/道具（身份最难，与锚协议优先级一致），
    # 同类内点名者在前：阿澈 → 小满 → 灯塔 → 行李箱
    assert kinds == [("character", "阿澈"), ("character", "小满"),
                     ("scene", "灯塔"), ("prop", "行李箱")]


def test_select_face_precedes_assets_and_chain_replaces():
    ns = _extract(KEYFRAME_PY, {"_select_shot_references"},
                  {"_MAX_REF_IMAGES"})
    f = ns["_select_shot_references"]
    out = f(_refs(), "任意", face_imgs=[_img(), None])
    assert [e["kind"] for e in out][:2] == ["face", "character"]
    chain = _img()
    out2 = f(_refs(), "任意", chain_frame=chain)
    # 链帧整帧替换（v58/v61/v62 校准语义）：独占参考位
    assert len(out2) == 1
    assert out2[0]["kind"] == "chain" and out2[0]["image"] is chain


def test_select_cap_eight():
    ns = _extract(KEYFRAME_PY, {"_select_shot_references"},
                  {"_MAX_REF_IMAGES"})
    f = ns["_select_shot_references"]
    refs = [{"kind": "prop", "name": f"P{i}", "image": _img()}
            for i in range(10)]
    out = f(refs, "")
    assert len(out) == 8
    assert [e["name"] for e in out] == [f"P{i}" for i in range(8)]


# ── _shot_char_protocol ───────────────────────────────────────────

def _chars():
    return [{"kind": "character", "name": "小满"},
            {"kind": "character", "name": "阿澈"}]


def _char_protocol_ns() -> dict:
    """_shot_char_protocol 沙箱：连同 _char_anchor_name 一起抽取。"""
    ns = _extract(KEYFRAME_PY,
                  {"_shot_char_protocol", "_char_anchor_name"}, set())
    return ns


def test_char_protocol_named_subset():
    f = _char_protocol_ns()["_shot_char_protocol"]
    prompt, chosen = f(_chars(), "小满独自站在灯塔下")
    assert len(chosen) == 1 and chosen[0]["name"] == "小满"
    assert prompt.startswith("小满，外貌、服装与角色设定图严格一致")


def test_char_protocol_fallback_all_and_multi_wording():
    f = _char_protocol_ns()["_shot_char_protocol"]
    prompt, chosen = f(_chars(), "她与他在灯塔下相遇（代词未点名）")
    assert len(chosen) == 2
    assert "画面中的角色共2位：小满、阿澈" in prompt
    assert "除这些角色外不得出现任何其他人物" in prompt
    assert "始终只有这一个角色" not in prompt


def test_char_protocol_name_only_anchor():
    """2026-09-02 结构化属性移除后：锚 = 纯名字（残留 traits 键被忽略，
    不影响锚措辞）；无 traits 措辞与移除前一致。"""
    f = _char_protocol_ns()["_shot_char_protocol"]
    a1 = {"kind": "character", "name": "小满",
          "traits": {"gender": "女", "hair": "黑色双马尾"}}  # 历史残留键
    a2 = {"kind": "character", "name": "阿澈",
          "meta": {"traits": {"gender": "男", "age": "中年"}}}
    prompt, chosen = f([a1, a2], "小满与阿澈同行")
    assert len(chosen) == 2
    assert "小满" in prompt and "（女，黑色双马尾）" not in prompt
    assert "阿澈" in prompt and "（男，中年）" not in prompt
    # 单角色：措辞与旧版完全一致（回归保护）
    prompt2, _ = f([{"kind": "character", "name": "小满"}], "小满独行")
    assert prompt2 == "小满，外貌、服装与角色设定图严格一致"


def test_char_protocol_empty():
    f = _char_protocol_ns()["_shot_char_protocol"]
    assert f([], "任意") == ("", [])


# ── comfy _build_workflow（多图 ReferenceLatent 链）────────────────

def _shim():
    ns = _extract(COMFY_PY,
                  {"_build_workflow", "_resolve_reflatent",
                   "_ref_megapixels"},
                  {"_PAINT_FILES", "_PULID_FILE"},
                  class_name="ComfyPaintEngine")
    return type("_Shim", (), {
        "_resolve_reflatent": staticmethod(ns["_resolve_reflatent"]),
        "_build_workflow": ns["_build_workflow"]})(), ns


_BASE_PARAMS = {"prompt": "p", "negative": "n", "steps": 36, "cfg": 4.0,
                "width": 1280, "height": 720, "seed": 1}


def test_workflow_two_refs_chain_both_sides():
    shim, ns = _shim()
    wf = shim._build_workflow(_BASE_PARAMS, ["a.png", "b.png"], "paint/t")
    # 正侧级联：ref_pos conditioning←pos，ref_pos1←ref_pos；guider 收尾
    assert wf["ref_pos"]["inputs"]["conditioning"] == ["pos", 0]
    assert wf["ref_pos1"]["inputs"]["conditioning"] == ["ref_pos", 0]
    assert wf["guider"]["inputs"]["positive"] == ["ref_pos1", 0]
    # both 模式负侧对称级联
    assert wf["ref_neg1"]["inputs"]["conditioning"] == ["ref_neg", 0]
    assert wf["guider"]["inputs"]["negative"] == ["ref_neg1", 0]
    # 1-2 图满幅预算
    assert wf["scale_img1"]["inputs"]["megapixels"] == 1.0


def test_workflow_ref_megapixels_tiers():
    shim, _ = _shim()
    for n, mp in ((3, 0.5), (5, 0.35), (8, 0.35)):
        names = [f"r{i}.png" for i in range(n)]
        wf = shim._build_workflow(_BASE_PARAMS, names, "paint/t")
        assert wf["scale_img"]["inputs"]["megapixels"] == mp, n


def test_workflow_pos_mode_no_neg_chain():
    shim, _ = _shim()
    params = {**_BASE_PARAMS, "reflatent": "pos"}
    wf = shim._build_workflow(params, ["a.png", "b.png"], "paint/t")
    assert "ref_pos1" in wf
    assert not any(k.startswith("ref_neg") for k in wf)
    assert wf["guider"]["inputs"]["negative"] == ["neg", 0]


def test_workflow_off_and_empty_refs():
    shim, _ = _shim()
    wf = shim._build_workflow({**_BASE_PARAMS, "reflatent": "off"},
                              ["a.png"], "paint/t")
    assert not any(k.startswith(("load_img", "ref_")) for k in wf)
    wf2 = shim._build_workflow(_BASE_PARAMS, [], "paint/t")
    assert not any(k.startswith(("load_img", "ref_")) for k in wf2)


def test_ref_megapixels_pure():
    ns = _extract(COMFY_PY, {"_ref_megapixels"}, set())
    f = ns["_ref_megapixels"]
    assert f(1) == 1.0 and f(2) == 1.0
    assert f(3) == 0.5 and f(4) == 0.5
    assert f(5) == 0.35 and f(8) == 0.35
