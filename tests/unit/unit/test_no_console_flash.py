"""黑窗闪现哨兵（2026-09-05 用户报告：运行时总闪 cmd 窗一秒即逝）。

根因：无窗链（OmniSpace-Boot/Backend 等 GUI 子系统进程）拉起控制台
子系统工具（taskkill/powershell/ffmpeg/ffprobe/wmic/reg）时未挂
CREATE_NO_WINDOW，Windows 为其新开控制台即闪黑窗。

契约：src/ + launcher/ 内所有 subprocess.run/Popen/check_output
调用点，参数中含上述控制台工具者，其调用块（起点起 9 行内）必须出现
CREATE_NO_WINDOW（或等值 0x08000000）。新代码引用新的控制台工具时
请把工具名加进本哨兵的 _CONSOLE_TOOLS。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCAN_DIRS = ("backend", "launcher")
EXCLUDE_PARTS = {
    "tests", "debug_archive", "ComfyUI_windows_portable",
    "__pycache__", "node_modules",
}
_CONSOLE_TOOLS = re.compile(
    r"taskkill|powershell|ffmpeg|ffprobe|wmic|[\s\[\"']reg[\s,\"\ ']",
    re.IGNORECASE)
SPAWN_RE = re.compile(r"subprocess\.(?:run|Popen|check_output)\(")
WINDOW = 9  # 调用块窗口行数（覆盖多行参数）


def _iter_product_py() -> list[Path]:
    out: list[Path] = []
    for d in SCAN_DIRS:
        for p in (ROOT / d).rglob("*.py"):
            if any(part in p.parts for part in EXCLUDE_PARTS):
                continue
            out.append(p)
    return out


def test_console_tools_spawn_without_window_flag() -> None:
    offenders: list[str] = []
    for p in _iter_product_py():
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, ln in enumerate(lines):
            if not SPAWN_RE.search(ln):
                continue
            block = "\n".join(lines[max(0, i - 2):i + WINDOW])
            if not _CONSOLE_TOOLS.search(block):
                continue
            if "CREATE_NO_WINDOW" not in block and "0x08000000" not in block:
                rel = p.relative_to(ROOT)
                offenders.append(f"{rel}:{i + 1} {ln.strip()[:70]}")
    assert not offenders, (
        "发现控制台工具裸拉起（会闪黑窗）——请挂 "
        "creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0：\n"
        + "\n".join(offenders))
# 本项目仅供学习使用，商业授权请+Q 3559331368
