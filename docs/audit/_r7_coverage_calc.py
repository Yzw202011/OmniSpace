"""R7 收官扫覆盖计算：六轮报告点名过的文件 vs 实际文件清单，输出从未点名者。"""
import pathlib
import re

root = pathlib.Path('.')
covered = set()
covered_names = set()
for rpt in root.glob('docs/audit/round-2026-10-2*-rootcause-r*.md'):
    text = rpt.read_text(encoding='utf-8', errors='ignore').replace('\\', '/')
    for m in re.findall(
            r'(?:src|frontend/src|launcher|updater|tools|tests|skills|'
            r'plugin|examples)/[\w./-]+\.(?:py|ts|tsx|json|yaml)', text):
        covered.add(m)
        covered_names.add(m.rsplit('/', 1)[-1])
    for m in re.findall(r'[\w-]+\.(?:py|ts|tsx)\b', text):
        covered_names.add(m)
covered_dirs = {c.rsplit('/', 1)[0] for c in covered}

uncovered = []
for pattern in ['src/**/*.py', 'frontend/src/**/*.ts',
                'frontend/src/**/*.tsx', 'launcher/*.py', 'updater/*.py']:
    for f in root.glob(pattern):
        if not f.is_file():
            continue
        rel = f.as_posix()
        d = str(pathlib.Path(rel).parent)
        name = f.name
        if rel in covered or name in covered_names:
            continue
        if d in covered_dirs and len(list(pathlib.Path(d).glob('*.py'))) <= 15:
            continue
        try:
            lines = len(f.read_text(encoding='utf-8', errors='ignore')
                        .splitlines())
        except Exception:
            lines = 0
        uncovered.append((lines, rel))

uncovered.sort(reverse=True)
total = sum(n for n, _ in uncovered)
print(f'未被任何报告点名: {len(uncovered)} 文件 / {total} 行')
for n, f in uncovered[:45]:
    print(f'{n:6d}  {f}')
