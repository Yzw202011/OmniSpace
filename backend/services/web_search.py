"""联网搜索 v1（架构升级计划 B-阶段一；用户 2026-09-05 拍板免费双通道，
推翻《AI能力升级计划-2026-09-04》决策 #2 的「博查单主力」原推荐）。

Provider 插座（可插拔，默认浏览器自搜=零配置零成本）：
  browser  无头浏览器自搜（必应/百度/搜狗/360 模板轮换）——主力
  searxng  用户自建 SearXNG 实例（设置页填地址即接，标准 JSON API）
  bocha    博查 API（用户自带 Key 才启用；内置成本归零、条款责任干净）

产品纪律（2026-09-05 拍板）：功能默认关闭；只上传查询词、不上传聊天
历史；资料不足以回答时由提示词要求明说、不编造。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger("omnispace.web_search")

# ── 配置（kv: system_settings["web_search"]）────────────────────
DEFAULT_SETTINGS: dict = {
    "enabled": False,        # 默认关闭（离线买断定位与联网能力的平衡点）
    "provider": "browser",   # browser / searxng / bocha（主力）
    "searxng_url": "",       # SearXNG 实例地址（如 http://127.0.0.1:8888）
    "bocha_key": "",         # 博查 API Key（用户自带，空=该通道禁用）
    "trigger": "auto",       # auto=时效意图判定 / always=每问必搜 / off=仅"搜索:"前缀
    "top_k": 6,              # 取前 N 条结果
    "timeout_s": 20.0,       # 单 Provider 超时预算
}
_SETTINGS_KEY = "web_search"
_CACHE_TTL_S = 3600.0      # 同查询 1h 内直接命中缓存（新鲜事搜索的抖动抑制）

# 时效/事实意图判据（v1：只认时效词+显式前缀，避免"是什么"类误触发）
_TIMELY_RE = re.compile(
    r"今天|今日| latest|最新|最近|近来|现在|目前|当前|新闻|热搜|价格|多少钱|"
    r"报价|发布|上线|更新|版本|比分|天气|汇率|股价|排行|榜单|下载|"
    r"202[4-9]|20[3-9][0-9]", re.I)
_FORCE_PREFIX = ("搜索:", "搜索：", "search:", "联网")


def get_web_search_settings() -> dict:
    """读联网搜索配置（kv 缺失/损坏回退默认值，未知键丢弃）。"""
    out = dict(DEFAULT_SETTINGS)
    try:
        from ..data.database import get_db_safe
        db = get_db_safe()
        if db is not None:
            row = db.query_one(
                "SELECT value FROM system_settings WHERE key=?", (_SETTINGS_KEY,))
            if row:
                saved = json.loads(row["value"])
                if isinstance(saved, dict):
                    for k in DEFAULT_SETTINGS:
                        if k in saved:
                            out[k] = saved[k]
    except Exception as exc:  # noqa: BLE001 - 配置读取失败走默认（默认关=安全）
        log.debug("联网搜索配置读取失败（用默认）: %s", exc)
    return out


def update_web_search_settings(patch: dict) -> dict:
    """合并写回联网搜索配置（未知键丢弃；enabled/provider 类型校验）。"""
    cur = get_web_search_settings()
    for k in DEFAULT_SETTINGS:
        if k in patch:
            cur[k] = patch[k]
    cur["enabled"] = bool(cur["enabled"])
    if cur["provider"] not in ("browser", "searxng", "bocha"):
        cur["provider"] = "browser"
    if cur["trigger"] not in ("auto", "always", "off"):
        cur["trigger"] = "auto"
    from ..data.database import get_db_safe
    db = get_db_safe()
    if db is not None:
        db.sql(
            "INSERT INTO system_settings (key, value, updated_at)"
            " VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET"
            " value=excluded.value, updated_at=excluded.updated_at",
            (_SETTINGS_KEY, json.dumps(cur, ensure_ascii=False), time.time()))
    return cur


# ── 意图判定（该不该联网）───────────────────────────────────────
def detect_web_needed(question: str,
                      trigger: str = "auto") -> bool:
    """判断本问是否需要联网（trigger=auto 时效词 / always 恒真 /
    off 仅显式"搜索:"前缀）。"""
    q = (question or "").strip()
    if not q:
        return False
    if q.startswith(_FORCE_PREFIX):
        return True
    if trigger == "always":
        return True
    if trigger == "off":
        return False
    return bool(_TIMELY_RE.search(q[:200]))


# ── 结果模型 ────────────────────────────────────────────────────
@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""          # provider 名（来源卡片展示）
    _extra: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        return {"title": self.title, "url": self.url,
                "snippet": self.snippet, "source": self.source}


# ── Provider 实现（同步阻塞；编排层负责线程池包装）──────────────
def _searxng_search(base_url: str, query: str, top_k: int,
                    timeout_s: float) -> list[SearchResult]:
    """SearXNG 标准 JSON API：GET /search?q=&format=json。"""
    import requests
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return []
    resp = requests.get(f"{base}/search",
                        params={"q": query, "format": "json"},
                        timeout=timeout_s)
    resp.raise_for_status()
    out: list[SearchResult] = []
    for r in (resp.json().get("results") or [])[:top_k]:
        out.append(SearchResult(
            title=str(r.get("title") or "")[:120],
            url=str(r.get("url") or ""),
            snippet=str(r.get("content") or "")[:400],
            source="searxng"))
    return out


def _bocha_search(api_key: str, query: str, top_k: int,
                  timeout_s: float) -> list[SearchResult]:
    """博查 Web Search API（用户自带 Key）。"""
    import requests
    if not (api_key or "").strip():
        return []
    resp = requests.post(
        "https://open.bochaai.com/v1/web-search",
        headers={"Authorization": f"Bearer {api_key.strip()}",
                 "Content-Type": "application/json"},
        json={"query": query, "count": top_k, "summary": True},
        timeout=timeout_s)
    resp.raise_for_status()
    pages = ((resp.json().get("data") or {}).get("webPages") or {}).get("value") or []
    out: list[SearchResult] = []
    for r in pages[:top_k]:
        out.append(SearchResult(
            title=str(r.get("name") or "")[:120],
            url=str(r.get("url") or ""),
            snippet=str(r.get("summary") or r.get("snippet") or "")[:400],
            source="bocha"))
    return out


# 搜索页噪音特征（2026-09-06 实弹诊断）：get_links 按 DOM 序返回，导航/
# 页脚（备案、反馈、帮助类站外链接）混在真结果前后——「先到先得」会抓
# 满页脚噪音（首搜实测抓到 3 条备案链接、真结果 0 条）。双重过滤：
# 域名/路径特征 + 标题特征 + 最短标题（纯导航 2~4 字）。
_JUNK_URL_PARTS = ("beian.", "miit.gov", "mps.gov", "miibeian", "12377",
                   "fankui", "fuwu.", "corp.", "passport", "/help", "/about",
                   "/terms", "/privacy", "javascript:")
_JUNK_TITLE_WORDS = ("备案", "许可证", "反馈", "免责声明", "关于我们", "帮助",
                     "企业推广", "返回首页", "跳至", "辅助功能", "意见反馈")
_MIN_TITLE_CHARS = 8


def _is_junk_result(title: str, href: str) -> bool:
    low = href.lower()
    if any(x in low for x in _JUNK_URL_PARTS):
        return True
    if any(w in title for w in _JUNK_TITLE_WORDS):
        return True
    return len(title) < _MIN_TITLE_CHARS


def _browser_search(query: str, top_k: int,
                    timeout_s: float) -> list[SearchResult]:
    """无头浏览器自搜（主力插座）：浏览器池取实例 → 搜索页直导航 →
    结果链接结构化（噪音过滤后全量收集）+ 搜索页文本粗切摘要。
    只读不填表（TC-S-012）。

    ⚠️ 实测档案（2026-09-06，四引擎×无头/有头全试）：必应/百度/搜狗/
    360 对自动化浏览器一律拦截——结果区不渲染（页面只含导航+页脚，
    bing 44 条链接全为站内导航；百度 0 链接；搜狗反爬拦截页），有头
    模式同蔽。当前态势下本通道大概率返回空 → 编排层自动回退/静默，
    对话主链零影响。**推荐用户配置 SearXNG 自建通道**（对程序友好、
    无反爬、质量稳）；本通道保留——搜索引擎态势缓解后自动受益。
    """
    if not query:
        return []
    from ..services.browser_agent_service import SEARCH_ENGINES, _is_content_link, build_search_url
    from ..services.browser_pool import get_browser_pool

    t0 = time.time()
    browser = None
    pool = None
    results: list[SearchResult] = []
    try:
        pool = get_browser_pool()
        browser = pool.acquire(timeout=5.0)
        engines = list(SEARCH_ENGINES)
        page_text = ""
        for eng in engines:  # 模板轮换：首个引擎异常（验证码/超时）换下一个
            try:
                browser.navigate(build_search_url(query, eng))
                try:
                    page_text = browser.get_text() or ""
                except Exception:  # noqa: BLE001
                    page_text = ""
                links = browser.get_links() or []
                collected: list[SearchResult] = []
                for link in links:
                    href = str((link or {}).get("href") or "")
                    title = str((link or {}).get("text") or "").strip()
                    if not href or not title or not _is_content_link(href):
                        continue
                    if _is_junk_result(title, href):
                        continue
                    collected.append(SearchResult(
                        title=title[:120], url=href,
                        snippet="", source="browser"))
                # 全量收集后再取 top_k（真结果在 DOM 里排在导航之后，
                # 「先到先得」会被前置噪音挤占——09-06 实弹教训）
                results = collected[:top_k]
                if results:
                    break
                if time.time() - t0 > timeout_s:
                    break
            except Exception as exc:  # noqa: BLE001 - 换下一个引擎
                log.debug("浏览器搜索引擎 %s 失败（轮换）: %s", eng, exc)
                continue
        # 摘要粗切：搜索页可见文本按行拆，含 query 首词的行优先作首条摘要
        if results and page_text:
            lines = [ln.strip() for ln in page_text.splitlines()
                     if len(ln.strip()) >= 20]
            q_head = query.split()[0] if query.split() else query
            for r in results:
                pick = next((ln for ln in lines
                             if (q_head and q_head in ln)
                             and ln != r.title), "")
                r.snippet = (pick or (lines[0] if lines else ""))[:300]
                if not pick and not lines:
                    break
        return results
    finally:
        try:
            if pool is not None:
                pool.release(browser)
        except Exception:  # noqa: BLE001
            pass


# ── 级联编排 + 缓存 ─────────────────────────────────────────────
def _cache_get(key: str) -> list[dict] | None:
    try:
        from ..data.database import get_db_safe
        db = get_db_safe()
        if db is None:
            return None
        row = db.query_one(
            "SELECT value FROM system_settings WHERE key=?", (key,))
        if row:
            data = json.loads(row["value"])
            if isinstance(data, dict) \
                    and time.time() - float(data.get("ts", 0)) < _CACHE_TTL_S:
                return data.get("results") or []
    except Exception as exc:  # noqa: BLE001 - 缓存读失败当未命中
        log.debug("搜索缓存读取失败（当未命中）: %s", exc)
    return None


def _cache_put(key: str, results: list[SearchResult]) -> None:
    try:
        from ..data.database import get_db_safe
        db = get_db_safe()
        if db is None:
            return
        payload = json.dumps({"ts": time.time(),
                              "results": [r.to_dict() for r in results]},
                             ensure_ascii=False)
        db.sql(
            "INSERT INTO system_settings (key, value, updated_at)"
            " VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET"
            " value=excluded.value, updated_at=excluded.updated_at",
            (key, payload, time.time()))
    except Exception as exc:  # noqa: BLE001 - 缓存写失败不影响主链
        log.debug("搜索缓存写入失败（忽略）: %s", exc)


def run_web_search(question: str, settings: dict | None = None) -> list[SearchResult]:
    """按配置执行联网搜索（级联回退：主力失败→浏览器兜底；带 1h 缓存）。

    同步阻塞（requests/浏览器池），调用方负责线程池包装。任何失败
    都返回空列表——搜索是增益能力，绝不阻断对话主链。
    """
    cfg = settings or get_web_search_settings()
    query = " ".join((question or "").strip().split())
    # "搜索:" 前缀剥离（意图判定的 force 标记不进查询词）
    query = re.sub(rf"^{'|'.join(_FORCE_PREFIX)}\s*", "", query).strip()
    if not query:
        return []

    cache_key = f"websearch:cache:{hashlib.md5(query.encode('utf-8')).hexdigest()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return [SearchResult(title=c.get("title", ""), url=c.get("url", ""),
                             snippet=c.get("snippet", ""),
                             source=c.get("source", ""))
                for c in cached]

    top_k = max(1, min(int(cfg.get("top_k", 6)), 10))
    timeout_s = float(cfg.get("timeout_s", 20.0))
    provider = str(cfg.get("provider") or "browser")

    def _try(name: str) -> list[SearchResult]:
        if name == "searxng":
            url = str(cfg.get("searxng_url") or "").strip()
            return _searxng_search(url, query, top_k, timeout_s) if url else []
        if name == "bocha":
            key = str(cfg.get("bocha_key") or "").strip()
            return _bocha_search(key, query, top_k, timeout_s) if key else []
        return _browser_search(query, top_k, timeout_s)

    # 级联：选定的主力优先，失败（异常/空结果）回退浏览器兜底；
    # 主力本就是 browser 时不重复尝试
    order: list[str] = []
    for name in (provider, "browser"):
        if name not in order:
            order.append(name)
    results: list[SearchResult] = []
    for name in order:
        try:
            results = _try(name)
        except Exception as exc:  # noqa: BLE001 - 单通道失败回退下一通道
            log.warning("联网搜索通道 %s 失败（回退）: %s", name, exc)
            results = []
        if results:
            break
    if results:
        _cache_put(cache_key, results)
    return results


def build_web_block(results: list[SearchResult]) -> str:
    """结果 → 【联网资料】上下文块（编号引用与【相关知识】同框架；
    提示词纪律：资料不足以回答时明说、不编造、必须标注【n】来源）。"""
    if not results:
        return ""
    lines = ["【联网资料】（以下为实时网络检索结果，回答时必须标注"
             "引用编号【1】【2】…；资料不足以回答时如实说明，禁止编造）"]
    for i, r in enumerate(results, 1):
        line = f"【{i}】{r.title}"
        if r.url:
            line += f"（{r.url}）"
        if r.snippet:
            line += f"：{r.snippet}"
        lines.append(line)
    return "\n".join(lines)
# 本项目仅供学习使用，商业授权请+Q 3559331368
