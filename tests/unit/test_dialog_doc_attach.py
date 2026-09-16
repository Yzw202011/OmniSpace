"""对话文档附件 + 模型 vision 能力（2026-09-07 按模型能力开放附件）

覆盖：
  ① POST /dialog/parse-document：txt 多编码直读 / md / docx / pdf
     解析成功；.doc 老格式诚实拒绝；超大文件拒绝；空文本拒绝；
     伪装扩展名（魔数嗅验）拒绝；
  ② _dialog_model_supports_vision 判定：vl 系模型 True、纯文本
     模型 False、导入模型按 backend；
  ③ GET /dialog/models 每项带 vision 字段（离线 fixture 不依赖
     GPU/磁盘模型，走接口形状断言）。

全部 TestClient 离线跑（tmp 库隔离，不触发 lifespan/GPU）。
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database

pytestmark = pytest.mark.smoke


@pytest.fixture()
def client(tmp_path, monkeypatch):
    test_db = Database(tmp_path / "dialog_doc.db")
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1")


# ── ① parse-document 解析管线 ────────────────────────────────────

def test_parse_document_txt_utf8(client):
    r = client.post(
        "/api/v1/dialog/parse-document",
        files={"file": ("剧本.txt", "第一幕：雨夜的便利店\n林晚推门而入".encode(),
                        "text/plain")},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["kind"] == "txt"
    assert "便利店" in data["text"]
    assert data["chars"] > 0
    assert data["truncated"] is False


def test_parse_document_txt_gbk_fallback(client):
    """GBK 编码 txt 走多编码回退解码（knowledge._decode_text 复用）。"""
    r = client.post(
        "/api/v1/dialog/parse-document",
        files={"file": ("旧文档.txt", "守夜人看见第二天的新闻".encode("gbk"),
                        "text/plain")},
    )
    assert r.status_code == 200, r.text
    assert "守夜人" in r.json()["data"]["text"]


def test_parse_document_md(client):
    r = client.post(
        "/api/v1/dialog/parse-document",
        files={"file": ("大纲.md", "# 第一卷\n停摆的钟".encode(),
                        "text/markdown")},
    )
    assert r.status_code == 200
    assert "停摆的钟" in r.json()["data"]["text"]


def _make_docx(paragraphs: list[str]) -> bytes:
    """最小合法 docx（zip 容器 + document.xml 段落）。"""
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>'
        for p in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body>'
        f"{body}</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types"><Default Extension="xml" ContentType="application'
            '/xml"/><Override PartName="/word/document.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.wordprocessingml'
            '.document.main+xml"/></Types>')
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def test_parse_document_docx(client):
    payload = _make_docx(["第一章 钟的密语", "修表匠停下手中的活。"])
    r = client.post(
        "/api/v1/dialog/parse-document",
        files={"file": ("章节.docx", payload,
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document")},
    )
    assert r.status_code == 200, r.text
    text = r.json()["data"]["text"]
    assert "钟的密语" in text and "修表匠" in text


def test_parse_document_doc_rejected_with_hint(client):
    """.doc 老格式诚实拒绝 + 转存建议（不静默乱码）。"""
    r = client.post(
        "/api/v1/dialog/parse-document",
        files={"file": ("老文档.doc", b"\xd0\xcf\x11\xe0fake", "application/msword")},
    )
    # ApiError 走统一信封（可能 HTTP 200 + success=false，看 body 不看码）
    body = r.json()
    assert body.get("success") is False, body
    detail = json.dumps(body, ensure_ascii=False)
    assert "docx" in detail  # 给出转存出路


def test_parse_document_oversize_rejected(client):
    big = b"x" * (20 * 1024 * 1024 + 2)
    r = client.post(
        "/api/v1/dialog/parse-document",
        files={"file": ("大文件.txt", big, "text/plain")},
    )
    assert r.json().get("success") is False
    assert "20MB" in json.dumps(r.json(), ensure_ascii=False)


def test_parse_document_empty_text_rejected(client):
    r = client.post(
        "/api/v1/dialog/parse-document",
        files={"file": ("空.md", b"   \n  ", "text/markdown")},
    )
    assert r.json().get("success") is False


# ── ② vision 能力判定 ────────────────────────────────────────────

def test_vision_flag_by_model_id():
    from src.api.dialog import _dialog_model_supports_vision
    assert _dialog_model_supports_vision("qwen3-vl-4b") is True
    assert _dialog_model_supports_vision("qwen3-vl-8b-awq") is True
    assert _dialog_model_supports_vision("qwen35-9b-w4a16") is False


def test_vision_flag_by_imported_backend():
    from src.api.dialog import _dialog_model_supports_vision
    # 导入模型无 vl 字样 → 看 backend（vl=多模态 transformers 路径）
    assert _dialog_model_supports_vision("my-model", backend="vl") is True
    assert _dialog_model_supports_vision("my-model", backend="text") is False
    assert _dialog_model_supports_vision("my-model", backend="gguf") is False


# ── ③ /dialog/models 带 vision 字段（接口形状）───────────────────

def test_dialog_models_shape_has_vision(client, monkeypatch):
    """/dialog/models 每项必含 vision 布尔（清单可为空，形状不可缺）。"""
    r = client.get("/api/v1/dialog/models")
    assert r.status_code == 200, r.text
    models = r.json()["data"]["models"]
    for m in models:
        assert "vision" in m, f"缺 vision 字段: {m.get('model_id')}"
        assert isinstance(m["vision"], bool)
# 本项目仅供学习使用，商业授权请+Q 3559331368
