"""MutationObserver 观察右键菜单挂载/卸载 + 事件序列记录"""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_context(viewport={"width": 1600, "height": 900}).new_page()
    page.set_default_timeout(10000)
    page.goto("http://127.0.0.1:5800/#/storyboard", wait_until="domcontentloaded")
    page.wait_for_selector(".st-table tbody tr", timeout=20000)
    page.wait_for_timeout(800)

    page.evaluate("""() => {
      window.__evt = [];
      for (const t of ['pointerdown','mousedown','mouseup','pointerup','contextmenu','click','auxclick','scroll']) {
        window.addEventListener(t, (e) => {
          window.__evt.push(t + '@' + (e.target.tagName||'') + '.' + (e.target.className?.toString?.().slice(0,30)||''));
        }, true);
      }
      window.__mut = [];
      const mo = new MutationObserver((recs) => {
        for (const r of recs) {
          r.addedNodes.forEach(n => { if (n.nodeType===1 && /context-menu/.test(n.className||'')) window.__mut.push('ADD ' + n.className); });
          r.removedNodes.forEach(n => { if (n.nodeType===1 && /context-menu/.test(n.className||'')) window.__mut.push('REMOVE ' + n.className); });
        }
      });
      mo.observe(document.body, {childList: true, subtree: true});
    }""")

    page.locator(".st-table tbody tr").nth(1).locator("td.st-col-shot_number").click(button="right")
    page.wait_for_timeout(1000)
    print("事件序列:", page.evaluate("window.__evt"))
    print("菜单突变:", page.evaluate("window.__mut"))
    print("最终菜单数量:", page.locator(".st-context-menu").count())
    b.close()
