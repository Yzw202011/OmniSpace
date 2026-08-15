"""批 5 设置/系统端点冒烟测试（直接打运行中的 5800 后端）。

覆盖：SET-008 硬件档位覆盖 / SET-010 推理配置 / SET-012~014 网络配置 /
SET-015 训练默认参数 / SET-019 自动备份配置 / SET-021 全量导出 /
SET-023 API Key 管理 / SET-025 日志级别 / SET-026 日志查询/导出/清理 /
SET-029 重启确认 token 校验（不触发真实重启）/ SET-030 磁盘概览。

说明：/system/restart 仅测缺 confirm 的 400 路径，绝不触发真实重启；
全量导出产生的 tar.gz 保留在 data/generated/exports/（回归前可清）。
"""
import json
import time
import urllib.error
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
            raw = resp.read().decode()
            try:
                return json.loads(raw)
            except Exception:
                return {"success": True, "raw": raw[:200]}
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


# ── SET-008 硬件档位覆盖 ─────────────────────────────────────────
r = call("PUT", "/hardware/tier", {"tier": "rtx3060"})
check("SET-008 手动档位", r)
record("SET-008 matched_by=manual",
       r.get("data", {}).get("effective", {}).get("matched_by") == "manual")
r = call("GET", "/hardware/info")
check("SET-008 info回读", r)
record("SET-008 tier_override持久化",
       r.get("data", {}).get("tier_override") == "rtx3060"
       and r.get("data", {}).get("tier", {}).get("tier") == "rtx3060")
check("SET-008 非法档位",
      call("PUT", "/hardware/tier", {"tier": "gtx999"}),
      expect_code="SYSTEM_PARAM_INVALID")
r = call("PUT", "/hardware/tier", {"tier": "auto"})
check("SET-008 恢复auto", r)
record("SET-008 auto恢复自动探测",
       r.get("data", {}).get("effective", {}).get("matched_by") != "manual")

# ── SET-010 推理配置 ─────────────────────────────────────────────
r = call("GET", "/system/inference/config")
check("SET-010 读推理配置", r)
orig_threads = r.get("data", {}).get("num_threads", 0)
record("SET-010 含cpu_cores", r.get("data", {}).get("cpu_cores", 0) > 0)
r = call("PUT", "/system/inference/config",
         {"num_threads": 2, "clear_cache_on_unload": True})
check("SET-010 写推理配置", r)
record("SET-010 即时生效标记", r.get("data", {}).get("applied_now") is True)
r = call("GET", "/system/inference/config")
record("SET-010 持久化回读",
       r.get("data", {}).get("num_threads") == 2
       and r.get("data", {}).get("clear_cache_on_unload") is True)
check("SET-010 越界线程数",
      call("PUT", "/system/inference/config", {"num_threads": 99999}),
      expect_code="SYSTEM_PARAM_INVALID")
call("PUT", "/system/inference/config", {"num_threads": orig_threads})

# ── SET-012/013/014 网络配置 ─────────────────────────────────────
r = call("GET", "/system/network/config")
check("SET-012 读网络配置", r)
r = call("PUT", "/system/network/config",
         {"proxy_url": "http://127.0.0.1:7890",
          "hf_mirror": "https://hf-mirror.com", "bandwidth_mbps": 50})
check("SET-013 写代理+镜像", r)
r = call("GET", "/system/network/config")
d = r.get("data", {})
record("SET-013 持久化回读",
       d.get("proxy_url") == "http://127.0.0.1:7890"
       and d.get("hf_mirror") == "https://hf-mirror.com"
       and d.get("bandwidth_mbps") == 50)
check("SET-013 非法代理前缀",
      call("PUT", "/system/network/config", {"proxy_url": "ftp://x"}),
      expect_code="SYSTEM_PARAM_INVALID")
check("SET-014 非法镜像",
      call("PUT", "/system/network/config", {"hf_mirror": "not-a-url"}),
      expect_code="SYSTEM_PARAM_INVALID")
r = call("PUT", "/system/network/config",
         {"proxy_url": "", "hf_mirror": "", "bandwidth_mbps": 0})
check("SET-012 清空恢复", r)

# ── SET-015 训练默认参数 ─────────────────────────────────────────
r = call("GET", "/learn/train/defaults")
check("SET-015 读训练默认", r)
record("SET-015 含lora_rank", "lora_rank" in r.get("data", {}))
r = call("PUT", "/learn/train/defaults",
         {"lora_rank": 8, "learning_rate": 0.00001})
check("SET-015 写训练默认", r)
r = call("GET", "/learn/train/defaults")
d = r.get("data", {})
record("SET-015 持久化回读",
       d.get("lora_rank") == 8 and abs(d.get("learning_rate", 0) - 1e-5) < 1e-9)
