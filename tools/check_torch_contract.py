"""torch 版本契约单源检查器（B1 2026-09-13）。

真源 = src/torch_contract.json（升级 torch 只改那一处）。
本工具比对契约与三处运行时的 torch/version.py 实际版本：
  - 主运行时 py310 不符 → exit 1（阻断 boot 预检语义）
  - vLLM py313 / ComfyUI 便携包不符 → exit 1 并标注旁链（自包含栈，
    由调用方决定警告或阻断）
用法：
  runtime/py310/python.exe tools/check_torch_contract.py [--warn-only]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "backend" / "torch_contract.json"


def read_torch_version(version_py: Path) -> str | None:
    try:
        text = version_py.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    m = re.search(r"""__version__\s*=\s*['"]([^'"]+)['"]""", text)
    return m.group(1) if m else None


def main() -> int:
    warn_only = "--warn-only" in sys.argv
    try:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[torch-contract] ✗ 契约文件不可读: {exc}")
        return 1
    expected = str(contract.get("torch") or "")
    if not expected:
        print("[torch-contract] ✗ 契约缺少 torch 字段")
        return 1

    bad_primary = False
    bad_side = False
    for label, rel in (contract.get("targets") or {}).items():
        actual = read_torch_version(ROOT / rel)
        if actual is None:
            print(f"[torch-contract] ✗ {label}: version.py 不可读（{rel}）")
            bad_side = True if "py310" not in label else False
            bad_primary = bad_primary or ("py310" in label)
            continue
        if actual == expected:
            print(f"[torch-contract] ✓ {label}: {actual}")
            continue
        print(f"[torch-contract] ✗ {label}: 实际 {actual} ≠ 契约 {expected}")
        if "py310" in label:
            bad_primary = True
        else:
            bad_side = True

    if not (bad_primary or bad_side):
        print(f"[torch-contract] ✓ 三处运行时全部对齐契约 {expected}")
        return 0
    if warn_only:
        print("[torch-contract] ⚠ warn-only 模式：不符仅告警")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
