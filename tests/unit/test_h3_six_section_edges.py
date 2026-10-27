"""转义层两个边角修复单测（2026-10-25）。

边角一：B 段栏目标题首行（如「高密度世界观构建」，短行无句读）按
句号截摘要时混进 summary 与世界状态段 → 短无标点首行按标题剥除。
边角二：无说话人前缀的语气括号台词（如「（内心 OS）我只是路过啊…」）
落到匿名 S1、且语气直译不通顺 → 语气摘出归 manner、OS 类动词映射
「内心独白式地说」、单主角绑定时台词归主角色（多主角维持匿名）。
"""
from __future__ import annotations

from src.services.inference.h3_chain_engine import _six_section

_DESC = """A. 全局风格
3D CG 超写实真人风格，高精度真人级 3D 建模。全程无字幕、无背景音乐，仅保留环境自然音效与角色台词。

B. 高密度世界观构建
盛夏午后的热浪裹着栀子花香漫过街道，陌生街景在视野里无限铺展。

C.分镜时间轴
[0.0s-3.0s] 画面：侧景跟拍夏沐沐拖着行李箱沿步道前行 | 运镜：平稳横向跟移 | 台词：无 | 音效：风声
[3.0s-6.0s] 画面：围栏里的大黄狗冲着她龇牙低吼，夏沐沐吓得后缩半步 | 运镜：推镜至狗特写 | 台词：（内心 OS）我只是路过啊，为什么要对着我吠。| 音效：犬吠炸开
[6.0s-10.0s] 画面：夏沐沐神色委屈又无措，脚尖微微转向离开的方向 | 运镜：面部近景 | 台词：无 | 音效：犬吠渐弱"""

_REFS_ONE = [{"kind": "character", "name": "夏沐沐白裙", "src": ""}]
_REFS_TWO = [{"kind": "character", "name": "夏沐沐白裙", "src": ""},
             {"kind": "character", "name": "陈雪", "src": ""}]


def test_b_title_stripped_from_summary_and_world_state() -> None:
    """keep_b_section=true 回退旧行为：标题剥除但 B 正文保留。"""
    import src.services.inference.h3_chain_engine as eng_mod
    orig = eng_mod.get_config
    eng_mod.get_config = lambda: {"manga": {"h3_keep_b_section": True}}
    try:
        out = _six_section(_DESC, _REFS_ONE, 10.0)
    finally:
        eng_mod.get_config = orig
    # 标题剥除仍生效（不把「高密度世界观构建」带进合同）
    assert "高密度世界观构建\n盛夏" not in out
    assert "[世界与人物状态] 高密度世界观构建" not in out
    assert "盛夏午后的热浪裹着栀子花香" in out  # 正文保留
    assert "[世界与人物状态] 盛夏午后的热浪" in out


def test_default_drops_b_section_silently() -> None:
    """默认吞 B：summary 只剩首拍、[世界与人物状态] 整段省略，
    B 段散文不给 H3（朗读稿源切除）。"""
    out = _six_section(_DESC, _REFS_ONE, 10.0)
    assert "盛夏午后的热浪" not in out
    assert "[世界与人物状态]" not in out
    # A/C 段照常
    assert "3D CG 超写实真人风格" in out
    assert "[Shot 1]" in out and "[Shot 3]" in out


def test_os_dialogue_bound_to_single_character() -> None:
    out = _six_section(_DESC, _REFS_ONE, 10.0)
    # 语气括号摘出 + 单主角绑定 + OS 动词映射
    assert "夏沐沐白裙 (S1) 内心独白式地说: <d>[Chinese] 我只是路过啊" in out
    # 台词行内不再残留语气括号
    assert "<d>[Chinese] （内心 OS）" not in out


def test_multichar_unattributed_stays_anonymous() -> None:
    out = _six_section(_DESC, _REFS_TWO, 10.0)
    # 多主角无法归属：维持匿名 S1，但 OS 动词映射与语气摘除仍生效
    assert "(S1) 内心独白式地说: <d>[Chinese] 我只是路过啊" in out
    assert "夏沐沐白裙 (S1)" not in out


_DESC_MD = '''A. **全局风格**：超写实治愈系现代3D电影质感，电影级自然光影。**全程无字幕、无背景音乐、只有音效**。

B. **高密度世界观构建 (High-Density World Building)**：盛夏午后的海滨别墅区街道，栀子花瓣随海风飘落。

C. **分镜时间轴 (Scene-Embedded Frame Split)**：
* **[0.0s-0.1s] 画面：参考图保持 | 运镜：固定 | 音效：无**
* **[0.1s-3.5s] 画面**: [夏日街景] 远景，开满花卉的别墅区柏油街道向远处延伸。 | **运镜**: 缓慢前推 | **音效**: 悠长的蝉鸣声、远处隐约的海浪声
* **[3.5s-6.5s] 画面**: [核对地址] 中景，黑长直少女站在路边，白色行李箱立在她脚边。 | **运镜**: 从脚边行李箱缓慢上移至少女面部 | **音效**: 纸张轻微揉搓声、风扫过发丝的轻响
* **[6.5s-10.0s] 画面**: [茫然呢喃] 近景特写，少女轻轻咬了咬下唇，嘴唇微动小声呢喃。 | **运镜**: 固定特写 | **音效**: 少女轻柔的台词："嗯，到底在哪儿呢？"'''


