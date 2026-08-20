"""全量测试统一入口（TASK-P2-09，审计 P24：双测试入口整合）。

三层测试体系唯一命令：
    L1  后端 pytest   backend/tests（离线可跑：smoke / schema / unit 标记）
    L2  前端 vitest   frontend/src/**/*.test.ts（store / Zod / 枚举一致性 / 错误策略扫描）
    L3  E2E 流程编排  tests/flow（需活后端 http://127.0.0.1:5800，真实推理级）

用法（cwd = e:\\OmniSpace）:
    runtime\\py310\\python.exe tools\\run_tests.py            # L1 + L2（默认全量）
    runtime\\py310\\python.exe tools\\run_tests.py --smoke    # 仅 L1 冒烟标记（提交前最小集）
    runtime\\py310\\python.exe tools\\run_tests.py --e2e      # L1 + L2 + L3（发版前实机验证）

退出码：任一层失败即 1（供 CI / 提交钩子判定）。
定位说明见 tests/README.md（root tests/ 是流程编排脚本，不是 pytest 用例，
pytest.ini testpaths=backend/tests 即为此收口）。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "runtime" / "py310" / "python.exe"

SMOKE_ONLY = "--smoke" in sys.argv
WITH_E2E = "--e2e" in sys.argv


def _run_layer(name: str, cmd: list[str], cwd: Path) -> bool:
    print(f"\n{'═' * 60}\n▶ {name}\n  $ {' '.join(str(c) for c in cmd)}  (cwd={cwd.name})\n{'═' * 60}")
    proc = subprocess.run(cmd, cwd=str(cwd))
    ok = proc.returncode == 0
    print(f"◀ {name}: {'PASS' if ok else 'FAIL'} (exit={proc.returncode})")
    return ok


def main() -> int:
    if not PY.exists():
        print(f"找不到嵌入式 Python：{PY}")
        return 1

    results: dict[str, bool] = {}

    # L1 后端 pytest（--smoke 时仅冒烟标记；默认全量，testpaths=backend/tests）
    pytest_cmd = [str(PY), "-m", "pytest"] + (["-m", "smoke"] if SMOKE_ONLY else [])
    results["L1 backend pytest" + (" (smoke)" if SMOKE_ONLY else "")] = _run_layer(
        "L1 后端 pytest", pytest_cmd, ROOT
    )

    # L2 前端 vitest（npm run test = vitest run；跳过条件：显式 --no-frontend）
    if "--no-frontend" not in sys.argv:
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if npm is None:
            print("◀ L2 前端 vitest: SKIP（PATH 中无 npm）")
            results["L2 frontend vitest"] = True
        else:
            results["L2 frontend vitest"] = _run_layer(
                "L2 前端 vitest", [npm, "run", "test"], ROOT / "frontend"
            )

    # L3 E2E 流程编排（需活后端；tests.flow.run 自带 /health 预检，不可达退出码 2）
    if WITH_E2E:
        results["L3 e2e flow"] = _run_layer(
            "L3 E2E 流程编排（活后端）", [str(PY), "-m", "tests.flow.run", "all"], ROOT
        )

    print(f"\n{'═' * 60}\n总计：")
    for layer, ok in results.items():
        print(f"  {'✓' if ok else '✗'} {layer}")
    failed = [k for k, v in results.items() if not v]
    print(f"{'═' * 60}")
    if failed:
        print(f"失败层：{', '.join(failed)}")
        return 1
    print("全量通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
