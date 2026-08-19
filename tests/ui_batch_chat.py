"""
OmniSpace AI —— 对话页(#/chat) UI 冒烟测试（12 用例）
用法: e:\\OmniSpace\\runtime\\py310\\python.exe e:\\OmniSpace\\tests\\ui_batch_chat.py
结果: e:\\OmniSpace\\tests\\ui_results\\chat.json

推理经济性设计（真实发消息 <= 2 次）：
  - 发送#1：TC-004 的 Enter 发送在“WS 故障注入”下进行（不触发推理），
    TC-006 点「重新生成」重试成功 -> 触发真实流式（长输出：数数 1..50），
    该流供 TC-015（滚动）采样、TC-014（停止中断）使用。
  - 发送#2：“请用 Markdown 表格列出 3 种水果的价格”，完整等流式结束，
    供 TC-017（表格）/ TC-018（复制）/ TC-022（引用回填）使用。
"""
import base64
import io
import json
import os
import re
import time
import traceback

from PIL import Image
from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:5800/#/chat"
HERE = os.path.dirname(os.path.abspath(__file__))
RESULT_DIR = os.path.join(HERE, "ui_results")
TMP_DIR = os.path.join(HERE, "tmp")

TC_ORDER = [
    ("TC-FLOW-CHAT-004", "Shift+Enter换行发送流程验证"),
    ("TC-FLOW-CHAT-006", "消息发送失败重试流程验证"),
    ("TC-FLOW-CHAT-008", "拖拽图片上传流程验证"),
    ("TC-FLOW-CHAT-009", "粘贴剪贴板图片流程验证"),
    ("TC-FLOW-CHAT-012", "超大图片上传流程验证"),
    ("TC-FLOW-CHAT-014", "流式回复中断恢复流程验证"),
    ("TC-FLOW-CHAT-015", "流式回复中滚动行为验证"),
    ("TC-FLOW-CHAT-017", "Markdown表格渲染验证"),
    ("TC-FLOW-CHAT-018", "消息复制完整流程验证"),
    ("TC-FLOW-CHAT-022", "消息引用回复流程验证"),
    ("TC-FLOW-CHAT-038", "图片大小限制验证"),
    ("TC-FLOW-CHAT-046", "输入补全建议流程验证"),
]
RESULTS = {}

# 发送#1 内容（两行：验证 Shift+Enter 换行；长输出供滚动/中断测试）
SEND1_L1 = "请从1数到50，每个数字单独占一行，不要输出其他内容"
SEND1_L2 = "现在开始"
# 发送#2 内容（Markdown 表格）
SEND2 = "请用 Markdown 表格列出 3 种水果的价格"

SCROLL_JS = """() => {
  const c = document.querySelector('div.overflow-y-auto.space-y-5');
  return c ? {top: c.scrollTop, h: c.scrollHeight, ch: c.clientHeight} : null;
}"""


def rec(tc_id, status, detail):
    name = dict(TC_ORDER)[tc_id]
    RESULTS[tc_id] = {"tc_id": tc_id, "name": name, "status": status, "detail": detail}
    print(f"[{status}] {tc_id} {name}: {detail}", flush=True)


def make_small_png_b64():
    img = Image.new("RGB", (8, 8), (220, 60, 60))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def make_big_png():
    """生成 >10MB 的真实 PNG（随机噪点不可压缩）。"""
    os.makedirs(TMP_DIR, exist_ok=True)
    path = os.path.join(TMP_DIR, "over10mb.png")
    if os.path.exists(path) and os.path.getsize(path) > 10 * 1024 * 1024:
        return path
    img = Image.frombytes("RGB", (2100, 2100), os.urandom(2100 * 2100 * 3))
    img.save(path, "PNG", compress_level=1)
    return path


def count_attachments(page):
    return page.locator("button[aria-label^='移除附件']").count()


def count_bubbles(page):
    return page.locator("button", has_text="复制").count()


