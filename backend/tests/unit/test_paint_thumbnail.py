"""绘画画廊缩略图端点回归（#5 2026-09-02 卡顿修复）。

病根：历史图 2560×1440 PNG（2-4MB/张），画廊每卡解码 3.7MP，
滚动几十张即百 MB 级解码抖动。修复 = /draw/image/{name}?thumb=1
返回 512px JPEG（首访生成、.thumbs/ 磁盘缓存、原子替换），
网格用缩略图、灯箱用原图；历史删除同步清缩略图防孤儿。

AST 沙箱（同 test_keyframe_char_anchor 约定）：提取
_paint_thumbnail + 常量到隔离命名空间。
"""
from __future__ import annotations

import ast
from pathlib import Path

DRAW_PY = (Path(__file__).resolve().parents[2] / "api" / "draw.py")


def _load_thumb_fn():
    tree = ast.parse(DRAW_PY.read_text(encoding="utf-8"))
    body = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == "_paint_thumbnail":
            body.append(n)
        elif isinstance(n, ast.Assign):
            tgt = n.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id in (
                    "_THUMB_DIR_NAME", "_THUMB_SIZE"):
                body.append(n)
    assert body, "draw.py 中未找到 _paint_thumbnail 及常量"
    ns: dict = {"log": type("L", (), {
        "debug": staticmethod(lambda *a, **k: None),
        "warning": staticmethod(lambda *a, **k: None)})()}
    exec(compile(ast.Module(body=body, type_ignores=[]),
                 "<extract>", "exec"), ns)  # noqa: S102 - 测试沙箱
    return ns


def _make_png(path: Path, w: int = 2560, h: int = 1440) -> None:
    from PIL import Image
    Image.new("RGB", (w, h), "steelblue").save(path, "PNG")


def test_thumbnail_generates_and_caches(tmp_path) -> None:
    ns = _load_thumb_fn()
    src = tmp_path / "img.png"
    _make_png(src)
    t1 = ns["_paint_thumbnail"](src)
    assert t1 is not None and t1.is_file()
    assert t1.suffix == ".jpg" and t1.parent.name == ns["_THUMB_DIR_NAME"]
    from PIL import Image
    with Image.open(t1) as im:
        assert max(im.size) <= ns["_THUMB_SIZE"], "缩略图边长须 ≤512"
        assert im.format == "JPEG"
    assert t1.stat().st_size < src.stat().st_size, "缩略图必须显著小于原图"
    # 二次调用命中缓存（同一路径对象，不重生成）
    mtime = t1.stat().st_mtime_ns
    t2 = ns["_paint_thumbnail"](src)
    assert t2 == t1 and t2.stat().st_mtime_ns == mtime


def test_thumbnail_falls_back_on_garbage(tmp_path) -> None:
    ns = _load_thumb_fn()
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image at all")
    assert ns["_paint_thumbnail"](bad) is None, "解码失败必须回落 None"


def test_endpoint_and_delete_wired() -> None:
    src = DRAW_PY.read_text(encoding="utf-8")
    assert "thumb: int = 0" in src, "端点须暴露 thumb 查询参数"
    assert "image/jpeg" in src
    # 历史删除必须同步清缩略图
    assert src.count("_THUMB_DIR_NAME") >= 2, "删除链须引用缩略图目录"
    assert 'f"{target.stem}_{_THUMB_SIZE}.jpg"' in src
