# -*- coding: utf-8 -*-
"""上传类型闸门单元测试（TASK-P0-03，审计发现 P27）。

直接测 upload_guard.validate 纯函数：后缀白名单、可执行体黑名单、
魔数嗅验三层。不依赖 FastAPI/网络/磁盘。
"""
from __future__ import annotations

import pytest

from backend.middleware import upload_guard
from backend.middleware.upload_guard import UploadRejected, validate

# 经典 PE 头（MZ + DOS stub，含 NUL 字节）——".exe 改名"攻击载荷
EXE_BYTES = (b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00"
             b"\xb8\x00\x00\x00\x00\x00\x00\x00@\x00\x00\x00\x00\x00\x00\x00")
ELF_BYTES = b"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00"
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 32
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 16
PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"\x00" * 0 + b"1 0 obj\n<<>>\nendobj\n"
ZIP_BYTES = b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + b"\x00" * 40
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 16


def _reject(filename: str, data: bytes, table) -> str:
    with pytest.raises(UploadRejected) as ei:
        validate(filename, data, table)
    return ei.value.reason


# ── 第一层：后缀白名单 ──────────────────────────────────────────

def test_rejects_missing_extension():
    assert _reject("readme", b"hello", upload_guard.DATASET_TABLE) == "ext"

def test_rejects_exe_extension():
    assert _reject("evil.exe", EXE_BYTES, upload_guard.MEDIA_TABLE) == "ext"

def test_rejects_disallowed_ext_on_document_table():
    assert _reject("doc.xls", b"abc", upload_guard.DOCUMENT_TABLE) == "ext"


# ── 第二层：可执行体绝对黑名单（伪装扩展名拦截核心）──────────────

def test_rejects_exe_disguised_as_jsonl():
    assert _reject("evil.jsonl", EXE_BYTES, upload_guard.DATASET_TABLE) == "exec"

def test_rejects_exe_disguised_as_docx():
    assert _reject("evil.docx", EXE_BYTES, upload_guard.DOCUMENT_TABLE) == "exec"

def test_rejects_exe_disguised_as_png():
    assert _reject("evil.png", EXE_BYTES, upload_guard.MEDIA_TABLE) == "exec"

def test_rejects_elf_disguised_as_txt():
    assert _reject("evil.txt", ELF_BYTES, upload_guard.DOCUMENT_TABLE) == "exec"


# ── 第三层：魔数与扩展名一致性 ──────────────────────────────────

def test_rejects_png_content_with_jpg_extension():
    assert _reject("fake.jpg", PNG_BYTES, upload_guard.MEDIA_TABLE) == "mismatch"

def test_rejects_zip_content_with_pdf_extension():
    assert _reject("fake.pdf", ZIP_BYTES, upload_guard.DOCUMENT_TABLE) == "mismatch"

def test_rejects_text_with_nul_bytes():
    assert _reject("data.jsonl", b'{"a":1}\n\x00\x00', upload_guard.DATASET_TABLE) == "mismatch"

def test_rejects_utf16_text():
    # 带 BOM 的 UTF-16 文本（Python 默认编码即带 BOM）引导用户转 UTF-8
    assert _reject("data.txt", "中文内容".encode("utf-16"),
                   upload_guard.DOCUMENT_TABLE) == "mismatch"


# ── 正常路径放行（拦截不得误伤）────────────────────────────────

def test_allows_utf8_jsonl():
    validate("ok.jsonl", '{"instruction":"a","input":"","output":"b"}\n'.encode(),
             upload_guard.DATASET_TABLE)

def test_allows_chinese_utf8_text():
    validate("中文文档.md", "# 标题\n\n正文段落。\n".encode(),
             upload_guard.DOCUMENT_TABLE)

def test_allows_real_pdf_and_docx():
    validate("ok.pdf", PDF_BYTES, upload_guard.DOCUMENT_TABLE)
    validate("ok.docx", ZIP_BYTES, upload_guard.DOCUMENT_TABLE)

def test_allows_real_image_and_video():
    validate("ok.png", PNG_BYTES, upload_guard.MEDIA_TABLE)
    validate("ok.jpg", JPEG_BYTES, upload_guard.MEDIA_TABLE)
    validate("ok.mp4", MP4_BYTES, upload_guard.MEDIA_TABLE)


# ── 表完整性：三张表覆盖端点声明的全部扩展名 ─────────────────────

def test_tables_cover_endpoint_declarations():
    assert set(upload_guard.DOCUMENT_TABLE) == {".pdf", ".docx", ".txt", ".md"}
    assert set(upload_guard.DATASET_TABLE) == {".jsonl", ".json", ".txt"}
    assert set(upload_guard.MEDIA_TABLE) == {
        ".png", ".jpg", ".jpeg", ".webp", ".bmp",
        ".mp4", ".webm", ".mov", ".mkv", ".avi"}
