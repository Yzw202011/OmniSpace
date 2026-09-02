#!/usr/bin/env python3
"""logs/ 目录治理（2026-09-01 日志机制方案 C）。

logs/ 应只存放运行日志（*.log / *.db / .heartbeat / usage/ 子目录）；
历史上被当临时工作区堆了调试脚本、帧目录、旧导出产物。本脚本把
非日志杂物【移动】到 tools/debug_archive/<日期>/（不删除，可回取），
并清理我此前重启产生的编号 boot_out*.log（内容已并入 boot.log 体系）。

用法：python tools/archive_debug_debris.py [--dry-run]
"""
from __future__ import annotations

import re
import shutil
import sys
from datetime import date
from pathlib import Path

LOGS = Path(r'E:\OmniSpace\logs').resolve()
ARCHIVE_ROOT = Path(r'E:\OmniSpace\tools\debug_archive')

# 保留在 logs/ 内的白名单：日志本体与运行期数据
# （events/ = 事件日志按天文件；*.db-shm/-wal = SQLite 边车，随库走）
_KEEP_SUFFIXES = {'.log', '.db', '.db-shm', '.db-wal', '.jsonl'}
_KEEP_NAMES = {'.heartbeat', 'usage', 'events'}
# 历史外层重定向产生的日志变体（boot.py 已统一写 boot.log）；
# 当前运行进程占用的文件跳过（下次重启后再清）
_LEGACY_RE = re.compile(
    r'^(boot_(out|err)\d*|boot_restart\d*|backend_(out|err|stdout|stderr'
    r'|h3chain)|_boot_(stdout|stderr)|launcher_(out|err'
    r'|restart_(out|err))|launch_(stdout|stderr)'
    r'|comfyui_(h3|manual(_err)?|paint))\.log$')


def main() -> int:
    dry = '--dry-run' in sys.argv
    dest = ARCHIVE_ROOT / date.today().strftime('%Y%m%d')
    moved = skipped = 0
    for entry in sorted(LOGS.iterdir()):
        name = entry.name
        if name.startswith('.'):
            skipped += 1
            continue
        if entry.is_dir():
            if name in _KEEP_NAMES:
                skipped += 1
                continue
            reason = '目录杂物'
        elif entry.suffix.lower() in _KEEP_SUFFIXES:
            if _LEGACY_RE.match(name):
                reason = '历史重定向日志'
            else:
                skipped += 1
                continue
        else:
            reason = '调试残留'
        moved += 1
        print(f'[{"dry " if dry else ""}move] {name}  ({reason})')
        if not dry:
            try:
                dest.mkdir(parents=True, exist_ok=True)
                target = dest / name
                if target.exists():
                    target = dest / f'{entry.stem}_{entry.suffix or "dir"}'
                shutil.move(str(entry), str(target))
            except (PermissionError, OSError) as exc:
                # 被运行中进程占用（如当前 boot 的重定向文件）：跳过，
                # 下次重启后再跑本脚本即可清掉
                moved -= 1
                print(f'  [skip-locked] {name}: {exc}')
    print(f'==> moved={moved} kept={skipped} -> {dest}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
