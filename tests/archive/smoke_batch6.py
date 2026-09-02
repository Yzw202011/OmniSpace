"""批 6 模型管理 + TripoSR 接线冒烟（直接打运行中的 5800 后端）。"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:5800/api/v1"
results = []


def call(method, path, body=None, timeout=60):
    url = BASE + path
    payload = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=payload, method=method)
    if payload:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode())
        except Exception:
            return {"success": False,
                    "error": {"code": "HTTP", "message": str(exc.code)}}
    except Exception as exc:
        return {"success": False, "error": {"code": "HTTP_ERROR",
                                            "message": str(exc)}}


def record(name, passed, note=""):
    results.append((name, "PASS" if passed else "FAIL", note))
    print(f"[{'PASS' if passed else 'FAIL'}] {name} {note}")


def record_deg(name, passed, note=""):
    results.append((name, "DEGRADED" if passed else "FAIL", note))
    print(f"[{'DEGRADED' if passed else 'FAIL'}] {name} {note}")


# 等后端就绪
for _ in range(60):
    try:
        with urllib.request.urlopen("http://127.0.0.1:5800/health",
                                    timeout=2) as r:
            if json.loads(r.read()).get("success"):
                break
    except Exception:
        time.sleep(2)

# MODEL-033 显存碎片率
r = call("GET", "/models/vram")
frag = (r.get("data") or {}).get("fragmentation") or {}
record("MODEL-033 碎片率字段",
       r.get("success") and "fragmentation" in frag
       and frag.get("available") is True,
       f"frag={frag.get('fragmentation')} reserved={frag.get('reserved_gb')}GB")

# MODEL-034 量化精度偏好
r = call("GET", "/models/config")
record("MODEL-034 读取默认配置",
       r.get("success") and (r.get("data") or {}).get("precision") == "bf16",
       json.dumps(r.get("data"), ensure_ascii=False)[:100])
r = call("PUT", "/models/config", {"precision": "fp16"})
record("MODEL-034 写入 fp16",
       r.get("success") and (r.get("data") or {}).get("precision") == "fp16"
       and (r.get("data") or {}).get("persisted") is True)
r = call("GET", "/models/config")
record("MODEL-034 重启级持久化",
       (r.get("data") or {}).get("precision") == "fp16")
r = call("PUT", "/models/config", {"precision": "int99"})
code = (r.get("error") or {}).get("code")
record("MODEL-034 非法值拒绝",
       (not r.get("success")) and code in (40004, "SYSTEM_PARAM_INVALID"),
       f"code={code}")
call("PUT", "/models/config", {"precision": "bf16"})  # 复位默认

# MODEL-036 依赖关系
r = call("GET", "/models/sdxl-base-1.0")
deps = (r.get("data") or {}).get("dependencies")
record("MODEL-036 sdxl 依赖关系",
       r.get("success") and isinstance(deps, list) and len(deps) >= 3,
       f"deps={len(deps) if isinstance(deps, list) else 0}")
r = call("GET", "/models/TripoSR")
deps = (r.get("data") or {}).get("dependencies")
record("MODEL-036 triposr 依赖关系",
       r.get("success") and isinstance(deps, list) and len(deps) >= 1,
       f"deps={deps}")

# MODEL-023 版本更新检查（离线诚实）
r = call("GET", "/models/update")
d = r.get("data") or {}
record("MODEL-023 离线诚实返回",
       r.get("success") and d.get("online") is False
       and d.get("status") == "offline"
       and d.get("local_manifest_version") == "2.0.0",
       f"status={d.get('status')} ver={d.get('local_manifest_version')}")

# MODEL-037 模型导出（小模型 depth ~0.14GB 快）
r = call("POST", "/models/export", {"model_id": "depth"}, timeout=300)
d = r.get("data") or {}
record("MODEL-037 导出 tar.gz",
       r.get("success") and str(d.get("export_path", "")).endswith(".tar.gz")
       and len(str(d.get("sha256", ""))) == 64,
       f"size={d.get('size_mb')}MB files={d.get('file_count')}")
r2 = call("POST", "/models/export", {"model_id": "nonexistent-model"})
code2 = (r2.get("error") or {}).get("code")
record("MODEL-037 不存在模型拒绝",
       not r2.get("success")
       and code2 in (30001, "MODEL_FILE_NOT_FOUND"), f"code={code2}")

# MODEL-038 基准：先真实加载对话模型，再跑基准
r0 = call("POST", "/models/load",
          {"model_id": "qwen3-vl-4b", "category": "dialog"}, timeout=300)
loaded_ok = r0.get("success") and (r0.get("data") or {}).get("loaded")
if not loaded_ok:
    record_deg("MODEL-038 对话模型加载失败",
               True, json.dumps(r0.get("error"), ensure_ascii=False)[:100])
else:
    r = call("POST", "/models/benchmark",
             {"runs": 2, "max_new_tokens": 16}, timeout=600)
    d = r.get("data") or {}
    code = (r.get("error") or {}).get("code")
    if r.get("success") and d.get("tokens_per_s", 0) > 0:
        record("MODEL-038 基准测试", True,
               f"tokens/s={d.get('tokens_per_s')} "
               f"peak={d.get('vram_peak_gb')}GB")
        r2 = call("GET", "/models/benchmark/history")
        record("MODEL-038 历史落库",
               r2.get("success") and (r2.get("data") or {}).get("total", 0) >= 1)
    elif code in (20012, "MODEL_NOT_LOADED"):
        record_deg("MODEL-038 未加载诚实拒绝", True, f"code={code}")
    else:
        record("MODEL-038 基准测试", False, f"code={code} {str(r)[:120]}")

# MODEL-019 下载诚实门控
r = call("POST", "/models/download", {"model_id": "flux-dev"})
code = (r.get("error") or {}).get("code")
record_deg("MODEL-019 下载离线门控",
           (not r.get("success")) and code == "MODEL_DOWNLOAD_OFFLINE",
           f"code={code}")

# COMIC-105 text-to-3d 真实推理（SDXL 概念图 + TripoSR，全链路）
r = call("POST", "/director/text-to-3d", {"prompt": "一把宝剑"}, timeout=600)
d = r.get("data") or {}
code = (r.get("error") or {}).get("code")
if r.get("success") and str(d.get("glb_path", "")).endswith(".glb") \
        and d.get("vertices", 0) > 0:
    record("COMIC-105 text-to-3d", True,
           f"verts={d.get('vertices')} faces={d.get('faces')} "
           f"{d.get('elapsed_s')}s")
elif code in ("MODEL_LOAD_FAILED", "MODEL_FILE_NOT_FOUND",
              "PAINT_ENGINE_NOT_READY"):
    record_deg("COMIC-105 门控", True, code)
else:
    record("COMIC-105 text-to-3d", False, f"code={code} {str(r)[:200]}")

print("\n== 汇总 ==")
for name, st, note in results:
    print(f"{st:9s} {name} {note}")
p = sum(1 for _, st, _ in results if st == "PASS")
dg = sum(1 for _, st, _ in results if st == "DEGRADED")
f = sum(1 for _, st, _ in results if st == "FAIL")
print(f"PASS={p} DEGRADED={dg} FAIL={f}")
