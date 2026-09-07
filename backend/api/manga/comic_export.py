"""漫画模块 C4：整页导出（PNG 长图 / PDF 多页，2026-09-08）。

把漫画项目的当前关键帧按分格顺序拼成成品：
- PNG：单列长图（面板等宽 1280，白底+间距），可选烘焙台词气泡；
- PDF：每格一页（零依赖极简 PDF 1.4 写入器，内嵌 JPEG/DCTDecode——
  离线产品不引入 reportlab/fpdf 等新包）；
- 台词气泡： storyboard_rows.original_dialogue（与前端覆盖层同源），
  PIL 绘制（微软雅黑等系统 CJK 字体，缺字体则跳过气泡并如实标注）。

落盘 DATA_DIR/generated/exports/（媒体白名单目录，/manga/media 可回读）。
诚实约束：无关键帧的分格跳过并在响应中列明（不伪造占位画面）。
"""
from __future__ import annotations

import io
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body
from pydantic import BaseModel, Field, ValidationError

from ...config import DATA_DIR
from ...data.database import get_db_safe
from ...middleware.error_handler import ApiError, ok
from .common import _now

logger = logging.getLogger("omnispace.api.manga.comic_export")

router = APIRouter()

_PANEL_W = 1280          # 长图面板宽（16:9 面板 → 高 720）
_GUTTER = 24              # 面板间距
_MARGIN = 24              # 长图左右留白
_BUBBLE_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",     # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",   # 黑体
    r"C:\Windows\Fonts\simsun.ttc",   # 宋体
)


class ComicExportRequest(BaseModel):
    """整页导出请求体。"""
    project_id: str = Field(min_length=1, max_length=64)
    format: str = Field(default="png", pattern="^(png|pdf)$")
    # 是否把台词气泡烘焙进图（前端覆盖层的导出镜像）
    with_bubbles: bool = True


def _find_cjk_font() -> str | None:
    """系统 CJK 字体探测（缺失返回 None=跳过气泡，不伪造）。"""
    for p in _BUBBLE_FONT_CANDIDATES:
        if Path(p).is_file():
            return p
    return None


