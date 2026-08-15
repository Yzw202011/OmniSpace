# -*- coding: utf-8 -*-
"""批 7 修复验证：COMIC-014 预览确认 / COMIC-020 面板联动 / COMIC-079 3D点选。"""
import json
import re

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5800"
RESULTS = []


def record(tc_id, name, status, detail):
    RESULTS.append({"tc_id": tc_id, "name": name, "status": status, "detail": detail})
    print(f"[{status}] {tc_id} {name}: {detail}")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--enable-unsafe-swiftshader"])
        page = browser.new_page(viewport={"width": 1600, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # ── 准备：漫剧页（默认项目有种子行）────────────────────
        page.goto(f"{BASE}/#/storyboard", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2500)

        # ── COMIC-020 选中分镜卡片 → 右侧属性面板联动 ─────────
        try:
            picker = page.locator(".st-row-picker").first
            picker.wait_for(state="visible", timeout=8000)
            shot_no = picker.inner_text().strip()
            picker.click()
            page.wait_for_timeout(600)
            active_rows = page.locator("tr.st-row-active").count()
            panel = page.locator(".right-panel")
            panel_text = panel.inner_text()
            ok = (active_rows == 1 and "选中分镜" in panel_text
                  and f"#{shot_no}" in panel_text)
            record("TC-FLOW-COMIC-020", "选中分镜卡片流程验证",
                   "PASS" if ok else "FAIL",
                   f"镜号#{shot_no}点击后 st-row-active={active_rows}，面板选中分镜区联动={'是' if ok else '否'}")
        except Exception as exc:
            record("TC-FLOW-COMIC-020", "选中分镜卡片流程验证", "FAIL", f"异常: {exc}")

        # ── COMIC-014 自动分镜预览与确认 ──────────────────────
        try:
            before = page.locator(".st-row-picker").count()
            page.get_by_role("button", name="AI 自动分镜").first.click()
            page.wait_for_timeout(500)
            page.locator("textarea").fill("第一镜台词\n第二镜台词\n第三镜台词")
            page.get_by_role("button", name="生成预览").click()
            page.wait_for_timeout(500)
            preview_items = page.locator(".fixed.inset-0 ul li").count()
            preview_visible = preview_items == 3
            # 返回修改可用
            page.get_by_role("button", name="返回修改").click()
            page.wait_for_timeout(300)
            back_ok = page.locator("textarea").is_visible()
            # 再次预览并确认导入
            page.get_by_role("button", name="生成预览").click()
            page.wait_for_timeout(400)
            page.get_by_role("button", name=re.compile("确认导入")).click()
            page.wait_for_timeout(2000)
            after = page.locator(".st-row-picker").count()
            ok = preview_visible and back_ok and after == before + 3
            record("TC-FLOW-COMIC-014", "自动分镜结果预览与确认验证",
                   "PASS" if ok else "FAIL",
                   f"预览{preview_items}行/返回修改={'OK' if back_ok else 'NG'}/确认后行数{before}→{after}")
        except Exception as exc:
            record("TC-FLOW-COMIC-014", "自动分镜结果预览与确认验证", "FAIL", f"异常: {exc}")

        # ── COMIC-079 3D 导演台选中/取消选中 ──────────────────
        try:
            page.goto(f"{BASE}/#/storyboard", wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2500)
            page.get_by_role("button", name="3D导演台").click()
            page.wait_for_timeout(3000)
            canvas = page.locator("canvas").first
            canvas.wait_for(state="visible", timeout=10000)
            page.get_by_role("button", name="+ 添加角色").click()
            page.wait_for_timeout(1200)
            info = page.locator(".ds-selected-info").first
            box = canvas.bounding_box()
            cx, cy = box["width"] / 2, box["height"] / 2
            # 以画布中心为圆心扫描点击（角色胶囊体应在中下部区域）
            selected = False
            for dx in (0, -40, 40, -80, 80, -120, 120):
                for dy in (0, -30, 30, -60, 60, -100, 100):
                    page.mouse.click(box["x"] + cx + dx, box["y"] + cy + dy)
                    page.wait_for_timeout(180)
                    if "已选中" in info.inner_text():
                        selected = True
                        break
                if selected:
                    break
            sel_text = info.inner_text()
            # 点空白处取消选中（点画布上缘天空区域）
            page.mouse.click(box["x"] + box["width"] - 30, box["y"] + 25)
            page.wait_for_timeout(300)
            desel_text = info.inner_text()
            deselected = "未选中" in desel_text
            ok = selected and deselected
            record("TC-FLOW-COMIC-079", "选中与取消选中3D对象验证",
                   "PASS" if ok else "FAIL",
                   f"点选={sel_text!r} / 取消={desel_text!r}")
        except Exception as exc:
            record("TC-FLOW-COMIC-079", "选中与取消选中3D对象验证", "FAIL", f"异常: {exc}")

        if errors:
            print("PAGE_ERRORS:", errors[:3])
        browser.close()

    with open(r"e:\OmniSpace\tests\ui_results\fix_verify.json", "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=1)
    print("DONE")


if __name__ == "__main__":
    main()
