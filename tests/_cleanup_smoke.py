"""清理冒烟测试残留项目（name LIKE '冒烟项目%'）。"""
import sqlite3

db = sqlite3.connect(r"E:\OmniSpace\data\omnispace.db")
rows = db.execute(
    "SELECT id, name FROM projects WHERE name LIKE '冒烟项目%'").fetchall()
for pid, name in rows:
    sb = db.execute("SELECT id FROM storyboards WHERE project_id=?",
                    (pid,)).fetchone()
    if sb:
        db.execute("DELETE FROM storyboard_rows WHERE storyboard_id=?",
                   (sb[0],))
        db.execute("DELETE FROM storyboards WHERE id=?", (sb[0],))
    for t in ("comic_assets", "keyframes", "scene_objects"):
        db.execute(f"DELETE FROM {t} WHERE project_id=?", (pid,))
    db.execute("DELETE FROM projects WHERE id=?", (pid,))
db.commit()
print("cleaned", len(rows), "projects:", [r[1] for r in rows])
