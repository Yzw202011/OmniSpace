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
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..event_log import log_event
from ..vram_policy import QWEN_GGUF_RAM_FLOOR_GB as _QWEN_RAM_FLOOR_GB

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

    2026-09-02 词序坑治理：判据从「编译正则」改为 keywords 关键词
    元组（pattern 由它派生，按长度降序交替——包内即最长匹配）。
    keywords 为空 = 显式绑定专用包（嗅探永不命中，如自定义包）。
    """
    sid: str                      # 风格 id（日志/前端展示）
    label: str                    # 中文名
    keywords: tuple[str, ...] = ()  # 嗅探判据关键词（空=不参与嗅探）
    style_block: str = ""         # 英文风格词块（紧跟 A 段注入）
    quality_block: str = ""       # 质量词块（特化）
    negative_hint: str = ""       # SDXL 降级路径负向提示
    post: str = "realistic"       # 后处理档位 realistic / stylized
    base_prefs: tuple[str, ...] = ()  # 底座偏好序（依可用性依序取）
    ignore_case: bool = True      # 拉丁词是否忽略大小写（沿袭旧正则旗标）
    pattern: re.Pattern | None = field(
        default=None, init=False, repr=False, compare=False)  # keywords 派生
    _match_units: tuple = field(
        default=(), init=False, repr=False, compare=False)  # (词,词长) 长→短

    def __post_init__(self) -> None:
        kws = sorted((k for k in self.keywords if k), key=len, reverse=True)
        flags = re.I if self.ignore_case else 0
        src = "|".join(re.escape(k) for k in kws) if kws else "(?!)"
        object.__setattr__(self, "pattern", re.compile(src, flags))
        object.__setattr__(self, "_match_units", tuple(
            (k.lower() if self.ignore_case else k, len(k)) for k in kws))

    def _hit(self, text: str) -> tuple[int, int, str]:
        """最长命中关键词的 (起始位, 长度, 词)。未命中返回 (0,0,"")。"""
        if not text:
            return 0, 0, ""
        t = text.lower() if self.ignore_case else text
        for unit, n in self._match_units:  # 长→短，首个即最长
            pos = t.find(unit)
            if pos >= 0:
                return pos, n, unit
        return 0, 0, ""

    def longest_hit(self, text: str) -> int:
        """文本中命中关键词的最长长度（0=未命中）。"""
        return self._hit(text)[1]

    def hit_keyword(self, text: str) -> str:
        """文本中命中的最长关键词（未命中返回 ""）。日志凭据用。"""
        return self._hit(text)[2]


_STYLE_BOOST_3D = (
    "3D game CG render, high-precision 3D modeling, PBR physically "
    "based rendering, realistic cinematic game cutscene visual quality"
)

# ── 风格词典 ──
# 历史（首序匹配时代，判据优先级 = 列表物理顺序）：
# 2026-08-27 V31 事故裁定：cg3d 提至首位——「3D CG 游戏风格…写实
# 二次元游戏质感」类 A 段中「写实」后缀词会抢先命中 photoreal，
# 导致 3D CG 主声明被写实词块覆盖。3D/PBR/建模/CG 是主声明级强
# 信号，先于 photoreal 的氛围级判据判别。
# 2026-08-29 市场调研裁定：上位词包先于会被截胡的旧包。
# 2026-09-02 词序坑治理（四步：护栏/显式化/最长匹配/可解释）：
# ① 优先级从「列表物理位置」升格为下方 PACK_PRIORITY 声明数据，
#   重排本元组不再改变路由语义；② 嗅探裁决改为「最长关键词胜，
#   平局比 PACK_PRIORITY」（词法分析 maximal munch + CSS 优先级
#   思路），复合词（如 mystery3d 的「国漫悬疑」）天然压过碎片词
#   （donghua3d 的「国漫」），不再依赖站位；③ 改动前真实行为已
#   快照为 tests/unit/unit/style_routing_golden.json，等价性由
#   test_style_pack_routing 金标准钉死。
STYLE_PACKS: tuple[StylePack, ...] = (
    # ── 2026-08-31 新增 12 主流包（整体置前：含「手绘动画/美式卡通/
    # 蒸汽朋克」等会被 anime/cyberpunk 关键词截胡的上位词，实测冲突
    # 检测后统一排最前）──
    StylePack(
        sid="comic_en", label="美式漫画",
        keywords=("美漫", "美式漫画", "美式超英", "american comic", "marvel", "dc comic"),
        style_block=(
            "american comic book art, bold ink outlines, halftone dot "
            "shading, dynamic action panel composition, high saturation "
            "primary colors, superhero comic style"
        ),
        quality_block=_QUALITY_STYLIZED,
        negative_hint="photorealistic, realistic skin pores, photo",
        post="stylized", base_prefs=("flux2-klein-9b",),
    ),
    StylePack(
        sid="manga_bw", label="日式黑白漫画",
        keywords=("黑白漫画", "日漫黑白", "monochrome manga", "screentone"),
        style_block=(
            "black and white japanese manga, screentone halftone shading, "
            "crisp ink linework, dramatic paneling, monochrome high "
            "contrast, detailed cross-hatching"
        ),
        quality_block=_QUALITY_STYLIZED,
        negative_hint="photorealistic, realistic skin pores, photo",
        post="stylized", base_prefs=("flux2-klein-9b",),
    ),
    StylePack(
        sid="ghibli", label="吉卜力手绘",
        keywords=("吉卜力", "宫崎骏", "手绘动画", "手繪動畫", "ghibli"),
        style_block=(
            "ghibli style hand-painted animation, soft watercolor "
            "backgrounds, gentle natural sunlight, warm nostalgic "
            "atmosphere, hand-drawn linework, lush painted scenery"
        ),
        quality_block=_QUALITY_STYLIZED,
        negative_hint="photorealistic, realistic skin pores, photo",
        post="stylized", base_prefs=("flux2-klein-9b",),
    ),
    StylePack(
        sid="pixel", label="像素风",
        keywords=("像素", "pixel"),
        style_block=(
            "pixel art, 16-bit retro game aesthetic, crisp aligned "
            "pixels, limited color palette, dithering shading, chibi "
            "sprite composition"
        ),
        quality_block=_QUALITY_STYLIZED,
        negative_hint="photorealistic, realistic skin pores, photo",
        post="stylized", base_prefs=("flux2-klein-9b",),
    ),
    StylePack(
        sid="uscartoon", label="美式卡通",
        keywords=("美式卡通", "美式动画", "american cartoon", "cartoon network"),
        style_block=(
            "american cartoon style, flat bold shapes, exaggerated "
            "expressive characters, thick clean outlines, bright "
            "playful colors, Cartoon Network aesthetic"
        ),
        quality_block=_QUALITY_STYLIZED,
        negative_hint="photorealistic, realistic skin pores, photo",
        post="stylized", base_prefs=("flux2-klein-9b",),
    ),
    StylePack(
        sid="steampunk", label="蒸汽朋克",
        keywords=("蒸汽朋克", "蒸气朋克", "steampunk"),
        style_block=(
            "steampunk, victorian brass machinery, gears and clockwork, "
            "copper steam pipes, leather and goggles, sepia warm tones, "
            "retro-futuristic victorian city"
        ),
        quality_block=_QUALITY_REALISTIC, negative_hint=_NEG_REALISTIC,
        post="realistic", base_prefs=("flux2-klein-9b",),
    ),
    StylePack(
        sid="flat", label="扁平插画",
        keywords=("扁平插画", "扁平设计", "扁平风", "flat design", "flat illustration"),
        style_block=(
            "flat design illustration, clean geometric shapes, minimal "
            "vector style, bold color blocks, no gradients, modern "
            "editorial infographic aesthetic"
        ),
        quality_block=_QUALITY_STYLIZED,
        negative_hint="photorealistic, realistic skin pores, photo",
        post="stylized", base_prefs=("flux2-klein-9b",),
    ),
    StylePack(
        sid="claymation", label="黏土定格",
        keywords=("黏土", "粘土", "claymation", "clay stop"),
        style_block=(
            "claymation, stop-motion clay puppet aesthetic, visible "
            "fingerprint texture, handcrafted plasticine surfaces, soft "
            "studio lighting, tilt-shift miniature set"
        ),
        quality_block=_QUALITY_REALISTIC, negative_hint=_NEG_REALISTIC,
        post="realistic", base_prefs=("flux2-klein-9b",),
    ),
    StylePack(
        sid="popart", label="波普复古",
        keywords=("波普", "pop art", "popart", "复古印刷", "复古海报", "retro print", "vintage poster"),
        style_block=(
            "pop art style, retro print poster, halftone dot texture, "
            "bold duotone color scheme, vintage advertisement "
            "typography-free layout, screenprint grain"
        ),
        quality_block=_QUALITY_STYLIZED,
        negative_hint="photorealistic, realistic skin pores, photo",
        post="stylized", base_prefs=("flux2-klein-9b",),
    ),
    StylePack(
        sid="storybook", label="童话绘本",
        keywords=("绘本", "童话", "儿插", "儿童插画", "storybook", "picture book"),
        style_block=(
            "children storybook illustration, soft gouache and crayon "
            "texture, warm pastel palette, cute rounded character "
            "shapes, cozy whimsical composition"
        ),
        quality_block=_QUALITY_STYLIZED,
        negative_hint="photorealistic, realistic skin pores, photo",
        post="stylized", base_prefs=("flux2-klein-9b",),
    ),
    StylePack(
        sid="gothic", label="哥特暗黑",
        keywords=("哥特", "暗黑奇幻", "吸血鬼", "gothic", "dark fantasy", "vampire"),
        style_block=(
            "gothic dark fantasy, baroque shadow architecture, ornate "
            "candelabra lighting, deep crimson and charcoal palette, "
            "dramatic chiaroscuro, victorian occult elegance"
        ),
        quality_block=_QUALITY_REALISTIC, negative_hint=_NEG_REALISTIC,
        post="realistic", base_prefs=("flux2-klein-9b",),
    ),
    StylePack(
        sid="lowpoly", label="低多边形3D",
        keywords=("低多边形", "低模", "low poly", "lowpoly", "low-poly"),
        style_block=(
            "low poly 3D render, faceted geometry, flat shaded "
            "polygons, stylized minimal shapes, clean gradient color "
            "blocking, isometric game asset aesthetic"
        ),
        quality_block=_QUALITY_STYLIZED,
        negative_hint="photorealistic, realistic skin pores, photo",
        post="stylized", base_prefs=("flux2-klein-9b",),
    ),
    StylePack(
        sid="thickpaint", label="厚涂CG玄幻",
        keywords=("厚涂", "厚塗", "impasto"),
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
    # ── 2026-09-02 新增 4 包（市场调研：3D国漫/悬疑/次世代二次元为
    #    爆款赛道空白）。xianxia_cg 必须先于 guofeng3d——「仙侠」判据
    #    在 guofeng3d，复合词（次世代二次元/二次元仙侠）优先归新包。
    StylePack(
        sid="xianxia_cg", label="次世代二次元仙侠CG",
        keywords=("次世代二次元", "二次元仙侠", "二次元古风", "二次元修仙", "二游"),
        ignore_case=False,
        style_block=(
            "next-gen anime game CG, cel-shaded anime character face "
            "with clean face shading and rim light, NPR stylized "
            "character over PBR realistic environment, ancient "
            "Chinese xianxia fantasy, sea of clouds, mountain-top "
            "immortal palaces, flying swords, glowing talismans, "
            "game cinematic quality render"
        ),
        quality_block=_QUALITY_STYLIZED,
        negative_hint=(
            "photorealistic human face, western cartoon, "
            "thick oil paint, watercolor"
        ),
        post="stylized",
        base_prefs=("flux2-klein-9b", "flux2-klein-4b"),
    ),
    StylePack(
        sid="guofeng3d", label="3D国风玄幻",
        keywords=("国风3D", "3D国风", "玄幻", "仙侠", "修仙", "法宝", "法术"),
        ignore_case=False,
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
    # mystery3d 先于 donghua3d：「3D国漫悬疑」类复合词优先归悬疑包
    # （国漫判据在 donghua3d，若其在前会抢走悬疑向文本）。两包均在
    # cg3d 之前——否则「3D」强信号会把 3D国漫/3D悬疑 抢进通用 CG 包。
    StylePack(
        sid="mystery3d", label="3D国漫悬疑暗黑",
        keywords=("国漫悬疑", "悬疑", "谋杀", "凶杀", "凶案", "凶宅", "密室", "推理", "侦探",
                  "murder", "mystery", "detective", "suspense"),
        style_block=(
            "3D Chinese donghua characters, murder mystery atmosphere, "
            "desaturated cold grey-blue palette, dramatic chiaroscuro "
            "lighting, single warm light accent, venetian blind shadows "
            "and backlit silhouettes, rain-soaked night streets, fog, "
            "old mansion crime scene, tense cinematic framing, "
            "film noir grading"
        ),
        quality_block=_QUALITY_REALISTIC,
        negative_hint=(
            "bright cheerful vibrant sunny colors, high saturation, "
            "fantasy glow, magic particles"
        ),
        post="realistic",
        base_prefs=("flux2-klein-9b", "flux2-klein-4b"),
    ),
    StylePack(
        sid="donghua3d", label="3D国漫写实",
        keywords=("国漫", "国产动画", "国创动画", "donghua"),
        style_block=(
            "3D Chinese donghua animation style, realistic grounded "
            "facial modeling with distinct character features, unreal "
            "engine 5 cinematic render, refined wuxia costume and hair "
            "detail, subsurface scattering skin texture, dramatic "
            "volumetric lighting, epic establishing-shot composition, "
            "film-grade muted color grading"
        ),
        quality_block=_QUALITY_REALISTIC,
        negative_hint=(
            "2D flat anime shading, thick outlines, western cartoon, "
            "plastic skin, toy figure"
        ),
        post="realistic",
        base_prefs=("flux2-klein-9b", "flux2-klein-4b"),
    ),
    # guofeng_hist 先于 guofeng2d——裸「古风」归 2D 国风是既有行为，
    # 本包只收显式 3D/写实古风信号（3D古风|古风写实|汉服|历史剧…）
    StylePack(
        sid="guofeng_hist", label="3D古风历史",
        keywords=("3D古风", "古风3D", "古风写实", "写实古风", "古装写实", "历史剧", "正剧", "汉服"),
        ignore_case=False,
        style_block=(
            "ancient Chinese historical 3D render, hanfu costume with "
            "realistic fabric texture, palace halls and wooden "
            "interiors, warm candlelight and soft daylight, muted "
            "elegant palette of ivory vermilion and ink black, movie "
            "poster composition, unreal engine 5 cinematic, photoreal "
            "materials, grounded realism"
        ),
        quality_block=_QUALITY_REALISTIC,
        negative_hint=(
            "glowing magic effects, fantasy particles, spiritual "
            "energy aura, neon, supernatural light"
        ),
        post="realistic",
        base_prefs=("flux2-klein-9b", "flux2-klein-4b"),
    ),
    StylePack(
        sid="guofeng2d", label="国风2D",
        keywords=("水墨淡彩", "国风", "工笔", "重彩", "古风", "古韵", "东方美学"),
        ignore_case=False,
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
        keywords=("韩漫", "韩系", "manhwa", "webtoon", "半写实"),
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
        keywords=("3D", "PBR", "建模", "CG", "渲染"),
        ignore_case=False,
        style_block=_STYLE_BOOST_3D,
        quality_block=_QUALITY_REALISTIC,
        negative_hint=_NEG_REALISTIC,
        post="realistic",
        base_prefs=("flux2-klein-9b",),
    ),
    StylePack(
        sid="photoreal", label="写实摄影",
        keywords=("写实", "照片", "摄影", "实拍", "纪实", "photoreal", "realistic"),
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
        keywords=("二次元", "动漫", "动画", "卡通", "赛璐璐", "立绘", "anime", "manga", "celshad"),
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
        keywords=("热血", "战斗", "燃系", "打斗", "shonen"),
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
        keywords=("水彩", "水粉", "淡彩", "watercolor", "gouache"),
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
        keywords=("油画", "厚涂", "古典绘", "oil paint", "oilpaint", "oil-paint", "impasto"),
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
        keywords=("水墨", "丹青", "国画", "写意", "山水", "留白", "ink wash", "inkwash", "ink-wash", "sumi"),
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
        keywords=("赛博", "朋克", "霓虹", "科幻", "未来", "机甲", "cyberpunk", "sci-fi", "sci fi", "scifi", "neon"),
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
    style_block="",
    quality_block=_QUALITY_REALISTIC,
    negative_hint=_NEG_REALISTIC,
    post="realistic",
    base_prefs=("flux2-klein-9b",),
)

# ── 卡片↔包显式绑定注册表（2026-08-31 用户裁定）──
# art_styles.pack 存这里的 sid；resolve_route(pack_id=...) 直取。
_PACK_BY_SID: dict[str, StylePack] = {p.sid: p for p in (*STYLE_PACKS, DEFAULT_PACK)}

# ── 包优先级表（2026-09-02 词序坑治理第二步：顺序从「列表物理位置」
#    升格为声明数据）。detect_style/explain_style 按 priority 升序
#    遍历；最长关键词平局时数值小者胜。数值=历史列表序×10，行为与
#    首序匹配时代完全一致（金标准钉死）。新增包必须在此登记，漏登记
#    会被 test_style_pack_routing 拦下；改值前先跑金标准。──
PACK_PRIORITY: dict[str, int] = {
    "comic_en": 10, "manga_bw": 20, "ghibli": 30, "pixel": 40,
    "uscartoon": 50, "steampunk": 60, "flat": 70, "claymation": 80,
    "popart": 90, "storybook": 100, "gothic": 110, "lowpoly": 120,
    "thickpaint": 130,
    # 复合词包平局压制碎片词包的既有关系（金标准快照口径）：
    # xianxia_cg「次世代二次元」压 guofeng3d「仙侠」；mystery3d
    # 「国漫悬疑」压 donghua3d「国漫」；guofeng_hist「3D古风」压
    # guofeng2d 裸「古风」；三张 3D 系包共同压 cg3d 裸「3D」。
    "xianxia_cg": 140, "guofeng3d": 150,
    "mystery3d": 160, "donghua3d": 170, "guofeng_hist": 180,
    "guofeng2d": 190, "manhwa": 200, "cg3d": 210, "photoreal": 220,
    "anime": 230, "battle": 240, "watercolor": 250, "oilpaint": 260,
    "inkwash": 270, "cyberpunk": 280,
}
assert set(PACK_PRIORITY) == {p.sid for p in STYLE_PACKS}, "PACK_PRIORITY 与 STYLE_PACKS 不一致"

# 按优先级排序的遍历序（模块加载时一次算好；平局裁决依赖此序稳定）
_SORTED_PACKS: tuple[StylePack, ...] = tuple(
    sorted(STYLE_PACKS, key=lambda p: PACK_PRIORITY[p.sid]))

# 自定义风格包 JSON 必填/可选字段（导入校验口径，comic.py 创建共用）
_CUSTOM_PACK_POSTS = ("realistic", "stylized")


def parse_custom_pack(raw: str) -> tuple[StylePack | None, str]:
    """解析自定义风格包 JSON（用户导入文件原文）。

    返回 (StylePack, "") 或 (None, 错误原因)。校验口径：
      - 必须是合法 JSON 对象；
      - name：1~30 字；
      - style_block：英文风格词块，1~400 字符（注入 A 段）；
      - post：realistic | stylized（后处理档位仅两档）；
      - base_prefs：字符串数组，仅保留含 "klein" 的底座（与 family
        锁 klein 一致），空/全被滤 → 回退 ("flux2-klein-9b",)；
      - quality_block / negative_hint：可选，≤200 字符。
    """
    import json as _json

    try:
        data = _json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - JSON 语法错误
        return None, f"不是合法 JSON：{exc}"
    if not isinstance(data, dict):
        return None, "顶层必须是 JSON 对象"
    name = str(data.get("name", "")).strip()
    if not (1 <= len(name) <= 30):
        return None, "name 必填（1~30 字）"
    style_block = str(data.get("style_block", "")).strip()
    if not (1 <= len(style_block) <= 400):
        return None, "style_block 必填（英文风格词块，1~400 字符）"
    post = str(data.get("post", "")).strip()
    if post not in _CUSTOM_PACK_POSTS:
        return None, f"post 必须是 {'/'.join(_CUSTOM_PACK_POSTS)} 之一"
    prefs_raw = data.get("base_prefs")
    prefs = ([str(m).strip() for m in prefs_raw if str(m).strip()]
             if isinstance(prefs_raw, list) else [])
    prefs = [m for m in prefs if "klein" in m] or ["flux2-klein-9b"]
    quality = str(data.get("quality_block", "") or _QUALITY_REALISTIC)[:200]
    negative = str(data.get("negative_hint", "") or _NEG_REALISTIC)[:200]
    import hashlib as _h
    sid = "custom:" + _h.md5(raw.encode("utf-8", "replace")).hexdigest()[:8]
    return StylePack(
        sid=sid, label=name,
        style_block=style_block, quality_block=quality,
        negative_hint=negative, post=post, base_prefs=tuple(prefs),
    ), ""  # keywords 空=显式绑定专用，不参与嗅探


def _custom_pack(def_dict: dict) -> StylePack | None:
    """pack_def dict → StylePack（解析失败返回 None 走嗅探兜底）。"""
    import json as _json

    pack, _err = parse_custom_pack(_json.dumps(def_dict, ensure_ascii=False))
    return pack


def detect_style(text: str) -> StylePack:
    """文本（A 段/提示词）→ 命中的风格包；未命中返回 DEFAULT_PACK。

    2026-09-02 裁决规则（两段式，词法 maximal munch + CSS 优先级）：
    ① 同位遮蔽：某包命中词的跨度被别的包更长命中词完全覆盖时淘汰
      ——「国漫」被「国漫悬疑」覆盖即属此类，复合词结构性压过碎片
      词，与包优先级无关（词序坑的根治点）。注意只比同位置重叠：
      不同位置的词不做长度比较（首版纯长度比较让英文词 webtoon≈7
      字压过中文词厚涂=2 字，金标准对账抓出 32 条漂移后废弃）。
    ② 优先级裁决：幸存者按 PACK_PRIORITY 小者胜（声明数据）。
    与旧「首序匹配」等价性由 test_style_pack_routing 金标准钉死。
    """
    if not text:
        return DEFAULT_PACK
    hits: list[tuple[int, StylePack, int, int]] = []  # (priority, pack, start, len)
    for pack in _SORTED_PACKS:
        start, n, _kw = pack._hit(text)
        if n:
            hits.append((PACK_PRIORITY[pack.sid], pack, start, n))
    if not hits:
        return DEFAULT_PACK
    alive = [
        (pri, pack, start, n)
        for i, (pri, pack, start, n) in enumerate(hits)
        if not any(
            other_n > n and other_s <= start and start + n <= other_s + other_n
            for j, (other_pri, other_p, other_s, other_n) in enumerate(hits)
            if j != i)
    ]
    alive.sort(key=lambda x: x[0])
    return alive[0][1]


def explain_style(text: str) -> tuple[StylePack, str]:
    """detect_style 的可解释版：返回 (风格包, 裁决凭据)。

    供日志/排障用——每次嗅探路由都能回答「谁赢了、凭什么赢」
    （OPA 决策日志思路），不再依赖脑内推演词序。
    """
    if not text:
        return DEFAULT_PACK, "空文本→默认包"
    hits: list[tuple[int, StylePack, int, int]] = []
    for pack in _SORTED_PACKS:
        start, n, _kw = pack._hit(text)
        if n:
            hits.append((PACK_PRIORITY[pack.sid], pack, start, n))
    if not hits:
        return DEFAULT_PACK, "无关键词命中→默认包"
    notes: list[str] = []
    alive: list[tuple[int, StylePack]] = []
    for i, (pri, pack, start, n) in enumerate(hits):
        shadow = next((
            (other_p, other_n) for j, (other_pri, other_p, other_s, other_n)
            in enumerate(hits)
            if j != i and other_n > n and other_s <= start
            and start + n <= other_s + other_n), None)
        if shadow is None:
            alive.append((pri, pack))
        else:
            notes.append(f"{pack.label}「{pack.hit_keyword(text)}」被"
                         f"{shadow[0].label}更长词遮蔽")
    alive.sort(key=lambda x: x[0])
    win = alive[0][1]
    note = f"命中「{win.hit_keyword(text)}」({win.label})"
    if notes:
        note += "；" + "；".join(notes)
    return win, note


@dataclass(frozen=True)
class RouteDecision:
    """路由决策：底座 + 风格包（插件）组合 + 决策依据（日志可溯）。"""
    model_id: str            # 选定的首选底座
    style_pack: StylePack    # 风格包（插件）侧
    base_chain: tuple[str, ...] = field(default_factory=tuple)  # 降级链
    reason: str = ""         # 决策原因（日志 detail 字段）
    switched_from: str = ""  # 之前驻留底座（保持不换载时记录）


# qwen-image-2512 GGUF 权重常驻 RAM ~12.31GB + 推理开销 ~3GB
#（2026-08-23 OOM 死亡事故实测边界）——可用 RAM 低于此线跳过。
# 2026-09-09 V9 尾款①：值搬至 services/vram_policy.py（对拍锁定），
# 此处以别名 import 保持既有符号名。


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
    pack_id: str = "",
    pack_def: dict | None = None,
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
        pack_id: 显式风格包 id（2026-08-31 卡片↔包显式绑定）：命中
            _PACK_BY_SID 时覆写正则嗅探——用户选「韩漫半写实」这类
            大族卡时底座/后处理确定切换，不再受 A 段措辞干扰
        pack_def: 自定义风格包定义（导入 JSON 解析后的 dict；优先级
            最高）——自定义风格创建时强制导入，见 parse_custom_pack

    Returns:
        RouteDecision——model_id 永不为空（全链路失败时 klein-9b 兜底，
        由调用方 ensure_loaded 落实加载成败的最终降级）。
    """
    if pack_def is not None:
        pack = _custom_pack(pack_def) or detect_style(prompt)
    elif pack_id and pack_id in _PACK_BY_SID:
        pack = _PACK_BY_SID[pack_id]
        # 显式包与嗅探结果不一致时以显式为准（日志可溯）
    else:
        pack = detect_style(prompt)

    # ① 底座偏好序（族过滤：关键帧锁 klein 家族）
    prefs = list(pack.base_prefs)
    if family == "flux":
        prefs = [m for m in prefs if "klein" in m] or ["flux2-klein-9b"]

    # ② 模块级选型配置白名单（管理员管控最高优先）+ 用户默认提权
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
        # 用户默认模型提权（2026-08-31 模型配置接线）：manga-paint 槽的
        # default 是用户在漫剧「模型配置」弹窗选的绘画模型——此前只读
        # 白名单、忽略 default，用户选 4b/9b 不生效；在偏好序内则提到
        # 首位（不在偏好序/不可用则按原序，磁盘过滤仍兜底）
        if _default and _default in prefs and prefs[0] != _default:
            prefs = [_default] + [m for m in prefs if m != _default]
            scope_note = (scope_note + f"；用户默认提权 → {_default}").lstrip("；")
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
