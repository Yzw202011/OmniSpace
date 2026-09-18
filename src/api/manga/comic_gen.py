"""漫画资产生成管线层：三视图转四视图 / 批量生成 / 重生成管线（无路由，由 comic_asset 路由调用）。

TASK-P2-01 自 manga.py 按路由域拆出（原文件 4521 行 → 包）。
"""
from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ...config import (
    DATA_DIR,
)
from ...data.database import get_db_safe, parse_json
from ...data.models import (
    AssetTurnaroundRequest,
)
from ...middleware.error_handler import ApiError
from ...services.inference.paint_engine import get_paint_engine
from ...services.inference.prompt_translator import translate_prompt_zh2en
from .common import (
    _ASSET_KIND_CONF,
    _COMIC_ASSET_DIR,
    _STYLE_NEGATIVE,
    _STYLE_PHOTO,
    _STYLE_WHITE_BG,
    IMG_TARGET_H,
    IMG_TARGET_W,
    _find_character_asset_stub,
    _flux_asset_params,
    _gen_size_for_target,
    _now,
    _remove_background,
    _upscale_to,
    broadcast_gen_progress,
    comfy_paint_generate,
    unload_paint_engines_sync,
)

if TYPE_CHECKING:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    from ...services.inference.paint_engine import PaintEngine

log = logging.getLogger("omnispace.api.manga.comic_gen")


class _ComfyGenAdapter:
    """legacy 引擎形状的 comfy klein 适配（W3-C 步3，2026-09-13）。

    generate/img2img 同参转发 comfy_paint_generate；progress_cb 接受但
    忽略（ComfyUI /history 轮询无步级粒度，粗粒度进度由调用方在提交/
    完成边界广播）。ensure_loaded 恒真（comfy_proc 常驻语义）。
    步数重映射：legacy klein-4b 的 20+ 步口径在 comfy 蒸馏 9B 上无
    意义且过慢——≥20 步一律压到 8 步/cfg1.0（Turbo LoRA 有效区间
    4~8 上沿），质量由既有 VL 视角校验关兜底。
    """

    is_ready = True
    is_loaded = False  # 常驻态由 comfy_proc 管理，无驻留可卸

    def ensure_loaded(self, model_id: str | None = None) -> bool:
        return True

    def unload_model(self) -> bool:
        from ...services.inference.comfy_paint_engine import (
            get_comfy_paint_engine,
        )
        get_comfy_paint_engine().unload()
        return True

    def get_status(self) -> dict:
        return {"state": "ready", "loaded": True, "model": "comfy-klein-9b"}

    @staticmethod
    def _remap(params: dict) -> dict:
        p = dict(params)
        if int(p.get("steps") or 0) >= 20:
            p["steps"] = 8
            p["cfg"] = 1.0
        p.pop("mask_margin", None)
        return p

    def generate(self, params: dict,
                 progress_cb=None) -> dict:  # noqa: ARG002 - 粗粒度进度
        return comfy_paint_generate(self._remap(params))

    def img2img(self, params: dict, ref_image,
                progress_cb=None) -> dict:  # noqa: ARG002
        return comfy_paint_generate(self._remap(params), ref_image=ref_image)

    def inpaint(self, params: dict, image, mask) -> dict:
        """潜空间修复转发（W3-C 步4-c 2026-09-13：comfy 侧 inpaint
        已落地——SetLatentNoiseMask+ReferenceLatent+软边回贴，A/B
        未遮罩区保留与 legacy 同级 3321 vs 6159 差异像素）。"""
        from ...services.inference.comfy_paint_engine import (
            get_comfy_paint_engine,
        )
        return get_comfy_paint_engine().inpaint(
            self._remap(params), image, mask)


def _pick_turnaround_engine():
    """四视图引擎档位选择（W3-C）。

    ⚠️ 与全局 paint.gen_engine **刻意解耦**：独立读
    manga.turnaround_engine（默认 legacy）。实测依据（2026-09-13
    首过率复验，tools/scratch/w3c_firstpass.py）：comfy 蒸馏 9B 在
    2560×1440 四格构图上格数遵循度 33~40% 且与步数无关
    （8步2/5、12步0/3、20步1/3、28步1/3），丢格为构图级失败、
    VL 修复环不可修（其修复逻辑假设四格齐在）——legacy 4B 构图
    完整率 ~100%（仅格内视角偶错，可单格 inpaint 修复）。在 comfy
    侧多格构图方案改进（独立格生成+拼格 / 区域条件）验证前，
    本开关保持 legacy；全局 gen_engine 切 comfy 不影响此处。
    """
    try:
        from ...config import get_config
        _v = str((get_config().get("manga") or {}).get(
            "turnaround_engine", "legacy")).strip().lower()
        if _v == "comfy":
            return _ComfyGenAdapter()
    except Exception:  # noqa: BLE001 - 配置异常按 legacy
        log.debug("_pick_turnaround_engine: 降级忽略", exc_info=True)
    return get_paint_engine()



# ── 角色多视图（四视图）生成（COMIC-033~037）────────────────────────
# 规格（对齐根目录 参考图.png，2026-08-20 裁定）：整图资产统一
# 2560×1440（16:9，1 行 4 列等宽竖格，每格 640×1440）；one-pass 主路径
# 一次推理整图直出；legacy 逐视图为四张独立 2560×1440 图（每张可单独
# 重生），canvas.png 按 1×4 横排 contain 拼图。FLUX.2 未随包时 SDXL
# 兜底并诚实标注 degraded。

_TURNAROUND_VIEWS = ("front", "side", "back", "closeup")
# 视图中文标签（进度条 label 显示用）
_VIEW_ZH_LABELS = {"front": "正面", "side": "侧面",
                   "back": "背面", "closeup": "特写"}
_TURNAROUND_W, _TURNAROUND_H = IMG_TARGET_W, IMG_TARGET_H  # 2560×1440
_TURNAROUND_GEN_W, _TURNAROUND_GEN_H = _gen_size_for_target(
    _TURNAROUND_W, _TURNAROUND_H)  # 1280×720 生成 + 2x 上采样

# 视图后缀映射：逐视图独立生成时的构图指令（替代旧整版式模板，
# 每张只画单人单视图，杜绝 4 宫格串图）。
_TURNAROUND_VIEW_SUFFIX = {
    "front": ", character reference sheet, front view full body, "
             "single person",
    "side": ", character reference sheet, side view full body, "
            "single person",
    "back": ", character reference sheet, back view full body, "
            "single person",
    "closeup": ", upper body close-up portrait, single person",
}

# ── one-pass 单图四视图（2026-08-20 竞品对齐重构）─────────────────
# 底座 flux2-klein-4b（Qwen3 中文文本编码器，512 token 上限）：中文
# 提示词全文直入，一次推理在单图内出四视图——竞品同款技术路线。
# 画幅 2560×1440（16:9，对齐根目录 参考图.png 版式：1 行 4 列等宽
# 竖格横排，每格 640×1440 全身竖构图；宽高均 8 的倍数满足 VAE 下采样
# 对齐；3.69MP 在 FLUX.2 的 4MP 上限内）。
_ONEPASS_W, _ONEPASS_H = 2560, 1440
_ONEPASS_MAX_ATTEMPTS = 5   # 三关闸门（视角/纯白/相符）换 seed 重 roll 上限
_ONEPASS_STEPS = 28          # FLUX.2 Klein distilled 推荐步数量级
_ONEPASS_GUIDANCE = 4.0      # 引擎默认 guidance

# 视图标签文案（竞品逐字对齐，PIL 叠加用）
_ONEPASS_VIEW_LABELS = {
    "front": "正面全身",
    "side": "侧面全身",
    "back": "背面全身",
    "closeup": "上半身特写",
}

# 中文字体候选（Windows 系统字体，按优先级回退）
_ZH_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
)


def _load_zh_font(size: int) -> ImageFont.FreeTypeFont | None:
    """加载中文字体（按候选路径回退；全失败返回 None 降级跳过标注）。"""
    from PIL import ImageFont
    for path in _ZH_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return None


def _sam_whiten(image: Image, bg: np.ndarray) -> Image | None:
    """SAM 人物分割背景漂白（主路径，2026-08-20 接线本地 sam-vit-h）。

    四视图整图逐格（1/4 画宽）多点联合提示（头/胸/腰/腿，覆盖整
    个人）→ SAM 人物精确 mask（发丝级，米白 T 恤/浅蓝牛仔裤完整
    保留）→ 成功格内反选背景一次性置纯白（灰渐变/脚边阴影/灰斑
    全清）。

    三重 mask 校验（2026-08-20 目视实测教训）：①裁回本格列范围
    （多点可能跨格吃到邻格人物）②提示点必须全部落在 mask 内
    （防「背景环」假 mask 挖空人物）③score≥0.5 且面积 2%~30%。
    校验失败的格保持原样不动（宁留灰底，不挖空人物）；全部失败
    返回 None 回退启发式。SAM 推理后立即卸载释放显存（2.5GB）。
    """
    import base64
    import io

    import numpy as np
    from PIL import Image

    arr = np.asarray(image.convert("RGB"), dtype=np.uint8)
    h, w = arr.shape[:2]
    fg_mask = np.zeros((h, w), dtype=bool)
    ok_zone = np.zeros((h, w), dtype=bool)  # 分割成功格的列范围
    got_any = False
    try:
        from src.services.inference.segment_engine import get_segment_engine
        seg = get_segment_engine()
        if not seg.load_model():
            log.warning("SAM 加载失败，背景漂白回退启发式: %s",
                        seg.unavailable_reason)
            return None
        try:
            cell_w = w // 4
            ys = (int(h * 0.12), int(h * 0.30), int(h * 0.50),
                  int(h * 0.75))
            def _check_mask(m: np.ndarray, score: float,
                            probe_pts: list[list[int]]) -> bool:
                """三重校验：点包含 + 分数 + 面积。通过 True。"""
                area = float(m.mean())
                pts_in = all(m[min(y, h - 1), min(px, w - 1)]
                             for px, y in probe_pts)
                if score >= 0.5 and pts_in and 0.02 < area < 0.30:
                    return True
                log.warning("格 %d SAM 结果异常弃用 "
                            "(score=%.3f pts_in=%s area=%.1f%%)",
                            idx, score, pts_in, area * 100)
                return False

            def _mask_of(r: dict) -> np.ndarray:
                m_img = Image.open(io.BytesIO(
                    base64.b64decode(r["mask_png_b64"]))).convert("L")
                m = np.asarray(m_img.resize((w, h))) > 127
                m[:, :idx * cell_w] = False
                m[:, (idx + 1) * cell_w:] = False
                return m

            for idx in range(4):
                cx = idx * cell_w + cell_w // 2
                pts = [[cx, y] for y in ys]
                try:
                    # ① 多点联合（头/胸/腰/腿）
                    r = seg.segment(image, points=pts)
                    m = _mask_of(r)
                    if not _check_mask(m, r["score"], pts):
                        # ② 腰部单点（瘦人物多点易给「背景环」假mask）
                        m = None
                        r = seg.segment(image, points=[[cx, int(h * 0.40)]])
                        m2 = _mask_of(r)
                        if _check_mask(m2, r["score"], [[cx, int(h * 0.40)]]):
                            m = m2
                    if m is None:
                        # ③ 整格包围盒（瘦长侧面人物最稳的提示方式）
                        r = seg.segment(image, box=[
                            idx * cell_w + 40, int(h * 0.03),
                            (idx + 1) * cell_w - 40, int(h * 0.97)])
                        m3 = _mask_of(r)
                        probe = [[cx, int(h * 0.40)], [cx, int(h * 0.10)]]
                        if _check_mask(m3, r["score"], probe):
                            m = m3
                    if m is not None:
                        fg_mask |= m
                        ok_zone[:, idx * cell_w:(idx + 1) * cell_w] = True
                        got_any = True
                except Exception as exc:  # noqa: BLE001 - 单格失败不阻塞
                    log.warning("格 %d SAM 分割失败: %s", idx, exc, exc_info=True)
        finally:
            seg.unload_model()
    except Exception as exc:  # noqa: BLE001 - SAM 不可用保人物原样
        log.warning("SAM 分割不可用，背景保持原样: %s", exc, exc_info=True)
        return None
    if not got_any:
        return None
    out = arr.copy()
    # 仅成功格内置白：失败格原样保留（防止假 mask 挖空人物）
    out[ok_zone & ~fg_mask] = 255
    return Image.fromarray(out)


