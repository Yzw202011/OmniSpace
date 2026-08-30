import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1])))

from pypdf import PdfReader

pdf_path = Path(r"e:\OmniSpace\docs\omnispace-tech-doc\omnispace-tech-doc.pdf")
r = PdfReader(str(pdf_path))
print("pages:", len(r.pages))
print("size: %.1f KB" % (pdf_path.stat().st_size / 1024))

wm_pages = 0
probes = {
    "YZW": 0,
    "OmniSpace": 0,
    "分镜": 0,
    "FLUX": 0,
    "keep_cats": 0,
}
texts = {}
for i, page in enumerate(r.pages):
    t = page.extract_text() or ""
    texts[i + 1] = t
    if "YZW" in t:
        wm_pages += 1
    for k in probes:
        if k in t:
            probes[k] += 1

print("pages containing YZW watermark:", wm_pages)
for k, v in probes.items():
    print(f"pages containing {k!r}: {v}")

print("\n--- page 1 head ---")
print((texts.get(1, "") or "")[:300].replace("\n", " | "))
mid = len(r.pages) // 2
print(f"\n--- page {mid} head ---")
print((texts.get(mid, "") or "")[:300].replace("\n", " | "))
print(f"\n--- page {len(r.pages)} head ---")
print((texts.get(len(r.pages), "") or "")[:300].replace("\n", " | "))