def draw_bubble(img: Any, text: str, font_path: str | None) -> None:
    """在面板顶部绘制台词气泡（原地修改，PIL Image）。

    气泡=左上白色圆角矩形+黑字，与前端覆盖层同位置口径（面板顶部）。
    字体缺失时静默跳过（调用方以 baked 标记如实上报）。
    """
    if not text.strip() or not font_path:
        return
    from PIL import ImageDraw, ImageFont

    draw = ImageDraw.Draw(img, "RGBA")
    fs = max(20, img.height // 22)
    try:
        font = ImageFont.truetype(font_path, fs)
    except OSError:
        return
    # CJK 简单折行：约每行 (面板宽-边距)/字宽 个字符
    max_chars = max(8, int((img.width - 80) / fs))
    lines = [text[i:i + max_chars] for i in range(0, len(text), max_chars)]
    lh = int(fs * 1.45)
    bw = min(img.width - 48, max_chars * fs + 36)
    bh = len(lines) * lh + 22
    x0, y0 = 20, 16
    draw.rounded_rectangle(
        [x0, y0, x0 + bw, y0 + bh], radius=14,
        fill=(255, 255, 255, 235), outline=(30, 30, 30, 200), width=2)
    for i, ln in enumerate(lines):
        draw.text((x0 + 18, y0 + 11 + i * lh), ln, font=font,
                  fill=(20, 20, 20, 255))


def _load_panels(project_id: str) -> tuple[list[tuple[Any, str]], list[int]]:
    """取项目分格的当前关键帧图（PIL），返回 ((图, 台词), ...) 与跳过镜号表。"""
    from PIL import Image

    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法导出")
    rows = db.query(
        "SELECT id, shot_number, original_dialogue FROM storyboard_rows"
        " WHERE storyboard_id=(SELECT id FROM storyboards WHERE project_id=?)"
        " ORDER BY sort_index ASC, shot_number ASC", (project_id,))
    panels: list[tuple[Any, str]] = []
    skipped: list[int] = []
    for r in rows:
        kf = db.query_one(
            "SELECT file_path FROM keyframes WHERE row_id=? AND is_current=1",
            (r["id"],))
        if kf is None:  # 版本历史里挑最新作兜底
            kf = db.query_one(
                "SELECT file_path FROM keyframes WHERE row_id=?"
                " ORDER BY version DESC LIMIT 1", (r["id"],))
        if kf is None or not kf.get("file_path"):
            skipped.append(int(r.get("shot_number") or 0))
            continue
        path = DATA_DIR / str(kf["file_path"])
        try:
            im = Image.open(path).convert("RGB")
        except OSError:
            skipped.append(int(r.get("shot_number") or 0))
            continue
        panels.append((im, str(r.get("original_dialogue") or "")))
    return panels, skipped


def compose_png(panels: list[tuple[Any, str]], font_path: str | None) -> Any:
    """单列长图合成（面板等宽 1280，白底，可选烘焙气泡）。"""
    from PIL import Image

    if not panels:
        raise ApiError(40008, "没有可导出的分格画面（先完成生图）")
    resized = []
    for im, dialogue in panels:
        h = int(im.height * _PANEL_W / im.width)
        panel = im.resize((_PANEL_W, h))
        if dialogue:
            draw_bubble(panel, dialogue, font_path)
        resized.append(panel)
    total_h = sum(p.height for p in resized) + _GUTTER * (len(resized) - 1) + _MARGIN * 2
    canvas = Image.new("RGB", (_PANEL_W + _MARGIN * 2, total_h), (250, 250, 250))
    y = _MARGIN
    for panel in resized:
        canvas.paste(panel, (_MARGIN, y))
        y += panel.height + _GUTTER
    return canvas


def _img_to_jpeg(im: Any, quality: int = 88) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def jpegs_to_pdf(pages: list[tuple[bytes, int, int]]) -> bytes:
    """极简 PDF 1.4 写入器：每页一张 JPEG（DCTDecode 直嵌）。

    零依赖（离线铁律不引 reportlab）；对象布局：
    1=Catalog 2=Pages，第 i 页三元组 = 3+3i(Page) 4+3i(Content) 5+3i(Image)。
    """
    if not pages:
        raise ApiError(40008, "没有可导出的分格画面")
    n = len(pages)
    kids = " ".join(f"{3 + 3 * i} 0 R" for i in range(n))
    objs: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
    }
    for i, (jpeg, w, h) in enumerate(pages):
        pid, cid, iid = 3 + 3 * i, 4 + 3 * i, 5 + 3 * i
        objs[pid] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}]"
            f" /Resources << /XObject << /Im0 {iid} 0 R >> >>"
            f" /Contents {cid} 0 R >>").encode()
        content = f"q {w} 0 0 {h} 0 0 cm /Im0 Do Q".encode()
        objs[cid] = (
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
            + content + b"\nendstream")
        objs[iid] = (
            (f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h}"
             f" /ColorSpace /DeviceRGB /BitsPerComponent 8"
             f" /Filter /DCTDecode /Length {len(jpeg)} >>").encode()
            + b"\nstream\n" + jpeg + b"\nendstream")
    max_id = max(objs)
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for oid in sorted(objs):
        offsets[oid] = out.tell()
        out.write(f"{oid} 0 obj\n".encode() + objs[oid] + b"\nendobj\n")
    xref_pos = out.tell()
    out.write(f"xref\n0 {max_id + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for oid in range(1, max_id + 1):
        out.write(f"{offsets.get(oid, 0):010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {max_id + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF".encode())
    return out.getvalue()


@router.post("/manga/comic/export-page")
def comic_export_page(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """整页导出：当前关键帧 → PNG 长图 / PDF 多页（可选烘焙台词气泡）。"""
    try:
        req = ComicExportRequest(**(body or {}))
    except ValidationError as exc:
        raise ApiError("PARAM_INVALID",
                       "参数不合法：" + "; ".join(
                           f"{'/'.join(str(loc) for loc in e['loc'])}: {e['msg']}"
                           for e in exc.errors())) from exc
    panels, skipped = _load_panels(req.project_id)
    if not panels:
        raise ApiError(40008,
                       "该项目没有已生成分格画面，先完成分格生图再导出",
                       detail={"skipped_shots": skipped})
    font_path = _find_cjk_font() if req.with_bubbles else None
    out_dir = DATA_DIR / "generated" / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    if req.format == "png":
        canvas = compose_png(panels, font_path)
        out_path = out_dir / f"comic_{req.project_id[:8]}_{ts}.png"
        canvas.save(out_path, format="PNG")
        pages = len(panels)
    else:
        pages_list = []
        for im, dialogue in panels:
            if dialogue and font_path:
                draw_bubble(im, dialogue, font_path)
            pages_list.append((_img_to_jpeg(im), im.width, im.height))
        pdf_bytes = jpegs_to_pdf(pages_list)
        out_path = out_dir / f"comic_{req.project_id[:8]}_{ts}.pdf"
        out_path.write_bytes(pdf_bytes)
        pages = len(pages_list)
    rel = str(out_path.relative_to(DATA_DIR)).replace("\\", "/")
    logger.info("漫画整页导出: project=%s format=%s pages=%d skipped=%d bubble_font=%s",
                req.project_id, req.format, pages, len(skipped),
                bool(font_path))
    return ok({
        "file_path": rel,
        "format": req.format,
        "pages": pages,
        "baked_bubbles": bool(font_path),
        "skipped_shots": skipped,
        "exported_at": _now(),
    })
