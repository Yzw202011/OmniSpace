# -*- coding: utf-8 -*-
"""聚焦调试：行右键菜单为何不出现"""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_context(viewport={"width": 1600, "height": 900}).new_page()
    page.set_default_timeout(10000)
    page.goto("http://127.0.0.1:5800/#/storyboard", wait_until="domcontentloaded")
    page.wait_for_selector(".st-table tbody tr", timeout=20000)
    page.wait_for_timeout(800)

    # A. 原生监听确认 contextmenu 事件是否到达 tr
    page.evaluate("""() => {
      window.__cmLog = [];
      document.addEventListener('contextmenu', (e) => {
        const tr = e.target.closest('tr');
        window.__cmLog.push({tag: e.target.tagName, cls: e.target.className?.toString?.().slice(0,60), inTr: !!tr});
      }, true);
    }""")

    tr = page.locator(".st-table tbody tr").nth(1)
    tr.click(button="right")
    page.wait_for_timeout(500)
    print("A) contextmenu 事件日志:", page.evaluate("window.__cmLog"))
    print("   菜单数量:", page.locator(".st-context-menu").count())

    # B. 直接在 td 上分派 contextmenu（绕过坐标命中）
    page.evaluate("""() => {
      const tr = document.querySelectorAll('.st-table tbody tr')[2];
      const r = tr.getBoundingClientRect();
      tr.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true,
        clientX: r.x + 60, clientY: r.y + 5, button: 2}));
    }""")
    page.wait_for_timeout(500)
    print("B) 分派 contextmenu 后菜单数量:", page.locator(".st-context-menu").count())
    if page.locator(".st-context-menu").count():
        print("   菜单文本:", page.locator(".st-context-menu").inner_text().replace("\n", " | "))
    b.close()
