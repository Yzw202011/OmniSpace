"""批 1 漫剧模块新端点冒烟测试（直接打运行中的 5800 后端）。"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:5800/api/v1"
TS = str(int(time.time()))[-6:]
NAME_A = f"冒烟项目A{TS}"
NAME_A2 = f"冒烟项目A2-{TS}"
results = []


def call(method, path, body=None, files=None, raw_query="", timeout=60):
    url = BASE + path + raw_query
    if files:
        boundary = "----smokeboundary"
        parts = []
        for name, (fname, data, ctype) in files.items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                         f"name=\"{name}\"; filename=\"{fname}\"\r\n"
                         f"Content-Type: {ctype}\r\n\r\n".encode())
            parts.append(data)
            parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        payload = b"".join(parts)
        req = urllib.request.Request(url, data=payload, method=method)
        req.add_header("Content-Type",
                       f"multipart/form-data; boundary={boundary}")
    else:
        payload = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=payload, method=method)
        if payload:
            req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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


# 1.1 项目 CRUD
r = call("POST", "/comic/project/create",
         {"name": NAME_A, "template": "comic_drama"})
check("COMIC-001/002 create+template", r)
pid = (r.get("data") or {}).get("project_id", "")
tpl_rows = len((r.get("data") or {}).get("rows", []))
results.append(("COMIC-002 模板5行", "PASS" if tpl_rows == 5 else "FAIL",
                f"rows={tpl_rows}"))

r = call("POST", "/comic/project/create", {"name": NAME_A})
check("COMIC-003 重名", r, expect_code="COMIC_PROJECT_NAME_DUPLICATED")

r = call("GET", "/comic/project/list")
check("COMIC-004 list", r)

r = call("PUT", f"/comic/project/{pid}", {"name": NAME_A2})
check("COMIC-004 rename", r)

# 1.3 分镜字段（取模板第一行）
r = call("GET", f"/manga/storyboard/{pid}")
row_id = (r.get("data") or {}).get("rows", [{}])[0].get("id", "")
r = call("PUT", f"/manga/storyboard/{pid}/rows/{row_id}",
         {"camera_type": "特写", "camera_angle": "俯视", "camera_movement": "推",
          "duration": 5.0, "transition": "叠化", "speed": 1.2, "volume": -3.0})
check("COMIC-058/059/061/062/069 合法导演字段", r)
r = call("PUT", f"/manga/storyboard/{pid}/rows/{row_id}",
         {"camera_type": "非法镜头"})
check("COMIC-058 非法枚举", r, expect_code="SYSTEM_PARAM_INVALID")
r = call("PUT", f"/manga/storyboard/{pid}/rows/{row_id}", {"duration": 99})
check("COMIC-061 越界时长", r, expect_code="SYSTEM_PARAM_INVALID")

# 1.2 DSL 上传
dsl_text = "shot: 第一镜 主角登场\nshot: 第二镜 冲突爆发\n"
r = call("POST", "/comic/script/import-dsl", files={
    "file": ("script.dsl", dsl_text.encode(), "text/plain")},
    raw_query=f"?project_id={pid}&strict=true")
check("COMIC-005/009 DSL上传strict", r)
r = call("POST", "/comic/script/import-dsl", files={
    "file": ("script.txt", "无标记文本\n第二行\n".encode(), "text/plain")},
    raw_query=f"?project_id={pid}&strict=true")
check("COMIC-009 无shot标记strict", r, expect_code="COMIC_DSL_FORMAT_INVALID")
r = call("POST", "/comic/script/import-dsl", files={
    "file": ("script.exe", b"MZ", "application/octet-stream")},
    raw_query=f"?project_id={pid}")
check("COMIC-010 扩展名白名单", r, expect_code="UNSUPPORTED_FORMAT")

# 1.4 资产库（不实际生成——引擎加载耗时；验证查询/绑定校验路径）
r = call("GET", "/comic/asset/library", raw_query=f"?project_id={pid}")
check("COMIC-031 asset/library", r)
r = call("PUT", "/comic/asset/bind", {"asset_id": "nope", "row_id": row_id})
check("COMIC-032 bind不存在资产", r, expect_code="SYSTEM_RESOURCE_NOT_FOUND")

# 1.6 关键帧查询/回退校验
r = call("GET", "/manga/keyframe/list", raw_query=f"?row_id={row_id}")
check("COMIC-121 keyframe/list", r)
r = call("POST", "/manga/keyframe/rollback", {"keyframe_id": "nope"})
check("COMIC-124 rollback不存在", r, expect_code="SYSTEM_RESOURCE_NOT_FOUND")

# 1.7 情绪识别
r = call("POST", "/manga/storyboard/emotion-detect",
         {"text": "你这个混蛋！我气死了！"})
emo = (r.get("data") or {}).get("emotion", "")
results.append(("COMIC-070 emotion-detect",
                "PASS" if r.get("success") and emo in
                ("喜悦", "愤怒", "悲伤", "惊讶", "恐惧", "温柔", "默认")
                else "FAIL", emo or str(r.get("error"))))
print("   emotion =", emo, "engine =", (r.get("data") or {}).get("engine"))

# 1.7 场景对象
r = call("PUT", "/comic/scene/object/update",
         {"project_id": pid, "object_id": "char1", "name": "主角",
          "position": {"x": 1, "y": 0, "z": 2}})
check("COMIC-090 scene/object/update", r)
r = call("GET", "/comic/scene/object/list", raw_query=f"?project_id={pid}")
cnt = (r.get("data") or {}).get("total", 0)
results.append(("COMIC-090 scene/object/list",
                "PASS" if r.get("success") and cnt >= 1 else "FAIL",
                f"total={cnt}"))

# 1.7 text-to-3d（TripoSR 已接线：期望真实 glb 产物；门控码为诚实降级）
r = call("POST", "/director/text-to-3d", {"prompt": "一把剑"}, timeout=300)
d3d = r.get("data") or {}
code = (r.get("error") or {}).get("code", "")
if r.get("success") and d3d.get("glb_path", "").endswith(".glb") \
        and d3d.get("vertices", 0) > 0:
    results.append(("COMIC-105 text-to-3d", "PASS",
                    f"verts={d3d.get('vertices')} faces={d3d.get('faces')}"))
elif code in ("MODEL_LOAD_FAILED", "MODEL_FILE_NOT_FOUND",
              "PAINT_ENGINE_NOT_READY"):
    results.append(("COMIC-105 text-to-3d门控", "DEGRADED", code))
else:
    results.append(("COMIC-105 text-to-3d", "FAIL", code or str(r)[:120]))

# 1.7 视频取消（不存在任务 → 404 语义）
r = call("POST", "/manga/video/nope/cancel")
check("COMIC-131 cancel不存在任务", r, expect_code="SYSTEM_RESOURCE_NOT_FOUND")

# 1.7 分镜导出 png-seq / pdf
r = call("GET", f"/manga/storyboard/{pid}/export",
         raw_query="?format=png-seq")
fp = (r.get("data") or {}).get("file_path", "")
results.append(("COMIC-135 export png-seq",
                "PASS" if r.get("success") and fp.endswith(".zip") else "FAIL",
                fp or str(r.get("error"))))
r = call("GET", f"/manga/storyboard/{pid}/export", raw_query="?format=pdf")
fp = (r.get("data") or {}).get("file_path", "")
results.append(("COMIC-136 export pdf",
                "PASS" if r.get("success") and fp.endswith(".pdf") else "FAIL",
                fp or str(r.get("error"))))
print("   pdf engine =", (r.get("data") or {}).get("engine"))

# 1.7 合并打包
r = call("POST", "/comic/export/bundle", {"project_id": pid})
fp = (r.get("data") or {}).get("file_path", "")
results.append(("COMIC-139 export/bundle",
                "PASS" if r.get("success") and fp.endswith(".zip") else "FAIL",
                fp or str(r.get("error"))))

# 音色克隆诚实降级
r = call("POST", "/manga/voices/clone", files={
    "file": ("a.wav", b"RIFF" + b"\x00" * 100, "audio/wav")},
    raw_query="?name=test")
check("COMIC-053 clone降级", r, expect_code="VOICE_CLONE_UNAVAILABLE")

# 清理：删除项目
r = call("DELETE", f"/comic/project/{pid}")
check("COMIC-004 delete", r)

print("\n===== 批 1 冒烟结果 =====")
fails = 0
for cid, status, extra in results:
    mark = "✓" if status == "PASS" else "✗"
    if status != "PASS":
        fails += 1
    print(f"{mark} {cid}: {status} {extra}")
print(f"\n共 {len(results)} 项，失败 {fails} 项")
sys.exit(1 if fails else 0)
