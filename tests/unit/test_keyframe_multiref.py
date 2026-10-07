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

from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
KEYFRAME_PY = BACKEND / "api" / "manga" / "keyframe.py"
COMMON_PY = BACKEND / "api" / "manga" / "common.py"
COMFY_PY = BACKEND / "services" / "inference" / "comfy_paint_engine.py"

# B9（2026-09-13）：沙箱 exec 改真 import。原 AST 提取方案绕开重导入，
# 但代价是「模块 import 期接线坏了测试照样绿」（沙箱根本不执行模块
# 顶层代码）。py312 主链下全套依赖在位、实测模块导入干净（~2.7s 无
# 副作用），改为真 import——接线损坏从此在收集期即炸。
_PATH_TO_MODULE = {
    KEYFRAME_PY: "src.api.manga.keyframe",
    COMMON_PY: "src.api.manga.common",
    COMFY_PY: "src.services.inference.comfy_paint_engine",
}


def _extract(source: Path, funcs: set[str], consts: set[str],
             class_name: str | None = None) -> dict:
    """真 import 取函数/常量（签名与原 AST 沙箱版一致，调用点零改动）。

    funcs 先查类（class_name 给定时），缺失项回退模块级——与原沙箱
    「类体+模块体双扫」语义对齐（如 _ref_megapixels 即模块级函数）。"""
    import importlib
    module = importlib.import_module(_PATH_TO_MODULE[source])
    owner = getattr(module, class_name) if class_name else module
    ns: dict = {}
    for name in funcs:
        if class_name and hasattr(owner, name):
            ns[name] = getattr(owner, name)
        else:
            ns[name] = getattr(module, name)  # 缺失即 AttributeError，等效原断言
    for name in consts:
        ns[name] = getattr(module, name)
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
    # 2026-10-06 语义收紧：多角色绑定且正文全泛指 → 只锚首角色
    # （防画风相近的多参考图复制多份同框——用户实报）
    prompt, chosen = f(_chars(), "她与他在灯塔下相遇（代词未点名）")
    assert len(chosen) == 1 and chosen[0]["name"] == "小满"
    assert prompt.startswith("小满，外貌、服装与角色设定图严格一致")
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
                   "_ref_megapixels",
                   # 批1a（2026-09-12）：步数/CFG 档位解析助手随
                   # _build_workflow 一并提取（纯函数 + 配置兜底）
                   "_effective_steps_cfg", "_fast_preset"},
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


# ── 2026-10-06 用户实报修（第三格两个陈雪）─────────────────────────
# 软锁特写镜 face+character 双人图并列进 latent＝多视图册信号，
# qwen21 复制成正面+侧面双人像；镜文本景别与固定脚本冲突时提示词
# 自相矛盾。修法：face 顶替 character + 景别仲裁（正文明示 > 脚本）。

def _face_char_scene_refs():
    return [
        {"kind": "face", "name": "", "image": _img()},
        {"kind": "character", "name": "陈雪", "image": _img()},
        {"kind": "scene", "name": "大门", "image": _img()},
    ]


def test_comfy_refs_face_replaces_character_softlock():
    ns = _extract(KEYFRAME_PY, {"_comfy_ref_entries"}, set())
    f = ns["_comfy_ref_entries"]
    refs = _face_char_scene_refs()
    out, mps = f(refs, pulid_img=False, reflat_hint="pos",
                 multi_anchor=False)
    # face 顶替 character：同人双图不再并列（场景保留）
    assert len(out) == 2
    assert mps == [1.0, None]


def test_comfy_refs_pulid_and_multi_anchor_unchanged():
    ns = _extract(KEYFRAME_PY, {"_comfy_ref_entries"}, set())
    f = ns["_comfy_ref_entries"]
    refs = _face_char_scene_refs()
    # PuLID 激活：face 归 attention 通道不入 latent，character 保留
    out, _ = f(refs, pulid_img=True, reflat_hint="pos", multi_anchor=False)
    assert len(out) == 2
    # 多角色锚定：face 不入（单人面部图诱发复制病理），双立绘保留
    out2, _ = f(refs, pulid_img=False, reflat_hint="pos", multi_anchor=True)
    assert len(out2) == 2


def test_shot_text_framing_arbiter():
    ns = _extract(KEYFRAME_PY, {"_shot_text_framing"}, set())
    f = ns["_shot_text_framing"]
    assert f("全景，固定机位。女人的背影向着公路走去") == "wide"
    assert f("大远景，地平线") == "wide"
    assert f("特写收束镜头") == "close"
    assert f("中近景过渡镜头") == "close"
    assert f("近景，侧面视角") == "close"
    assert f("中景，低角度仰拍") == ""
    assert f("") == ""


def test_text_framing_overrides_script_slot():
    ns = _extract(KEYFRAME_PY, {"_shot_image_prompt_from_abc"}, set())
    f = ns["_shot_image_prompt_from_abc"]
    desc = ("A. 全局风格：\n网漫风格\n\nB. 高密度世界观构建：\n背景。\n\n"
            "C. 分镜时间轴：\n[0.0s-2.0s] 画面：她站在门口")
    grid = {"layout": "2x2", "shots": [{"text": "全景，远处背影"}]}
    # 槽 2 脚本钦定 MCU，正文全景 → 构图行让位远景行
    sp = f(desc, grid, 2, char_prompt="陈雪，与设定图严格一致",
           text_framing="wide")
    assert "Wide establishing shot" in sp
    assert "Medium close-up" not in sp
    # 槽 0 脚本是远景，正文特写 → 构图行借 MCU 行
    sp2 = f(desc, grid, 0, char_prompt="陈雪，与设定图严格一致",
            text_framing="close")
    assert "Medium close-up" in sp2
    assert "Wide establishing shot" not in sp2


def test_recede_re_single_instance() -> None:
    """纵深远去措辞命中（2026-10-07 v39 第二格两个陈雪）。"""
    ns = _extract(KEYFRAME_PY, {"_RECEDE_RE"}, set())
    rx = ns["_RECEDE_RE"]
    assert rx.search("女人的背影向着公路延伸的方向走去，越走越远")
    assert rx.search("身影在广阔天地间显得渺小但坚定")
    assert rx.search("渐远的旅程")
    assert not rx.search("她转过身，背对镜头")
    assert not rx.search("中景，低角度仰拍")
