"""定位：右键命中不同单元格时菜单是否出现"""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_context(viewport={"width": 1600, "height": 900}).new_page()
    page.set_default_timeout(10000)
    page.goto("http://127.0.0.1:5800/#/storyboard", wait_until="domcontentloaded")
    page.wait_for_selector(".st-table tbody tr", timeout=20000)
    page.wait_for_timeout(800)

    def count_menu():
        return page.locator(".st-context-menu").count()

    # 1) 右键镜号单元格（td 直接子节点）
    page.locator(".st-table tbody tr").nth(0).locator("td.st-col-shot_number").click(button="right")
    page.wait_for_timeout(400)
    print("右键镜号单元格 → 菜单:", count_menu())
    page.evaluate("document.elementFromPoint(1,1)?.dispatchEvent(new MouseEvent('click',{bubbles:true}))")
    page.wait_for_timeout(200)

    # 2) 右键台词列（span.st-cell-text 目标）
    page.locator(".st-table tbody tr").nth(1).locator("td.st-col-original_dialogue").click(button="right")
    page.wait_for_timeout(400)
    print("右键台词列 → 菜单:", count_menu())
    page.evaluate("document.elementFromPoint(1,1)?.dispatchEvent(new MouseEvent('click',{bubbles:true}))")
    page.wait_for_timeout(200)

    # 3) 右键描述列
    page.locator(".st-table tbody tr").nth(2).locator("td.st-col-description").click(button="right")
    page.wait_for_timeout(400)
    print("右键描述列 → 菜单:", count_menu())

    # 4) 持续观察：若菜单出现后 50ms 内又消失，说明被全局监听误关
    page.locator(".st-table tbody tr").nth(0).locator("td.st-col-shot_number").click(button="right")
    for ms in (16, 50, 100, 300):
        page.wait_for_timeout(ms)
        print(f"   镜号右键后 +{ms}ms 菜单:", count_menu())
    b.close()
