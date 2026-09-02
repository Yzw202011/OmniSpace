"""批 3 学习模块增强冒烟测试（直接打运行中的 5800 后端）。

覆盖：LEARN-003 同名拒绝 / LEARN-004 depth 映射 / LEARN-005 seed_urls /
LEARN-006 列表筛选排序 / LEARN-069 克隆 / LEARN-037 资源阈值 /
LEARN-063 容量告警 / LEARN-040 知识筛选 / LEARN-041 知识编辑 /
LEARN-046 知识导入 / LEARN-032 图谱导出 / LEARN-049~052 分析 /
LEARN-035 LoRA 版本对比。
"""
import json
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:5800/api/v1"
TS = str(int(time.time()))[-6:]
TOPIC = f"冒烟主题{TS}"
results = []


def call(method, path, body=None, files=None, form=None, raw_query=""):
    url = BASE + path + raw_query
    if files or form:
        boundary = "----smokeb3"
        parts = []
        for name, val in (form or {}).items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                         f"name=\"{name}\"\r\n\r\n{val}\r\n".encode())
        for name, (fname, data, ctype) in (files or {}).items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                         f"name=\"{name}\"; filename=\"{fname}\"\r\n"
                         f"Content-Type: {ctype}\r\n\r\n".encode())
            parts.append(data)
            parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(url, data=b"".join(parts), method=method)
        req.add_header("Content-Type",
                       f"multipart/form-data; boundary={boundary}")
    else:
        payload = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=payload, method=method)
        if payload:
            req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        return {"success": False, "error": {"code": "HTTP_ERROR",
                                            "message": str(exc)}}


def check(case_id, resp, expect_success=True, expect_code=None):
    ok_flag = resp.get("success", False)
    code = (resp.get("error") or {}).get("code", "")
    if expect_code:
        passed = (not ok_flag) and code == expect_code
    else:
        passed = ok_flag == expect_success
    results.append((case_id, "PASS" if passed else "FAIL",
                    code if not ok_flag else ""))
    return resp


# ── LEARN-004/005/003：主题创建（depth 映射 + seed_urls + 同名拒绝） ──
r = call("POST", "/learn/topic/create",
         {"name": TOPIC, "keywords": ["冒烟", "测试"], "depth": "deep",
          "seed_urls": ["https://example.com/a", "https://example.com/b"]})
check("LEARN-004/005 create depth+seeds", r)
data = r.get("data") or {}
tid = data.get("id", "")
ok_map = data.get("max_pages") == 50 and data.get("depth") == "deep" \
    and len(data.get("seed_urls", [])) == 2
results.append(("LEARN-004 depth→50页映射", "PASS" if ok_map else "FAIL",
                json.dumps({k: data.get(k) for k in
                            ("depth", "max_pages", "seed_urls")})))

check("LEARN-004 非法depth",
      call("POST", "/learn/topic/create",
           {"name": f"{TOPIC}-x", "depth": "extreme"}),
      expect_code="SYSTEM_PARAM_INVALID")
check("LEARN-005 非法seed_url",
      call("POST", "/learn/topic/create",
           {"name": f"{TOPIC}-y", "seed_urls": ["ftp://bad"]}),
      expect_code="SYSTEM_PARAM_INVALID")
check("LEARN-003 同名拒绝",
      call("POST", "/learn/topic/create", {"name": TOPIC}),
      expect_code="LEARN_TOPIC_NAME_DUPLICATED")

# ── LEARN-006：列表筛选排序（中文 keyword 需 URL 编码） ──
r = call("GET", "/learn/topic/list",
         raw_query="?keyword=" + urllib.parse.quote("冒烟")
         + "&status=active&sort=name")
data = r.get("data") or {}
ok_list = r.get("success") and any(
    t.get("id") == tid for t in data.get("items", []))
results.append(("LEARN-006 list筛选命中", "PASS" if ok_list else "FAIL", ""))
r2 = call("GET", "/learn/topic/list", raw_query="?keyword=nothitxyz")
results.append(("LEARN-006 list筛选排空",
                "PASS" if (r2.get("data") or {}).get("total") == 0
                else "FAIL", ""))

