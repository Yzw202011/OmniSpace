"""批 2 视频风格模块新端点冒烟测试（直接打运行中的 5800 后端）。

覆盖：STYLE-017 任务控制 / STYLE-018 续训 / STYLE-023 强度校验 /
STYLE-024 融合 / STYLE-025 导出 / STYLE-026 克隆 / STYLE-027 列表筛选 /
STYLE-028 重命名删除 / STYLE-030 指标 / STYLE-032 模板。
"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:5800/api/v1"
TS = str(int(time.time()))[-6:]
results = []


def call(method, path, body=None, raw_query=""):
    url = BASE + path + raw_query
    payload = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=payload, method=method)
    if payload:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
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


# ── STYLE-017 任务控制：不存在任务 → STYLE_TASK_NOT_FOUND ──
check("STYLE-017 pause不存在", call("POST", "/style/tasks/nope/pause"),
      expect_code="STYLE_TASK_NOT_FOUND")
check("STYLE-017 resume不存在", call("POST", "/style/tasks/nope/resume"),
      expect_code="STYLE_TASK_NOT_FOUND")
check("STYLE-017 cancel不存在", call("POST", "/style/tasks/nope/cancel"),
      expect_code="STYLE_TASK_NOT_FOUND")

# ── STYLE-018 续训：不存在 → STYLE_TASK_NOT_FOUND ──
check("STYLE-018 resume-training不存在",
      call("POST", "/style/tasks/nope/resume-training"),
      expect_code="STYLE_TASK_NOT_FOUND")

# ── STYLE-023 预览：无版本语义 → STYLE_VERSION_NOT_FOUND ──
r = call("POST", "/style/preview", {"version": "vX", "strength": 1.5})
code = (r.get("error") or {}).get("code", "")
results.append(("STYLE-023 无版本语义", "PASS" if code == "STYLE_VERSION_NOT_FOUND" else "FAIL",
                code))

# ── STYLE-024 融合参数校验 ──
check("STYLE-024 merge版本数不足",
      call("POST", "/style/merge", {"versions": ["v1"], "weights": [1.0]}),
      expect_code="SYSTEM_PARAM_INVALID")
check("STYLE-024 merge权重数不匹配",
      call("POST", "/style/merge",
           {"versions": ["v1", "v2"], "weights": [1.0]}),
      expect_code="SYSTEM_PARAM_INVALID")
check("STYLE-024 merge版本不存在",
      call("POST", "/style/merge",
           {"versions": ["vX", "vY"], "weights": [0.5, 0.5]}),
      expect_code="STYLE_VERSION_NOT_FOUND")

# ── STYLE-025 导出：不存在 → STYLE_VERSION_NOT_FOUND ──
check("STYLE-025 export不存在",
      call("POST", "/style/export", {"version": "vX"}),
      expect_code="STYLE_VERSION_NOT_FOUND")

# ── STYLE-026 克隆：不存在 → STYLE_VERSION_NOT_FOUND ──
check("STYLE-026 clone不存在",
      call("POST", "/style/clone", {"version": "vX"}),
      expect_code="STYLE_VERSION_NOT_FOUND")

# ── STYLE-027 列表筛选（空库也应正常返回结构；search 用 ASCII 避免 URL 编码） ──
r = call("GET", "/style/list", raw_query="?search=nothit&status=ready")
data = r.get("data") or {}
ok_struct = r.get("success") and data.get("total") == 0 \
    and isinstance(data.get("items"), list)
results.append(("STYLE-027 list筛选结构", "PASS" if ok_struct else "FAIL", ""))

# ── STYLE-028 重命名/删除：不存在 → STYLE_VERSION_NOT_FOUND ──
check("STYLE-028 rename不存在",
      call("PUT", "/style/vX", {"name": "x"}),
      expect_code="STYLE_VERSION_NOT_FOUND")
check("STYLE-028 delete不存在",
      call("DELETE", "/style/vX"), expect_code="STYLE_VERSION_NOT_FOUND")

# ── STYLE-030 指标：不存在 → STYLE_VERSION_NOT_FOUND ──
check("STYLE-030 metrics不存在",
      call("GET", "/style/vX/metrics"), expect_code="STYLE_VERSION_NOT_FOUND")

# ── STYLE-032 模板 GET/POST ──
r = call("GET", "/style/templates")
tpl_total = (r.get("data") or {}).get("total", -1)
ok_tpl = r.get("success") and tpl_total >= 0
results.append(("STYLE-032 templates列表", "PASS" if ok_tpl else "FAIL", ""))

r = call("POST", "/style/templates",
         {"name": f"冒烟模板{TS}", "style_prompt": "赛博朋克霓虹",
          "lora_rank": 32, "epochs": 5})
check("STYLE-032 template创建", r)
tpl_id = (r.get("data") or {}).get("id", "")
r2 = call("GET", "/style/templates")
names = [t.get("name", "") for t in (r2.get("data") or {}).get("items", [])]
results.append(("STYLE-032 template持久化",
                "PASS" if f"冒烟模板{TS}" in names else "FAIL", ""))

# ── 汇总 ──
passed = sum(1 for _, s, _ in results if s == "PASS")
print(f"\n===== 批 2 冒烟：{passed}/{len(results)} PASS =====")
for cid, status, code in results:
    mark = "✓" if status == "PASS" else "✗"
    print(f"  {mark} {cid}: {status}" + (f" ({code})" if code else ""))
if tpl_id:
    print(f"  [清理提示] 模板 id={tpl_id}（templates 为 JSON 文件存储，"
          "测试模板残留无害）")
raise SystemExit(0 if passed == len(results) else 1)
