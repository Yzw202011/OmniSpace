"""生图路由引擎：底座 × 风格包组合自动切换（2026-08-27）。

三大生图要求（用户裁定，结果导向）：
  1. 支持生成各种风格的图片 → 风格词典 STYLE_PACKS（8 风格包，
     判据 + 英文风格词块 + 底座亲和 + 后处理档位）；
  2. 质量对齐竞品画面质量 → 每风格包带特化质量块；写实/科幻走
     realistic 后处理（v29 全管线），艺术风格走 stylized（保笔触/
     保腮红——风格特征不是瑕疵）；
  3. 图片符合输入源要求 → 图文一致性由既有 A/B/C 协议 + 资产锚
     保证，路由不触碰协议，只换底座与风格词。

底座亲和依据（全链路实测沉淀）：
  - qwen-image-2512：写实源（VLM 94/100，毛孔/血色/真实人脸比例）
  - flux2-klein-9b：高质量叙事档（GGUF Q6_K 原生分辨率直出）
    2026-08-29 模型裁剪：AI 绘画仅保留 9b + qwen-image 两底座；
    flux2-klein-4b 保留引擎候选（漫剧角色生图 comic_gen 专用）、
    sdxl-base-1.0 保留引擎候选（LoRA 训练底座），均不入风格路由
    偏好序。

约束链（优先级从高到低）：
  模块级选型配置白名单（models.module_config，2026-08-27 管控）
  → RAM 闸门（qwen GGUF 12.31GB 常驻，可用 RAM 不足跳过）
  → 磁盘就绪（engine.available_models()）
  → 底座偏好序（风格包 base_prefs）

每次路由决策写结构化事件日志（log_event，event=gen_route_switch）
——系统日志模块三视图（事件时间线/执行流程）即时可见，切换流程
带 trace_id 可串联同一次生成的完整决策链。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..event_log import log_event

# ── 通用质量块（写实向，v22 定稿原文——default/cg3d 包沿用保行为）──
_QUALITY_REALISTIC = (
    "highly detailed sharp render, realistic physically-based "
    "materials, realistic facial proportions, realistic skin texture "
    "with natural bare-skin complexion without makeup or blush, "
    "individual hair strands with clean edges, soft natural shadow "
    "transitions with ambient light, cinematic color grading with "
    "balanced exposure and rich tonal range, no crushed shadows, no "
    "clipped highlights, clean smooth gradients without noise, no "
    "visible text, no letters, no signage, no watermark"
)
_QUALITY_STYLIZED = (
    "highly detailed masterful artwork, crisp clean lines, "
    "professional illustration quality, refined color harmony, "
    "gallery-grade composition, no visible text, no watermark"
)
_NEG_REALISTIC = "anime, cartoon, flat illustration, 3d toy figure, plastic skin"


@dataclass(frozen=True)
class StylePack:
    """风格包：路由引擎的「插件」侧——提示词工程块 + 后处理档位。

    底座（base）与插件（style_pack）成对切换（用户需求：「底座
    插件一起」）；LoRA 权重插件为未来扩展位（磁盘 lora 资产出现
    时挂 lora_hint 字段）。
    """
    sid: str                      # 风格 id（日志/前端展示）
    label: str                    # 中文名
    pattern: re.Pattern           # 判据（对 A 段/提示词全文匹配）
    style_block: str              # 英文风格词块（紧跟 A 段注入）
    quality_block: str            # 质量词块（特化）
    negative_hint: str            # SDXL 降级路径负向提示
    post: str                     # 后处理档位 realistic / stylized
    base_prefs: tuple[str, ...]   # 底座偏好序（依可用性依序取）


_STYLE_BOOST_3D = (
    "3D game CG render, high-precision 3D modeling, PBR physically "
    "based rendering, realistic cinematic game cutscene visual quality"
)

# ── 风格词典（判据优先级 = 列表序）──
# 2026-08-27 V31 事故裁定：cg3d 提至首位——「3D CG 游戏风格…写实
# 二次元游戏质感」类 A 段中「写实」后缀词会抢先命中 photoreal，
# 导致 3D CG 主声明被写实词块覆盖（画风漂移 + realistic 后处理
# 偏离资产图立绘风）。3D/PBR/建模/CG 是主声明级强信号，先于
# photoreal 的氛围级判据（写实/照片）判别；纯写实文本不含 3D
# 词不受影响。
# 2026-08-29 市场调研裁定：新增 4 包插在最前——判据含上位词，
# 必须先于会被其截胡的旧包：thickpaint(厚涂) 先于 guofeng3d
# （「厚涂CG玄幻」含玄幻）；guofeng3d(玄幻/国风3D) 先于 cg3d
# （「国风3D玄幻」含 3D/渲染）；guofeng2d(国风/工笔) 先于
# inkwash（工笔自 inkwash 移交）；manhwa(半写实) 先于 photoreal
# （「半写实」含写实）。
STYLE_PACKS: tuple[StylePack, ...] = (
    StylePack(
        sid="thickpaint", label="厚涂CG玄幻",
        pattern=re.compile(r"厚涂|厚塗|impasto", re.I),
        style_block=(
            "digital thick paint CG illustration, impasto brush "
            "strokes, rich material textures, dramatic volumetric "
            "lighting, layered depth, epic fantasy atmosphere"
        ),
        quality_block=_QUALITY_REALISTIC,
        negative_hint=_NEG_REALISTIC,
        post="realistic",
        base_prefs=("flux2-klein-9b", "flux2-klein-4b"),
    ),
    StylePack(
        sid="guofeng3d", label="3D国风玄幻",
        pattern=re.compile(r"国风3D|3D国风|玄幻|仙侠|修仙|法宝|法术"),
        style_block=(
            "chinese fantasy 3D animation render, xianxia aesthetic, "
            "gorgeous ancient costumes with intricate embroidery, "
            "glowing magic effects, grand celestial architecture, "
            "epic cinematic lighting"
        ),
        quality_block=_QUALITY_REALISTIC,
        negative_hint=_NEG_REALISTIC,
        post="realistic",
        base_prefs=("flux2-klein-9b",),
    ),
    StylePack(
        sid="guofeng2d", label="国风2D",
        pattern=re.compile(r"国风|工笔|重彩|古风|古韵|东方美学|水墨淡彩"),
        style_block=(
            "chinese guofeng 2D anime illustration, gongbi meticulous "
            "line art, elegant oriental aesthetics, ink-wash tinted "
            "pastel colors, graceful traditional costume design, "
            "semi-3D soft shading"
        ),
        quality_block=_QUALITY_STYLIZED,
        negative_hint="photorealistic, photo, 3d render, plastic skin",
        post="stylized",
        base_prefs=("flux2-klein-9b",),
    ),
    StylePack(
        sid="manhwa", label="韩漫半写实",
        pattern=re.compile(r"韩漫|韩系|manhwa|webtoon|半写实", re.I),
        style_block=(
            "korean manhwa webtoon style, semi-realistic proportions, "
            "layered soft shading with subtle gradients, clean "
            "polished faces, trendy cinematic color grading"
        ),
        quality_block=_QUALITY_STYLIZED,
        negative_hint="chibi, super deformed, thick outlines, western cartoon",
        post="stylized",
        base_prefs=("qwen-image-2512", "flux2-klein-9b"),
    ),
    StylePack(
        sid="cg3d", label="3D/CG 渲染",
        pattern=re.compile(r"3D|PBR|建模|CG|渲染"),
        style_block=_STYLE_BOOST_3D,
        quality_block=_QUALITY_REALISTIC,
        negative_hint=_NEG_REALISTIC,
        post="realistic",
        base_prefs=("flux2-klein-9b",),
    ),
    StylePack(
        sid="photoreal", label="写实摄影",
        pattern=re.compile(r"写实|照片|摄影|实拍|纪实|photoreal|realistic", re.I),
        style_block=(
            "photorealistic photography, shot on 85mm portrait lens, "
            "shallow depth of field with natural bokeh, natural skin "
            "texture with visible pores, true-to-life color rendition, "
            "soft directional lighting"
        ),
        quality_block=_QUALITY_REALISTIC,
        negative_hint=_NEG_REALISTIC,
        post="realistic",
        base_prefs=("qwen-image-2512", "flux2-klein-9b"),
    ),
    StylePack(
        sid="anime", label="二次元/动漫",
        pattern=re.compile(r"二次元|动漫|动画|卡通|赛璐璐|立绘|anime|manga|celshad", re.I),
        style_block=(
            "anime style illustration, clean expressive line art, cel "
            "shading, detailed anime character design, vibrant anime "
            "color palette, large expressive eyes"
        ),
        quality_block=_QUALITY_STYLIZED,
        negative_hint="photorealistic, realistic skin pores, photo",
        post="stylized",  # 腮红/平滑肤是二次元风格特征，不做写实消解
        base_prefs=("flux2-klein-9b",),
    ),
    StylePack(
        # 2026-08-29 风格治理：预置「热血战斗」此前无包（落 default），
        # 补包使预置清单全部有包支撑；置于 anime 之后（「热血战斗动漫」
        # 类文本先命中动漫主声明）
        sid="battle", label="热血战斗",
        pattern=re.compile(r"热血|战斗|燃系|打斗|shonen", re.I),
        style_block=(
            "shonen battle anime style, dynamic action poses, high "
            "contrast dramatic lighting, intense energy effects, speed "
            "lines, bold shadows, fiery aura"
        ),
        quality_block=_QUALITY_STYLIZED,
        negative_hint="photorealistic, realistic skin pores, photo",
        post="stylized",
        base_prefs=("flux2-klein-9b",),
    ),
    StylePack(
        sid="watercolor", label="水彩",
        pattern=re.compile(r"水彩|水粉|淡彩|watercolor|gouache", re.I),
        style_block=(
            "watercolor painting, soft wet-on-wet washes, translucent "
            "layered pigments, gentle color bleeding, delicate paper "
            "texture, visible expressive brush strokes"
        ),
        quality_block=_QUALITY_STYLIZED,
        negative_hint="photorealistic, photo, 3d render",
        post="stylized",
        base_prefs=("flux2-klein-9b", "flux2-klein-4b"),
    ),
    StylePack(
        sid="oilpaint", label="油画",
        pattern=re.compile(r"油画|厚涂|古典绘|oil.?paint|impasto", re.I),
        style_block=(
            "oil painting, rich impasto brushwork, classical "
            "chiaroscuro lighting, textured canvas surface, painterly "
            "realism, museum quality fine art"
        ),
        quality_block=_QUALITY_STYLIZED,
        negative_hint="photorealistic, photo, anime",
        post="stylized",
        base_prefs=("flux2-klein-9b", "flux2-klein-4b"),
    ),
    StylePack(
        sid="inkwash", label="国风水墨",
        # 2026-08-29：「工笔」移交 guofeng2d（工笔重彩归国风2D包），
        # 新增「丹青」承接水墨描述词（inkwash 描述已改「水墨丹青」）
        pattern=re.compile(r"水墨|丹青|国画|写意|山水|留白|ink.?wash|sumi", re.I),
        style_block=(
            "traditional Chinese ink wash painting, sumi-e style, "
            "expressive calligraphic brush strokes, misty atmospheric "
            "perspective, rice paper texture, restrained ink tones "
            "with subtle color accents, generous negative space"
        ),
        quality_block=_QUALITY_STYLIZED,
        negative_hint="photorealistic, photo, 3d render, anime",
        post="stylized",
        base_prefs=("flux2-klein-9b", "flux2-klein-4b"),
    ),
    StylePack(
        sid="cyberpunk", label="赛博朋克/科幻",
        pattern=re.compile(r"赛博|朋克|霓虹|科幻|未来|机甲|cyberpunk|sci.?fi|neon", re.I),
        style_block=(
            "cyberpunk aesthetic, neon-lit futuristic megacity, "
            "volumetric fog with neon glow, high-tech low-life "
            "atmosphere, cinematic sci-fi color grading, reflective "
            "wet surfaces, holographic signage"
        ),
        quality_block=_QUALITY_REALISTIC,
        negative_hint="anime, flat illustration, watercolor",
        post="realistic",
        base_prefs=("flux2-klein-9b",),
    ),
)

# 兜底包：判据未命中（A 段风格词未知/纯叙事）——行为与 v22 完全
# 一致（style_block 空、通用写实质量块），无回归风险
DEFAULT_PACK = StylePack(
    sid="default", label="通用高质量",
    pattern=re.compile(r"(?!)"),  # 永不命中
    style_block="",
    quality_block=_QUALITY_REALISTIC,
    negative_hint=_NEG_REALISTIC,
    post="realistic",
    base_prefs=("flux2-klein-9b",),
)


def detect_style(text: str) -> StylePack:
    """文本（A 段/提示词）→ 命中的风格包；未命中返回 DEFAULT_PACK。

    判据按词典序（优先级）首个命中即返回——cg3d 主声明级强信号
    （3D/PBR/建模/CG）先于 photoreal 氛围级判据，防「写实二次元
    游戏质感」类混合词的「写实」后缀覆盖 3D CG 主声明（V31 事故）。
    """
    if not text:
        return DEFAULT_PACK
    for pack in STYLE_PACKS:
        if pack.pattern.search(text):
            return pack
    return DEFAULT_PACK


@dataclass(frozen=True)
class RouteDecision:
    """路由决策：底座 + 风格包（插件）组合 + 决策依据（日志可溯）。"""
    model_id: str            # 选定的首选底座
    style_pack: StylePack    # 风格包（插件）侧
    base_chain: tuple[str, ...] = field(default_factory=tuple)  # 降级链
    reason: str = ""         # 决策原因（日志 detail 字段）
    switched_from: str = ""  # 之前驻留底座（保持不换载时记录）


# qwen-image-2512 GGUF 权重常驻 RAM ~12.31GB + 推理开销 ~3GB
#（2026-08-23 OOM 死亡事故实测边界）——可用 RAM 低于此线跳过
_QWEN_RAM_FLOOR_GB = 15.5


def _ram_available_gb() -> float:
    try:
        import psutil
        return psutil.virtual_memory().available / (1 << 30)
    except Exception:  # noqa: BLE001 - psutil 缺失按无限 RAM 处理
        return 99.0


def _available_models() -> set[str]:
    """磁盘就绪模型集合（paint_engine 候选目录探测；失败回空集）。"""
    try:
        from .paint_engine import get_paint_engine
        return set(get_paint_engine().available_models())
    except Exception:  # noqa: BLE001 - 引擎未初始化不阻断路由
        return set()


def resolve_route(
    slot: str,
    prompt: str,
    *,
    family: str | None = None,
    prefer_keep: str = "",
    trace_id: str = "",
) -> RouteDecision:
    """路由决策入口：风格画像 → (底座, 风格包) 组合。

    Args:
        slot: 模块槽位（paint / manga-paint）——白名单约束来源
        prompt: 提示词/A 段风格文本（风格判据输入）
        family: 底座族限制——"flux" 时锁定 klein 家族（漫剧关键帧
            资产一致性协议：klein 参考条件机制闭环验证，换族不保
            一致性）；None = 全量（AI 绘画）
        prefer_keep: 当前驻留底座（在偏好序内且可用时优先保留，
            避免反复换载——与 draw.py「sdxl 已驻留不换 qwen」同语义）
        trace_id: 流程追踪 ID（系统日志执行流程面板串联同次生成）

    Returns:
        RouteDecision——model_id 永不为空（全链路失败时 klein-9b 兜底，
        由调用方 ensure_loaded 落实加载成败的最终降级）。
    """
    pack = detect_style(prompt)

    # ① 底座偏好序（族过滤：关键帧锁 klein 家族）
    prefs = list(pack.base_prefs)
    if family == "flux":
        prefs = [m for m in prefs if "klein" in m] or ["flux2-klein-9b"]

    # ② 模块级选型配置白名单（管理员管控最高优先）
    scope_note = ""
    try:
        from ...api.models import get_module_model_scope
        allowed, _default = get_module_model_scope(slot)
        if allowed is not None:
            in_scope = [m for m in prefs if m in allowed]
            if in_scope:
                prefs = in_scope
                scope_note = f"白名单过滤后 {len(in_scope)}/{len(allowed)}"
            elif allowed:
                # 偏好全被白名单滤掉 → 管理员意图优先，取白名单内
                # 第一个磁盘就绪模型
                avail = _available_models()
                alt = next((m for m in allowed if m in avail), allowed[0])
                prefs = [alt] + [m for m in prefs if m not in (alt,)]
                scope_note = f"偏好序被白名单覆盖 → {alt}"
    except Exception:  # noqa: BLE001 - 配置读取失败不阻断路由
        scope_note = ""

    # ③ 资源闸门（RAM——qwen GGUF 常驻 12.31GB 边界）
    ram_gb = _ram_available_gb()
    if ram_gb < _QWEN_RAM_FLOOR_GB:
        dropped = [m for m in prefs if m.startswith("qwen-image")]
        if dropped:
            prefs = [m for m in prefs if not m.startswith("qwen-image")]
            scope_note = (scope_note + f"；RAM {ram_gb:.1f}GB 不足 "
                          f"跳过 {dropped[0]}").lstrip("；")

    # ④ 磁盘就绪过滤 + 兜底保尾（2026-08-29 裁剪：兜底 = klein-9b，
    #    加载成败由调用方 ensure_loaded 最终裁决）
    avail = _available_models()
    usable = [m for m in prefs if m in avail] or \
        [m for m in prefs if not m.startswith("qwen-image")] or \
        ["flux2-klein-9b"]

    # ⑤ 驻留保持（在可用序内且风格兼容时优先不换载）
    model_id = usable[0]
    if prefer_keep and prefer_keep in usable:
        model_id = prefer_keep

    decision = RouteDecision(
        model_id=model_id,
        style_pack=pack,
        base_chain=tuple(usable),
        reason=(f"style={pack.sid} prefs={list(pack.base_prefs)} "
                f"{scope_note}".strip()),
        switched_from=prefer_keep,
    )

    # ⑥ 系统日志埋点（永不抛错——log_event 内部兜底）
    try:
        log_event(
            "paint", "gen_route_switch",
            f"生图路由：风格「{pack.label}」→ 底座 {model_id}"
            f" + 风格包 {pack.sid}"
            + (f"（驻留 {prefer_keep} 保持不换载）"
               if model_id == prefer_keep and prefer_keep else ""),
            level="info",
            detail=json.dumps({
                "slot": slot, "style": pack.sid,
                "model_id": model_id, "base_chain": list(decision.base_chain),
                "switched_from": prefer_keep, "ram_available_gb": round(ram_gb, 1),
                "reason": decision.reason, "family": family or "all",
            }, ensure_ascii=False),
            trace_id=trace_id,
        )
    except Exception:  # noqa: BLE001 - 日志失败不影响路由
        pass
    return decision
