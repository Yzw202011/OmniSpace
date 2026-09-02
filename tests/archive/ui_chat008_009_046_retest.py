"""CHAT-008/009/046 修复聚焦复验：拖拽上传、粘贴上传、斜杠命令补全浮层。"""
import base64
import io
import sys

from PIL import Image
from playwright.sync_api import sync_playwright


def small_png_b64():
    img = Image.new("RGB", (8, 8), (220, 60, 60))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def count_attachments(page):
    return page.locator("button[aria-label^='移除附件']").count()


def main():
    b64 = small_png_b64()
    results = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("http://127.0.0.1:5800/#/chat")
        page.wait_for_selector("textarea", timeout=15000)
        page.wait_for_timeout(1500)
        # 新建会话使输入框可用
        btn = page.locator("button", has_text="新建对话").first
        if btn.count() > 0:
            btn.click()
            page.wait_for_timeout(1000)

        # ---- CHAT-008 拖拽上传 ----
        page.evaluate("""(b64) => {
          const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
          const file = new File([bytes], 'drop_test.png', {type: 'image/png'});
          const dt = new DataTransfer();
          dt.items.add(file);
          const ta = document.querySelector('textarea');
          const box = ta.closest('div.border-t') || ta.parentElement;
          for (const type of ['dragenter', 'dragover', 'drop']) {
            box.dispatchEvent(new DragEvent(type, {bubbles: true, cancelable: true, dataTransfer: dt}));
          }
        }""", b64)
        page.wait_for_timeout(900)
        n = count_attachments(page)
        results["TC-FLOW-CHAT-008"] = (
            ("PASS", f"drop 一张 PNG 后附件预览出现（{n} 个）") if n > 0
            else ("FAIL", "drop 后无附件预览"))
        # 清理附件
        while count_attachments(page) > 0:
            page.locator("button[aria-label^='移除附件']").first.click()
            page.wait_for_timeout(200)

        # ---- CHAT-009 粘贴上传 ----
        page.evaluate("""(b64) => {
          const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
          const file = new File([bytes], 'paste_test.png', {type: 'image/png'});
          const dt = new DataTransfer();
          dt.items.add(file);
          const ta = document.querySelector('textarea');
          ta.dispatchEvent(new ClipboardEvent('paste', {bubbles: true, cancelable: true, clipboardData: dt}));
        }""", b64)
        page.wait_for_timeout(900)
        n = count_attachments(page)
        results["TC-FLOW-CHAT-009"] = (
            ("PASS", f"paste PNG 后附件预览出现（{n} 个）") if n > 0
            else ("FAIL", "paste 后无附件预览"))
        # 粘贴图片时文件名文本不应进入输入框
        leak = page.locator("textarea").first.input_value()
        if "paste_test" in leak:
            results["TC-FLOW-CHAT-009"] = ("FAIL", f"图片粘贴成功但文件名文本泄漏进输入框: {leak!r}")
        while count_attachments(page) > 0:
            page.locator("button[aria-label^='移除附件']").first.click()
            page.wait_for_timeout(200)

        # ---- CHAT-046 斜杠命令补全 ----
        ta = page.locator("textarea").first
        ta.fill("/")
        page.wait_for_timeout(700)
        popup = page.locator("[role='listbox']").count()
        opts = page.locator("[role='option']").count()
        ok = popup > 0 and opts >= 2
        detail = f"输入 / 出现 listbox（{opts} 个候选）" if ok else "未出现补全浮层"
        # 键盘应用第一条命令「/新建对话」→ 输入框应被清空（命令真实生效）
        ta.press("Enter")
        page.wait_for_timeout(600)
        after_val = ta.input_value()
        if ok and after_val == "":
            detail += "；Enter 应用命令后输入框已清空（命令真实生效）"
        elif ok:
            ok = False
            detail += f"；但 Enter 应用后输入框残留 {after_val!r}"
        results["TC-FLOW-CHAT-046"] = (("PASS", detail) if ok else ("FAIL", detail))

        browser.close()

    fails = 0
    for tc, (st, dt) in results.items():
        print(f"[{st}] {tc}: {dt}")
        if st != "PASS":
            fails += 1
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
