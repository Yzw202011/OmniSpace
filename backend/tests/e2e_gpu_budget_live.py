"""gpu_budget 顾问模式实弹观察脚本（显存调度机制批2，2026-09-10）。

观察者模式（不主动占 GPU——遵守 GPU 测试活动门铁律）：tail 后端
日志，检查批2 顾问帧指纹 ``[gpu-budget]`` 是否出现。后端需已加载
批2 新代码（重启后）且发生过至少一次本地图像/视频任务。

用法（cwd = 仓库根）:
    runtime\\py310\\python.exe backend\\tests\\e2e_gpu_budget_live.py
    runtime\\py310\\python.exe backend\\tests\\e2e_gpu_budget_live.py --wait-mins 10

退出码：0 = PASS（找到顾问帧）；2 = SKIP（后端未重启/尚无任务，
指纹未出现——不判 FAIL，观察者模式诚实跳过）；3 = 后端日志不可读。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = ROOT / "logs" / "backend.log"
FINGERPRINT = "[gpu-budget]"


def _scan(path: Path) -> list[str]:
    """读日志（容错轮转：backend.log 缺失时试 backend.log.1）。"""
    for cand in (path, path.with_suffix(".log.1")):
        try:
            if cand.exists():
                return cand.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
    return []


def main() -> int:
    wait_mins = 10.0
    if "--wait-mins" in sys.argv:
        try:
            wait_mins = float(sys.argv[sys.argv.index("--wait-mins") + 1])
        except (ValueError, IndexError):
            print("用法: e2e_gpu_budget_live.py [--wait-mins N]")
            return 3

    lines = _scan(LOG_PATH)
    if not lines:
        print(f"SKIP: 后端日志不可读（{LOG_PATH}）——确认后端已启动")
        return 3

    hits = [ln for ln in lines if FINGERPRINT in ln]
    if hits:
        print(f"PASS: 日志含 {len(hits)} 条顾问帧（最近 5 条）：")
        for ln in hits[-5:]:
            print("  " + ln.strip()[:200])
        verdicts = {v for ln in hits for v in ("granted", "wait", "deny")
                    if f"verdict={v}" in ln}
        print(f"  覆盖 verdict: {sorted(verdicts)}")
        released = sum(1 for ln in hits if "release" in ln)
        print(f"  release 收尾帧: {released} 条")
        return 0

    deadline = time.time() + wait_mins * 60
    print(f"未见顾问帧指纹——后端可能未加载批2 新代码（需重启），或尚无本地任务。"
          f"进入观察模式等待 {wait_mins:.0f} 分钟（期间正常使用绘画/漫剧即可）…")
    pos = len(lines)
    while time.time() < deadline:
        time.sleep(10)
        lines = _scan(LOG_PATH)
        new_hits = [ln for ln in lines[pos:] if FINGERPRINT in ln]
        if new_hits:
            print(f"PASS: 观察窗口内出现 {len(new_hits)} 条顾问帧：")
            for ln in new_hits[-5:]:
                print("  " + ln.strip()[:200])
            return 0
        pos = len(lines)
    print("SKIP: 观察窗口内无顾问帧——请确认后端已重启加载批2 代码并跑过"
          "一次本地图像/视频任务后重试（观察者模式不判 FAIL）")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
# 本项目仅供学习使用，商业授权请+Q 3559331368
