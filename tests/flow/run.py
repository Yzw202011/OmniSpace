"""测试执行入口。

用法:
    runtime/py310/python.exe -m tests.flow.run <module|all> [--modules m1,m2]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 保证 tests 包可导入（cwd=e:\OmniSpace）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.flow.harness import Client, run_module, write_results, RESULT_DIR  # noqa: E402

MODULES = ["sys", "chat", "paint", "comic", "learn", "model", "style",
           "set", "cross"]


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    target = args[0] if args else "all"
    mods = MODULES if target == "all" else [m.strip() for m in target.split(",")]

    # 按选择导入用例模块（注册副作用）
    import importlib
    for m in mods:
        importlib.import_module(f"tests.flow.cases_{m}")

    client = Client()
    # 后端可达性预检
    try:
        env = client.get("/health")
        assert env.get("success"), env
    except Exception as exc:
        print(f"后端不可达: {exc}")
        return 2

    summaries = []
    for m in mods:
        print(f"\n═══ 模块 {m} ═══")
        rec = run_module(m, client)
        path = write_results(rec)
        s = rec.summary()
        summaries.append(s)
        print(f"  → {s['counts']} 已写入 {path}")

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    (RESULT_DIR / "summary.json").write_text(json.dumps(
        summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    total = {"PASS": 0, "DEGRADED": 0, "FAIL": 0, "SKIP": 0, "BLOCKED": 0}
    for s in summaries:
        for k, v in s["counts"].items():
            total[k] = total.get(k, 0) + v
    print(f"\n═══ 总计 ═══ {total}")
    return 1 if total.get("FAIL") else 0


if __name__ == "__main__":
    raise SystemExit(main())
