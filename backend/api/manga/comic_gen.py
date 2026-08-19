"""漫画资产生成管线层：三视图转四视图 / 批量生成 / 重生成管线（无路由，由 comic_asset 路由调用）。

TASK-P2-01 自 manga.py 按路由域拆出（原文件 4521 行 → 包）。
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter

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
    _gen_size_for_target,
    _now,
    _remove_background,
    _upscale_to,
)

router = APIRouter()
log = logging.getLogger("omnispace.api.manga.comic_gen")



# ── 角色多视图（四视图）生成（COMIC-033~037）────────────────────────
# 规格（竞品 yl.man-tui.com 对齐，2026-08-14 改造）：四张独立 16:9 图
# （正面全身/侧面全身/背面全身/上半身特写，每张可单独重生），逐视图
# 1280×720 生成 + LANCZOS 2x 上采样至 2560×1440；canvas.png 为 2×2
# 拼图。FLUX.1-dev 未随包时 SDXL 兜底并诚实标注 degraded。

_TURNAROUND_VIEWS = ("front", "side", "back", "closeup")
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

# 资产生成历史留痕上限（meta.history，超出截掉最旧）
_ASSET_HISTORY_MAX = 12


def _sanitize_character_prompt_zh(text: str) -> str:
    """剥掉角色描述词中的版式/标注指令（四视图指令/图片标注/禁止类），
    保留人物外观描述。

    竞品描述词模板含「生成角色4视图：正面全身、侧面全身、背面全身、
    上半身特写。」等整版式指令；逐视图独立生成前必须在中文阶段剥掉，
    否则每张图都会画成 4 宫格。白底/禁止类由 _STYLE_WHITE_BG /
    _STYLE_NEGATIVE 统一兜底。
    """
    import re
    cleaned = text or ""
    for pat in (r"[^。]*[四4]视图[^。]*(?:。|$)",   # 生成角色4视图：…。
                r"图片[左右]上角[^。]*(?:。|$)",     # 图片左上角/右上角…。
                r"[^。]*标注[^。]*(?:。|$)",         # 含「标注」的整句
                r"禁止[^。]*(?:。|$)",               # 禁止纹理/投影等禁令
                r"纯白色背景[^。]*(?:。|$)"):        # 白底（风格词兜底）
        cleaned = re.sub(pat, "", cleaned)
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


def _load_reference_image(out_dir: Path, gen_w: int, gen_h: int):
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
        log.warning("参考图读取失败，回退 txt2img: %s (%s)", ref_path, exc)
        return None


def _generate_single_view(engine, prompt_en: str, view: str, seed: int,
                          out_dir: Path, ref_image=None,
                          transparent: bool = False) -> dict:
    """生成单个角色视图：1280×720 生成 → LANCZOS 2x 上采样 2560×1440
    → 落盘 portrait_views/{view}.png。

    prompt = 译后描述词 + 视图后缀 + _STYLE_PHOTO + _STYLE_WHITE_BG；
    ref_image 非空时走 img2img（strength=0.55），失败回退 txt2img
    （记 ref_fallback，不抛错）。out_dir/portrait_views 须已存在。
    """
    prompt = (prompt_en + _TURNAROUND_VIEW_SUFFIX[view]
              + _STYLE_PHOTO + _STYLE_WHITE_BG)
    params = {"prompt": prompt, "negative": _STYLE_NEGATIVE,
              "steps": 24, "cfg": 7.0,
              "width": _TURNAROUND_GEN_W, "height": _TURNAROUND_GEN_H,
              "seed": seed}
    ref_used = ref_fallback = False
    if ref_image is not None:
        params["strength"] = 0.55
        try:
            result = engine.img2img(params, ref_image)
            ref_used = True
        except Exception as exc:  # noqa: BLE001 - img2img 失败回退 txt2img
            log.warning("视图 %s img2img 失败，回退 txt2img: %s", view, exc)
            ref_fallback = True
            params.pop("strength", None)
            result = engine.generate(params)
    else:
        result = engine.generate(params)
    image = _upscale_to(result["images"][0], _TURNAROUND_W, _TURNAROUND_H)
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
                log.warning("视图读取失败 %s: %s", p, exc)
    return imgs


def _rebuild_turnaround_canvas(out_dir: Path,
                               view_imgs: dict | None = None) -> str:
    """由四视图重建 canvas.png 2×2 拼图（每格 1280×720，总 2560×1440）。

    格序 front/side/back/closeup（左上/右上/左下/右下）；缺失/失败格
    白底占位。view_imgs 缺省时从 portrait_views/ 读盘。
    返回 canvas 的 DATA_DIR 相对路径。
    """
    from PIL import Image
    if view_imgs is None:
        view_imgs = _load_view_images(out_dir)
    cell_w, cell_h = _TURNAROUND_W // 2, _TURNAROUND_H // 2  # 1280×720
    canvas = Image.new("RGB", (_TURNAROUND_W, _TURNAROUND_H),
                       (255, 255, 255))
    for idx, view in enumerate(_TURNAROUND_VIEWS):
        im = view_imgs.get(view)
        if im is None:
            continue
        cell = im.convert("RGB").resize((cell_w, cell_h), Image.LANCZOS)
        canvas.paste(cell, ((idx % 2) * cell_w, (idx // 2) * cell_h))
    canvas_path = out_dir / "canvas.png"
    canvas.save(canvas_path, "PNG")
    return str(canvas_path.relative_to(DATA_DIR)).replace("\\", "/")


def _generate_four_views(engine, prompt_en: str, seed: int,
                         out_dir: Path, transparent: bool = False) -> dict:
    """四视图逐张独立生成（竞品对齐：四张独立 16:9 图，每张可单独重生）。

    四张同 seed 保一致性（seed<0 时先解析为固定随机种子）；资产目录
    reference.png 存在时走 img2img（strength=0.55），失败回退 txt2img。
    单视图失败不阻塞其他视图（per-view 错误记入 errors）；全部失败才
    抛 ApiError。canvas.png 为 2×2 拼图。out_dir 须已存在。
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
    for view in _TURNAROUND_VIEWS:
        try:
            r = _generate_single_view(engine, prompt_en, view, seed,
                                      out_dir, ref_image, transparent)
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
    """同步执行四视图生成（竞品对齐：四张独立 16:9 图逐视图生成 →
    2x 上采样 2560×1440 → 落盘 → 入库）。

    目录结构（COMIC-037）：characters/{name}/portrait.png（=front）
    + portrait_views/{front,side,back,closeup}.png + canvas.png（2×2
    拼图）。由线程池调用（端点为 async，避免阻塞事件循环）。
    """
    # 中文描述词先净化（剥离四视图版式指令，防止逐视图生成时每张都
    # 画成 4 宫格）再译英（SDXL CLIP 不理解中文）；翻译先于 paint 加载。
    prompt_en = _prepare_turnaround_prompt_en(req.prompt)
    engine = get_paint_engine()
    if not engine.is_ready and not engine.ensure_loaded(None):
        status = engine.get_status()
        raise ApiError("PAINT_ENGINE_NOT_READY",
                       status.get("last_error") or "绘画模型未就绪")
    asset_id = uuid.uuid4().hex
    out_dir = _COMIC_ASSET_DIR / req.project_id / "characters" / req.name
    out_dir.mkdir(parents=True, exist_ok=True)
    gen = _generate_four_views(engine, prompt_en, req.seed, out_dir,
                               transparent=req.transparent)
    views = gen["views"]
    # portrait.png 约定为正面视图（COMIC-037 资产目录结构）
    _sync_portrait_from_views(out_dir)
    rel_path = str((out_dir / "portrait.png")
                   .relative_to(DATA_DIR)).replace("\\", "/")

    meta = {"turnaround": True, "width": _TURNAROUND_W,
            "height": _TURNAROUND_H, "views": views,
            "canvas": gen["canvas"],
            "consistency": gen["consistency"],
            "seed": gen["seed"], "model": gen["model"],
            "prompt_en": prompt_en,
            "transparent": bool(req.transparent), "resized": True,
            "degraded": True,
            "degrade_reason": "FLUX.1-dev 未随包：SDXL 兜底逐视图生成四张 "
                              "2560×1440 独立视图（1280×720 生成 + 2x "
                              "上采样），视图一致性为尽力而为"}
    if gen["errors"]:
        # 单视图失败不阻塞其他视图，per-view 错误留痕
        meta["view_errors"] = gen["errors"]
    # ref 标记反映最近一次生成实况（无参考图/未走 img2img 时清除）
    if gen["ref_used"]:
        meta["ref_used"] = True
    else:
        meta.pop("ref_used", None)
    if gen["ref_fallback"]:
        meta["ref_fallback"] = True
    else:
        meta.pop("ref_fallback", None)
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
    return {"asset_id": asset_id, "project_id": req.project_id,
            "kind": "character", "name": req.name, "file_path": rel_path,
            "prompt": req.prompt, "views": views,
            "consistency": gen["consistency"],
            "view_errors": gen["errors"] or None,
            "degraded": True, "degrade_reason": meta["degrade_reason"],
            "meta": meta}