# ── LEARN-069：克隆 ──
r = call("POST", "/learn/topic/clone", {"topic_id": tid})
check("LEARN-069 clone", r)
cdata = r.get("data") or {}
ok_clone = cdata.get("name", "").startswith(f"{TOPIC}-副本") \
    and cdata.get("depth") == "deep" \
    and cdata.get("source") == "clone" \
    and cdata.get("progress") == 0.0
results.append(("LEARN-069 clone字段",
                "PASS" if ok_clone else "FAIL", cdata.get("name", "")))
clone_id = cdata.get("id", "")
check("LEARN-069 clone不存在主题",
      call("POST", "/learn/topic/clone", {"topic_id": "nope"}),
      expect_code="LEARN_TOPIC_NOT_FOUND")

# ── LEARN-037：资源阈值设置 ──
check("LEARN-037 cpu阈值越界",
      call("PUT", "/learn/settings", {"cpu_percent_limit": 0.5}),
      expect_code="SYSTEM_PARAM_INVALID")
r = call("PUT", "/learn/settings",
         {"cpu_percent_limit": 25.0, "bandwidth_mbps_limit": 8.0})
check("LEARN-037 阈值写入", r)
r2 = call("GET", "/learn/settings")
s = r2.get("data") or {}
ok_persist = s.get("cpu_percent_limit") == 25.0 \
    and s.get("bandwidth_mbps_limit") == 8.0
results.append(("LEARN-037 阈值持久化", "PASS" if ok_persist else "FAIL", ""))
call("PUT", "/learn/settings",
     {"cpu_percent_limit": 20.0, "bandwidth_mbps_limit": 4.0})  # 还原默认

# ── LEARN-063：容量告警字段 ──
r = call("GET", "/learn/quota")
cap = ((r.get("data") or {}).get("capacity") or {})
ok_cap = r.get("success") and "near_capacity" in cap
results.append(("LEARN-063 near_capacity字段",
                "PASS" if ok_cap else "FAIL", ""))

# ── LEARN-040：知识筛选 ──
r = call("GET", "/learn/knowledge/list",
         raw_query="?type=concept&min_score=0.0&page_size=5")
check("LEARN-040 list筛选", r)
r2 = call("GET", "/learn/knowledge/list",
          raw_query="?type=nosuchtype&min_score=9.9")
ok_filter = r2.get("success") and (r2.get("data") or {}).get("total") == 0
results.append(("LEARN-040 筛选排空", "PASS" if ok_filter else "FAIL", ""))

# ── LEARN-041：知识编辑（不存在 → KNOWLEDGE_NOT_FOUND） ──
check("LEARN-041 编辑不存在",
      call("PUT", "/learn/knowledge/nope", {"content": "x"}),
      expect_code="KNOWLEDGE_NOT_FOUND")
check("LEARN-041 空字段拒绝",
      call("PUT", "/learn/knowledge/nope", {}),
      expect_code="SYSTEM_PARAM_INVALID")

# ── LEARN-046：知识导入（json，dedup 二次导入应跳过） ──
payload = {"items": [
    {"content": f"冒烟知识Alpha{TS}：Python 的 GIL 是全局解释器锁。",
     "type": "fact", "topic": TOPIC, "title": "GIL", "quality_score": 0.8},
    {"content": f"SmokeBeta{TS}: SQLite supports JSON1 extension.",
     "type": "fact", "topic": TOPIC, "title": "SQLite JSON1",
     "quality_score": 0.7},
]}
blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
r = call("POST", "/learn/knowledge/import",
         files={"file": (f"k{TS}.json", blob, "application/json")},
         form={"merge_strategy": "dedup", "topic": TOPIC})
check("LEARN-046 import首次", r)
idata = r.get("data") or {}
results.append(("LEARN-046 import计数",
                "PASS" if idata.get("imported", 0) >= 1 else "FAIL",
                json.dumps({k: idata.get(k) for k in
                            ("imported", "skipped_duplicates", "failed")})))
r2 = call("POST", "/learn/knowledge/import",
          files={"file": (f"k{TS}.json", blob, "application/json")},
          form={"merge_strategy": "dedup"})