def test_markdown_c_section_fields_parse() -> None:
    """LLM 排版风描述词（** 加粗 + * 列表）：剥除后字段全部解析进合同
    （修复前运镜/音效整字段丢失、** 残留 9 处进合同）。"""
    out = _six_section(_DESC_MD, _REFS_ONE, 10.0)
    assert "**" not in out, "加粗残留进合同"
    assert "缓慢前推" in out, "运镜字段丢失"
    assert "从脚边行李箱缓慢上移至少女面部" in out, "运镜字段丢失"
    assert "纸张轻微揉搓声" in out, "音效字段丢失"
    assert "悠长的蝉鸣声" in out, "音效字段丢失"


def test_markdown_b_section_still_dropped_by_default() -> None:
    out = _six_section(_DESC_MD, _REFS_ONE, 10.0)
    assert "栀子花瓣随海风飘落" not in out  # 吞 B 默认（Markdown 版同样吞）


def test_named_manner_regression() -> None:
    desc = _DESC.replace("台词：（内心 OS）我只是路过啊，为什么要对着我吠。",
                         "台词：夏沐沐（轻声）：我只是路过啊。")
    out = _six_section(desc, _REFS_ONE, 10.0)
    # 既有具名「角色（语气）：」写法回归：语气照译不触发 OS 映射
    assert "夏沐沐白裙 (S1) 轻声地说: <d>[Chinese] 我只是路过啊。</d>" in out


# ---------------------------------------------------------------------------
# 句号内联字段解析（2026-10-27 拍板 A）：LLM 产出常把「运镜：/台词：/
# 音效：」用句号内联进画面行（漫剧 row fe65da70 实锤）——旧分隔符集只认
# | 与换行：台词零 <d> 标记（H3 朗读描述词根因）+「无。」假具名音效。
_C_INLINE = """[0.0s-2.3s] 画面：[街角驻足] 少女立于大门前，左手轻拢衣角，嘴角微启。运镜：平视中景，镜头缓慢前推，机位高度 1.7 米。台词：无 | 音效：无。
[2.3s-6.1s] 画面：[惊叹低语] 少女轻咬下唇，眼眸放光，抬头凝望。运镜：小幅匀速前推，机位高度 1.6 米。台词：夏沐沐（轻喃）：哇，这小区，好高级啊。 | 音效：无。
[6.1s-10s] 画面：[街角回望] 少女转头，衣角随风轻扬。运镜：慢速后拉。台词：无 | 音效：无。"""

_REFS_EXACT = [{"kind": "character", "name": "夏沐沐", "src": ""}]


def test_inline_period_joined_fields_extract() -> None:
    """句号内联的运镜/台词/音效字段照常提取（锚点补切）。"""
    from src.services.inference.h3_chain_engine import _parse_c_shots
    shots, _note = _parse_c_shots(_C_INLINE, 10.125)
    assert len(shots) == 3
    assert shots[0]["cam"].startswith("平视中景")
    assert shots[0]["sfx"] == "无"  # 「无。」尾句号剥除、不判具名
    assert shots[0]["text"].endswith("嘴角微启。")  # 画面文本不再吞运镜/台词
    assert shots[1]["dlg"] == ["夏沐沐（轻喃）：哇，这小区，好高级啊。"]
    assert shots[2]["dlg"] == ["无"]


def test_inline_fields_dialogue_marked_and_sfx_clean() -> None:
    """端到端：具名台词获 <d> 标记 + S1 绑定；裸台词条不再残留画面文本；
    soundscape 无「无。」垃圾 token；has_dialogue 判真。"""
    desc = "A. 全局风格\n3D CG 超写实真人风格。\n\nC. 分镜时间轴\n" + _C_INLINE
    out = _six_section(desc, _REFS_EXACT, 10.125)
    assert "夏沐沐 (S1) 轻喃地说: <d>[Chinese] 哇，这小区，好高级啊。</d>" in out
    assert "台词：夏沐沐（轻喃）" not in out
    assert "镜头运动：小幅匀速前推" in out
    assert "无。；" not in out and "无。。" not in out
    assert "角色台词已在时间轴中给出" in out


def test_chain_audio_spec_inline_all_none_mutes() -> None:
    """内联「台词：无 | 音效：无。」全 none → 显式双无成立（铁栏惊吠
    场景在内联格式下仍触发文件级静音；修复前「无。」假具名锁死该通道）。"""
    from src.services.inference.h3_chain_engine import _chain_audio_spec
    c_only = "[0.0s-2.0s] 画面：铁栏边大黄犬低吠。运镜：固定。台词：无 | 音效：无。"
    has_dlg, has_sfx, explicit_mute = _chain_audio_spec(
        ["C. 分镜时间轴\n" + c_only], 10.0)
    assert (has_dlg, has_sfx, explicit_mute) == (False, False, True)