def _whiten_background(image: Image) -> Image:
    """背景漂白（交付格式保底，对齐 参考图.png 纯白底）。

    FLUX.2 Klein 对「纯白背景」遵循不稳定（2026-08-20 实测角点
    RGB≈(207,221) 灰底）——与中文标注同哲学：交付格式不交给概率
    模型，代码确定性完成。

    编排：边框采样背景色（中位数），本身 ≥250 零改动 → SAM 人物
    分割精确漂白 → SAM 显存不足时卸载绘画管线（FLUX.2 ~13GB +
    SAM 2.5GB > 16GB）重试一次 → 仍失败保持原图（人物完整优先
    于背景纯白，启发式漂白已实测腐蚀浅色衣物故弃用）。
    """
    import numpy as np
    from PIL import Image
    arr = np.asarray(image.convert("RGB"), dtype=np.float32)
    border = np.concatenate([
        arr[:2].reshape(-1, 3), arr[-2:].reshape(-1, 3),
        arr[:, :2].reshape(-1, 3), arr[:, -2:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    if float(bg.min()) >= 250.0:
        return Image.fromarray(arr.astype(np.uint8))

    out = _sam_whiten(image, bg)
    if out is not None:
        return out
    # SAM 直接失败常见于显存被绘画管线占满（FLUX.2 ~13GB + SAM
    # 2.5GB > 16GB）：卸载绘画管线腾显存重试一次（下次生成为ensure_
    # loaded 语义，自动重载）
    try:
        # W3-C：comfy 与 legacy 双栈都可能驻留显存，让位=两代同卸
        unload_paint_engines_sync()
        out = _sam_whiten(image, bg)
        if out is not None:
            return out
    except Exception as exc:  # noqa: BLE001 - 卸载失败保持原图
        log.warning("绘画管线卸载重试 SAM 失败: %s", exc, exc_info=True)
    log.warning("背景漂白未生效，保持原图（人物完整优先于背景纯白）")
    return Image.fromarray(arr.astype(np.uint8))


def _verify_view_layout(image: Image) -> tuple[bool | None, list[str]]:
    """VL 视角组合校验（本地 qwen3-vl 多模态，2026-08-20 接线）。

    修复「视图与标签不对应」：one-pass 是概率模型整图直出，四格
    视角组合有抽卡率（实测第2格画背面/第3格残缺等）。校验器把
    缩略图交给 VL 模型逐格判视角，返回 (是否全对, 逐格判定list)。

    返回 (True, labels)=组合正确 / (False, labels)=错位（labels
    供调用方定位错格做局部修复）/ (None, [])=VL 不可用（跳过校验，
    不阻塞交付）。显存协调：校验前卸载绘画管线（FLUX.2 ~13GB 与
    VL 4B ~9GB 互斥）；修复格/重生时 ensure_loaded 自动重载。
    """
    import re
    try:
        from ...services.inference.dialog_engine import get_dialog_engine
        eng = get_dialog_engine()
        # FLUX.2 让位 VL（16GB 显存互斥；W3-C 双栈同卸）
        unload_paint_engines_sync()
        if not eng.is_ready and not eng.ensure_loaded("qwen3-vl-4b"):
            log.warning("VL 模型不可用，跳过视角校验: %s",
                        eng.get_status().get("last_error", ""))
            return None, []
        thumb = image.copy()
        thumb.thumbnail((1280, 720))  # 省 prefill，判视角足够
        msgs = [{"role": "user", "content": [
            {"type": "image"},
            {"type": "text", "text": (
                "这张图从左到右有4格，每格应是一个人物的视图。"
                "请逐格严格判定视角类型，裁决规则：\n"
                "1. 正面：身体完全正对镜头，两只眼睛都可见，"
                "双肩左右对称；\n"
                "2. 侧面：身体斜向一侧约45度、只能看到一只眼睛"
                "或明显侧脸轮廓的，也算侧面（禁止判为正面）；"
                "正侧身90度更是侧面；\n"
                "3. 背面：完全背对镜头，看不到任何面部五官；"
                "若能看到侧脸或一只眼睛，判 异常（斜背身不合格）；\n"
                "4. 特写：胸部以上近景、面部大而清晰；若构图"
                "到达腰部以下或全身可见，判 异常；\n"
                "5. 该格是剪影、空白轮廓、残缺人物或没有人物，"
                "判 异常。\n"
                "四格视角必须互不相同：若两格都是侧面或视角"
                "重复，相应格判 异常。\n"
                "只输出4个判定词，用逗号分隔。")},
        ]}]
        reply = eng.chat(msgs, images=[thumb], temperature=0.1,
                         max_new_tokens=24).strip()
        found = re.findall(r"正面|侧面|背面|特写|异常", reply)
        if len(found) < 4:
            log.warning("VL 视角校验答案不可解析（%r），跳过", reply)
            return None, []
        labels = found[:4]
        ok = labels == ["正面", "侧面", "背面", "特写"]
        log.info("VL 视角校验: %s → %s", "/".join(labels),
                 "正确" if ok else "错位")
        return ok, labels
    except Exception as exc:  # noqa: BLE001 - 校验失败不阻塞交付
        log.warning("VL 视角校验异常（跳过）: %s", exc, exc_info=True)
        return None, []


def _verify_background_white(image: Image) -> bool:
    """交付前背景纯白硬校验（2026-08-20 三关闸门·关2）。

    像素级确定性判定（不依赖概率模型）：边框采样（上下各 2 行 +
    左右各 2 列）中位数 RGB 三通道均 ≥245 才算纯白。漂白
    （_whiten_background）后仍不达标（SAM 分割失败保持原图的灰底）
    → 判不合格，触发换 seed 重 roll，不交付。

    2026-09-02 按格判定（小林实拍修复）：四视图为 1×4 横排拼图，
    「部分格有场景背景」时背景在图**内部**而非整图边框上——整图
    边框中位数被白格拉白误判通过（bg_verified=True 但右两格是
    便利店场景）。现按格独立采样边框，任一格不白即整体不通过。
    非标准宽度（单视图 legacy）退回整图边框判定。
    """
    import numpy as np
    arr = np.asarray(image.convert("RGB"), dtype=np.float32)
    h, w = arr.shape[:2]
    # 1×4 横排拼图（宽 ≈ 高 × 16/9 且可整除 4）按格判定
    if w >= h * 3 and w % 4 == 0:
        cell_w = w // 4
        for k in range(4):
            cell = arr[:, k * cell_w:(k + 1) * cell_w]
            border = np.concatenate([
                cell[:2].reshape(-1, 3), cell[-2:].reshape(-1, 3),
                cell[:, :2].reshape(-1, 3), cell[:, -2:].reshape(-1, 3)])
            med = np.median(border, axis=0)
            if med.min() < 245.0:
                log.info("背景纯白校验未过（第 %d 格）: 边框中位数 RGB=%s",
                         k + 1, [int(v) for v in med])
                return False
        return True
    border = np.concatenate([
        arr[:2].reshape(-1, 3), arr[-2:].reshape(-1, 3),
        arr[:, :2].reshape(-1, 3), arr[:, -2:].reshape(-1, 3)])
    med = np.median(border, axis=0)
    ok = bool(med.min() >= 245.0)
    if not ok:
        log.info("背景纯白校验未过: 边框中位数 RGB=%s",
                 [int(v) for v in med])
    return ok


def _verify_prompt_match(image: Image, desc_zh: str) -> bool | None:
    """VL 图文符合度校验（2026-08-20 三关闸门·关3）。

    判断图中人物外观（发型发色/脸型/服装款式与颜色/鞋子等主要
    特征）与角色设定描述词是否大体相符——不要求逐字逐句，防 VL
    对细粒度文本过度苛刻导致无限重 roll。

    返回 True=相符 / False=不符（触发重 roll）/ None=VL 不可用
    或答案不可解析（跳过，不阻塞交付——与视角校验同哲学：VL 尽
    力校验，像素校验（关2）才是硬闸）。显存协调：校验前卸载绘画
    管线（FLUX.2 与 VL 16GB 互斥）。
    """
    desc = (desc_zh or "").strip()[:300]  # Qwen3 编码器 512 token 内
    if not desc:
        return None
    try:
        from ...services.inference.dialog_engine import get_dialog_engine
        eng = get_dialog_engine()
        # FLUX.2 让位 VL（16GB 显存互斥；W3-C 双栈同卸）
        unload_paint_engines_sync()
        if not eng.is_ready and not eng.ensure_loaded("qwen3-vl-4b"):
            log.warning("VL 模型不可用，跳过图文符合度校验: %s",
                        eng.get_status().get("last_error", ""))
            return None
        thumb = image.copy()
        thumb.thumbnail((1280, 720))
        msgs = [{"role": "user", "content": [
            {"type": "image"},
            {"type": "text", "text": (
                "判断图中人物的外观是否与以下角色描述相符"
                "（发型发色、脸型、服装款式与颜色、鞋子等主要特征"
                "大体一致即算相符，不要求逐字逐句）。"
                "只回答两个词之一：相符 或 不相符。\n"
                f"角色描述：{desc}")},
        ]}]
        reply = eng.chat(msgs, images=[thumb], temperature=0.1,
                         max_new_tokens=8).strip()
        if "不相符" in reply:
            log.info("VL 图文符合度校验: 不相符（%r）", reply[:40])
            return False
        if "相符" in reply:
            return True
        log.warning("VL 图文符合度答案不可解析（%r），跳过", reply[:40])
        return None
    except Exception as exc:  # noqa: BLE001 - VL 失败不阻塞交付
        log.warning("VL 图文符合度校验异常（跳过）: %s", exc, exc_info=True)
        return None


def _repair_view_cell(engine: PaintEngine, image: Image, idx: int,
                      prompt_zh_clean: str, seed: int) -> Image:
    """错位格局部修复（VL 校验定位 → 单格 FLUX.2 inpaint，2026-08-20）。

    整图重生换 seed 是「推倒重来」——实测 6 连抽每次恰好只错 1 格。
    本函数只重绘错格（整格 mask + margin 8 不越格污染邻格），其余
    三格逐像素保留，收敛性远优于整图重 roll。显存前置：卸载 VL
    （校验时占位）。返回修复后整图（失败返回原图）。

    W3-C（2026-09-13）：修复引擎跟随四视图档位
    （manga.turnaround_engine）——comfy 潜空间 inpaint 已落地并
    A/B 达标（SetLatentNoiseMask+ReferenceLatent+软边回贴，未遮罩
    区保留 3321 vs legacy 6159 差异像素，机制=真潜空间修复优于
    legacy 的 bbox 裁剪 img2img 降级）；四视图默认 legacy 故修复
    默认同为 legacy。
    """
    import random

    from PIL import Image, ImageDraw

    engine = _pick_turnaround_engine()  # W3-C：跟随四视图档位
    view = _TURNAROUND_VIEWS[idx]
    label = _ONEPASS_VIEW_LABELS[view]
    # 逐格正度约束（2026-08-24 用户实测：整图修复后仍有斜背身/
    # 3/4 侧身混入——修复格同样需要量化锚点）
    precision = {
        "front": "身体完全正对镜头呈0度，双肩左右对称，"
                 "两只眼睛、完整面部清晰可见，从头到脚完整",
        "side": "身体正侧对镜头呈90度，双肩重叠成一条线，"
                "只见单侧脸部轮廓与单只眼睛，从头到脚完整",
        "back": "身体完全背对镜头呈180度，双肩左右对称，"
                "看不到任何面部五官，只见后脑勺与背影，"
                "从头到脚完整",
        "closeup": "胸部以上近景构图，头部占画面高度约三分之"
                   "一，面部大而清晰",
    }[view]
    prompt = (
        f"角色设定图。角色：{prompt_zh_clean}。"
        f"画面：同一角色的{label}视图，单人。构图要求：{precision}。"
        "背景：纯白色，均匀干净。"
        "美术风格：韩国网漫风，干净线稿，清晰上色，"
        "表情表现力强，色彩干净明快。"
        "姿态：常态平静表情，眼睛平视，空手，画面干净无文字。"
    )
    try:
        # VL 让位 FLUX.2（16GB 互斥）
        from ...services.inference.dialog_engine import get_dialog_engine
        eng = get_dialog_engine()
        if eng.is_ready:
            eng.unload_model()
        if not engine.ensure_loaded("flux2-klein-4b"):
            log.warning("格修复失败：FLUX.2 不可用，保持原格")
            return image
        W, H = image.size
        cell_w = W // 4
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).rectangle(
            [idx * cell_w, 0, (idx + 1) * cell_w - 1, H - 1], fill=255)
        if seed is None or seed < 0:
            seed = random.randint(0, 2 ** 31 - 1)
        params = {"prompt": prompt, "steps": _ONEPASS_STEPS,
                  "cfg": _ONEPASS_GUIDANCE, "seed": seed, "mask_margin": 8}
        res = engine.inpaint(params, image, mask)
        fixed = res["images"][0].convert("RGB")
        if fixed.size != (W, H):
            fixed = fixed.resize((W, H), Image.LANCZOS)
        log.info("格 %d（%s）局部修复完成", idx, view)
        return fixed
    except Exception as exc:  # noqa: BLE001 - 修复失败保原图不阻塞
        log.warning("格 %d 局部修复失败（保持原格）: %s", idx, exc, exc_info=True)
        return image


# 剧情态场景信号词（2026-09-02 角色设定图去剧情化）：推理描述词把
# 剧本「世界状态」写进角色设定（如「雨夜便利店场景中，衣角微湿」）
# → 设定图后两格被画成剧情场景。含信号词的整句剥离（恒定外观与
# 恒定随身物保留：工牌/眼镜等不在信号词表内）。
_SCENE_STATE_RE = re.compile(
    r"[^。\n]*(?:场景中|雨夜|雨天|雨中|深夜|雪夜|战场上|废墟|"
    r"淋湿|衣角微湿|浑身湿透|湿漉漉)[^。\n]*(?:。|$)", )
# 剧情态伴随的湿衣描写短语（非整句时逐短语剥）
_SCENE_STATE_PHRASE_RE = re.compile(
    r"[，,、]?(?:衣角(?:微)?湿|浑身湿透|湿漉漉[^，,、。]*)")


def _strip_scene_state_clauses(text: str) -> str:
    """剥角色描述词中的剧情态场景句/湿衣短语（保留恒定外观）。

    角色设定图 = 恒定外观快照（白底、干燥、中性站姿）。剧本剧情态
    （雨夜/湿衣/特定场景）混入会让设定图带上场景背景（小林实拍）。
    剥除后可能残留尾随句读，统一清理。
    """
    cleaned = _SCENE_STATE_RE.sub("", text or "")
    cleaned = _SCENE_STATE_PHRASE_RE.sub("", cleaned)
    cleaned = re.sub(r"[，,、]\s*[。；;]", "。", cleaned)
    cleaned = re.sub(r"[，,、]\s*$", "。", cleaned.strip())
    cleaned = re.sub(r"。\s*。", "。", cleaned)
    return cleaned.strip()


def _build_onepass_prompt_zh(prompt_zh_clean: str) -> str:
    """装配 one-pass 中文长提示词（人设前置 + 全正向语义，2026-08-20
    v2 重构：修复「与描述词差距过大」）。

    实测教训：4B 蒸馏小模型对否定语义（禁止X）处理差——负向禁令
    占半篇幅时正面指令被稀释，且模型易画出被禁止的内容。v2 结构：
    ①人设置首（512 token 截断时最先保住，最核心）②版式逐格正向
    描述 ③背景/风格正向短语 ④姿态。全篇无「禁止」，篇幅 ~200
    token（v1 ~360）。纯白底与去风格化由 SAM 漂白 + PIL 标注代
    码保底，不依赖模型遵循。

    2026-09-02 剧情态剥离（v3）：上游推理描述词可能携带剧情场景
    状态（如「雨夜便利店场景中，衣角微湿」）——角色设定图必须是
    去剧情化的恒定外观，场景词会让后两格画成剧情场景（小林实拍
    事故）。生成前按场景信号词剥整句。
    """
    prompt_zh_clean = _strip_scene_state_clauses(prompt_zh_clean)
    return (
        f"角色设定图。角色：{prompt_zh_clean}。"
        "画面：同一角色的四视图设定图，从左到右依次为——"
        "第1格正面全身：身体完全正对镜头呈0度，双肩左右对称，"
        "两只眼睛、完整面部清晰可见；"
        "第2格左侧面全身：身体正侧对镜头呈90度，双肩重叠成"
        "一条线，只见单侧脸部轮廓与单只眼睛，看不到另一侧肩；"
        "第3格背面全身：身体完全背对镜头呈180度，双肩左右对称，"
        "完全看不到任何面部五官，只见头发覆盖的后脑勺与衣服"
        "背面的背影；"
        "第4格上半身特写：胸部以上近景构图，头部占本格高度"
        "约三分之一，面部大而清晰。"
        "恰好四格，四格视角互不相同，横向等宽排成一行；"
        "前三格为竖构图全身像，人物从头到脚完整。"
        "背景：纯白色，均匀干净，无任何环境与场景陈设，"
        "角色处于干燥整洁的日常状态。"
        "美术风格：韩国网漫风，干净线稿，清晰上色，"
        "表情表现力强，色彩干净明快。"
        "姿态：常态平静表情，眼睛平视，空手，画面干净无文字。"
    )


def _draw_label_with_backdrop(canvas: Image, draw: ImageDraw.ImageDraw,
                              xy: tuple[int, int], text: str,
                              font: ImageFont.FreeTypeFont, *,
                              pad: int = 14, radius: int = 12) -> None:
    """白底圆角衬底 + 黑字标注：角色肢体可能延伸到画幅底部，黑字直接
    叠深色衣物即失去对比度（2026-08-20 冒烟实测「背面全身」不可读）。"""
    from PIL import ImageDraw
    x, y = xy
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    backdrop = ImageDraw.Draw(canvas)
    backdrop.rounded_rectangle(
        [x - pad, y - pad, x + tw + pad, y + th + pad + bbox[1]],
        radius=radius, fill=(255, 255, 255))
    draw.text((x, y), text, fill=(24, 24, 24), font=font)


def _draw_onepass_labels(image: Image, name: str) -> Image:
    """整图叠加中文标注（竞品交付形态对齐）：左上角角色名 +
    各视图格下方视图标签（白底衬底保证任意构图下可读）。
    字体缺失时跳过标注（降级不阻断）。"""
    from PIL import ImageDraw
    font_name = _load_zh_font(56)
    font_label = _load_zh_font(40)
    if font_name is None or font_label is None:
        log.warning("中文字体不可用，跳过四视图标注叠加")
        return image
    canvas = image.convert("RGB")
    draw = ImageDraw.Draw(canvas)
    _draw_label_with_backdrop(canvas, draw, (72, 56), name, font_name)
    cell_w = canvas.width // 4
    for idx, view in enumerate(_TURNAROUND_VIEWS):
        label = _ONEPASS_VIEW_LABELS[view]
        bbox = draw.textbbox((0, 0), label, font=font_label)
        tw = bbox[2] - bbox[0]
        cx = idx * cell_w + (cell_w - tw) // 2
        _draw_label_with_backdrop(canvas, draw,
                                  (cx, canvas.height - 104), label,
                                  font_label)
    return canvas


def _slice_onepass_views(image: Image) -> dict:
    """整图等分四格横排裁切（front/side/back/closeup，左→右）。

    提示词约束四视图等宽并排，等分即视图边界；格间白底分隔使
    裁切边缘干净。返回 view → PIL.Image。
    """
    canvas = image.convert("RGB")
    cell_w = canvas.width // 4
    views: dict = {}
    for idx, view in enumerate(_TURNAROUND_VIEWS):
        views[view] = canvas.crop((idx * cell_w, 0,
                                   (idx + 1) * cell_w, canvas.height))
    return views


def _load_onepass_reference(out_dir: Path) -> Image | None:
    """读取资产目录参考图（原尺寸保比例——FLUX.2 管线内部缩至 ≤1MP
    作条件 token，等价竞品「参考图」层）。缺失/损坏返回 None 忽略。"""
    ref_path = out_dir / "reference.png"
    if not ref_path.is_file():
        return None
    try:
        from PIL import Image
        with Image.open(ref_path) as im:
            return im.convert("RGB")
    except Exception as exc:  # noqa: BLE001 - 参考图损坏则忽略不阻断
        log.warning("参考图读取失败，one-pass 忽略参考图: %s", exc, exc_info=True)
        return None


def _generate_turnaround_onepass(engine: PaintEngine, out_dir: Path, *, name: str,
                                 prompt: str, seed: int,
                                 transparent: bool = False,
                                 ctx_id: str = "") -> dict:
    """one-pass 单图四视图核心（竞品技术路线对齐，2026-08-20 重构）。

    1. 中文长文直入：六段式模板（版式/约束/风格/人设/姿态），FLUX.2
       Klein 的 Qwen3 文本编码器 512 token 全量消化，无中译英环节
    2. 一次推理 2560×1440（16:9）整图出四视图（1 行 4 列等宽竖格，
       每格 640×1440，对齐根目录 参考图.png 版式）
    3. PIL 叠加中文标注（角色名 + 视图标签——字体零漂移，不交给概率模型）
    4. 干净整图（master.png）等分裁切四格落盘 portrait_views/，
       供分镜引用与单视图局部重生

    竞品语义：全图一体成败（无逐视图局部修补）；资产目录 reference.png
    存在时作参考条件图（同 seed + 参考图，可复现）。ctx_id 非空时
    采样步级广播 WS 实时进度（重 roll 换 seed 时归零重来）。
    """
    import random

    from PIL import Image

    if seed is None or seed < 0:
        seed = random.randint(0, 2 ** 31 - 1)
    prompt_zh = _build_onepass_prompt_zh(_sanitize_character_prompt_zh(prompt))
    params = {"prompt": prompt_zh, "steps": _ONEPASS_STEPS,
              "cfg": _ONEPASS_GUIDANCE, "seed": seed,
              "width": _ONEPASS_W, "height": _ONEPASS_H}
    ref_image = _load_onepass_reference(out_dir)
    ref_used = ref_image is not None

    # 三关验证闸门（2026-08-20 用户裁定）：每轮生成后依次过三关——
    # ①VL 视角组合（正面/侧面/背面/特写，≤2 格错先局部修复再复检）
    # ②背景漂白 + 像素级纯白硬校验 ③VL 图文符合度（人物外观 vs
    # 角色设定）。任一关不过即弃图换 seed 无感重 roll（用户全程
    # 无感知，只见最终合格图）；全部轮次未收敛抛错，由
    # _run_turnaround_pipeline 回退逐视图路径——绝不落盘不合格图。
    result = None
    image = None
    layout_verified: bool | None = None
    bg_verified: bool | None = None
    match_verified: bool | None = None
    clean_prompt = _sanitize_character_prompt_zh(prompt)
    expected = ["正面", "侧面", "背面", "特写"]
    passed = False
    for attempt in range(1, _ONEPASS_MAX_ATTEMPTS + 1):
        if attempt > 1:
            params["seed"] = seed = random.randint(0, 2**31 - 1)
            # VL 校验让位时卸载过 FLUX.2：重生前重载
            engine.ensure_loaded("flux2-klein-4b")
        # 采样步级进度：一轮 92% 上限（尾 8% 留给验证/裁切/落盘），
        # attempt>1 归零重来（用户只见「重新生成」标签，无感换 seed）
        attempt_lbl = "" if attempt == 1 else f"（重试 {attempt}）"

        def _map_step(p: int, _lbl: str = attempt_lbl) -> None:
            broadcast_gen_progress(
                "asset", ctx_id, percent=int(max(0, min(100, p)) * 0.92),
                label=f"生成四视图{_lbl}")

        broadcast_gen_progress("asset", ctx_id, percent=2,
                               label=f"生成四视图{attempt_lbl}")
        step_cb = _map_step if ctx_id else None
        if ref_used:
            # 参考条件生成（FLUX.2 reference conditioning，画幅显式指定）
            result = engine.img2img(params, ref_image, progress_cb=step_cb)
        else:
            result = engine.generate(params, progress_cb=step_cb)
        image = result["images"][0].convert("RGB")
        if image.size != (_ONEPASS_W, _ONEPASS_H):
            image = image.resize((_ONEPASS_W, _ONEPASS_H), Image.LANCZOS)

        # 关1：VL 视角校验（实测整图直出每次恰好错 1 格的概率结构
        # ——修复单格远优于重 roll；≥3 格错说明整轮质量差直接弃）
        ok, labels = _verify_view_layout(image)
        layout_verified = ok
        labels2: list = []
        if ok is False:
            wrong = [i for i in range(4)
                     if labels and labels[i] != expected[i]]
            if labels and 0 < len(wrong) <= 2:
                log.warning("视角错位格 %s（第 %d 轮），局部 inpaint 修复",
                            wrong, attempt)
                for i in wrong:
                    image = _repair_view_cell(
                        engine, image, i, clean_prompt,
                        random.randint(0, 2 ** 31 - 1))
                layout_verified, labels2 = _verify_view_layout(image)
            if layout_verified is False:
                log.warning("第 %d 轮关1（视角）未过（%s），弃图换 seed",
                            attempt,
                            "/".join(labels2) if labels2 else "?")
                continue

        # 关2：背景漂白 + 像素级纯白硬校验（确定性判定，不过必弃）
        image = _whiten_background(image)
        bg_verified = _verify_background_white(image)
        if not bg_verified:
            log.warning("第 %d 轮关2（纯白背景）未过，弃图换 seed",
                        attempt)
            continue

        # 关3：VL 图文符合度（None=VL 不可用跳过，不阻塞交付）
        match_verified = _verify_prompt_match(image, clean_prompt)
        if match_verified is False:
            log.warning("第 %d 轮关3（图文相符）未过，弃图换 seed",
                        attempt)
            continue

        passed = True
        break

    if not passed:
        raise ApiError(
            "ASSET_QUALITY_CHECK_FAILED",
            f"四视图 {attempt} 轮生成均未通过质量校验"
            f"（视角={layout_verified}/纯白={bg_verified}/"
            f"相符={match_verified}），已弃全部轮次，不交付不合格图")

    views_dir = out_dir / "portrait_views"
    views_dir.mkdir(parents=True, exist_ok=True)
    views: dict[str, str] = {}
    view_imgs = []
    for view, cell in _slice_onepass_views(image).items():
        if transparent:
            cell = _remove_background(cell)
        p = views_dir / f"{view}.png"
        cell.save(p, "PNG")
        views[view] = str(p.relative_to(DATA_DIR)).replace("\\", "/")
        view_imgs.append(cell)

    # 干净整图留档（单视图局部重生的底图）+ 标注交付图（竞品单图交付形态）
    master_path = out_dir / "master.png"
    image.save(master_path, "PNG")
    canvas_path = out_dir / "canvas.png"
    _draw_onepass_labels(image, name).save(canvas_path, "PNG")
    broadcast_gen_progress("asset", ctx_id, percent=95, label="裁切落盘")

    return {
        "pipeline": "onepass",
        "views": views,
        "canvas": str(canvas_path.relative_to(DATA_DIR)).replace("\\", "/"),
        "master": str(master_path.relative_to(DATA_DIR)).replace("\\", "/"),
        "consistency": _views_consistency(view_imgs),
        "seed": result.get("seed", seed),
        "model": result.get("model", ""),
        "prompt_zh": prompt_zh,
        "ref_used": ref_used,
        "view_errors": {},
        "layout_verified": layout_verified,
        "bg_verified": bg_verified,
        "match_verified": match_verified,
        "verify_attempts": attempt,
    }


# ── zviews 分张管线（Z2 2026-09-15：Z-Image 逐视图 + 参考锚身份 + ──
# 代码贴字）。动因（ab5 四视图对标实测）：单图 one-pass 版式指令服从
# 是生成模型能力边界（klein 双栈丢格/特写错、长标签错字），竞品级
# 交付只能靠"拆活儿"——每视图单独生成（语义=提示词保证）+ PIL 贴字
# （零错字）+ 参考图条件锚身份（TextEncodeZImageOmni 原生参考）。
_ZVIEW_SIZE_W, _ZVIEW_SIZE_H = 576, 1296   # 与画布格 640×1440 同比例
_ZVIEW_DIRECTIVES = {
    "front": "正面全身站立照：正面正对镜头，双臂自然下垂，"
             "头顶到脚底全身入画",
    "side": "侧面全身站立照：严格正侧面90度朝向，全身入画",
    "back": "背面全身站立照：背对镜头仅见背影，全身入画",
    "closeup": "上半身特写肖像：胸部以上构图，正视镜头，平静表情",
}
# 参考图条件只给与参考姿势同向的视图（Z-Image 参考是编辑级强度：
# 实弹 2026-09-15，全视图挂参考会把侧面/背面全带成正面脸+参考图
# 碎片复刻——与 klein 关键帧 R2/ECU「face 入 latent 致脸复制」同课）。
# 侧/背视图身份由同种子+同设定文本锚定。
_ZVIEW_REF_VIEWS: tuple[str, ...] = ()  # A/B 待定：挂参考的视图（编辑条件会带参考压缩伪影，见 14:2x 实测）


def _zview_use_ref(view: str, ref_image: Image | None) -> bool:
    return ref_image is not None and view in _ZVIEW_REF_VIEWS


def _zview_prep_ref(ref: Image, w: int, h: int) -> Image:
    """参考图预处理（Z2）：白边补齐到出图宽高比（不裁切不变形）。

    Z-Image 参考隐空间须与采样隐空间同形（编辑语义），官方
    ImageScale crop=center 会硬裁人像产生构图伪影；改为先等比
    缩放 + 白边补齐（参考图本就是白底，白边与背景融为一体），
    LANCZOS 高质量重采样到精确输出尺寸。"""
    from PIL import Image

    tw, th = w, h
    scale = min(tw / ref.width, th / ref.height)
    nw, nh = max(1, round(ref.width * scale)), max(1, round(ref.height * scale))
    canvas = Image.new("RGB", (tw, th), (255, 255, 255))
    res = ref.convert("RGB").resize((nw, nh), Image.LANCZOS)
    canvas.paste(res, ((tw - nw) // 2, (th - nh) // 2))
    return canvas


def _zview_whiten(image: Image) -> Image:
    """zviews 专用背景净化（区别于 _whiten_background 的 SAM 路线）。

    原理：取四边边界色中位数=背景色 → 容差内且**与边界连通**的像素
    洗成纯白（洪泛填充）。人物内部的白色衣物不与边界连通故不受影响
    （SAM 路线在「白T恤 vs 白底」场景实测误啃衣物，且逐视图 SAM 显存
    不足会卸载 ComfyUI 造成逐张冷重启——2026-09-15 实弹教训）。"""
    import numpy as np
    from PIL import Image

    arr = np.asarray(image.convert("RGB"), dtype=np.int16)
    border = np.concatenate([
        arr[:2].reshape(-1, 3), arr[-2:].reshape(-1, 3),
        arr[:, :2].reshape(-1, 3), arr[:, -2:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    if float(bg.min()) >= 250.0:
        return image.convert("RGB")
    dist = np.abs(arr - bg).max(axis=2)
    cand = dist <= 20
    flooded = np.zeros(cand.shape, dtype=bool)
    flooded[0, :] = cand[0, :]
    flooded[-1, :] = cand[-1, :]
    flooded[:, 0] = cand[:, 0]
    flooded[:, -1] = cand[:, -1]
    while True:
        grown = flooded.copy()
        grown[1:, :] |= flooded[:-1, :]
        grown[:-1, :] |= flooded[1:, :]
        grown[:, 1:] |= flooded[:, :-1]
        grown[:, :-1] |= flooded[:, 1:]
        grown &= cand
        n = int(grown.sum())
        if n == int(flooded.sum()):
            break
        flooded = grown
    arr[flooded] = 255
    return Image.fromarray(arr.astype(np.uint8))


def _zviews_enabled() -> bool:
    """zviews 分张管线开关（默认开；manga.turnaround_engine=legacy/
    comfy 可强制回旧管线）。z-image 权重不在位时调用方自然回退。"""
    try:
        from ...config import get_config
        _v = str((get_config().get("manga") or {}).get(
            "turnaround_engine", "zviews")).strip().lower()
        return _v in ("", "zviews", "auto")
    except Exception:  # noqa: BLE001 - 配置异常按默认开
        return True


def _zview_view_prompt(prompt: str, view: str) -> str:
    """单视图提示词：净化后的人物设定 + 视图指令 + 白底棚拍约束。"""
    return (f"{_sanitize_character_prompt_zh(prompt)}\n"
            f"{_ZVIEW_DIRECTIVES[view]}。纯白无缝背景（#FFFFFF），"
            "影棚白底，无投影无阴影，画面中不得出现任何文字。")


def _generate_turnaround_zviews(out_dir: Path, *, name: str, prompt: str,
                                seed: int, transparent: bool = False,
                                ctx_id: str = "") -> dict:
    """zviews 分张四视图核心（Z2 2026-09-15）。

    1. 逐视图生成：Z-Image-Turbo 8 步蒸馏，576×1296（与画布格
       640×1440 同比例）；正/侧/背/特写各一条专用提示词——视图语义
       由提示词构造性保证，不再赌单图版式指令服从
    2. 身份锚：资产目录 reference.png 走 TextEncodeZImageOmni 原生
       参考条件（实弹：脸即参考图本人）；四视图同种子
    3. 背景确定性漂白（_whiten_background）+ 透明模式抠底
    4. 合成 2560×1440 画布 + PIL 中文标注（复用 one-pass 标注器，
       零错字）——视图语义/标注两大痛点在此构造性消失
    """
    import random

    from PIL import Image

    if seed is None or seed < 0:
        seed = random.randint(0, 2 ** 31 - 1)
    clean_prompt = _sanitize_character_prompt_zh(prompt)
    ref_image = _load_onepass_reference(out_dir)
    ref_used = ref_image is not None

    # 角色 LoRA（Z4）：角色目录 lora.safetensors 在场即挂载（同卷硬链
    # 进 ComfyUI loras，与关键帧 D-LoRA 同约定）；触发词 trigger.txt
    lora_name = ""
    trigger = ""
    _lora_path = out_dir / "lora.safetensors"
    if _lora_path.is_file():
        import hashlib
        import os
        import shutil
        loras_dir = (Path(__file__).resolve().parents[3] / "tools" /
                     "ComfyUI_windows_portable" / "ComfyUI" / "models" /
                     "loras")
        loras_dir.mkdir(parents=True, exist_ok=True)
        tag = hashlib.md5(str(_lora_path).encode()).hexdigest()[:8]
        dst = loras_dir / f"char_{tag}.safetensors"
        if not dst.is_file():
            try:
                os.link(_lora_path, dst)
            except OSError:  # noqa: PERF203 - 跨卷退回复制
                shutil.copy(_lora_path, dst)
        lora_name = dst.name
        _trig = out_dir / "trigger.txt"
        if _trig.is_file():
            trigger = _trig.read_text(encoding="utf-8").strip()

    # 云端档（ZC 2026-09-15；**拍板 B 2026-09-15：本地优先+云端显式选**）：
    # 旧实现「asset.image 槽绑定云端端点即走云」=默认烧配额，产品语义
    # 变化未经用户知情确认——现改为 config manga.turnaround_cloud=on
    # 显式开启才走云（默认 off=本地 Z-Image-Turbo）。云端单张约 5~15s，
    # 四张 30~60s，qwen-image 原生中文/竞品级质量。
    _cloud_ep = None
    try:
        from ...config import get_config as _get_cfg
        _cloud_on = str((_get_cfg().get("manga") or {}).get(
            "turnaround_cloud", "off")).strip().lower() in ("on", "true", "1")
    except Exception:  # noqa: BLE001 - 配置读取失败按本地
        _cloud_on = False
    if _cloud_on:
        try:
            from ...services.cloud_provider_service import (
                SLOT_ASSET_IMAGE,
                get_image_endpoint,
            )
            _cloud_ep = get_image_endpoint(SLOT_ASSET_IMAGE)
        except Exception:  # noqa: BLE001 - 绑定读取失败按本地
            _cloud_ep = None

    views_dir = out_dir / "portrait_views"
    views_dir.mkdir(parents=True, exist_ok=True)
    views: dict[str, str] = {}
    view_imgs: list[Image.Image] = []
    # 先全部生成（ComfyUI 只冷启一次），再统一净化——净化不涉及
    # ComfyUI，避免 SAM 路线「显存不足→卸载→下一张冷重启」的抖动
    raw_imgs: list[Image.Image] = []
    for idx, view in enumerate(_TURNAROUND_VIEWS):
        label = _ONEPASS_VIEW_LABELS[view]
        broadcast_gen_progress(
            "asset", ctx_id, percent=3 + idx * 22,
            label=f"生成{label}")
        vp = _zview_view_prompt(prompt, view)
        if lora_name and trigger:
            vp = f"{trigger}, {vp}"
        params = {"prompt": vp,
                  "seed": (seed + idx) % (2 ** 31),
                  "width": _ZVIEW_SIZE_W, "height": _ZVIEW_SIZE_H,
                  "steps": 8, "cfg": 1.0, "model": "z-image-turbo"}
        if lora_name:
            params["lora_name"] = lora_name
            params["lora_scale"] = 1.0
        if _cloud_ep is not None:
            from ...services.inference.cloud_image_client import (
                generate_image as _cloud_gen,
            )
            raw_imgs.append(_cloud_gen(
                _cloud_ep, vp, width=_ZVIEW_SIZE_W,
                height=_ZVIEW_SIZE_H,
                slot="asset.image").convert("RGB"))
            continue
        _ref = (_zview_prep_ref(ref_image, _ZVIEW_SIZE_W, _ZVIEW_SIZE_H)
                if _zview_use_ref(view, ref_image) else None)
        r = comfy_paint_generate(params, ref_image=_ref)
        raw_imgs.append(r["images"][0].convert("RGB"))

    for view, img in zip(_TURNAROUND_VIEWS, raw_imgs, strict=True):
        # 洪泛净化（边界连通背景→纯白；SAM 路线在白T恤vs白底场景
        # 实测误啃衣物，弃用）——此阶段批量做，无 ComfyUI 交互
        img = _zview_whiten(img)
        if transparent:
            img = _remove_background(img)
        p = views_dir / f"{view}.png"
        img.save(p, "PNG")
        views[view] = str(p.relative_to(DATA_DIR)).replace("\\", "/")
        view_imgs.append(img)

    broadcast_gen_progress("asset", ctx_id, percent=93, label="合成标注")
    master = Image.new("RGB", (_ONEPASS_W, _ONEPASS_H), "white")
    cell_w, cell_h = _ONEPASS_W // 4, _ONEPASS_H
    for idx, img in enumerate(view_imgs):
        master.paste(img.convert("RGB").resize((cell_w, cell_h),
                                               Image.LANCZOS),
                     (idx * cell_w, 0))
    master_path = out_dir / "master.png"
    master.save(master_path, "PNG")
    canvas_path = out_dir / "canvas.png"
    _draw_onepass_labels(master, name).save(canvas_path, "PNG")
    broadcast_gen_progress("asset", ctx_id, percent=97, label="裁切落盘")

    return {
        "pipeline": "zviews",
        "views": views,
        "canvas": str(canvas_path.relative_to(DATA_DIR)).replace("\\", "/"),
        "master": str(master_path.relative_to(DATA_DIR)).replace("\\", "/"),
        "consistency": _views_consistency(view_imgs),
        "seed": seed,
        "model": (f"cloud:{_cloud_ep.provider_name}:{_cloud_ep.model}"
                  if _cloud_ep is not None else "z-image-turbo(comfy)"),
        "prompt_zh": clean_prompt,
        "ref_used": ref_used,
        "view_errors": {},
    }


def _run_turnaround_pipeline(engine: PaintEngine, out_dir: Path, *, name: str,
                             prompt: str, seed: int,
                             transparent: bool = False,
                             ctx_id: str = "") -> dict:
    """四视图生成统一编排：one-pass 主路径（FLUX.2 Klein 中文直入，
    竞品同款 1 次推理单图四视图）→ 不可用/推理失败时回退 legacy
    SDXL 逐视图四张独立图（中译英 + 2x 上采样，诚实降级）。

    返回统一 gen dict：{pipeline, views, canvas, consistency, seed,
    model, ref_used, view_errors, prompt_zh | prompt_en}。ctx_id 非空
    时全程广播 WS 实时进度（task_progress → 前端按钮进度条）。
    """
    # W3-C：引擎档位在编排口统一裁决（comfy 适配器 / legacy klein-4b；
    # comfy onepass 失败自然落入下方 legacy 全链回退）
    engine = _pick_turnaround_engine()
    # Z2（2026-09-15）：zviews 分张管线优先——Z-Image 逐视图+参考锚
    # 身份+代码贴字，视图语义/标注两大痛点构造性消失；失败或 z-image
    # 不可用时自然回落 one-pass / legacy 逐视图（诚实降级链不变）
    try:
        # 注意三点相对导入：comic_gen 在 src.api.manga 下，
        # src.services 需跨两级包（.. 只有 src.api）
        from ...services.inference.comfy_paint_engine import (
            comfy_paint_available as _z_avail,
        )
        # 2026-09-15 审计修复：按 z 槽权重探测（旧实现查的是 klein
        # 三件套——z 权重缺失时闸仍绿，冷启动后才炸）
        _z_ok = _z_avail("z-image-turbo")
    except Exception:
        log.exception("Z2 gate: 可用性探测抛异常（按不可用）")
        _z_ok = False
    log.warning("Z2 gate: enabled=%s z_ok=%s engine_hint=%s",
                _zviews_enabled(), _z_ok,
                type(engine).__name__)
    if _zviews_enabled() and _z_ok:
        try:
            gen = _generate_turnaround_zviews(
                out_dir, name=name, prompt=prompt, seed=seed,
                transparent=transparent, ctx_id=ctx_id)
            log.info("zviews 四视图完成: %s seed=%s model=%s",
                     name, gen["seed"], gen["model"])
            return gen
        except Exception as exc:  # noqa: BLE001 - 分张失败回退旧管线
            log.exception("zviews 四视图生成失败，回退 one-pass/legacy: %s",
                          exc)
    gen = None
    if engine.ensure_loaded("flux2-klein-4b"):
        try:
            gen = _generate_turnaround_onepass(
                engine, out_dir, name=name, prompt=prompt, seed=seed,
                transparent=transparent, ctx_id=ctx_id)
            log.info("one-pass 四视图完成: %s seed=%d model=%s",
                     name, gen["seed"], gen["model"])
        except Exception as exc:  # noqa: BLE001 - FLUX.2 失败回退逐视图
            log.exception("one-pass 四视图生成失败，回退逐视图路径: %s", exc)
            engine.unload_model()  # 释放 FLUX.2 显存给回退路径
    else:
        log.warning("FLUX.2 Klein 不可用（%s），四视图走逐视图回退路径",
                    engine.get_status().get("last_error", ""))
    if gen is not None:
        return gen

    # legacy 回退：SDXL 逐视图（中文净化→译英→四张独立 16:9）
    prompt_en = _prepare_turnaround_prompt_en(prompt)
    if not engine.is_ready and not engine.ensure_loaded(None):
        status = engine.get_status()
        raise ApiError("PAINT_ENGINE_NOT_READY",
                       status.get("last_error") or "绘画模型未就绪")
    gen = _generate_four_views(engine, prompt_en, seed, out_dir,
                               transparent=transparent, ctx_id=ctx_id)
    gen["pipeline"] = "views4"
    gen["prompt_en"] = prompt_en
    return gen


def _apply_turnaround_meta(meta: dict, gen: dict) -> None:
    """把 gen dict 刷新进资产 meta（one-pass / zviews / views4 归一）。"""
    meta["turnaround"] = True
    meta["pipeline"] = gen["pipeline"]
    meta["onepass"] = gen["pipeline"] == "onepass"
    meta["views"] = gen["views"]
    meta["canvas"] = gen["canvas"]
    meta["consistency"] = gen["consistency"]
    meta["seed"] = gen["seed"]
    meta["model"] = gen["model"]
    if gen["pipeline"] == "onepass":
        meta["width"], meta["height"] = _ONEPASS_W, _ONEPASS_H
        meta["view_width"], meta["view_height"] = _ONEPASS_W // 4, _ONEPASS_H
        meta["prompt_zh"] = gen["prompt_zh"]
        meta["master"] = gen["master"]
        meta["layout_verified"] = gen.get("layout_verified")
        meta["bg_verified"] = gen.get("bg_verified")
        meta["match_verified"] = gen.get("match_verified")
        meta["verify_attempts"] = gen.get("verify_attempts")
        meta.pop("prompt_en", None)
        meta.pop("degraded", None)
        meta.pop("degrade_reason", None)
    elif gen["pipeline"] == "zviews":
        # zviews：与 one-pass 同画布规格（标注/裁切/单视图重生同构），
        # 无 VL 三关（视图语义由分张提示词构造性保证）
        meta["width"], meta["height"] = _ONEPASS_W, _ONEPASS_H
        meta["view_width"], meta["view_height"] = _ONEPASS_W // 4, _ONEPASS_H
        meta["prompt_zh"] = gen["prompt_zh"]
        meta["master"] = gen["master"]
        for k in ("layout_verified", "bg_verified", "match_verified",
                  "verify_attempts", "prompt_en", "degraded",
                  "degrade_reason"):
            meta.pop(k, None)
    else:
        meta["width"], meta["height"] = _TURNAROUND_W, _TURNAROUND_H
        meta.pop("view_width", None)
        meta.pop("view_height", None)
        meta.pop("master", None)
        meta.pop("prompt_zh", None)
        meta["prompt_en"] = gen["prompt_en"]
        meta["degraded"] = True
        meta["degrade_reason"] = (
            "FLUX.2 Klein 不可用：SDXL 兜底逐视图生成四张 2560×1440 "
            "独立视图（1280×720 生成 + 2x 上采样），视图一致性为尽力而为")
    if gen.get("view_errors"):
        meta["view_errors"] = gen["view_errors"]
    else:
        meta.pop("view_errors", None)
    if gen.get("ref_used"):
        meta["ref_used"] = True
    else:
        meta.pop("ref_used", None)
    if gen.get("ref_fallback"):
        meta["ref_fallback"] = True
    else:
        meta.pop("ref_fallback", None)

# 资产生成历史留痕上限（meta.history，超出截掉最旧）
_ASSET_HISTORY_MAX = 12


def _sanitize_character_prompt_zh(text: str) -> str:
    """剥掉角色描述词中的版式/标注指令（四视图指令/图片标注/禁止类），
    保留人物外观描述。

    竞品描述词模板含「生成角色4视图：正面全身、侧面全身、背面全身、
    上半身特写。」等整版式指令；逐视图独立生成前必须在中文阶段剥掉，
    否则每张图都会画成 4 宫格。白底/禁止类由 _STYLE_WHITE_BG /
    _STYLE_NEGATIVE 统一兜底。

    2026-08-20 角色推理 v2：prompt 为五段式 AI 描述词（【角色】/
    绘图提示词/美术风格/时代背景/角色设定）——剥版式与标注句之外，
    额外剥「【角色】：xxx」「绘图提示词：」「时代背景：xxx。」段
    标签行与固定段（美术风格/时代背景由生图模板统一注入网漫风，
    角色设定正文与时代背景语义词保留），防段标签与模板重复冲突。
    """
    import re
    cleaned = text or ""
    # 句内正则一律 [^。\n]*（不跨行）——2026-08-20 实测教训：[^。]*
    # 含换行，行尾残句会跨行吞掉后续整段（美术风格/时代背景/角色
    # 设定首句被「纯白色背景，」残句连吃）
    for pat in (r"【[^】]*】[：:][^\n。]*。?",           # 【角色】：夏沐沐
                r"[^。\n]*[四4]视图[^。\n]*(?:。|$)",   # 生成角色4视图：…。
                r"图片[左右]上角[^。\n]*(?:。|$)",     # 图片左上角/右上角…。
                r"[^。\n]*标注[^。\n]*(?:。|$)",       # 含「标注」的整句
                r"禁止[^。\n]*(?:。|$)",               # 禁止纹理/投影等禁令
                r"纯白色背景[^。\n]*(?:。|$)",         # 白底（风格词兜底）
                r"全局光照[^。\n]*(?:。|$)",           # 全局光照（模板兜底）
                r"^\s*绘图提示词[：:]\s*$",          # 段标签行（内容已剥空）
                r"^\s*美术风格[：:]\s*[^。\n]*(?:。|$)",  # 美术风格段（模板注入）
                r"^\s*时代背景[：:]\s*",             # 时代背景段标签（正文保留）
                ):  # noqa: E128
        cleaned = re.sub(pat, "", cleaned, flags=re.MULTILINE)
    # 剥模板自有段（防与六段式模板重复）：角色设定前缀 + 姿态短语
    # （模板尾部统一收口「常态平静表情，眼睛平视镜头，空手」）
    cleaned = re.sub(r"^\s*角色设定[：:]\s*", "", cleaned,
                     flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*角色[：:]\s*", "", cleaned, flags=re.MULTILINE)
    for phrase in ("常态平静表情", "眼睛平视镜头"):
        cleaned = cleaned.replace(phrase, "")
    cleaned = re.sub(r"(?:^|[。,，;；])空手(?=[。,，;；]|$)", "", cleaned)
    # 清理多余标点与空白（剥句后残留的孤标点/连续句号/首尾标点）
    cleaned = re.sub(r"[，,；;、\s]+。", "。", cleaned)
    cleaned = re.sub(r"。{2,}", "。", cleaned)
    cleaned = re.sub(r"^[，,；;、\s。]+|[，,；;、\s。]+$", "", cleaned)
    return cleaned.strip()


def _prepare_turnaround_prompt_en(prompt_zh: str) -> str:
    """四视图路径描述词预处理：中文净化（剥版式指令）→ 译英。

    必须在 paint ensure_loaded 之前翻译：译后绘画引擎腾挪显存卸载
    对话模型，避免双模型换载抖动。
    """
    return translate_prompt_zh2en(_sanitize_character_prompt_zh(prompt_zh))


def _append_asset_history(meta: dict, kind: str, view: str | None,
                          file_path: str) -> None:
    """往 meta.history 追加一条生成留痕（上限 12 条，超出截掉最旧）。"""
    history = meta.get("history")
    if not isinstance(history, list):
        history = []
    history.append({"ts": _now(), "kind": kind, "view": view,
                    "file": file_path})
    meta["history"] = history[-_ASSET_HISTORY_MAX:]


def _load_reference_image(out_dir: Path, gen_w: int, gen_h: int) -> Image | None:
    """读取资产目录 AI 参考图（reference.png），统一缩至生成尺寸。

    img2img 输出尺寸跟随 init_image，预缩至 1280×720 保证 16:9 产出。
    参考图不存在/损坏时返回 None（回退 txt2img，不阻断生成）。
    """
    ref_path = out_dir / "reference.png"
    if not ref_path.is_file():
        return None
    try:
        from PIL import Image
        with Image.open(ref_path) as im:
            return im.convert("RGB").resize((gen_w, gen_h), Image.LANCZOS)
    except Exception as exc:  # noqa: BLE001 - 参考图损坏回退 txt2img
        log.warning("参考图读取失败，回退 txt2img: %s (%s)", ref_path, exc, exc_info=True)
        return None


def _generate_single_view(engine: PaintEngine, prompt_en: str, view: str,
                          seed: int, out_dir: Path,
                          ref_image: Image | None = None,
                          transparent: bool = False,
                          on_step: Callable[[int], None] | None = None) -> dict:
    """生成单个角色视图：1280×720 生成 → LANCZOS 2x 上采样 2560×1440
    → 落盘 portrait_views/{view}.png。

    prompt = 译后描述词 + 视图后缀 + _STYLE_PHOTO + _STYLE_WHITE_BG；
    ref_image 非空时走 img2img（strength=0.55），失败回退 txt2img
    （记 ref_fallback，不抛错）。out_dir/portrait_views 须已存在。
    on_step(percent) 采样步级回调 → WS 实时进度条。
    """
    prompt = (prompt_en + _TURNAROUND_VIEW_SUFFIX[view]
              + _STYLE_PHOTO + _STYLE_WHITE_BG)
    params = {"prompt": prompt, "negative": _STYLE_NEGATIVE,
              "steps": 24, "cfg": 7.0,
              "width": _TURNAROUND_GEN_W, "height": _TURNAROUND_GEN_H,
              "seed": seed}
    step_cb = (lambda p, _s: on_step(p)) if on_step else None
    ref_used = ref_fallback = False
    if ref_image is not None:
        params["strength"] = 0.55
        try:
            result = engine.img2img(params, ref_image, progress_cb=step_cb)
            ref_used = True
        except Exception as exc:  # noqa: BLE001 - img2img 失败回退 txt2img
            log.warning("视图 %s img2img 失败，回退 txt2img: %s", view, exc, exc_info=True)
            ref_fallback = True
            params.pop("strength", None)
            result = engine.generate(params, progress_cb=step_cb)
    else:
        result = engine.generate(params, progress_cb=step_cb)
    image = _upscale_to(result["images"][0], _TURNAROUND_W, _TURNAROUND_H)
    # 交付格式保底：漂白至纯白底（transparent 抠图前先漂白，白底更净）
    image = _whiten_background(image)
    if transparent:
        # 四视图一键去背（COMIC-036，PIL 阈值降级；SAM 未接 /art/segment）
        image = _remove_background(image)
    p = out_dir / "portrait_views" / f"{view}.png"
    image.save(p, "PNG")
    return {"view": view, "image": image,
            "path": str(p.relative_to(DATA_DIR)).replace("\\", "/"),
            "seed": result.get("seed", seed),
            "model": result.get("model", ""),
            "ref_used": ref_used, "ref_fallback": ref_fallback}


def _load_view_images(out_dir: Path) -> dict:
    """读取 portrait_views/ 下已存在的四视图 PNG（view → PIL.Image）。"""
    from PIL import Image
    imgs: dict = {}
    for view in _TURNAROUND_VIEWS:
        p = out_dir / "portrait_views" / f"{view}.png"
        if p.is_file():
            try:
                imgs[view] = Image.open(p)
            except Exception as exc:  # noqa: BLE001 - 单图损坏不阻塞拼图
                log.warning("视图读取失败 %s: %s", p, exc, exc_info=True)
    return imgs


def _rebuild_turnaround_canvas(out_dir: Path,
                               view_imgs: dict | None = None) -> str:
    """由四视图重建 canvas.png 1×4 横排拼图（对齐 参考图.png 版式：
    1 行 4 列等宽竖格，每格 640×1440，总 2560×1440）。

    格序 front/side/back/closeup（左→右）；16:9 横构图视图等比 contain
    居中贴入竖格（白底留边，人物完整）；缺失/失败格白底占位。
    view_imgs 缺省时从 portrait_views/ 读盘。
    返回 canvas 的 DATA_DIR 相对路径。
    """
    from PIL import Image
    if view_imgs is None:
        view_imgs = _load_view_images(out_dir)
    cell_w, cell_h = _TURNAROUND_W // 4, _TURNAROUND_H  # 640×1440
    canvas = Image.new("RGB", (_TURNAROUND_W, _TURNAROUND_H),
                       (255, 255, 255))
    for idx, view in enumerate(_TURNAROUND_VIEWS):
        im = view_imgs.get(view)
        if im is None:
            continue
        im = im.convert("RGB")
        # 等比缩放 contain 进 640×1440 竖格，白底垂直居中
        ratio = min(cell_w / im.width, cell_h / im.height)
        fit = (max(1, round(im.width * ratio)),
               max(1, round(im.height * ratio)))
        cell = im.resize(fit, Image.LANCZOS)
        canvas.paste(cell, (idx * cell_w + (cell_w - fit[0]) // 2,
                            (cell_h - fit[1]) // 2))
    canvas_path = out_dir / "canvas.png"
    canvas.save(canvas_path, "PNG")
    return str(canvas_path.relative_to(DATA_DIR)).replace("\\", "/")


def _generate_four_views(engine: PaintEngine, prompt_en: str, seed: int,
                         out_dir: Path, transparent: bool = False,
                         ctx_id: str = "") -> dict:
    """四视图逐张独立生成（竞品对齐：四张独立 16:9 图，每张可单独重生）。

    四张同 seed 保一致性（seed<0 时先解析为固定随机种子）；资产目录
    reference.png 存在时走 img2img（strength=0.55），失败回退 txt2img。
    单视图失败不阻塞其他视图（per-view 错误记入 errors）；全部失败才
    抛 ApiError。canvas.png 为 1×4 横排拼图（2560×1440，对齐参考图
    版式）。out_dir 须已存在。ctx_id 非空时逐视图广播 WS 实时进度。
    """
    if seed < 0:
        # 解析为固定种子：四视图共用同一种子保证角色一致性
        import random
        seed = random.randint(0, 2**31 - 1)
    views_dir = out_dir / "portrait_views"
    views_dir.mkdir(parents=True, exist_ok=True)
    ref_image = _load_reference_image(out_dir, _TURNAROUND_GEN_W,
                                      _TURNAROUND_GEN_H)
    views: dict[str, str] = {}
    view_imgs: dict = {}
    errors: dict[str, str] = {}
    ref_used = ref_fallback = False
    last_model = ""
    n_total = len(_TURNAROUND_VIEWS)
    for vi, view in enumerate(_TURNAROUND_VIEWS):
        zh = _VIEW_ZH_LABELS.get(view, view)
        broadcast_gen_progress("asset", ctx_id, current=vi + 1,
                               total=n_total,
                               percent=int(vi * 100 / n_total),
                               label=f"生成{zh}视图")

        def _map_step(p: int, _vi: int = vi, _zh: str = zh) -> None:
            broadcast_gen_progress(
                "asset", ctx_id, current=_vi + 1, total=n_total,
                percent=int((_vi + max(0, min(100, p)) / 100.0)
                            * 100 / n_total),
                label=f"生成{_zh}视图")

        try:
            r = _generate_single_view(engine, prompt_en, view, seed,
                                      out_dir, ref_image, transparent,
                                      on_step=_map_step if ctx_id else None)
        except Exception as exc:  # noqa: BLE001 - 单视图失败不阻塞其他视图
            log.exception("四视图 %s 生成失败: %s", view, exc)
            errors[view] = str(exc)[:200]
            continue
        views[view] = r["path"]
        view_imgs[view] = r["image"]
        seed = r["seed"] if r["seed"] is not None else seed
        last_model = r["model"] or last_model
        ref_used = ref_used or r["ref_used"]
        ref_fallback = ref_fallback or r["ref_fallback"]
    if not views:
        raise ApiError("PAINT_GENERATION_FAILED",
                       "四视图全部生成失败：" + "; ".join(
                           f"{v}: {e}" for v, e in errors.items())[:300])
    consistency = _views_consistency(list(view_imgs.values()))
    canvas_rel = _rebuild_turnaround_canvas(out_dir, view_imgs)
    return {"views": views, "canvas": canvas_rel, "errors": errors,
            "consistency": consistency, "seed": seed, "model": last_model,
            "ref_used": ref_used, "ref_fallback": ref_fallback}


def _sync_portrait_from_views(out_dir: Path) -> None:
    """portrait.png 约定为正面视图（COMIC-037）；front 缺失时回退首个
    已生成视图，保证资产主图可用。"""
    views_dir = out_dir / "portrait_views"
    src = views_dir / "front.png"
    if not src.is_file():
        for view in _TURNAROUND_VIEWS[1:]:
            cand = views_dir / f"{view}.png"
            if cand.is_file():
                src = cand
                break
    if src.is_file():
        import shutil
        shutil.copyfile(src, out_dir / "portrait.png")


def _views_consistency(view_imgs: list) -> dict:
    """四视图色调一致性校验（COMIC-035）。

    64×64 缩略图 HSV 空间：色调取循环均值（忽略近背景的低饱和/低明度
    像素），报告四视图色调极差（度）与饱和度极差。阈值经验值：
    hue_spread ≤45° 且 sat_spread ≤0.25 判定一致。
    """
    import colorsys
    import math

    hues: list[float] = []
    sats: list[float] = []
    for im in view_imgs:
        thumb = im.convert("RGB").resize((64, 64))
        sx = cx = stot = 0.0
        n = 0
        for r8, g8, b8 in thumb.getdata():
            h, s, v = colorsys.rgb_to_hsv(r8 / 255, g8 / 255, b8 / 255)
            if v < 0.15 or s < 0.10:
                continue
            sx += math.sin(h * 2 * math.pi)
            cx += math.cos(h * 2 * math.pi)
            stot += s
            n += 1
        if n == 0:
            hues.append(0.0)
            sats.append(0.0)
            continue
        hues.append(math.degrees(math.atan2(sx / n, cx / n)) % 360.0)
        sats.append(stot / n)

    def _circ_spread(vals: list[float]) -> float:
        worst = 0.0
        for i in range(len(vals)):
            for j in range(i + 1, len(vals)):
                d = abs(vals[i] - vals[j]) % 360.0
                worst = max(worst, min(d, 360.0 - d))
        return round(worst, 1)

    hue_spread = _circ_spread(hues)
    sat_spread = round(max(sats) - min(sats), 3) if sats else 0.0
    consistent = hue_spread <= 45.0 and sat_spread <= 0.25
    return {"hue_spread_deg": hue_spread, "sat_spread": sat_spread,
            "consistent": consistent}


def _generate_turnaround_sync(req: AssetTurnaroundRequest) -> dict:
    """同步执行四视图生成（2026-08-20 竞品对齐重构：one-pass 单图四视图
    为主路径——中文长文直入 FLUX.2 Klein，1 次推理整图出四视图 + PIL
    中文标注；FLUX.2 不可用时回退 SDXL 逐视图四张独立图，诚实降级）。

    目录结构（COMIC-037）：characters/{name}/portrait.png（=front 切片）
    + portrait_views/{front,side,back,closeup}.png + canvas.png（one-pass
    为带中文标注的 2560×1440（16:9）横排整图 + master.png 干净底图；
    legacy 为同规格 1×4 横排拼图）。由线程池调用（端点为 async，避免
    阻塞事件循环）。
    """
    engine = get_paint_engine()
    asset_id = uuid.uuid4().hex
    out_dir = _COMIC_ASSET_DIR / req.project_id / "characters" / req.name
    out_dir.mkdir(parents=True, exist_ok=True)
    broadcast_gen_progress("asset", asset_id, percent=0, label="准备生成")
    try:
        gen = _run_turnaround_pipeline(engine, out_dir, name=req.name,
                                       prompt=req.prompt, seed=req.seed,
                                       transparent=req.transparent,
                                       ctx_id=asset_id)
    except Exception:
        broadcast_gen_progress("asset", asset_id, percent=0,
                               status="error", label="生成失败")
        raise
    views = gen["views"]
    # portrait.png 约定为正面视图（COMIC-037 资产目录结构）
    _sync_portrait_from_views(out_dir)
    rel_path = str((out_dir / "portrait.png")
                   .relative_to(DATA_DIR)).replace("\\", "/")

    meta: dict = {"pipeline": gen["pipeline"],
                  "transparent": bool(req.transparent)}
    _apply_turnaround_meta(meta, gen)
    _append_asset_history(meta, "generate", None, rel_path)
    db = get_db_safe()
    if db is not None:
        # 同项目存在同名 character 资产桩（infer-entities 推断 / 无图片
        # 版本）时回填既有行，避免资产库出现两个同名角色；否则新建
        stub = _find_character_asset_stub(db, req.project_id, req.name)
        if stub is not None:
            asset_id = stub["id"]
            old_history = parse_json(stub.get("meta"), {}).get("history")
            if isinstance(old_history, list) and old_history:
                meta["history"] = (old_history
                                   + meta.get("history", []))[
                                  -_ASSET_HISTORY_MAX:]
            db.update("comic_assets",
                      {"file_path": rel_path, "prompt": req.prompt,
                       "meta": meta},
                      "id=?", (asset_id,))
        else:
            db.insert("comic_assets", {
                "id": asset_id, "project_id": req.project_id,
                "kind": "character", "name": req.name, "file_path": rel_path,
                "prompt": req.prompt, "meta": meta, "created_at": _now()})
    # 终态广播（2026-09-02 幽灵任务修复）：此前只发进度不发 done——
    # 前端任务条建了「漫剧生成」任务后永远等不到完结，通知中心挂
    # 幽灵而后端早已完成释放显存（用户实报）。与 _regenerate_asset_sync
    # 的 1305 行终态同款。
    broadcast_gen_progress("asset", asset_id, percent=100,
                           status="done", label="生成完成")
    return {"asset_id": asset_id, "project_id": req.project_id,
            "kind": "character", "name": req.name, "file_path": rel_path,
            "views": views,
            "consistency": gen["consistency"],
            "view_errors": gen.get("view_errors") or None,
            "pipeline": gen["pipeline"],
            "onepass": gen["pipeline"] == "onepass",
            # 降级语义仅指 views4（SDXL 兜底）；zviews 是 Z2 新主力
            "degraded": gen["pipeline"] == "views4",
            "degrade_reason": meta.get("degrade_reason"),
            "meta": meta}


def _regenerate_asset_sync(asset: dict) -> dict:
    """同步重生成资产图。

    以资产现有 kind/prompt 重新生成，覆盖 file_path 指向的图片文件
    （file_path 为空时按资产目录约定新建），并刷新 meta 留痕。
    四视图资产：one-pass 整图重 roll（FLUX.2 Klein 中文直入，竞品
    「全图一体成败」语义）为主路径，FLUX.2 不可用回退 SDXL 逐视图。
    引擎未就绪抛 PAINT_ENGINE_NOT_READY（由端点收敛为 degraded 响应）。
    """
    kind = asset.get("kind", "character")
    conf = _ASSET_KIND_CONF.get(kind, _ASSET_KIND_CONF["character"])
    meta = parse_json(asset.get("meta"), {})
    if not isinstance(meta, dict):
        meta = {}
    # 多视图资产必须走四视图管线——否则单肖像模板会把四视图资产
    # 覆盖成单图，构图与描述词不符。
    is_turnaround = bool(meta.get("turnaround"))
    engine = get_paint_engine()
    rel_path = (asset.get("file_path") or "").strip()
    if rel_path:
        out_path = DATA_DIR / rel_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = (_COMIC_ASSET_DIR / asset.get("project_id", "")
                   / conf["subdir"] / (asset.get("name") or "asset"))
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / ("portrait.png" if kind == "character"
                              else "image.png")
        rel_path = str(out_path.relative_to(DATA_DIR)).replace("\\", "/")
    if is_turnaround:
        # 与首次生成同构：one-pass 整图重 roll（新 seed）→ legacy 回退
        _ctx = asset.get("asset_id") or ""
        broadcast_gen_progress("asset", _ctx, percent=0,
                               label="准备重生成")
        try:
            gen = _run_turnaround_pipeline(
                engine, out_path.parent, name=asset.get("name") or "asset",
                prompt=asset["prompt"], seed=-1,
                transparent=bool(meta.get("transparent")),
                ctx_id=_ctx)
        except Exception:
            broadcast_gen_progress("asset", _ctx, percent=0,
                                   status="error", label="生成失败")
            raise
        broadcast_gen_progress("asset", _ctx, percent=97,
                               label="裁切落盘")
        _sync_portrait_from_views(out_path.parent)
        # 主图同步到 file_path（2026-09-02 修正）：主图 = front 正面切片
        # ——此前曾用 master 整图覆盖 file_path（2026-08-27 为保参考图
        # 新鲜），导致表格头像/资产卡显示整张四视图拼版（人物缩成一条）。
        # front 切片同样「新鲜 + 无标注文字」且语义正确（单人全身像）。
        # out_path 与约定 portrait.png 不同名的历史数据也显式覆盖。
        _views_dir = out_path.parent / "portrait_views"
        _front = _views_dir / "front.png"
        if not _front.is_file():
            for _v in _TURNAROUND_VIEWS[1:]:
                _cand = _views_dir / f"{_v}.png"
                if _cand.is_file():
                    _front = _cand
                    break
        if _front.is_file() and _front.resolve() != out_path.resolve():
            import shutil as _shutil
            _shutil.copyfile(_front, out_path)
        _apply_turnaround_meta(meta, gen)
        # regenerate-view 单视图重生子目录路径由 out_path.parent 覆盖同源
        width, height = meta["width"], meta["height"]
        seed_out, model_out = gen["seed"], gen["model"]
        prompt_out: str | None = None
    else:
        width = max(256, min(IMG_TARGET_W,
                             int(meta.get("width") or IMG_TARGET_W)))
        height = max(256, min(IMG_TARGET_H,
                              int(meta.get("height") or IMG_TARGET_H)))
        # ① FLUX.2 中文直入（主路径，2026-08-24 与 _generate_asset_sync
        #    对齐——此前 regenerate 漏改，SDXL 半分辨率放大=模糊根因）
        # prompt_out 必须先初始化：FLUX 子路径不产出英文译文（21:27
        # e2e 实测 UnboundLocalError——四视图分支的初始化与本分支
        # 互斥执行，覆盖不到这里）；本行不再重复类型注解（Cython 拒绝
        # 同作用域重声明，首处已声明 str | None）
        prompt_out = None
        engine_used = "sdxl"
        _ctx = asset.get("asset_id") or ""

        def _map_step(p: int) -> None:
            broadcast_gen_progress("asset", _ctx, percent=p, label="正在生成")

        broadcast_gen_progress("asset", _ctx, percent=2, label="正在生成")
        try:
            if engine.ensure_loaded("flux2-klein-4b"):
                params = _flux_asset_params(asset["prompt"], kind,
                                            width, height)
                result = engine.generate(params, progress_cb=_map_step
                                         if _ctx else None)
                image = result["images"][0]
                if image.size != (width, height):
                    from PIL import Image as _PILImage
                    image = image.resize((width, height), _PILImage.LANCZOS)
                engine_used = "flux2"
            else:
                # ② SDXL 回退：中文描述词先译英（CLIP 不理解中文）；
                # 翻译先于 paint 加载，避免双模型显存换载抖动
                prompt_en = translate_prompt_zh2en(asset["prompt"])
                if not engine.is_ready and not engine.ensure_loaded(None):
                    status = engine.get_status()
                    raise ApiError("PAINT_ENGINE_NOT_READY",
                                   status.get("last_error") or "绘画模型未就绪")
                # 参考图风格对齐：角色/道具纯白底，场景写实影调不加白底
                style = _STYLE_PHOTO + (_STYLE_WHITE_BG
                                        if kind in ("character", "prop") else "")
                prompt = conf["tpl"].format(prompt=prompt_en) + style
                gen_w, gen_h = _gen_size_for_target(width, height)
                params = {"prompt": prompt, "negative": _STYLE_NEGATIVE,
                          "steps": 24, "cfg": 7.0,
                          "width": gen_w, "height": gen_h, "seed": -1}
                result = engine.generate(params, progress_cb=_map_step
                                         if _ctx else None)
                image = _upscale_to(result["images"][0], width, height)
                prompt_out = prompt_en
        except Exception:
            broadcast_gen_progress("asset", _ctx, percent=0,
                                   status="error", label="生成失败")
            raise
        if kind == "prop" and meta.get("transparent"):
            image = _remove_background(image)
        image.save(out_path, "PNG")
        broadcast_gen_progress("asset", _ctx, percent=97, label="落盘登记")
        seed_out = result.get("seed", -1)
        model_out = result.get("model", "")
        meta["engine"] = engine_used
    meta.update({"width": width, "height": height,
                 "seed": seed_out,
                 "model": model_out,
                 "regenerated_at": _now()})
    if prompt_out is not None:
        meta["prompt_en"] = prompt_out
    _append_asset_history(meta, "regenerate", None, rel_path)
    db = get_db_safe()
    if db is not None:
        db.update("comic_assets", {"file_path": rel_path, "meta": meta},
                  "id=?", (asset["asset_id"],))
    asset = {**asset, "file_path": rel_path, "meta": meta}
    broadcast_gen_progress("asset", _ctx, percent=100, status="done",
                           label="生成完成")
    return asset


def _regenerate_view_sync(asset: dict, view: str, prompt_zh: str) -> dict:
    """同步重生成四视图资产的单个视图（线程池调用）。

    one-pass 资产（meta.onepass）：master.png 该视图整格参考条件重绘
    （超越竞品——竞品不支持单视图重生，只能整图重 roll），其余三格
    原样保留 → 重新裁切/标注/交付图重建。
    legacy 资产：净化 → 译英 → 该视图后缀，1280×720 生成 → LANCZOS
    2x 上采样 2560×1440 覆盖 portrait_views/{view}.png；view==front
    时同步覆盖 portrait.png；重建 canvas.png 2×2 拼图并刷新 meta。
    """
    meta = parse_json(asset.get("meta"), {})
    if not isinstance(meta, dict):
        meta = {}
    if meta.get("onepass"):
        return _regenerate_view_onepass(asset, view, prompt_zh, meta)
    if meta.get("pipeline") == "zviews":
        return _regenerate_view_zviews(asset, view, prompt_zh, meta)
    # 中文描述词先净化（剥离版式指令）再译英；翻译先于 paint 加载。
    prompt_en = _prepare_turnaround_prompt_en(prompt_zh)
    engine = get_paint_engine()
    if not engine.is_ready and not engine.ensure_loaded(None):
        status = engine.get_status()
        raise ApiError("PAINT_ENGINE_NOT_READY",
                       status.get("last_error") or "绘画模型未就绪")
    rel_path = (asset.get("file_path") or "").strip()
    if not rel_path:
        raise ApiError(40008, "资产缺少主图文件，无法定位视图目录",
                       detail={"asset_id": asset.get("asset_id")})
    out_dir = (DATA_DIR / rel_path).parent
    views_dir = out_dir / "portrait_views"
    views_dir.mkdir(parents=True, exist_ok=True)
    # 沿用资产种子保四视图一致性；缺省/非法时解析为固定随机种子
    try:
        seed = int(meta.get("seed", -1))
    except (TypeError, ValueError):
        seed = -1
    if seed < 0:
        import random
        seed = random.randint(0, 2**31 - 1)
    ref_image = _load_reference_image(out_dir, _TURNAROUND_GEN_W,
                                      _TURNAROUND_GEN_H)
    r = _generate_single_view(engine, prompt_en, view, seed, out_dir,
                              ref_image,
                              transparent=bool(meta.get("transparent")))
    if view == "front":
        # portrait.png 约定为正面视图（COMIC-037），随 front 同步覆盖
        _sync_portrait_from_views(out_dir)
    # 由最新四视图重建 2×2 拼图并刷新一致性
    view_imgs = _load_view_images(out_dir)
    meta["canvas"] = _rebuild_turnaround_canvas(out_dir, view_imgs)
    views = meta.get("views")
    if not isinstance(views, dict):
        views = {}
    views[view] = r["path"]
    meta["views"] = views
    meta["consistency"] = _views_consistency(list(view_imgs.values()))
    # ref 标记反映最近一次生成实况（无参考图/未走 img2img 时清除）
    if r["ref_used"]:
        meta["ref_used"] = True
    else:
        meta.pop("ref_used", None)
    if r["ref_fallback"]:
        meta["ref_fallback"] = True
    else:
        meta.pop("ref_fallback", None)
    meta["prompt_en"] = prompt_en
    meta["regenerated_at"] = _now()
    _append_asset_history(meta, "view", view, r["path"])
    db = get_db_safe()
    if db is not None:
        db.update("comic_assets", {"meta": meta}, "id=?",
                  (asset["asset_id"],))
    asset = {**asset, "meta": meta}
    return {"asset": asset, "view": view, "file_path": r["path"]}


def _regenerate_view_onepass(asset: dict, view: str, prompt_zh: str,
                             meta: dict) -> dict:
    """one-pass 资产单视图局部重生（线程池调用，超越竞品能力）。

    竞品「不支持单视图重生，整图重 roll」；本路径对干净整图
    master.png 的该视图整格做 FLUX.2 参考条件重绘（遮罩区域 inpaint，
    其余三格逐像素保留）→ 重新裁切该视图格 → 重建中文标注交付图。
    底图缺失（历史资产）时报错引导整图重生成，不静默换形态。
    """
    import random

    from PIL import Image, ImageDraw

    engine = get_paint_engine()
    # 显式点名 FLUX.2（is_ready 可能是 SDXL 在载——ensure_loaded 负责
    # 底座切换；已是 flux2 则短路零开销）
    if not engine.ensure_loaded("flux2-klein-4b"):
        status = engine.get_status()
        raise ApiError("PAINT_ENGINE_NOT_READY",
                       status.get("last_error") or "绘画模型未就绪")
    rel_path = (asset.get("file_path") or "").strip()
    if not rel_path:
        raise ApiError(40008, "资产缺少主图文件，无法定位视图目录",
                       detail={"asset_id": asset.get("asset_id")})
    out_dir = (DATA_DIR / rel_path).parent
    master_path = out_dir / "master.png"
    if not master_path.is_file():
        raise ApiError(40008, "one-pass 底图缺失，请先整图重生成",
                       detail={"asset_id": asset.get("asset_id")})
    with Image.open(master_path) as im:
        master = im.convert("RGB")

    W, H = master.size  # 竞品形态 2560×1440；容忍历史尺寸按实宽等分
    cell_w = W // 4
    idx = _TURNAROUND_VIEWS.index(view)
    # 遮罩 = 该视图整格（引擎 inpaint 内部 bbox 裁剪 + 软边回贴，
    # margin 压到 8px 让重绘尽量不越格污染邻格）
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rectangle(
        [idx * cell_w, 0, (idx + 1) * cell_w - 1, H - 1], fill=255)

    try:
        seed = int(meta.get("seed", -1))
    except (TypeError, ValueError):
        seed = -1
    if seed < 0:
        seed = random.randint(0, 2 ** 31 - 1)
    label = _ONEPASS_VIEW_LABELS[view]
    prompt = (
        f"生成角色视图：{label}，单人，竖构图全身像，人物完整呈现。"
        "背景必须为纯白色（#FFFFFF），从边缘到中心完全均匀，"
        "禁止灰色调，禁止米色，禁止渐变，禁止阴影，禁止投影，禁止纹理，"
        "禁止环境景物，画面中禁止出现任何文字。"
        "美术风格：照片级写实人像摄影，真实自然的肤色与皮肤质感，"
        "柔和均匀的影棚白光布光，画面锐利通透，高细节。"
        "禁止动漫风格，禁止卡通风格，禁止插画，禁止绘画笔触，"
        "禁止素描，禁止3D渲染感。"
        f"角色设定：{_sanitize_character_prompt_zh(prompt_zh)}。"
        "常态平静表情，眼睛平视镜头，空手。"
    )
    params = {"prompt": prompt, "steps": _ONEPASS_STEPS,
              "cfg": _ONEPASS_GUIDANCE, "seed": seed, "mask_margin": 8}
    res = engine.inpaint(params, master, mask)
    new_master = res["images"][0].convert("RGB")
    if new_master.size != (W, H):
        new_master = new_master.resize((W, H), Image.LANCZOS)
    new_master = _whiten_background(new_master)
    new_master.save(master_path, "PNG")

    # 重切该视图格落盘 + 重建标注交付图
    views_dir = out_dir / "portrait_views"
    views_dir.mkdir(parents=True, exist_ok=True)
    cell = _slice_onepass_views(new_master)[view]
    if meta.get("transparent"):
        cell = _remove_background(cell)
    p = views_dir / f"{view}.png"
    cell.save(p, "PNG")
    view_rel = str(p.relative_to(DATA_DIR)).replace("\\", "/")
    if view == "front":
        # portrait.png 约定为正面视图（COMIC-037），随 front 同步覆盖
        _sync_portrait_from_views(out_dir)
    _draw_onepass_labels(new_master,
                         asset.get("name") or "asset").save(
        out_dir / "canvas.png", "PNG")

    view_imgs = _load_view_images(out_dir)
    views = meta.get("views")
    if not isinstance(views, dict):
        views = {}
    views[view] = view_rel
    meta["views"] = views
    meta["consistency"] = _views_consistency(list(view_imgs.values()))
    meta["seed"] = res.get("seed", seed)
    meta["regenerated_at"] = _now()
    _append_asset_history(meta, "view", view, view_rel)
    db = get_db_safe()
    if db is not None:
        db.update("comic_assets", {"meta": meta}, "id=?",
                  (asset["asset_id"],))
    asset = {**asset, "meta": meta}
    return {"asset": asset, "view": view, "file_path": view_rel}


def _regenerate_view_zviews(asset: dict, view: str, prompt_zh: str,
                            meta: dict) -> dict:
    """zviews 资产单视图重生（Z2 2026-09-15）：该视图 Z-Image 单独
    重生成（参考图条件锚身份，同种子基址），其余三格原样保留 →
    重合成 master 1×4 拼版与中文标注交付图。"""
    import random

    from PIL import Image

    if view not in _TURNAROUND_VIEWS:
        raise ApiError(40008, f"未知视图: {view}")
    rel_path = (asset.get("file_path") or "").strip()
    if not rel_path:
        raise ApiError(40008, "资产缺少主图文件，无法定位视图目录",
                       detail={"asset_id": asset.get("asset_id")})
    out_dir = (DATA_DIR / rel_path).parent
    views_dir = out_dir / "portrait_views"
    views_dir.mkdir(parents=True, exist_ok=True)
    try:
        seed = int(meta.get("seed", -1))
    except (TypeError, ValueError):
        seed = -1
    if seed < 0:
        seed = random.randint(0, 2 ** 31 - 1)
    idx = _TURNAROUND_VIEWS.index(view)
    ref_image = _load_onepass_reference(out_dir)
    params = {"prompt": _zview_view_prompt(prompt_zh, view),
              "seed": (seed + idx) % (2 ** 31),
              "width": _ZVIEW_SIZE_W, "height": _ZVIEW_SIZE_H,
              "steps": 8, "cfg": 1.0, "model": "z-image-turbo"}
    _ref = (_zview_prep_ref(ref_image, _ZVIEW_SIZE_W, _ZVIEW_SIZE_H)
            if _zview_use_ref(view, ref_image) else None)
    r = comfy_paint_generate(params, ref_image=_ref)
    img = _zview_whiten(r["images"][0].convert("RGB"))
    if bool(meta.get("transparent")):
        img = _remove_background(img)
    p = views_dir / f"{view}.png"
    img.save(p, "PNG")
    view_rel = str(p.relative_to(DATA_DIR)).replace("\\", "/")
    if view == "front":
        # portrait.png 约定为正面视图（COMIC-037），随 front 同步覆盖
        _sync_portrait_from_views(out_dir)

    # 重合成 master 1×4 拼版 + 中文标注 canvas
    view_imgs: list = []
    for v in _TURNAROUND_VIEWS:
        with Image.open(views_dir / f"{v}.png") as im:
            view_imgs.append(im.convert("RGB").copy())
    master = Image.new("RGB", (_ONEPASS_W, _ONEPASS_H), "white")
    cell_w, cell_h = _ONEPASS_W // 4, _ONEPASS_H
    for i, im in enumerate(view_imgs):
        master.paste(im.resize((cell_w, cell_h), Image.LANCZOS),
                     (i * cell_w, 0))
    master.save(out_dir / "master.png", "PNG")
    _draw_onepass_labels(master,
                         asset.get("name") or "asset").save(
        out_dir / "canvas.png", "PNG")

    views = meta.get("views")
    if not isinstance(views, dict):
        views = {}
    views[view] = view_rel
    meta["views"] = views
    meta["consistency"] = _views_consistency(view_imgs)
    meta["canvas"] = str((out_dir / "canvas.png").relative_to(
        DATA_DIR)).replace("\\", "/")
    meta["master"] = str((out_dir / "master.png").relative_to(
        DATA_DIR)).replace("\\", "/")
    if ref_image is not None:
        meta["ref_used"] = True
    else:
        meta.pop("ref_used", None)
    meta["regenerated_at"] = _now()
    _append_asset_history(meta, "view", view, view_rel)
    db = get_db_safe()
    if db is not None:
        db.update("comic_assets", {"meta": meta}, "id=?",
                  (asset["asset_id"],))
    asset = {**asset, "meta": meta}
    return {"asset": asset, "view": view, "file_path": view_rel}
# 本项目仅供学习使用，商业授权请+Q 3559331368
