"""漫画模块 C4「整页导出」单测（2026-09-08）。

覆盖：
- 极简 PDF 写入器结构（%PDF 头/页数/xref 偏移可解析）；
- PNG 长图合成（面板高度累加+间距）与气泡烘焙路径；
- API 冒烟（tmp 库+tmp DATA_DIR 假关键帧图）：PNG/PDF 双格式落盘、
  无画格分格诚实跳过、空项目诚实报错。
"""
from __future__ import annotations

import io
import pathlib

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.api.manga import comic_export as ce
from backend.api.manga.common import _ensure_storyboard, _make_row, _now, _public_row_to_db
from backend.data import database as db_mod
from backend.data.database import Database


def _fake_jpeg(w: int = 320, h: int = 180, color=(120, 140, 200)) -> bytes:
    im = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    im.save(buf, format="JPEG")
    return buf.getvalue()


# ── 纯函数 ───────────────────────────────────────────────────────

def test_pdf_writer_structure():
    pages = [(_fake_jpeg(color=(200, 60, 60)), 320, 180),
             (_fake_jpeg(color=(60, 200, 90)), 320, 180)]
    pdf = ce.jpegs_to_pdf(pages)
    assert pdf.startswith(b"%PDF-1.4")
    assert b"/Count 2" in pdf, "页数须为 2"
    assert pdf.count(b"/Type /Page ") == 2
    assert pdf.rstrip().endswith(b"%%EOF")
    # xref 偏移可回解析（每行 20 字节对齐的 N 项表）
    tail = pdf.rstrip()
    sx = int(tail.split(b"startxref\n")[1].split(b"\n%%EOF")[0])
    assert pdf[sx:sx + 4] == b"xref", "startxref 必须指向 xref 表头"
    assert b"/DCTDecode" in pdf, "JPEG 直嵌须走 DCTDecode"


def test_compose_png_stacks_panels():
    panels = [(Image.new("RGB", (1280, 720)), "台词甲"),
              (Image.new("RGB", (640, 360)), "")]
    canvas = ce.compose_png(panels, None)  # 无字体=跳过气泡
    # 全部面板等宽 1280：640×360 面板按比例拉到 1280×720
    expect_h = 720 + 720 + ce._GUTTER * 1 + ce._MARGIN * 2
    assert canvas.height == expect_h and canvas.width == 1280 + ce._MARGIN * 2


def test_draw_bubble_needs_font():
    im = Image.new("RGB", (640, 360))
    ce.draw_bubble(im, "无字体路径时不画", None)
    # 无字体：图像应保持原样（取角落像素比对）
    assert im.getpixel((5, 5)) == (0, 0, 0)


@pytest.mark.skipif(not pathlib.Path(ce._BUBBLE_FONT_CANDIDATES[0]).is_file(),
                    reason="本机无微软雅黑（CI 环境）")
def test_draw_bubble_bakes_text():
    im = Image.new("RGB", (640, 360), (10, 20, 30))
    ce.draw_bubble(im, "你好漫画", ce._BUBBLE_FONT_CANDIDATES[0])
    assert im.getpixel((40, 40)) != (10, 20, 30), "气泡区域应被覆盖"


# ── API 冒烟（tmp 库 + tmp DATA_DIR）───────────────────────────

@pytest.fixture()
def field(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = Database(tmp_path / "export.db")
    monkeypatch.setattr(db_mod, "_db_instance", db)
    monkeypatch.setattr(ce, "DATA_DIR", data_dir)
    from backend.main import app
    return SimpleNamespaceLike(client=TestClient(app, base_url="http://127.0.0.1"),
                               db=db, data_dir=data_dir)


class SimpleNamespaceLike:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _seed_project_with_keyframes(field, n_panels: int, dialogue: str = "测试台词") -> str:
    """建项目+分格行+假关键帧图，返回 project_id。"""
    r = field.client.post("/api/v1/comic/project/create",
                          json={"name": f"导出{n_panels}", "project_type": "comic"})
    pid = r.json()["data"]["project_id"]
    sb = _ensure_storyboard(field.db, pid)
    for i in range(n_panels):
        row = _make_row(i + 1, description=f"格{i + 1}", original_dialogue=dialogue)
        field.db.insert("storyboard_rows", _public_row_to_db(row, sb["id"], sort_index=i))
        # 假关键帧：真实小图落 tmp DATA_DIR
        im = Image.new("RGB", (320, 180), (30 + i * 40, 90, 160))
        kf_dir = field.data_dir / "keyframes" / pid
        kf_dir.mkdir(parents=True, exist_ok=True)
        rel = f"keyframes/{pid}/p{i}.png"
        im.save(field.data_dir / rel)
        field.db.insert("keyframes", {
            "id": f"kf{i}", "project_id": pid, "row_id": row["id"],
            "version": 1, "file_path": rel, "prompt": "", "status": "done",
            "is_current": 1, "created_at": _now()})
    # 一行无关键帧（诚实跳过用）
    row = _make_row(n_panels + 1, description="没画面的格")
    field.db.insert("storyboard_rows", _public_row_to_db(row, sb["id"], sort_index=n_panels))
    return pid


def test_export_png_and_pdf(field):
    pid = _seed_project_with_keyframes(field, 2)
    r = field.client.post("/api/v1/manga/comic/export-page",
                          json={"project_id": pid, "format": "png"})
    assert r.json()["success"], r.text
    d = r.json()["data"]
    assert d["pages"] == 2 and d["skipped_shots"] == [3]
    out = field.data_dir / d["file_path"]
    assert out.is_file() and out.suffix == ".png"
    with Image.open(out) as im:
        im.verify()

    r2 = field.client.post("/api/v1/manga/comic/export-page",
                           json={"project_id": pid, "format": "pdf"})
    d2 = r2.json()["data"]
    assert d2["pages"] == 2
    pdf = (field.data_dir / d2["file_path"]).read_bytes()
    assert pdf.startswith(b"%PDF") and b"/Count 2" in pdf


def test_export_empty_project_honest_error(field):
    r = field.client.post("/api/v1/comic/project/create",
                          json={"name": "空导出", "project_type": "comic"})
    pid = r.json()["data"]["project_id"]
    r2 = field.client.post("/api/v1/manga/comic/export-page",
                           json={"project_id": pid, "format": "png"})
    body = r2.json()
    assert body["success"] is False and "没有已生成分格画面" in str(body.get("error"))
