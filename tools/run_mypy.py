"""mypy 增量闸门（编程语言规范合规批 4：基线冻结、只增不减）。

三层测试体系之外的静态类型第四闸（.githooks/pre-commit 第 4 步调用）。
策略（docs/编程语言规范合规计划-2026-09-04.md 批 4）：
  - 存量类型错误一次性冻结在 tools/mypy_baseline.txt，不阻塞提交；
  - 基线外新增错误 → 退出码 1 拦截提交；
  - 基线漂移（重构导致行号/消息变化）→ 人工确认后 --rebaseline 重生成。
基线键 = 相对路径|错误消息（含 [错误码]，不含行号——行号漂移不误报）。
范围 = src + launcher（用户令：packaging_console / license_console 冻结）。

用法（cwd 任意，内部归位仓库根）:
    runtime/py310/python.exe tools/run_mypy.py             # 全量对比基线
    runtime/py310/python.exe tools/run_mypy.py --staged    # 只查暂存区（pre-commit）
    runtime/py310/python.exe tools/run_mypy.py --rebaseline  # 重生成基线（有意为之）
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "tools" / "mypy.ini"
BASELINE = ROOT / "tools" / "mypy_baseline.txt"
# src 扁平化重构（2026-09-15）：backend/ → src/，闸门范围随之迁移
SCOPE = ("src", "launcher")

ERR_RE = re.compile(r"^(?P<path>[^:]+):(?P<line>\d+): (?P<kind>error|note): (?P<msg>.*)$")


def run_mypy(targets: list[str]) -> list[str]:
    cmd = [sys.executable, "-m", "mypy",
           "--config-file", str(CONFIG),
           "--no-error-summary", "--no-pretty"] + targets
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    return [ln for ln in (proc.stdout + "\n" + proc.stderr).splitlines() if ln.strip()]


def parse_errors(lines: list[str]) -> set[str]:
    """抽 error 行 → 基线键（路径统一 / 分隔；去行号防行号漂移误报）。"""
    keys: set[str] = set()
    for ln in lines:
        m = ERR_RE.match(ln.strip())
        if m and m.group("kind") == "error":
            path = m.group("path").replace("\\", "/")
            keys.add(f"{path}|{m.group('msg')}")
    return keys


def staged_py_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
    )
    files = [f for f in out.stdout.split("\0")
             if f.endswith(".py") and f.startswith(SCOPE)]
    return files


def main() -> int:
    if "--rebaseline" in sys.argv:
        # B9（2026-09-13）确认闸：重生成基线=重置「只增不减」约束的
        # 记账起点，误用会把真实新增错误洗白成存量。无 --yes 且 stderr
        # 非 TTY（CI/脚本场景无人确认）时拒绝执行。
        if "--yes" not in sys.argv:
            if not sys.stderr.isatty():
                print("[mypy-gate] ✗ --rebaseline 需要显式确认："
                      "非交互环境请加 --yes（并自查 diff）")
                return 2
            print("[mypy-gate] ⚠ 即将重生成 mypy 基线（存量错误记账起点重置）。")
            print("            这会丢弃基线差异告警能力——请先 review "
                  "runtime/py312/python.exe tools/run_mypy.py 的输出。")
            try:
                ans = input("确认重生成？输入 yes 继续：").strip().lower()
            except EOFError:
                ans = ""
            if ans != "yes":
                print("[mypy-gate] 已取消")
                return 2
        errors = parse_errors(run_mypy(list(SCOPE)))
        BASELINE.write_text(
            "\n".join(sorted(errors)) + ("\n" if errors else ""),
            encoding="utf-8", newline="")
        print(f"[mypy-gate] 基线已重生成：{len(errors)} 条存量错误冻结于 tools/mypy_baseline.txt")
        return 0

    if "--staged" in sys.argv:
        files = staged_py_files()
        if not files:
            print("[mypy-gate] [4/4] 暂存区无 src/launcher 的 .py，跳过")
            return 0
        print(f"[mypy-gate] [4/4] mypy 增量类型检查（{len(files)} 个暂存文件，基线冻结只增不减）...")
    else:
        print("[mypy-gate] mypy 全量类型检查（对比基线）...")

    errors = parse_errors(run_mypy(files if "--staged" in sys.argv else list(SCOPE)))

    baseline: set[str] = set()
    if BASELINE.exists():
        baseline = {ln for ln in BASELINE.read_text(
            encoding="utf-8").splitlines() if ln.strip()}

    fresh = sorted(errors - baseline)
    if fresh:
        print(f"[mypy-gate] ✗ 新增类型错误 {len(fresh)} 条（基线外，修复或 --rebaseline 重审）：")
        for key in fresh[:30]:
            print(f"  {key}")
        if len(fresh) > 30:
            print(f"  ...（其余 {len(fresh) - 30} 条略）")
        return 1
    print(f"[mypy-gate] ✓ 无新增类型错误（存量 {len(baseline & errors)} 条冻结于基线）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
