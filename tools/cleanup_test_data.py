"""UAT 测试数据清理（M-10，2026-08-31 台账）。

背景：四轮 UAT / E2E 遗留的测试数据直达用户界面——
  - 对话历史含「鸡兔同笼」算术测试会话（e2e_r1_test）
  - 作品列表含 E2E-R1/R2/R3、纯数字名（123456789/1234/1113）、
    「测试作品-*」「*·一致性测试」等测试作品

用法（项目根目录执行）：
  runtime/py313/python.exe tools/cleanup_test_data.py            # dry-run 预览
  runtime/py313/python.exe tools/cleanup_test_data.py --apply    # 备份后执行删除

判定口径（保守，宁漏勿删）：
  - 作品：名称 E2E- 前缀 / id e2e 前缀 / 纯数字名 / 「测试作品」前缀 /
    以「·一致性测试」结尾。「竞品对齐-*」为正式对齐工作产物，不删。
  - 对话：id e2e_ 前缀 / 标题 E2E 前缀。空「新对话」会话属 M-43/M-44
    范畴，不在本脚本清理范围。

级联范围：projects → storyboards → storyboard_rows → video_tasks，
及 keyframes / comic_assets / scene_objects（project_id 维度）；
对话删 dialog_messages → dialog_sessions。keyframes/comic_assets/
video_tasks 指向 data/ 内的产物文件一并清理（仅限 data/ 子树，防穿越）。

安全约定：所有 SQL 均为内联字面量（零拼接、零变量传递）；
待删 ID 经临时表参数绑定传递，杜绝注入面。
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "omnispace.db"
BACKUP_DIR = ROOT / "data" / "backups"
DATA_DIR = ROOT / "data"

PURE_DIGITS = re.compile(r"^\d+$")


def is_test_project(pid: str, name: str) -> bool:
    return bool(
        pid.startswith("e2e")
        or name.startswith("E2E-")
        or name.startswith("测试作品")
        or PURE_DIGITS.match(name)
        or name.endswith("·一致性测试")
    )


def is_test_session(sid: str, title: str) -> bool:
    return sid.startswith("e2e_") or title.startswith("E2E")


def safe_unlink(path_str: str | None) -> bool:
    """删除 data/ 子树内的产物文件（相对路径以 data/ 为基准）。

    守卫：库内个别字段（video_tasks.screenshot_4in1）存的是 base64
    数据而非路径——超长字符串交给 Path/resolve 会在 Windows 上
    长时间阻塞（实测分钟级），先按长度直接排除。
    """
    if not path_str or not isinstance(path_str, str) or len(path_str) > 500:
        return False
    p = Path(path_str)
    if not p.is_absolute():
        p = DATA_DIR / p
    try:
        rp = p.resolve()
        if DATA_DIR.resolve() not in rp.parents:
            return False  # 越界保护：仅清 data/ 内文件
        if rp.is_file():
            rp.unlink()
            return True
    except OSError:
        pass
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="UAT 测试数据清理（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="执行删除（先自动备份）")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"[错误] 数据库不存在: {DB_PATH}")
        return 1

    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()
    cur.execute("PRAGMA foreign_keys = ON")
    cur.execute("CREATE TEMP TABLE del_projects(id TEXT PRIMARY KEY)")
    cur.execute("CREATE TEMP TABLE del_sessions(id TEXT PRIMARY KEY)")
    cur.execute("CREATE TEMP TABLE del_storyboards(id TEXT PRIMARY KEY)")
    cur.execute("CREATE TEMP TABLE del_rows(id TEXT PRIMARY KEY)")

    projects = [
        (pid, name)
        for pid, name in cur.execute("SELECT id, name FROM projects").fetchall()
        if is_test_project(pid, name)
    ]
    sessions = [
        (sid, title)
        for sid, title in cur.execute("SELECT id, title FROM dialog_sessions").fetchall()
        if is_test_session(sid, title)
    ]

    cur.executemany(
        "INSERT OR IGNORE INTO del_projects VALUES (?)", [(p[0],) for p in projects]
    )
    cur.executemany(
        "INSERT OR IGNORE INTO del_sessions VALUES (?)", [(s[0],) for s in sessions]
    )
    cur.execute(
        "INSERT OR IGNORE INTO del_storyboards"
        " SELECT id FROM storyboards WHERE project_id IN (SELECT id FROM del_projects)"
    )
    cur.execute(
        "INSERT OR IGNORE INTO del_rows"
        " SELECT id FROM storyboard_rows WHERE storyboard_id IN (SELECT id FROM del_storyboards)"
    )

    print("=" * 62)
    print(f"待清理测试作品 {len(projects)} 个：")
    for pid, name in projects:
        print(f"  - [{pid}] {name}")
    print(f"待清理测试会话 {len(sessions)} 个：")
    for sid, title in sessions:
        print(f"  - [{sid}] {title}")
    print("-" * 62)
    n_storyboards = cur.execute(
        "SELECT COUNT(*) FROM storyboards WHERE project_id IN (SELECT id FROM del_projects)"
    ).fetchone()[0]
    n_rows = cur.execute(
        "SELECT COUNT(*) FROM storyboard_rows WHERE storyboard_id IN (SELECT id FROM del_storyboards)"
    ).fetchone()[0]
    n_assets = cur.execute(
        "SELECT COUNT(*) FROM comic_assets WHERE project_id IN (SELECT id FROM del_projects)"
    ).fetchone()[0]
    n_scene = cur.execute(
        "SELECT COUNT(*) FROM scene_objects WHERE project_id IN (SELECT id FROM del_projects)"
    ).fetchone()[0]
    n_keyframes = cur.execute(
        "SELECT COUNT(*) FROM keyframes WHERE project_id IN (SELECT id FROM del_projects)"
    ).fetchone()[0]
    n_videos = cur.execute(
        "SELECT COUNT(*) FROM video_tasks WHERE storyboard_row_id IN (SELECT id FROM del_rows)"
    ).fetchone()[0]
    n_messages = cur.execute(
        "SELECT COUNT(*) FROM dialog_messages WHERE session_id IN (SELECT id FROM del_sessions)"
    ).fetchone()[0]
    for label, n in (
        ("storyboards", n_storyboards),
        ("storyboard_rows", n_rows),
        ("comic_assets", n_assets),
        ("scene_objects", n_scene),
        ("keyframes", n_keyframes),
        ("video_tasks", n_videos),
        ("dialog_messages", n_messages),
    ):
        if n:
            print(f"  关联记录 {label}: {n}")
    print("=" * 62)

    if not args.apply:
        print("[dry-run] 未执行删除。加 --apply 执行（将先自动备份到 data/backups/）。")
        con.close()
        return 0

    # ── 备份（后端未运行时的直接文件复制；backup API 在大库上出现过
    #    长时间不返回，文件复制对本场景更直接） ────────────────────
    import shutil

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = BACKUP_DIR / ("omnispace_cleanup_" + stamp + ".db")
    shutil.copy2(DB_PATH, backup)
    print(f"[备份] {backup}", flush=True)

    # ── 产物文件（先收集再删记录） ────────────────────────────────
    files_removed = 0
    for (fp,) in cur.execute(
        "SELECT file_path FROM keyframes WHERE project_id IN (SELECT id FROM del_projects)"
    ).fetchall():
        files_removed += safe_unlink(fp)
    for (fp,) in cur.execute(
        "SELECT file_path FROM comic_assets WHERE project_id IN (SELECT id FROM del_projects)"
    ).fetchall():
        files_removed += safe_unlink(fp)
    for fp in cur.execute(
        "SELECT screenshot_4in1, audio_path, file_path FROM video_tasks"
        " WHERE storyboard_row_id IN (SELECT id FROM del_rows)"
    ).fetchall():
        files_removed += sum(safe_unlink(x) for x in fp)
    print(f"[文件] 产物清理完成，共删 {files_removed} 个", flush=True)

    # ── 级联删记录（子代在前） ────────────────────────────────────
    cur.execute(
        "DELETE FROM video_tasks WHERE storyboard_row_id IN (SELECT id FROM del_rows)"
    )
    cur.execute("DELETE FROM storyboard_rows WHERE id IN (SELECT id FROM del_rows)")
    cur.execute(
        "DELETE FROM keyframes WHERE project_id IN (SELECT id FROM del_projects)"
    )
    cur.execute(
        "DELETE FROM comic_assets WHERE project_id IN (SELECT id FROM del_projects)"
    )
    cur.execute(
        "DELETE FROM scene_objects WHERE project_id IN (SELECT id FROM del_projects)"
    )
    cur.execute("DELETE FROM storyboards WHERE id IN (SELECT id FROM del_storyboards)")
    cur.execute("DELETE FROM projects WHERE id IN (SELECT id FROM del_projects)")
    cur.execute(
        "DELETE FROM dialog_messages WHERE session_id IN (SELECT id FROM del_sessions)"
    )
    cur.execute("DELETE FROM dialog_sessions WHERE id IN (SELECT id FROM del_sessions)")

    con.commit()
    con.close()
    print(f"[完成] 删除作品 {len(projects)}、会话 {len(sessions)}、产物文件 {files_removed} 个。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
