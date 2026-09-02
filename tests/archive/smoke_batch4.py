"""批 4 绘画画廊/inpaint 冒烟测试（直接打运行中的 5800 后端）。

覆盖：PAINT-027/030/031 inpaint 提交与遮罩校验 / PAINT-041 取消 /
PAINT-042 队列快照 / PAINT-044 优先级调整 / PAINT-046 画廊筛选 /
PAINT-048 收藏 / PAINT-050/051 单条与批量删除。

说明：历史记录数据通过 sqlite3 直插合成行（避免触发 GPU 真实生成）；
inpaint 合法提交后立即取消（协作式取消在模型加载前生效，不耗显存）。
"""
import base64
import io
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:5800/api/v1"
TS = str(int(time.time()))[-6:]
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "omnispace.db"
results = []


def call(method, path, body=None):
    url = BASE + path
    payload = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=payload, method=method)
    if payload:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode())
        except Exception:
            return {"success": False, "error": {
                "code": "HTTP_ERROR", "message": f"{exc.code} {exc}"}}
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


def record(case_id, passed, note=""):
    results.append((case_id, "PASS" if passed else "FAIL", note))


def png_b64(size, color, box=None):
    """生成测试 PNG base64；box=(l,t,r,b) 时用白色填充该区域。"""
    from PIL import Image
    img = Image.new("RGB", size, color)
    if box:
        from PIL import ImageDraw
        ImageDraw.Draw(img).rectangle(box, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


IMG = png_b64((64, 64), (30, 60, 90))
MASK_VALID = png_b64((64, 64), (0, 0, 0), box=(16, 16, 40, 40))
MASK_EMPTY = png_b64((64, 64), (0, 0, 0))
MASK_FULL = png_b64((64, 64), (255, 255, 255))

# ── PAINT-030/031：inpaint 校验 ──────────────────────────────────
check("PAINT-030 缺image",
      call("POST", "/art/inpaint", {"mask": MASK_VALID}),
      expect_code="SYSTEM_PARAM_INVALID")
check("PAINT-030 缺mask",
      call("POST", "/art/inpaint", {"image": IMG}),
      expect_code="SYSTEM_PARAM_INVALID")
check("PAINT-030 空遮罩",
      call("POST", "/art/inpaint", {"image": IMG, "mask": MASK_EMPTY}),
      expect_code="SYSTEM_PARAM_INVALID")
check("PAINT-031 遮罩过大",
      call("POST", "/art/inpaint", {"image": IMG, "mask": MASK_FULL}),
      expect_code="SYSTEM_PARAM_INVALID")
check("PAINT-030 坏base64",
      call("POST", "/art/inpaint", {"image": "!!!", "mask": MASK_VALID}),
      expect_code="SYSTEM_PARAM_INVALID")

# ── PAINT-027/041：合法提交 → 立即取消 ───────────────────────────
r = call("POST", "/art/inpaint",
         {"image": IMG, "mask": MASK_VALID, "prompt": "smoke repaint",
          "steps": 5, "priority": 7})
check("PAINT-027 inpaint提交", r)
tid = (r.get("data") or {}).get("task_id", "")
record("PAINT-044 提交回显priority",
       (r.get("data") or {}).get("priority") == 7,
       str(r.get("data")))

r = call("POST", f"/paint/task/{tid}/cancel")
check("PAINT-041 取消排队任务", r)
record("PAINT-041 cancelled状态",
       (r.get("data") or {}).get("status") == "cancelled", str(r.get("data")))
# 协作式取消：若已被调度进入运行态，需等当前推理步/加载动作收敛
final = ""
for _ in range(40):
    time.sleep(1.5)
    final = (call("GET", f"/paint/result/{tid}").get("data") or {}
             ).get("status", "")
    if final in ("cancelled", "error", "done"):
        break
record("PAINT-041 结果状态收敛", final in ("cancelled", "error"),
       f"final={final}")

check("PAINT-041 取消不存在",
      call("POST", "/paint/task/nope/cancel"),
      expect_code="SYSTEM_RESOURCE_NOT_FOUND")
check("PAINT-041 重复取消",
      call("POST", f"/paint/task/{tid}/cancel"),
      expect_code="SYSTEM_PARAM_INVALID")

# ── PAINT-042/044：队列快照 + 优先级调整 ─────────────────────────
r = call("GET", "/paint/queue")
check("PAINT-042 队列快照", r)
q = r.get("data") or {}
record("PAINT-042 队列字段",
       all(k in q for k in ("pending", "running", "pending_count")),
       str(list(q.keys())))

check("PAINT-044 调优先级不存在",
      call("POST", "/paint/task/nope/priority", {"priority": 9}),
      expect_code="SYSTEM_RESOURCE_NOT_FOUND")
check("PAINT-044 非法优先级",
      call("POST", f"/paint/task/{tid}/priority", {"priority": "abc"}),
      expect_code="SYSTEM_PARAM_INVALID")
r = call("POST", f"/paint/task/{tid}/priority", {"priority": 9})
check("PAINT-044 调整已结束任务", r)
record("PAINT-044 非等待effective=false",
       (r.get("data") or {}).get("effective") is False, str(r.get("data")))

# ── 合成历史行（sqlite3 直插，避免 GPU 生成）──────────────────────
HIDS = [f"smoke4{TS}a", f"smoke4{TS}b", f"smoke4{TS}c"]
now = time.time()
con = sqlite3.connect(str(DB_PATH))
con.execute(
    "CREATE TABLE IF NOT EXISTS paint_history ("
    "task_id TEXT PRIMARY KEY, prompt TEXT NOT NULL DEFAULT '',"
    " negative TEXT DEFAULT '', params_json TEXT DEFAULT '{}',"
    " file_path TEXT DEFAULT '', seed INTEGER DEFAULT -1,"
    " created_at REAL NOT NULL DEFAULT 0)")
cols = {row[1] for row in con.execute("PRAGMA table_info(paint_history)")}
if "favorite" not in cols:
    con.execute("ALTER TABLE paint_history "
                "ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
rows = [
    (HIDS[0], f"smoke画廊猫{TS}", "", json.dumps(
        {"width": 512, "height": 512}), "", 101, now - 3600, 1),
    (HIDS[1], f"smoke画廊狗{TS}", "", json.dumps(
        {"width": 1024, "height": 768}), "", 102, now - 60, 0),
    (HIDS[2], f"smoke画廊猫{TS}二号", "", json.dumps(
        {"width": 512, "height": 512}), "", 103, now, 0),
]
con.executemany(
    "INSERT OR REPLACE INTO paint_history "
    "(task_id, prompt, negative, params_json, file_path, seed,"
    " created_at, favorite) VALUES (?,?,?,?,?,?,?,?)", rows)
con.commit()
con.close()

# ── PAINT-046：画廊筛选 ──────────────────────────────────────────
KW = urllib.parse.quote(f"smoke画廊猫{TS}")
r = call("GET", f"/paint/history?keyword={KW}")
check("PAINT-046 keyword筛选", r)
record("PAINT-046 keyword计数",
       (r.get("data") or {}).get("total") == 2, str(r.get("data")))

r = call("GET", f"/paint/history?keyword={urllib.parse.quote('smoke画廊')}"
         "&favorite=1")
check("PAINT-046 favorite筛选", r)
hits = [i for i in (r.get("data") or {}).get("items", [])
        if i.get("task_id") in HIDS]
record("PAINT-046 favorite=1命中",
       len(hits) == 1 and hits[0]["task_id"] == HIDS[0]
       and hits[0].get("favorite") is True, str(hits))

r = call("GET", "/paint/history?width=1024&height=768")
check("PAINT-046 尺寸筛选", r)
hits = [i for i in (r.get("data") or {}).get("items", [])
        if i.get("task_id") in HIDS]
record("PAINT-046 尺寸命中",
       len(hits) == 1 and hits[0]["task_id"] == HIDS[1], str(hits))

r = call("GET", f"/paint/history?start={now - 120}&end={now + 10}")
check("PAINT-046 时间范围", r)
hits = [i for i in (r.get("data") or {}).get("items", [])
        if i.get("task_id") in HIDS]
record("PAINT-046 时间命中近2条",
       {i["task_id"] for i in hits} == {HIDS[1], HIDS[2]}, str(hits))

# ── PAINT-048：收藏切换 ──────────────────────────────────────────
r = call("POST", f"/paint/history/{HIDS[1]}/favorite", {})
check("PAINT-048 切换收藏", r)
record("PAINT-048 置为true",
       (r.get("data") or {}).get("favorite") is True, str(r.get("data")))
r = call("POST", f"/paint/history/{HIDS[1]}/favorite", {"favorite": False})
record("PAINT-048 显式置false",
       (r.get("data") or {}).get("favorite") is False, str(r.get("data")))
check("PAINT-048 收藏不存在",
      call("POST", "/paint/history/nope/favorite", {}),
      expect_code="SYSTEM_RESOURCE_NOT_FOUND")

# ── PAINT-050/051：删除 ──────────────────────────────────────────
r = call("DELETE", f"/paint/history/{HIDS[0]}")
check("PAINT-050 单条删除", r)
record("PAINT-050 deleted=true",
       (r.get("data") or {}).get("deleted") is True, str(r.get("data")))
check("PAINT-050 重复删除",
      call("DELETE", f"/paint/history/{HIDS[0]}"),
      expect_code="SYSTEM_RESOURCE_NOT_FOUND")

check("PAINT-051 空ids",
      call("POST", "/paint/history/batch-delete", {"ids": []}),
      expect_code="SYSTEM_PARAM_INVALID")
r = call("POST", "/paint/history/batch-delete",
         {"ids": [HIDS[1], HIDS[2], "nope"]})
check("PAINT-051 批量删除", r)
d = r.get("data") or {}
record("PAINT-051 删除计数",
       d.get("deleted") == 2 and d.get("missing") == ["nope"], str(d))

r = call("GET", f"/paint/history?keyword={urllib.parse.quote('smoke画廊')}")
left = [i for i in (r.get("data") or {}).get("items", [])
        if i.get("task_id") in HIDS]
record("PAINT-051 清理确认", len(left) == 0, str(len(left)))

# ── 汇总 ─────────────────────────────────────────────────────────
passed = sum(1 for _, s, _ in results if s == "PASS")
print(f"\n批4冒烟: {passed}/{len(results)} PASS")
for cid, st, note in results:
    mark = "✓" if st == "PASS" else "✗"
    print(f"  {mark} {cid} {st} {note[:100]}")
raise SystemExit(0 if passed == len(results) else 1)