check("SET-015 空body拒绝",
      call("PUT", "/learn/train/defaults", {}),
      expect_code="SYSTEM_PARAM_INVALID")
call("PUT", "/learn/train/defaults",
     {"lora_rank": 16, "learning_rate": 2e-5})

# ── SET-019 自动备份配置 ─────────────────────────────────────────
r = call("GET", "/system/backup/config")
check("SET-019 读备份配置", r)
record("SET-019 含last_backup_at", "last_backup_at" in r.get("data", {}))
r = call("PUT", "/system/backup/config",
         {"enabled": True, "interval_hours": 12})
check("SET-019 写备份配置", r)
r = call("GET", "/system/backup/config")
d = r.get("data", {})
record("SET-019 持久化回读",
       d.get("enabled") is True and d.get("interval_hours") == 12)
check("SET-019 间隔越界",
      call("PUT", "/system/backup/config", {"interval_hours": 0}),
      expect_code="SYSTEM_PARAM_INVALID")
call("PUT", "/system/backup/config", {"enabled": False, "interval_hours": 24})

# ── SET-021 全量数据导出 ─────────────────────────────────────────
r = call("POST", "/system/export", timeout=180)
check("SET-021 全量导出", r)
d = r.get("data", {})
record("SET-021 含SHA256", len(d.get("sha256", "")) == 64)
record("SET-021 tar.gz非空", d.get("size_bytes", 0) > 1024)

# ── SET-023 API Key 管理 ─────────────────────────────────────────
r = call("POST", "/system/apikeys", {"name": f"smoke_{int(time.time())}"})
check("SET-023 创建Key", r)
d = r.get("data", {})
full_key = d.get("key", "")
kid = d.get("id", "")
record("SET-023 完整Key仅一次", full_key.startswith("osk-"))
r = call("GET", "/system/apikeys")
check("SET-023 Key列表", r)
items = r.get("data", {}).get("items", [])
hit = [it for it in items if it.get("id") == kid]
record("SET-023 脱敏显示",
       bool(hit) and "***" in hit[0].get("masked", "")
       and full_key not in json.dumps(items))
check("SET-023 缺name拒绝",
      call("POST", "/system/apikeys", {}),
      expect_code="SYSTEM_PARAM_INVALID")
r = call("DELETE", f"/system/apikeys/{kid}")
check("SET-023 删除Key", r)
check("SET-023 删不存在",
      call("DELETE", f"/system/apikeys/{kid}"),
      expect_code="SYSTEM_RESOURCE_NOT_FOUND")

# ── SET-025/026 日志管理 ─────────────────────────────────────────
r = call("PUT", "/system/logs/level", {"level": "DEBUG"})
check("SET-025 切日志级别", r)
check("SET-025 非法级别",
      call("PUT", "/system/logs/level", {"level": "VERBOSE"}),
      expect_code="SYSTEM_PARAM_INVALID")
call("PUT", "/system/logs/level", {"level": "INFO"})
r = call("GET", "/system/logs?page=1&page_size=50")
check("SET-026 日志分页", r)
record("SET-026 分页结构",
       "items" in r.get("data", {}) and "total" in r.get("data", {}))
r = call("GET", "/system/logs?level=ERROR&page_size=10")
check("SET-026 级别过滤", r)
r = call("GET", "/system/logs/export")
check("SET-026 日志导出", r)
r = call("POST", "/system/logs/cleanup", {"keep_days": 7})
check("SET-026 日志清理", r)
record("SET-026 清理结构", "removed_count" in r.get("data", {}))
check("SET-026 清理参数非法",
      call("POST", "/system/logs/cleanup", {"keep_days": "abc"}),
      expect_code="SYSTEM_PARAM_INVALID")

# ── SET-029 重启确认 token（绝不触发真实重启）─────────────────────
check("SET-029 缺confirm拒绝",
      call("POST", "/system/restart", {}),
      expect_code="SYSTEM_PARAM_INVALID")
check("SET-029 错token拒绝",
      call("POST", "/system/restart", {"confirm": "yes"}),
      expect_code="SYSTEM_PARAM_INVALID")

# ── SET-030 磁盘概览 ─────────────────────────────────────────────
r = call("GET", "/system/disk")
check("SET-030 磁盘概览", r)
d = r.get("data", {})
record("SET-030 卷信息", len(d.get("volumes", [])) > 0)
record("SET-030 数据目录", "omnispace.db" in d.get("data_usage_mb", {}))

# ── 汇总 ──────────────────────────────────────────────────────────
passed = sum(1 for _, s, _ in results if s == "PASS")
failed = [(c, code) for c, s, code in results if s != "PASS"]
print(f"\n===== 批5 冒烟：{passed}/{len(results)} PASS =====")
if failed:
    print("FAIL 明细：")
    for c, code in failed:
        print(f"  - {c}  [{code}]")
