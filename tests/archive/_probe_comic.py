"""复现 comic_a 关键交互链：切页签/打开分镜弹窗/行右键/镜号选中联动"""
import json
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5800"
API = BASE + "/api/v1"

def api(method, path, body=None):
    req = urllib.request.Request(API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())

# 与 comic_a 相同的种子
SEED = [{"original_dialogue": f"台词{i}", "description": f"描述{i}-唯一标记RP" if i == 0 else f"描述{i}",
         "characters": ["角色A"], "scene": "天台", "props": []} for i in range(5)]
api("PUT", "/manga/storyboard/default", {"rows": SEED})

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_context(viewport={"width": 1600, "height": 900}).new_page()
    page.set_default_timeout(10000)
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(BASE + "/#/storyboard", wait_until="domcontentloaded")
    page.wait_for_selector(".st-table tbody tr", timeout=20000)
    page.wait_for_timeout(800)

    # 1) 镜号点击 → 右侧面板联动（COMIC-020 修复后的正确交互）
    page.locator(".st-table tbody tr td.st-col-shot_number").nth(0).click()
    page.wait_for_timeout(600)
    mark = page.locator("text=唯一标记RP").count()
    panel_txt = page.locator("aside.right-panel").inner_text()
    print("1) 镜号点击后 唯一标记 出现次数:", mark, "| 面板含「选中分镜」:", "选中分镜" in panel_txt,
          "| 面板含语速滑块:", page.locator("#rp-row-speed").count() > 0)

    # 2) 行右键菜单（COMIC-021）
    page.locator(".st-table tbody tr").nth(1).click(button="right")
    page.wait_for_timeout(600)
    print("2) 右键后 .st-context-menu:", page.locator(".st-context-menu").count(),
          "| [role=menu]:", page.locator("[role='menu']").count(),
          "| class*=context-menu:", page.locator("[class*='context-menu']").count())
    page.keyboard.press("Escape")
    page.mouse.click(10, 10)
    page.wait_for_timeout(300)

    # 3) 搜索框（COMIC-023）
    sb = page.locator(".st-search")
    print("3) .st-search 数量:", sb.count())
    if sb.count():
        sb.first.fill("描述3")
        page.wait_for_timeout(400)
        print("   过滤后行数:", page.locator(".st-table tbody tr").count())
        sb.first.fill("")
        page.wait_for_timeout(400)

    # 4) 切页签：导出
    try:
        page.locator("div.inline-flex button", has_text="导出").first.click()
        page.wait_for_timeout(800)
        print("4) 切到导出 OK；含 DSL 示例:", "DSL 语法示例" in page.locator("main.main-content").inner_text())
    except Exception as e:
        print("4) 切导出 FAIL:", str(e)[:120])

    # 5) 切回分镜表，打开 AI 自动分镜弹窗
    try:
        page.locator("div.inline-flex button", has_text="分镜表").first.click()
        page.wait_for_timeout(600)
        page.locator('.st-toolbar button:has-text("AI 自动分镜")').click()
        page.wait_for_selector("div.fixed.inset-0 textarea", timeout=8000)
        btns = page.locator("div.fixed.inset-0 button")
        labels = [ (btns.nth(i).inner_text() or "").strip() for i in range(btns.count()) ]
        print("5) 分镜弹窗打开 OK，按钮:", labels)
    except Exception as e:
        print("5) 分镜弹窗 FAIL:", str(e)[:120])

    print("pageerrors:", errs[:3])
    b.close()