def _regenerate_asset_sync(asset: dict) -> dict:
    """同步重生成资产图（与 _generate_asset_sync 同一 SDXL 生成路径）。

    以资产现有 kind/prompt 重新文生图，覆盖 file_path 指向的图片文件
    （file_path 为空时按资产目录约定新建），并刷新 meta 留痕。
    四视图资产走 _generate_four_views 逐视图独立重生成（与首次生成
    同构）。引擎未就绪抛 PAINT_ENGINE_NOT_READY（由端点收敛为
    degraded 响应）。
    """
    kind = asset.get("kind", "character")
    conf = _ASSET_KIND_CONF.get(kind, _ASSET_KIND_CONF["character"])
    meta = parse_json(asset.get("meta"), {})
    if not isinstance(meta, dict):
        meta = {}
    # 多视图资产必须逐视图独立重生成——否则单肖像模板会把四视图资产
    # 覆盖成单图，构图与描述词不符。
    is_turnaround = bool(meta.get("turnaround"))
    # 中文描述词先译英（SDXL CLIP 不理解中文）；翻译先于 paint 加载，
    # 避免对话/绘画双模型显存换载抖动。四视图路径先净化剥离版式指令。
    if is_turnaround:
        prompt_en = _prepare_turnaround_prompt_en(asset["prompt"])
    else:
        prompt_en = translate_prompt_zh2en(asset["prompt"])
    engine = get_paint_engine()
    if not engine.is_ready and not engine.ensure_loaded(None):
        status = engine.get_status()
        raise ApiError("PAINT_ENGINE_NOT_READY",
                       status.get("last_error") or "绘画模型未就绪")
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
        # 与首次四视图生成同构：逐视图独立生成 → portrait=front → 2×2 canvas
        gen = _generate_four_views(engine, prompt_en, -1, out_path.parent,
                                   transparent=bool(meta.get("transparent")))
        _sync_portrait_from_views(out_path.parent)
        meta["views"] = gen["views"]
        meta["canvas"] = gen["canvas"]
        meta["consistency"] = gen["consistency"]
        if gen["errors"]:
            meta["view_errors"] = gen["errors"]
        else:
            meta.pop("view_errors", None)
        # ref 标记反映最近一次生成实况（无参考图/未走 img2img 时清除）
        if gen["ref_used"]:
            meta["ref_used"] = True
        else:
            meta.pop("ref_used", None)
        if gen["ref_fallback"]:
            meta["ref_fallback"] = True
        else:
            meta.pop("ref_fallback", None)
        width, height = _TURNAROUND_W, _TURNAROUND_H
        seed_out, model_out = gen["seed"], gen["model"]
    else:
        # 参考图风格对齐：角色/道具纯白底，场景写实影调不加白底
        style = _STYLE_PHOTO + (_STYLE_WHITE_BG
                                if kind in ("character", "prop") else "")
        prompt = conf["tpl"].format(prompt=prompt_en) + style
        width = max(256, min(IMG_TARGET_W,
                             int(meta.get("width") or IMG_TARGET_W)))
        height = max(256, min(IMG_TARGET_H,
                              int(meta.get("height") or IMG_TARGET_H)))
        gen_w, gen_h = _gen_size_for_target(width, height)
        params = {"prompt": prompt, "negative": _STYLE_NEGATIVE,
                  "steps": 24, "cfg": 7.0,
                  "width": gen_w, "height": gen_h, "seed": -1}
        result = engine.generate(params)
        image = _upscale_to(result["images"][0], width, height)
        if kind == "prop" and meta.get("transparent"):
            image = _remove_background(image)
        image.save(out_path, "PNG")
        seed_out = result.get("seed", -1)
        model_out = result.get("model", "")
    meta.update({"width": width, "height": height,
                 "seed": seed_out,
                 "model": model_out,
                 "prompt_en": prompt_en,
                 "regenerated_at": _now()})
    _append_asset_history(meta, "regenerate", None, rel_path)
    db = get_db_safe()
    if db is not None:
        db.update("comic_assets", {"file_path": rel_path, "meta": meta},
                  "id=?", (asset["asset_id"],))
    asset = {**asset, "file_path": rel_path, "meta": meta}
    return asset


def _regenerate_view_sync(asset: dict, view: str, prompt_zh: str) -> dict:
    """同步重生成四视图资产的单个视图（线程池调用）。

    净化 → 译英 → 该视图后缀，1280×720 生成 → LANCZOS 2x 上采样
    2560×1440 覆盖 portrait_views/{view}.png；view==front 时同步
    覆盖 portrait.png；重建 canvas.png 2×2 拼图并刷新 meta。
    资产目录 reference.png 存在时走 img2img（strength=0.55），
    img2img 失败回退 txt2img（记 ref_fallback，不抛错）。
    """
    meta = parse_json(asset.get("meta"), {})
    if not isinstance(meta, dict):
        meta = {}
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
