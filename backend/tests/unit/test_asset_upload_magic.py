"""资产上传魔数校验回归（2026-09-02 B4 污染实弹修复）。

实弹命中：/comic/asset/upload 与图片替换端点只校验扩展名+大小，
`<script>` HTML / MZ EXE 改名 .png 会先 write_bytes 落盘、PIL 尺寸
读取失败被吞（不阻断登记），伪装文件入库并可经 /manga/media 回读。
修复 = _assert_image_magic（PIL 解码+格式白名单+verify）在落盘前拒绝，
三个上传位（reference/upload/replace）统一设防。

B9（2026-09-13）：AST 沙箱改真 import；原 ApiError 等价桩直接换
真 ApiError（断言处按 .code 判别，形态不变）。
"""
from __future__ import annotations

import io
from pathlib import Path

import backend.api.manga.comic_asset as _asset_mod
from backend.middleware.error_handler import ApiError

ASSET_PY = (Path(__file__).resolve().parents[2]
            / "api" / "manga" / "comic_asset.py")

_StubApiError = ApiError  # 兼容历史断言命名（真对象）


def _load_magic_fn():
    assert hasattr(_asset_mod, "_assert_image_magic"), (
        "comic_asset.py 中未找到 _assert_image_magic")
    return _asset_mod._assert_image_magic


def _png_bytes() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(buf, "PNG")
    return buf.getvalue()


def test_html_masquerade_rejected() -> None:
    fn = _load_magic_fn()
    try:
        fn(b"<script>alert(1)</script>", "evil.png")
        raise AssertionError("HTML 伪装应被拒绝")
    except _StubApiError as e:
        assert e.code == "UNSUPPORTED_FORMAT"
        assert "魔数" in e.message


def test_exe_masquerade_rejected() -> None:
    fn = _load_magic_fn()
    try:
        fn(b"MZ\x90\x00\x03\x00\x00\x00\x04", "evil.png")
        raise AssertionError("EXE 伪装应被拒绝")
    except _StubApiError as e:
        assert e.code == "UNSUPPORTED_FORMAT"


def test_valid_png_passes() -> None:
    fn = _load_magic_fn()
    fn(_png_bytes(), "ok.png")  # 不抛即通过


def test_wrong_real_format_rejected() -> None:
    # BMP 是有效图片但不在白名单（png/jpg/jpeg/webp）——真实格式与
    # 声明不符必须拒绝
    fn = _load_magic_fn()
    bmp = (b"BM" + b"\x00" * 12 + (12).to_bytes(4, "little")
           + b"\x28\x00\x00\x00" + (1).to_bytes(4, "little")
           + (1).to_bytes(4, "little") + b"\x01\x00\x18\x00"
           + b"\x00" * 24 + b"\xff\x00\x00")
    try:
        fn(bmp, "fake.bmp.png")
        raise AssertionError("BMP 伪装 .png 应被拒绝")
    except _StubApiError as e:
        assert e.code == "UNSUPPORTED_FORMAT"


def test_all_three_upload_sites_guarded() -> None:
    src = ASSET_PY.read_text(encoding="utf-8")
    assert src.count("_assert_image_magic(raw, file.filename)") >= 3, (
        "三个上传位（reference/upload/replace）必须全部前置魔数校验")
# 本项目仅供学习使用，商业授权请+Q 3559331368
