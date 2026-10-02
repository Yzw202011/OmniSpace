"""云端四视图整图直出管线（2026-10-03 用户拍板：四视图/重生也走云端）。

用户给定提示词模板：云端 gpt-image 一次推理出整图——四个视图横排 +
中文角色名/视图标注直接画进图（云端模型中文渲染可靠，绕开本地 PIL
叠印）。出图 1536x1024（gpt-image 原生档，不拉伸）；视图边界用白沟
扫描智能切分（纯白背景下列扫描找视图间隔，比死板四等分容错）。

调用方约定：资产槽（asset.image）绑定云端连接才进来；失败由调用方
落回本地自动链（诚实降级不报死）。透明底不支持（模板即纯白背景）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from ..cloud_provider_service import CloudEndpoint

log = logging.getLogger("omnispace.inference.cloud_turnaround")

# gpt-image 系严格兑现的最大档（横排四视图用横版）
CLOUD_TURNAROUND_W, CLOUD_TURNAROUND_H = 1536, 1024

# 用户模板（2026-10-03 原文，2026-10-02 定稿的视角指令同源）：导语固定，
# {name} 角色名、末尾拼前端角色设定原文。等宽并排句为切分可靠性加注
# （与「各视图下方标注」共同锁布局，语义仍在用户模板意图内）。
_TURNAROUND_PREAMBLE = (
    "【角色】：{name}\n"
    "绘图提示词：生成角色4视图：正面全身、侧面全身、背面全身、"
    "上半身特写。四个视图等宽并排，从左到右依次为：正面全身、"
    "侧面全身、背面全身、上半身特写。纯白色背景，禁止纹理，"
    "全局光照，禁止投影。图片左上角标注中文角色名：{name}；"
    "各视图下方标注中文：正面全身、侧面全身、背面全身、上半身特写；"
    "禁止出现任何其他文字；禁止外语字符。美术风格：3D CG 游戏风格、"
    "高精度 3D 建模、PBR 物理渲染、清晰硬表面质感、游戏实时光影、"
    "干净明暗过渡、写实二次元游戏质感、画面锐利通透。\n"
    "{user_prompt}"
)

# 视图顺序 = 模板声明顺序（左→右），与本地管线 _TURNAROUND_VIEWS 同源
_VIEWS: tuple[str, ...] = ("front", "side", "back", "closeup")


def build_turnaround_cloud_prompt(name: str, prompt: str) -> str:
    """拼云端四视图整图提示词（模板导语 + 角色设定原文）。"""
    return _TURNAROUND_PREAMBLE.format(
        name=(name or "角色").strip(), user_prompt=(prompt or "").strip())


def _find_view_gutters(gray, n: int = 4) -> list[int]:
    """纯白背景列扫描：返回 n-1 条视图分界 x 坐标（找不到足够白沟回退等分）。

    gray: numpy 2D 灰度阵（0-255）。白沟=整列 ≥97% 像素近白（≥245）
    且连续宽度 ≥ 总宽 1%；标注文字列因含笔画天然不满足，正好被排除。
    候选沟按中心位置就近匹配 1/4、2/4、3/4 等分点（防误取画布边缘
    留白或视图内部大面积白区）。
    """
    import numpy as np

    w = gray.shape[1]
    col_white = (np.asarray(gray) >= 245).mean(axis=0) > 0.97
    min_run = max(4, w // 100)
    runs: list[tuple[int, int]] = []
    start = None
    for x, is_white in enumerate(col_white):
        if is_white and start is None:
            start = x
        elif not is_white and start is not None:
            if x - start >= min_run:
                runs.append((start, x))
            start = None
    if start is not None and w - start >= min_run:
        runs.append((start, w))
    centers = [(a + b) // 2 for a, b in runs]
    gutters: list[int] = []
    for i in range(1, n):
        target = round(w * i / n)
        cands = [c for c in centers
                 if not any(abs(c - g) < min_run for g in gutters)]
        if not cands:
            return [round(w * i / n) for i in range(1, n)]
        gutters.append(min(cands, key=lambda c: abs(c - target)))
    return sorted(gutters)


def slice_views_by_white_gutters(image, n: int = 4) -> list:
    """整图按白沟切 n 条视图（PIL.Image → list[PIL.Image]）。

    白沟缺失（如视图贴边/满幅）回退等宽切分，保证恒有 n 条产出。
    """
    import numpy as np
    from PIL import Image

    canvas = image.convert("RGB")
    gray = np.asarray(canvas.convert("L"))
    bounds = _find_view_gutters(gray, n)
    edges = [0, *bounds, canvas.width]
    views: list[Image.Image] = []
    for lo, hi in zip(edges, edges[1:], strict=False):
        if hi - lo < 8:  # 异常窄条防御：并入等分
            cell = canvas.width // n
            return [canvas.crop((i * cell, 0, (i + 1) * cell,
                                 canvas.height)) for i in range(n)]
        views.append(canvas.crop((lo, 0, hi, canvas.height)))
    return views


def generate_turnaround_cloud(
        out_dir: Path, *, name: str, prompt: str,
        cloud_ep: CloudEndpoint,
        on_progress: Callable[[int, str], None] | None = None,
        check_cancel: Callable[[], None] | None = None,
        data_dir: Path | None = None) -> dict:
    """云端整图直出四视图并落盘（文件契约与本地管线一致）。

    落盘：master.png/canvas.png（云端标注已画入，两者同图）、
    portrait_views/{front,side,back,closeup}.png。返回 gen dict 带
    view_imgs 瞬态键（调用方取去算 consistency 后删除）。
    """
    import time

    t0 = time.time()
    full_prompt = build_turnaround_cloud_prompt(name, prompt)

    def _map_cloud_progress(pct: int) -> None:
        if on_progress:
            on_progress(int(5 + max(0, min(100, pct)) * 0.75), "云端生成中")

    if on_progress:
        on_progress(5, "云端整图直出四视图")
    from .cloud_image_client import generate_image
    img = generate_image(
        cloud_ep, full_prompt,
        width=CLOUD_TURNAROUND_W, height=CLOUD_TURNAROUND_H,
        slot="asset.image", check_cancel=check_cancel,
        on_progress=_map_cloud_progress if on_progress else None)
    if img.size != (CLOUD_TURNAROUND_W, CLOUD_TURNAROUND_H):
        from PIL import Image
        img = img.resize((CLOUD_TURNAROUND_W, CLOUD_TURNAROUND_H),
                         Image.Resampling.LANCZOS)
    if on_progress:
        on_progress(85, "白沟切分标注落盘")

    base = data_dir if data_dir is not None else _default_data_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    master_path = out_dir / "master.png"
    img.save(master_path, "PNG")
    canvas_path = out_dir / "canvas.png"
    img.save(canvas_path, "PNG")  # 云端标注已画入，canvas=master 同图
    views_dir = out_dir / "portrait_views"
    views_dir.mkdir(parents=True, exist_ok=True)
    views: dict[str, str] = {}
    view_imgs = slice_views_by_white_gutters(img, len(_VIEWS))
    for view, vimg in zip(_VIEWS, view_imgs, strict=True):
        p = views_dir / f"{view}.png"
        vimg.save(p, "PNG")
        views[view] = str(p.relative_to(base)).replace("\\", "/")
    if on_progress:
        on_progress(97, "云端四视图完成")
    elapsed = time.time() - t0
    log.info("云端整图直出四视图完成: %s %dx%d 耗时=%.1fs",
             name, img.size[0], img.size[1], elapsed)
    return {
        "pipeline": "cloud-onepass",
        "views": views,
        "canvas": str(canvas_path.relative_to(base)).replace("\\", "/"),
        "master": str(master_path.relative_to(base)).replace("\\", "/"),
        "seed": -1,  # 云端不吃 seed（重生=新 roll）
        "model": f"cloud:{cloud_ep.provider_name}:{cloud_ep.model}",
        "prompt_zh": full_prompt,
        "width": img.size[0],
        "height": img.size[1],
        "ref_used": False,
        "view_errors": {},
        "view_imgs": view_imgs,  # 瞬态键：调用方取去算 consistency 后删
    }


def _default_data_dir() -> Path:
    from ...config import DATA_DIR
    return DATA_DIR


def resolve_asset_cloud_endpoint() -> CloudEndpoint | None:
    """资产槽（asset.image）云端端点；未绑定/停用/解析失败返回 None
    （=调用方走本地链，不阻断生成）。"""
    try:
        from ..cloud_provider_service import SLOT_ASSET_IMAGE, get_image_endpoint
        return get_image_endpoint(SLOT_ASSET_IMAGE)
    except Exception:  # noqa: BLE001 - 解析失败按本地
        return None


def regenerate_asset_cloud_image(prompt: str, width: int, height: int,
                                 on_progress=None):
    """普通资产重生的云端生成（未绑定/失败返回 None=调用方走本地）。

    Returns:
        (PIL.Image, "cloud:<服务商名>") 二元组；云端不可用时 None。
    """
    ep = resolve_asset_cloud_endpoint()
    if ep is None:
        return None
    try:
        from .cloud_image_client import generate_image
        img = generate_image(ep, prompt, width=width, height=height,
                             slot="asset.image", on_progress=on_progress)
        return img, f"cloud:{ep.provider_name}"
    except Exception as exc:  # noqa: BLE001 - 云端失败落本地
        log.warning("资产重生云端失败，落回本地引擎: %s", exc)
        return None