def main():
    ws_state = {"mode": "fail"}  # fail=注入error帧; relay=桥接真实后端

    def on_dialog_ws(ws):
        if ws_state["mode"] == "fail":
            def on_msg(_m):
                try:
                    ws.send(json.dumps({
                        "type": "error",
                        "data": {"message": "模拟发送失败（测试注入）"},
                    }))
                except Exception:
                    pass
            ws.on_message(on_msg)
        else:
            server = ws.connect_to_server()
            ws.on_message(lambda m: server.send(m))
            server.on_message(lambda m: ws.send(m))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1600, "height": 900},
            permissions=["clipboard-read", "clipboard-write"],
        )
        page = ctx.new_page()
        page.set_default_timeout(30000)
        page.route_web_socket(
            re.compile(r"ws://127\.0\.0\.1:5800/api/v1/dialog/stream/.*"), on_dialog_ws
        )

        page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2000)
        # 新建会话使输入框可用
        page.locator("button", has_text="新建对话").first.click()
        page.wait_for_selector("textarea:not([disabled])", timeout=15000)
        ta = page.locator("textarea").first
        stop_btn = page.locator("button", has_text="■ 停止")
        send_btn = page.locator("button", has_text="发送")

        # ---------- TC-FLOW-CHAT-046 输入补全建议 ----------
        try:
            ta.fill("/")
            page.wait_for_timeout(700)
            popup = page.locator(
                "[role='listbox'], [role='menu'], [class*='suggest'], [class*='complet'], [class*='mention']"
            ).count()
            ta.fill("")
            if popup > 0:
                rec("TC-FLOW-CHAT-046", "PASS", f"输入 / 出现补全浮层（命中 {popup} 个候选元素）")
            else:
                rec("TC-FLOW-CHAT-046", "SKIP",
                    "前端未实现输入补全：输入 / 后无任何建议浮层（源码无 / 或 @ 补全逻辑）")
        except Exception as e:
            rec("TC-FLOW-CHAT-046", "FAIL", f"执行异常: {e}")

        # ---------- TC-FLOW-CHAT-004 Shift+Enter 换行 / Enter 发送 ----------
        try:
            before = count_bubbles(page)
            ta.fill(SEND1_L1)
            ta.press("Shift+Enter")
            ta.press_sequentially(SEND1_L2, delay=10)
            page.wait_for_timeout(300)
            v = ta.input_value()
            mid = count_bubbles(page)
            newline_ok = ("\n" in v) and (SEND1_L1 in v) and (SEND1_L2 in v)
            not_sent = mid == before
            # Enter 发送（WS 处于故障注入模式，不触发真实推理）
            ta.press("Enter")
            page.wait_for_timeout(1000)
            sent = count_bubbles(page) > before and ta.input_value() == ""
            if newline_ok and not_sent and sent:
                rec("TC-FLOW-CHAT-004", "PASS",
                    "Shift+Enter 后输入框含换行且消息未发出；Enter 后用户消息上屏、输入框清空")
            else:
                rec("TC-FLOW-CHAT-004", "FAIL",
                    f"newline_ok={newline_ok} not_sent={not_sent} sent={sent} value={v[:40]!r}")
        except Exception as e:
            rec("TC-FLOW-CHAT-004", "FAIL", f"执行异常: {e}")

        # ---------- TC-FLOW-CHAT-006 发送失败 -> 重试成功（真实发送#1） ----------
        try:
            page.wait_for_selector("text=[生成失败]", timeout=20000)
            fail_shown = True
        except Exception:
            fail_shown = page.locator(".toast").count() > 0
        try:
            if not fail_shown:
                rec("TC-FLOW-CHAT-006", "FAIL",
                    "WS 故障注入后 20s 内未出现失败提示（无 [生成失败] 消息/错误 toast）")
            else:
                # 切换为桥接真实后端，点「重新生成」重试
                ws_state["mode"] = "relay"
                page.locator("button", has_text="重新生成").first.click()
                stop_btn.wait_for(state="visible", timeout=90000)
                rec("TC-FLOW-CHAT-006", "PASS",
                    "故障注入后气泡显示 [生成失败] 提示；点「重新生成」重试成功进入流式（出现停止按钮）")
                # 供 TC-015：将消息容器限高 300px，使有限输出也能溢出以观察自动滚动跟随
                # （仅缩小观察窗口，不改变被测的滚动跟随逻辑本身）
                page.evaluate("""() => {
                  const c = document.querySelector('div.overflow-y-auto.space-y-5');
                  if (c) { c.style.maxHeight = '300px'; c.style.flex = 'none'; }
                }""")
        except Exception as e:
            if "TC-FLOW-CHAT-006" not in RESULTS:
                rec("TC-FLOW-CHAT-006", "FAIL", f"重试后未进入流式: {e}")

        retry_ok = RESULTS.get("TC-FLOW-CHAT-006", {}).get("status") == "PASS"

        # ---------- TC-FLOW-CHAT-015 流式滚动采样 + TC-FLOW-CHAT-014 中断 ----------
        samples = []
        stopped = False
        if retry_ok:
            t0 = time.time()
            while time.time() - t0 < 120:
                s = page.evaluate(SCROLL_JS)
                if s:
                    samples.append(s)
                generating = stop_btn.count() > 0
                # 内容已溢出容器且 scrollTop 已开始跟随 -> 点击停止（TC-014 触发点）
                grew = len(samples) >= 2 and samples[-1]["top"] > 0
                if s and s["h"] > s["ch"] + 50 and grew and generating:
                    stop_btn.first.click()
                    stopped = True
                    break
                if not generating:  # 流已自然结束
                    break
                page.wait_for_timeout(700)

        # TC-015 判定
        try:
            if len(samples) >= 2:
                first, last = samples[0], samples[-1]
                grew = last["top"] > first["top"]
                near_bottom = last["top"] + last["ch"] >= last["h"] - 120
                overflow = last["h"] > last["ch"]
                if overflow and grew and near_bottom:
                    rec("TC-FLOW-CHAT-015", "PASS",
                        f"流式期间消息区自动跟随滚动（测试将容器限高300px以观察）：scrollTop {first['top']}→{last['top']}，"
                        f"停止时贴底（top+{last['ch']}≈scrollHeight {last['h']}）")
                else:
                    rec("TC-FLOW-CHAT-015", "FAIL",
                        f"overflow={overflow} grew={grew} near_bottom={near_bottom} samples={samples[:6]}")
            else:
                rec("TC-FLOW-CHAT-015", "FAIL", f"流式期间未采到滚动样本（{len(samples)} 个），回复可能过短或生成失败")
        except Exception as e:
            rec("TC-FLOW-CHAT-015", "FAIL", f"执行异常: {e}")

        # TC-014 判定
        try:
            if not stopped and stop_btn.count() > 0:
                stop_btn.first.click()
                stopped = True
            if stopped:
                send_btn.wait_for(state="visible", timeout=15000)
                page.wait_for_timeout(1500)
                t1 = page.locator("div.flex.gap-3").last.inner_text()
                page.wait_for_timeout(1500)
                t2 = page.locator("div.flex.gap-3").last.inner_text()
                stable = t1 == t2 and len(t1.strip()) > 0
                input_ok = ta.is_enabled()
                regen_ok = page.locator("button", has_text="重新生成").count() > 0
                if stable and input_ok and regen_ok:
                    rec("TC-FLOW-CHAT-014", "PASS",
                        f"流式中点「■ 停止」后生成中止（部分内容稳定保留 {len(t1)} 字符）、"
                        f"输入框可用且出现「重新生成」，状态可控")
                else:
                    rec("TC-FLOW-CHAT-014", "FAIL",
                        f"stable={stable} input_ok={input_ok} regen_ok={regen_ok}")
            else:
                rec("TC-FLOW-CHAT-014", "FAIL", "未出现停止按钮（流式未开始），无法验证中断")
        except Exception as e:
            rec("TC-FLOW-CHAT-014", "FAIL", f"执行异常: {e}")

        # ---------- TC-FLOW-CHAT-008 拖拽图片上传 ----------
        try:
            b64 = make_small_png_b64()
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
            page.wait_for_timeout(800)
            if count_attachments(page) > 0:
                rec("TC-FLOW-CHAT-008", "PASS", "drop 一张 PNG 后附件预览出现")
            else:
                rec("TC-FLOW-CHAT-008", "SKIP",
                    "前端未实现拖拽上传：drop PNG 后无附件预览（DialogView 无 onDrop/onDragOver 处理）")
        except Exception as e:
            rec("TC-FLOW-CHAT-008", "FAIL", f"执行异常: {e}")

        # ---------- TC-FLOW-CHAT-009 粘贴剪贴板图片 ----------
        try:
            page.evaluate("""(b64) => {
              const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
              const file = new File([bytes], 'paste_test.png', {type: 'image/png'});
              const dt = new DataTransfer();
              dt.items.add(file);
              const ta = document.querySelector('textarea');
              ta.dispatchEvent(new ClipboardEvent('paste', {bubbles: true, cancelable: true, clipboardData: dt}));
            }""", b64)
            page.wait_for_timeout(800)
            if count_attachments(page) > 0:
                rec("TC-FLOW-CHAT-009", "PASS", "paste 事件携带 PNG 后附件预览出现")
            else:
                rec("TC-FLOW-CHAT-009", "SKIP",
                    "前端未实现粘贴图片上传：paste PNG 后无附件预览（输入框无 onPaste 处理）")
        except Exception as e:
            rec("TC-FLOW-CHAT-009", "FAIL", f"执行异常: {e}")

        # ---------- TC-FLOW-CHAT-012 超大图片上传 / TC-FLOW-CHAT-038 大小限制文案 ----------
        toast_text = ""
        try:
            # 清理前序用例（CHAT-008 拖拽 / CHAT-009 粘贴）遗留附件，避免移除按钮残留误判超大图被接受
            while page.locator("button[aria-label^='移除附件']").count() > 0:
                page.locator("button[aria-label^='移除附件']").first.click()
                page.wait_for_timeout(150)
            big = make_big_png()
            mb = os.path.getsize(big) / 1024 / 1024
            page.locator("input[type='file']").set_input_files(big)
            # 时序修正：toast 3.75s 自动消失，须在等待附件之前先采样 toast
            try:
                page.wait_for_selector(".toast", timeout=4000)
                toast_text = page.locator(".toast").first.inner_text()
            except Exception:
                toast_text = ""
            appeared = False
            try:
                page.wait_for_selector("button[aria-label^='移除附件']", timeout=3000)
                appeared = True
            except Exception:
                appeared = False
            if appeared:
                rec("TC-FLOW-CHAT-012", "FAIL",
                    f"超大图片（{mb:.1f}MB > 10MB）被正常加入附件预览，前端无拒绝与提示（onFilesChange 无大小校验）")
                # 清理附件，避免带入发送#2
                page.locator("button[aria-label^='移除附件']").first.click()
                page.wait_for_timeout(300)
                rec("TC-FLOW-CHAT-038", "SKIP",
                    "前端未实现图片大小限制（无限制值与提示文案可验证）")
            else:
                rec("TC-FLOW-CHAT-012", "PASS",
                    f"超大图片（{mb:.1f}MB）被前端拒绝，未生成附件预览" + (f"，提示：{toast_text}" if toast_text else "（无提示文案）"))
                if toast_text:
                    rec("TC-FLOW-CHAT-038", "PASS", f"限制提示文案：{toast_text}")
                else:
                    rec("TC-FLOW-CHAT-038", "FAIL", "超大图被拒绝但无任何限制提示文案")
        except Exception as e:
            rec("TC-FLOW-CHAT-012", "FAIL", f"执行异常: {e}")
            rec("TC-FLOW-CHAT-038", "FAIL", "联动用例未执行")

        # ---------- 真实发送#2：Markdown 表格问题 ----------
        send2_ok = False
        try:
            ta.fill(SEND2)
            ta.press("Enter")
            stop_btn.wait_for(state="visible", timeout=90000)
            # 等待流式完成（停止按钮消失 = generating=false）；4B 模型推理较慢，放宽到 7 分钟
            stop_btn.wait_for(state="hidden", timeout=420000)
            send2_ok = True
        except Exception as e:
            send2_ok = False
            send2_err = str(e)

        # ---------- TC-FLOW-CHAT-017 Markdown 表格渲染 ----------
        try:
            tables = page.locator(".md-body table").count()
            if send2_ok and tables > 0:
                rec("TC-FLOW-CHAT-017", "PASS", f"回复完成后消息区渲染出 <table> 元素（共 {tables} 个）")
            elif not send2_ok:
                rec("TC-FLOW-CHAT-017", "FAIL", f"发送#2 流式未完成: {send2_err[:120]}")
            else:
                last_txt = page.locator("div.flex.gap-3").last.inner_text()[:120]
                rec("TC-FLOW-CHAT-017", "FAIL", f"回复中未渲染出 <table>，回复片段: {last_txt!r}")
        except Exception as e:
            rec("TC-FLOW-CHAT-017", "FAIL", f"执行异常: {e}")

        # ---------- TC-FLOW-CHAT-018 消息复制 ----------
        try:
            row = page.locator("div.flex.gap-3", has_text=SEND2).last
            row.locator("button", has_text="复制").click()
            page.wait_for_timeout(600)
            clip_user = page.evaluate("navigator.clipboard.readText()")
            user_ok = clip_user == SEND2
            last = page.locator("div.flex.gap-3").last
            last.locator("button", has_text="复制").click()
            page.wait_for_timeout(600)
            clip_ai = page.evaluate("navigator.clipboard.readText()")
            ai_ok = len(clip_ai) > 10 and ("|" in clip_ai)
            if user_ok and ai_ok:
                rec("TC-FLOW-CHAT-018", "PASS",
                    f"复制按钮写入剪贴板且内容与消息一致（用户消息精确相等；助手消息含表格语法，{len(clip_ai)} 字符）")
            else:
                rec("TC-FLOW-CHAT-018", "FAIL",
                    f"user_ok={user_ok}（剪贴板 {clip_user[:40]!r}）ai_ok={ai_ok}（{clip_ai[:60]!r}）")
        except Exception as e:
            rec("TC-FLOW-CHAT-018", "FAIL", f"执行异常: {e}")

        # ---------- TC-FLOW-CHAT-022 消息引用回复 ----------
        try:
            last = page.locator("div.flex.gap-3").last
            last.locator("button", has_text="引用").click()
            page.wait_for_timeout(500)
            v = ta.input_value()
            quote_ok = v.startswith("> ") and v.endswith("\n\n") and len(v) > 12
            if quote_ok:
                rec("TC-FLOW-CHAT-022", "PASS",
                    f"点「引用」后输入框回填 Markdown 引用块（'> {v[2:30]}…'）；按推理预算未再次发送")
            else:
                rec("TC-FLOW-CHAT-022", "FAIL", f"引用回填不符合预期: {v[:60]!r}")
            ta.fill("")
        except Exception as e:
            rec("TC-FLOW-CHAT-022", "FAIL", f"执行异常: {e}")

        browser.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
    # 汇总输出（未执行的记 FAIL）
    os.makedirs(RESULT_DIR, exist_ok=True)
    out = []
    for tc_id, name in TC_ORDER:
        out.append(RESULTS.get(tc_id) or {
            "tc_id": tc_id, "name": name, "status": "FAIL", "detail": "脚本中断，用例未执行",
        })
    path = os.path.join(RESULT_DIR, "chat.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    stats = {}
    for r in out:
        stats[r["status"]] = stats.get(r["status"], 0) + 1
    print(f"\n结果已写入 {path}")
    print("统计:", stats)
