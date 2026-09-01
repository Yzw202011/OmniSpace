"""导出用户 04:45 任务的原始输入图（复测用同一张图）。"""
import base64
import sqlite3

db = sqlite3.connect(r'e:\OmniSpace\data\omnispace.db')
db.row_factory = sqlite3.Row
row = db.execute(
    "SELECT screenshot_4in1 FROM video_tasks "
    "WHERE id='3d1ff5adf4454ac193f69231e3783ebc'").fetchone()
data = base64.b64decode(row['screenshot_4in1'])
out = r'e:\OmniSpace\logs\_frames\user_input.png'
with open(out, 'wb') as f:
    f.write(data)
print('saved', out, len(data), 'bytes')
