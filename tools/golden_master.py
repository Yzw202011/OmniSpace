#!/usr/bin/env python3
"""金母版基线（P2）：上锁/出包前的「标准答案」。

大白话：用同一套固定输入敲后端的门，把每扇门的回应拍成指纹（SHA256）存档；
以后每次加密构建、每次出包，重敲一遍和档案比对——指纹变了=行为悄悄变了，
当场报警。这就是业界 golden master / approval 测试。

探针原则：只测确定性逻辑（校验/清单/CRUD/安全拒绝），不碰模型推理
（GPU 推理归 P3 加密前后的人工基线，见 v4 计划）。

用法（对着一个空库实例跑）：
  runtime/py310/python.exe tools/golden_master.py record --base http://127.0.0.1:5803
  runtime/py310/python.exe tools/golden_master.py verify --base http://127.0.0.1:5803

基线文件：tests/golden/baseline.json（仓库内，不随发行包走）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import httpx

BASELINE_FILE = Path(__file__).resolve().parent.parent / "tests/golden/baseline.json"

# 归一化时剥离的易变键：时间戳/构建号/请求号等——金母版只看行为指纹；
# gpu=实时硬件读数（显存/利用率每秒都变）；name=探针自身随机后缀
VOLATILE_KEYS = {
    "request_id", "timestamp", "duration_ms", "meta", "uptime_s",
    "created_at", "updated_at", "started_at", "last_active", "elapsed",
    "version", "build", "session_id", "topic_id", "id",
    "gpu", "name",
}

# 探针序列（顺序执行；空库前提下全部确定性）
# 注：learn_topic_create 用随机后缀名保证可重复执行（响应中的 name 已剥离）
import uuid  # noqa: E402

_PROBE_NAME = "gm-" + uuid.uuid4().hex[:8]

PROBES = [
    ("health", "GET", "/health", None),
    ("models_list", "GET", "/api/v1/models", None),
    ("models_status", "GET", "/api/v1/models/status", None),
    ("models_import_traversal", "POST", "/api/v1/models/import",
     {"model_id": "../../evil", "source_path": "Q:/nonexistent"}),
    ("chat_empty", "POST", "/api/v1/chat/send", {"message": ""}),
    ("chat_huge", "POST", "/api/v1/chat/send", {"message": "x" * 200000}),
    ("learn_topic_create", "POST", "/api/v1/learn/topic/create",
     {"name": _PROBE_NAME, "description": "golden master 固定输入",
      "source": "network", "priority": "normal",
      "budget": {"max_time_minutes": 30, "max_pages": 20}}),
    ("knowledge_list", "GET", "/api/v1/knowledge/list?page=1&page_size=10", None),
    ("style_list", "GET", "/api/v1/style/list", None),
]


def normalize(obj):
    """递归剥离易变键 + 排序，产出确定性 JSON 文本。"""
    if isinstance(obj, dict):
        return {k: normalize(v) for k, v in sorted(obj.items())
                if k not in VOLATILE_KEYS}
    if isinstance(obj, list):
        return [normalize(v) for v in obj]
    return obj


def fingerprint(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:
        body = {"_raw": resp.text[:500]}
    canon = json.dumps({"status": resp.status_code, "body": normalize(body)},
                       ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def run_probes(base: str) -> dict[str, str]:
    out: dict[str, str] = {}
    with httpx.Client(base_url=base, timeout=30) as c:
        for name, method, path, payload in PROBES:
            try:
                if method == "GET":
                    r = c.get(path)
                else:
                    r = c.post(path, json=payload)
                out[name] = fingerprint(r)
            except Exception as exc:  # noqa: BLE001 - 异常也指纹化，保持可比对
                out[name] = "ERROR:" + type(exc).__name__
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["record", "verify"])
    ap.add_argument("--base", default="http://127.0.0.1:5800")
    args = ap.parse_args()

    results = run_probes(args.base)

    if args.mode == "record":
        BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_FILE.write_text(
            json.dumps({"base": args.base, "probes": results},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"📸 基线已录制：{len(results)} 个探针 → {BASELINE_FILE}")
        for k, v in results.items():
            print(f"  {k}: {v[:16]}")
        return 0

    if not BASELINE_FILE.exists():
        print("❌ 基线不存在，先 record")
        return 1
    baseline = json.loads(BASELINE_FILE.read_text("utf-8"))["probes"]
    fails = []
    for name, _, _, _ in PROBES:
        exp, got = baseline.get(name), results.get(name)
        if exp == got:
            print(f"  ✓ {name}: {str(got)[:16]}")
        else:
            fails.append(name)
            print(f"  ✗ {name}: 基线 {str(exp)[:16]} ≠ 实测 {str(got)[:16]}")
    if fails:
        print(f"\n❌ {len(fails)} 个探针行为漂移：{fails}")
        return 1
    print(f"\n✅ {len(results)} 个探针与基线完全一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
