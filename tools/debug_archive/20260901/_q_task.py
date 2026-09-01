import json
import sqlite3

db = sqlite3.connect(r'e:\OmniSpace\data\omnispace.db')
db.row_factory = sqlite3.Row
tables = [r[0] for r in db.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%video%'")]
print('tables:', tables)
for t in tables:
    cols = [c[1] for c in db.execute(f'PRAGMA table_info({t})')]
    print(f'\n== {t} cols: {cols}')
    rows = db.execute(
        f'SELECT * FROM {t} ORDER BY rowid DESC LIMIT 2').fetchall()
    for r in rows:
        d = {k: (str(v)[:160] if v else v) for k, v in dict(r).items()}
        print(json.dumps(d, ensure_ascii=False))
