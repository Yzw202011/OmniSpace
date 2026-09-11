"""升级备份还原纯函数（升级机制批3，docs/升级机制方案-2026-09-08.md §2.4）。

被两方共用（都经 importlib 文件路径加载，stdlib-only 铁律同 core.py）：
- updater/updater.py：升级失败自动回滚
- launcher/boot.py 恢复钩子：断电/中断自愈（启动最前端还原后备份数据）

还原语义：backup/<时间戳>/ 镜像安装根相对布局，把备份里存在的文件
逐一复制回原位；目标父目录不存在则创建。不做删除（备份只记被替换/
被删除的旧文件，还原=旧文件回位，多余的新文件保留无害）。
"""
from __future__ import annotations

import shutil
from pathlib import Path


def restore_backup(install_root: Path, backup_dir: Path) -> tuple[int, list[str]]:
    """把 backup_dir 镜像回 install_root。返回 (还原文件数, 错误清单)。

    单文件失败不中断整体（错误进清单，调用方决定是否继续）——
    断电自愈场景下「能救多少救多少」优先于「全有或全无」。
    """
    root = Path(install_root)
    backup = Path(backup_dir)
    restored = 0
    errors: list[str] = []
    if not backup.is_dir():
        return 0, [f"备份目录不存在: {backup}"]
    for src in sorted(backup.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(backup)
        dst = root / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                dst.chmod(dst.stat().st_mode | 0o200)  # 清只读再覆盖
            shutil.copy2(src, dst)
            restored += 1
        except OSError as exc:
            errors.append(f"{rel}: {exc}")
    return restored, errors
