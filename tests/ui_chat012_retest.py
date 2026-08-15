# -*- coding: utf-8 -*-
"""CHAT-012/038 修复聚焦复验：超大图片拒绝 + toast 提示文案（正确时序采样）。"""
import os
import sys

from PIL import Image
from playwright.sync_api import sync_playwright

TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp")


def make_big_png():
    os.makedirs(TMP, exist_ok=True)
    p = os.path.join(TMP, "over10mb.png")
    if os.path.exists(p) and os.path.getsize(p) > 10 * 1024 * 1024:
        return p
    Image.frombytes("RGB", (2100, 2100), os.urandom(2100 * 2100 * 3)).save(
        p, "PNG", compress_level=1)
    return p


def main():
    big = make_big_png()
    mb = os.path.getsize(big) / 1024 / 1024
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("http://127.0.0.1:5800/#/chat")
        page.wait_for_selector("textarea", timeout=15000)
        page.wait_for_timeout(1500)

        page.locator("input[type='file']").set_input_files(big)
        toast_text = ""
        try:
            page.wait_for_selector(".toast", timeout=4000)
            toast_text = page.locator(".toast").first.inner_text()
        except Exception:
            pass
        appeared = page.locator("button[aria-label^='移除附件']").count() > 0

        r012 = "FAIL" if appeared else "PASS"
        r038 = "PASS" if toast_text else "FAIL"
        print(f"[{r012}] TC-FLOW-CHAT-012: 13.3MB 超大图 {'被错误接受' if appeared else f'被拒绝（{mb:.1f}MB），无附件预览'}")
        print(f"[{r038}] TC-FLOW-CHAT-038: 提示文案 = {toast_text!r}")

        # 对照组：小图片应正常入列（拒绝逻辑不误伤）
        small = os.path.join(TMP, "small.png")
        Image.new("RGB", (8, 8), (60, 200, 60)).save(small, "PNG")
        page.locator("input[type='file']").set_input_files(small)
        try:
            page.wait_for_selector("button[aria-label^='移除附件']", timeout=5000)
            print("[PASS] 对照组：8x8 小图正常加入附件预览（未误伤）")
        except Exception:
            print("[FAIL] 对照组：小图未入列，拒绝逻辑误伤正常上传")
        browser.close()
        return 0 if (r012 == "PASS" and r038 == "PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
