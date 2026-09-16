"""联网搜索 v1 回归（架构升级计划 B-阶段一，免费双通道）。

锁定：意图判定三态、级联回退（主力失败→浏览器兜底）、1h 缓存命中、
【联网资料】块格式与纪律文案、dialog 编排的默认关/开两态注入。
"""
from __future__ import annotations

import asyncio

import pytest

from src.services import web_search as ws
from src.services.web_search import (
    SearchResult,
    build_web_block,
    detect_web_needed,
    run_web_search,
    update_web_search_settings,
)


class _FakeDb:
    """最小 kv 假库（system_settings key/value 语义，row 支持下标）。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def query_one(self, sql: str, params: tuple = ()):
        return self.rows.get(params[0])

    def sql(self, sql: str, params: tuple = ()) -> None:
        self.rows[params[0]] = {"value": params[1]}

    # 旧方法名保留（防回归对比）
    execute = sql


@pytest.fixture()
def fake_db(monkeypatch) -> _FakeDb:
    db = _FakeDb()
    monkeypatch.setattr("src.data.database.get_db_safe", lambda: db)
    return db


# ── 意图判定 ────────────────────────────────────────────────────
def test_detect_timely_words() -> None:
    assert detect_web_needed("今天天气怎么样") is True
    assert detect_web_needed("2026 年最新版本发布了什么") is True
    assert detect_web_needed("搜索:qwen3.5 显存占用") is True   # 显式前缀


def test_detect_trigger_modes() -> None:
    assert detect_web_needed("帮我写一首诗", "always") is True
    assert detect_web_needed("今天天气怎么样", "off") is False   # off 仅前缀
    assert detect_web_needed("搜索:新闻", "off") is True
    assert detect_web_needed("帮我写一首诗", "auto") is False   # 无时效词


# ── 级联与缓存 ──────────────────────────────────────────────────
def test_run_search_fallback_to_browser(monkeypatch, fake_db) -> None:
    calls: list[str] = []

    def _fail_searxng(base_url, query, top_k, timeout_s):
        calls.append("searxng")
        raise RuntimeError("连接失败")

    def _ok_browser(query, top_k, timeout_s):
        calls.append("browser")
        return [SearchResult(title="t", url="u", snippet="s", source="browser")]

    monkeypatch.setattr(ws, "_searxng_search", _fail_searxng)
    monkeypatch.setattr(ws, "_browser_search", _ok_browser)
    out = run_web_search("最新新闻", {"enabled": True, "provider": "searxng",
                                     "searxng_url": "http://x", "top_k": 3})
    assert calls == ["searxng", "browser"]          # 主力失败→浏览器兜底
    assert out[0].source == "browser"


def test_run_search_primary_success_no_fallback(monkeypatch, fake_db) -> None:
    calls: list[str] = []

    def _ok_searxng(base_url, query, top_k, timeout_s):
        calls.append("searxng")
        return [SearchResult(title="t", url="u", snippet="s", source="searxng")]

    def _empty_browser(query, top_k, timeout_s):
        calls.append("browser")
        return []

    monkeypatch.setattr(ws, "_searxng_search", _ok_searxng)
    monkeypatch.setattr(ws, "_browser_search", _empty_browser)
    run_web_search("最新新闻", {"provider": "searxng",
                                "searxng_url": "http://x"})
    assert calls == ["searxng"]                     # 主力成功不回退


def test_run_search_cache_hit(monkeypatch, fake_db) -> None:
    n = {"count": 0}

    def _browser(query, top_k, timeout_s):
        n["count"] += 1
        return [SearchResult(title="t", url="u", snippet="s", source="browser")]

    monkeypatch.setattr(ws, "_browser_search", _browser)
    cfg = {"provider": "browser"}
    run_web_search("今天股市行情", cfg)
    run_web_search("今天股市行情", cfg)             # 同查询第二次
    assert n["count"] == 1                          # 缓存命中不重复搜


def test_run_search_disabled_env_still_searches_when_called() -> None:
    # run_web_search 是编排底层（enabled 判定在 dialog 编排层）——
    # 直接调用应正常执行（本用例锁空查询防护）。
    assert run_web_search("") == []


# ── 资料块格式 ──────────────────────────────────────────────────
def test_build_web_block_format_and_discipline() -> None:
    block = build_web_block([
        SearchResult(title="标题一", url="http://a", snippet="摘要一"),
        SearchResult(title="标题二", url="", snippet=""),
    ])
    assert block.startswith("【联网资料】")
    assert "【1】标题一（http://a）：摘要一" in block
    assert "【2】标题二" in block
    assert "禁止编造" in block                       # 纪律文案在块内
    assert build_web_block([]) == ""


# ── 设置存取 ────────────────────────────────────────────────────
def test_settings_defaults_and_update(fake_db) -> None:
    cfg = ws.get_web_search_settings()
    assert cfg["enabled"] is False                  # 默认关（产品纪律）
    assert cfg["provider"] == "browser"
    updated = update_web_search_settings({"enabled": True,
                                          "provider": "searxng",
                                          "bogus_key": 1})
    assert updated["enabled"] is True
    assert updated["provider"] == "searxng"
    assert "bogus_key" not in updated               # 未知键丢弃
    bad = update_web_search_settings({"provider": "hack"})
    assert bad["provider"] == "browser"             # 非法值回退默认


# ── dialog 编排 ─────────────────────────────────────────────────
def test_dialog_augment_disabled_passthrough(monkeypatch) -> None:
    from src.api.dialog import _web_search_augment
    monkeypatch.setattr(ws, "get_web_search_settings",
                        lambda: {**ws.DEFAULT_SETTINGS, "enabled": False})
    kt, refs = asyncio.run(_web_search_augment("今天新闻", "原有知识"))
    assert kt == "原有知识" and refs == []          # 关闭态零副作用


def test_dialog_augment_injects_block(monkeypatch) -> None:
    from src.api.dialog import _web_search_augment
    monkeypatch.setattr(ws, "get_web_search_settings",
                        lambda: {**ws.DEFAULT_SETTINGS, "enabled": True})
    monkeypatch.setattr(ws, "run_web_search", lambda q, cfg: [
        SearchResult(title="新闻A", url="http://a", snippet="内容",
                     source="browser")])
    kt, refs = asyncio.run(_web_search_augment("今天有什么新闻", ""))
    assert "【联网资料】" in kt and "新闻A" in kt
    assert refs and refs[0]["url"] == "http://a"
# 本项目仅供学习使用，商业授权请+Q 3559331368
