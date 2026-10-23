"""R2-H7 僵尸向量双修单测（2026-10-24 终结书移交项）。

- 止血：delete_knowledge 向量删除失败→KnowledgeDeleteError 早退，
  元数据保留（账实一致）——此前 warning 后继续删元数据=僵尸固化
- 清存：purge_orphan_vectors 只清无元数据孤儿；元数据读取失败中止
  （宁不清勿误清）；零孤儿返回 0
- 错误码双表登记（KNOWLEDGE_DELETE_FAILED）
"""
from __future__ import annotations

import pytest

import src.middleware.error_handler as eh
from src.services.knowledge_service import (
    KnowledgeDeleteError,
    KnowledgeProcessingService,
)


class _VdbStub:
    """向量库桩：可注入删除失败/预置 id 清单。"""

    def __init__(self, ids=None, fail_delete=False):
        self.ids = list(ids or [])
        self.fail_delete = fail_delete
        self.deleted: list[str] = []

    def delete(self, ids):
        if self.fail_delete:
            raise RuntimeError("chroma down")
        self.deleted.extend(ids)
        self.ids = [i for i in self.ids if i not in set(ids)]

    def all_ids(self):
        return list(self.ids)


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    from src.data import database as db_mod
    from src.data.database import Database
    test_db = Database(tmp_path / "k.db")
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    s = KnowledgeProcessingService()
    test_db.insert("knowledge_meta", {
        "id": "live1", "topic": "t", "title": "活知识", "content": "正文",
        "type": "fact", "source_url": "", "quality_score": 1.0,
        "lifecycle": "active", "created_at": 1.0})
    return s, test_db


def test_delete_vector_failure_keeps_meta(svc):
    """止血核心：向量删失败→异常早退，元数据保留（不产僵尸）。"""
    s, db = svc
    s._vdb = _VdbStub(ids=["live1"], fail_delete=True)
    with pytest.raises(KnowledgeDeleteError):
        s.delete_knowledge("live1")
    row = db.query_one("SELECT id FROM knowledge_meta WHERE id='live1'")
    assert row is not None, "元数据被删=僵尸向量复发（R2-H7）"


def test_delete_success_path_unchanged(svc):
    s, db = svc
    stub = _VdbStub(ids=["live1"])
    s._vdb = stub
    assert s.delete_knowledge("live1") is True
    assert stub.deleted == ["live1"]
    assert db.query_one(
        "SELECT id FROM knowledge_meta WHERE id='live1'") is None


def test_purge_removes_only_orphans(svc):
    s, db = svc
    db.insert("knowledge_meta", {
        "id": "live2", "topic": "t", "title": "2", "content": "c",
        "type": "fact", "source_url": "", "quality_score": 1.0,
        "lifecycle": "active", "created_at": 1.0})
    stub = _VdbStub(ids=["live1", "live2", "ghost1", "ghost2"])
    s._vdb = stub
    n = s.purge_orphan_vectors()
    assert n == 2
    assert sorted(stub.ids) == ["live1", "live2"], "活知识向量被误清"


def test_purge_no_orphans_returns_zero(svc):
    s, _ = svc
    s._vdb = _VdbStub(ids=["live1"])
    assert s.purge_orphan_vectors() == 0


def test_purge_meta_read_failure_aborts(svc, monkeypatch):
    """宁不清勿误清：元数据读不到→返回 0 且不动向量库。"""
    s, _ = svc
    stub = _VdbStub(ids=["live1", "ghost1"])
    s._vdb = stub

    def _boom(q, p=()):
        raise RuntimeError("db down")
    monkeypatch.setattr(s._db, "query", _boom)
    assert s.purge_orphan_vectors() == 0
    assert len(stub.ids) == 2, "元数据不可读时清了向量=可能误清活知识"


def test_error_code_registered_both_tables():
    """双表登记闸（哨兵同款契约）：语义码+默认出路齐备。"""
    assert "KNOWLEDGE_DELETE_FAILED" in eh.SEMANTIC_CODES
    assert isinstance(
        eh.SUGGESTION_DEFAULTS.get("KNOWLEDGE_DELETE_FAILED"), str)
    assert eh.SUGGESTION_DEFAULTS["KNOWLEDGE_DELETE_FAILED"].strip()