d2 = r2.get("data") or {}
ok_dedup = r2.get("success") and d2.get("skipped_duplicates", 0) >= 1
results.append(("LEARN-046 dedup跳过", "PASS" if ok_dedup else "FAIL",
                json.dumps({k: d2.get(k) for k in
                            ("imported", "skipped_duplicates")})))
check("LEARN-046 非法策略",
      call("POST", "/learn/knowledge/import",
           files={"file": (f"k{TS}.json", blob, "application/json")},
           form={"merge_strategy": "replace_all"}),
      expect_code="SYSTEM_PARAM_INVALID")

# ── LEARN-066：导入条目应带 lang 判定（topic 中文需 URL 编码） ──
r = call("GET", "/learn/knowledge/list",
         raw_query="?topic=" + urllib.parse.quote(TOPIC) + "&page_size=10")
items = (r.get("data") or {}).get("items", [])
langs = {it.get("lang") for it in items}
ok_lang = ("zh" in langs) or ("en" in langs)
results.append(("LEARN-066 lang字段", "PASS" if ok_lang else "FAIL",
                f"langs={langs}"))

# ── LEARN-041（正向）：编辑刚导入的知识 ──
kid = items[0].get("id") if items else ""
if kid:
    r = call("PUT", f"/learn/knowledge/{kid}",
             {"topic": f"{TOPIC}-已编辑"})
    check("LEARN-041 编辑正向", r)
    ok_edit = (r.get("data") or {}).get("topic") == f"{TOPIC}-已编辑"
    results.append(("LEARN-041 编辑生效", "PASS" if ok_edit else "FAIL", ""))

# ── LEARN-032：图谱导出 ──
r = call("GET", "/learn/knowledge/graph/export", raw_query="?format=gexf")
gdata = r.get("data") or {}
ok_gexf = r.get("success") and "<?xml" in (gdata.get("content") or "")[:200]
results.append(("LEARN-032 gexf导出", "PASS" if ok_gexf else "FAIL", ""))
check("LEARN-032 非法格式",
      call("GET", "/learn/knowledge/graph/export", raw_query="?format=pdf"),
      expect_code="SYSTEM_PARAM_INVALID")

# ── LEARN-049~052：分析端点结构 ──
for cid, path in (("LEARN-049 efficiency", "/learn/analysis/efficiency"),
                  ("LEARN-050 sources", "/learn/analysis/sources"),
                  ("LEARN-051 trend", "/learn/analysis/trend?days=7"),
                  ("LEARN-052 topic-compare",
                   "/learn/analysis/topic-compare")):
    r = call("GET", path)
    check(cid, r)

# ── LEARN-035：LoRA 版本对比（库内已有历史 v1/v2 → 正向对比；
#    不存在版本 → SYSTEM_RESOURCE_NOT_FOUND） ──
r = call("GET", "/learn/lora/versions/compare", raw_query="?a=v1&b=v2")
cdata = r.get("data") or {}
ok_cmp = r.get("success") and cdata.get("a") == "v1" \
    and isinstance(cdata.get("fields"), dict) and len(cdata["fields"]) > 0
results.append(("LEARN-035 compare正向", "PASS" if ok_cmp else "FAIL",
                "" if r.get("success") else
                str((r.get("error") or {}).get("code", ""))))
check("LEARN-035 compare不存在版本",
      call("GET", "/learn/lora/versions/compare", raw_query="?a=vX&b=vY"),
      expect_code="SYSTEM_RESOURCE_NOT_FOUND")

# ── 清理：删除测试主题（克隆 + 原主题） ──
call("DELETE", "/learn/topic/delete", {"id": clone_id})
call("DELETE", "/learn/topic/delete", {"id": tid})

passed = sum(1 for _, s, _ in results if s == "PASS")
print(f"\n===== 批 3 冒烟：{passed}/{len(results)} PASS =====")
for cid, status, code in results:
    mark = "✓" if status == "PASS" else "✗"
    print(f"  {mark} {cid}: {status}" + (f" ({code})" if code else ""))
raise SystemExit(0 if passed == len(results) else 1)
