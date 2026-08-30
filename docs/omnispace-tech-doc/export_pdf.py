"""OmniSpace 技术文档 HTML → PDF 导出（含 YZW 每页水印 + 页码页脚）。

用法:
    e:\\OmniSpace\runtime\\py310\\python.exe export_pdf.py

依赖: 项目内嵌 Playwright Chromium（pydeps 已装）。
原理: Chromium print 管线中 position:fixed 元素会在每一页重复渲染，以此实现零依赖全页水印。
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
HTML = BASE / "omnispace-tech-doc.html"
OUT = BASE / "omnispace-tech-doc.pdf"

WM_HTML = '<div class="yzw-wm" aria-hidden="true"><span>YZW</span></div>'

WM_CSS = """
.yzw-wm {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
  z-index: 9999;
}
.yzw-wm span {
  font-family: 'Outfit', sans-serif;
  font-weight: 700;
  font-size: 150px;
  letter-spacing: 0.22em;
  color: rgba(16, 27, 45, 0.08);
  transform: rotate(-45deg);
}
"""

FOOTER = (
    "<div style=\"width:100%;text-align:center;font-size:8px;color:#8a97a8;"
    "font-family:'Segoe UI',sans-serif;\">"
    "OmniSpace AI 全量技术文档 v2.3.1 · YZW · "
    "<span class='pageNumber'></span> / <span class='totalPages'></span>"
    "</div>"
)


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(HTML.as_uri(), wait_until="load", timeout=60000)
        page.wait_for_selector(".mermaid svg", timeout=30000)
        page.evaluate("document.fonts ? document.fonts.ready : Promise.resolve()")
        page.wait_for_timeout(2000)
        page.add_style_tag(content=WM_CSS)
        page.evaluate(
            "(html) => document.body.insertAdjacentHTML('beforeend', html)", WM_HTML
        )
        page.pdf(
            path=str(OUT),
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template="<span></span>",
            footer_template=FOOTER,
            margin={"top": "10mm", "bottom": "14mm", "left": "11mm", "right": "11mm"},
        )
        browser.close()
    print("PDF written:", OUT)


if __name__ == "__main__":
    main()
