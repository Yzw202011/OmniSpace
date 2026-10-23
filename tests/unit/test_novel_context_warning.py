"""H9 断链警示链单测（2026-10-23 用户拍板「前端展示」）。

覆盖：
- _context_warning_text 纯函数：紧邻前章 done/非 done/无前章三态
- novel._chapter_row 下发提取：meta.context_warning → API 字段
- 临时库往返：警示写入 meta（与 novel_service 同款 db.update）后
  经 SELECT * → _chapter_row 读回，序列化契约（dumps→parse_json）成立
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from src.api.novel import _chapter_row
from src.data.database import Database
from src.services.novel_service import _context_warning_text


def test_warning_text_prev_not_done():
    prev = [{"chapter_index": 3, "status": "error", "title": "x"}]
    text = _context_warning_text(prev)
    assert "第 3 章" in text
    assert "error" in text
    assert "不连贯" in text


def test_warning_text_prev_pending():
    prev = [{"chapter_index": 2, "status": "pending", "title": "x"}]
    assert "第 2 章" in _context_warning_text(prev)
    assert "pending" in _context_warning_text(prev)


def test_warning_text_prev_done_or_empty():
    assert _context_warning_text(
        [{"chapter_index": 1, "status": "done"}]) == ""
    assert _context_warning_text([]) == ""
    # 状态缺失按「未知」如实提示
    assert "未知" in _context_warning_text([{"chapter_index": 1}])


def test_chapter_row_extracts_warning():
    r = {"id": "c1", "project_id": "p1", "chapter_index": 4,
         "meta": json.dumps({"context_warning": "上一章未完成"},
                            ensure_ascii=False)}
    assert _chapter_row(r)["context_warning"] == "上一章未完成"


def test_chapter_row_warning_absent_or_blank():
    base = {"id": "c1", "project_id": "p1", "chapter_index": 4}
    assert _chapter_row(dict(base))["context_warning"] == ""        # meta None
    assert _chapter_row(dict(base, meta="{}"))["context_warning"] == ""
    assert _chapter_row(dict(
        base, meta=json.dumps({"outline": "其他键"})))["context_warning"] == ""


def test_warning_roundtrip_temp_db():
    """写 meta → SELECT * → _chapter_row：序列化契约 + 自愈清除。"""
    tmp = tempfile.mkdtemp(prefix="omnispace_h9_")
    db = Database(Path(tmp) / "t.db")
    now = time.time()
    db.insert("novel_projects", {
        "id": "p1", "name": "h9", "genre": "测试", "description": "",
        "style_notes": "", "status": "active", "meta": "{}",
        "created_at": now, "updated_at": now})
    db.insert("novel_chapters", {
        "id": "c1", "project_id": "p1", "outline_id": "",
        "chapter_index": 2, "title": "第二章", "content": "",
        "summary": "", "word_count": 0, "status": "pending",
        "progress": 0.0, "error": "", "meta": "{}",
        "created_at": now, "updated_at": now})
    # 写入警示（与 novel_service 生成链同款调用：merge 既有 meta）
    db.update("novel_chapters",
              {"meta": json.dumps({"context_warning": "上一章未完成"},
                                  ensure_ascii=False)},
              "id=?", ("c1",))
    row = db.query_one("SELECT * FROM novel_chapters WHERE id=?", ("c1",))
    assert _chapter_row(row)["context_warning"] == "上一章未完成"
    # 前章恢复 done → 自愈清除（写空串，与生成链清除路径同款）
    db.update("novel_chapters",
              {"meta": json.dumps({"context_warning": ""},
                                  ensure_ascii=False)},
              "id=?", ("c1",))
    row = db.query_one("SELECT * FROM novel_chapters WHERE id=?", ("c1",))
    assert _chapter_row(row)["context_warning"] == ""
