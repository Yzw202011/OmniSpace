"""探测 storyboard 页签栏结构（排查 ui_batch_comic_a switch_tab 超时）"""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_context(viewport={"width": 1600, "height": 900}).new_page()
    errs = []
    page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto("http://127.0.0.1:5800/#/storyboard", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(4000)
    print("st-table rows:", page.locator(".st-table tbody tr").count())
    print("div.inline-flex buttons:", page.locator("div.inline-flex button").count())
    print("div.inline-flex count:", page.locator("div.inline-flex").count())
    for i in range(page.locator("div.inline-flex button").count()):
        print("  tab:", repr(page.locator("div.inline-flex button").nth(i).inner_text()))
    # 主内容区按钮一览（前 20 个）
    btns = page.locator("main.main-content button")
    print("main buttons:", btns.count())
    for i in range(min(btns.count(), 20)):
        t = (btns.nth(i).inner_text() or "").strip().replace("\n", " ")
        print(f"  btn{i}: {t[:40]!r}")
    print("console errors:", errs[:5])
    b.close()
