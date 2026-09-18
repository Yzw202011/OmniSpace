"""txt 上传真浏览器 E2E（2026-09-08）：对运行中的 5800 实弹。

链路：真 Chromium → setInputFiles 喂真实 txt（等价真实文件选择器产物）
→ 断言解析占位/成功 chip/toast → 发送 → 断言 WS 发送帧含 📎 文档块
→ 断言模型回复（WS 接收帧原文引用文档关键词）。
证据：截图 + WS 帧文本落盘 logs/e2e_txt_upload/。

断言纪律（历史污染教训）：每轮唯一文件名；模型回复只认 WS 接收帧——
页面正文断言会被历史消息/用户气泡里的文档原文污染成假阳性。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5800"
# 每轮唯一文件名：会话历史保留旧消息文本，重名会串台断言
TXT = Path(r"E:/OmniSpace/tests/unit/fixtures"
           f"/e2e_upload_{time.strftime('%H%M%S')}.txt")
SHOT_DIR = Path(r"E:/OmniSpace/logs/e2e_txt_upload")
DOC_KEY = "桌面控制复现"          # 文档正文关键词，模型回复应能引用
RESULTS: list[tuple[str, bool, str]] = []


def ensure_fixture() -> None:
    """自建测试文档（根目录不散件；正文含可被模型引用的关键词）。
    每轮唯一文件名（防历史串台），旧的顺手清掉防堆积。"""
    TXT.parent.mkdir(parents=True, exist_ok=True)
    for old in TXT.parent.glob("e2e_upload_*.txt"):
        old.unlink(missing_ok=True)
    TXT.write_text(
        f"这是{DOC_KEY}用的测试文档，共二十四个字。\n第二行内容。",
        encoding="utf-8", newline="\n")


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name} | {detail}", flush=True)


def main() -> int:
    ensure_fixture()
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    ws_frames: list[str] = []      # 本版 playwright 帧回调直接给 payload 字符串
    page_errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("websocket", lambda ws: (  # type: ignore[call-overload]
            ws.on("framesent",
                  lambda f: ws_frames.append(f"[S] {f}") if isinstance(f, str) else None),
            ws.on("framereceived",
                  lambda f: ws_frames.append(f"[R] {f}") if isinstance(f, str) else None),
        ))
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        page.goto(f"{BASE}/#/chat", wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_selector("textarea", timeout=30_000)
        page.wait_for_timeout(2500)  # 等模型状态/会话列表就绪
        page.screenshot(path=str(SHOT_DIR / "1_initial.png"))

        doc_input = page.locator('input[type=file][accept=".txt,.md,.docx,.pdf"]')
        check("文档上传 input 存在", doc_input.count() == 1, f"count={doc_input.count()}")

        # ---- 喂真实文件（真 Chromium 的 CDP setFileInputFiles）----
        doc_input.set_input_files(str(TXT))

        # 解析占位 chip（服务端 ~1ms，尽力捕获）
        saw_parsing = False
        t0 = time.time()
        while time.time() - t0 < 4:
            if page.get_by_text("解析中", exact=False).count() > 0:
                saw_parsing = True
                break
            page.wait_for_timeout(50)
        check("解析中占位出现", saw_parsing, "服务端 1ms 可能闪过" if not saw_parsing else "")

        # 成功 chip：唯一文件名 + 29字
        chip = page.locator(f"text={TXT.name}").first
        chip.wait_for(state="visible", timeout=8000)
        check("成功 chip 含文件名", chip.is_visible(), chip.inner_text()[:60])
        # 字数在解析完成后才渲染（占位 chip 先出），轮询等它
        saw_chars = False
        t0 = time.time()
        while time.time() - t0 < 3:
            if page.get_by_text("29字", exact=False).count() > 0:
                saw_chars = True
                break
            page.wait_for_timeout(100)
        check("成功 chip 含字数(29字)", saw_chars)

        # toast（自动消失，4s 窗口内轮询）
        saw_toast = False
        t0 = time.time()
        while time.time() - t0 < 4:
            if page.get_by_text("已附加", exact=False).count() > 0:
                saw_toast = True
                break
            page.wait_for_timeout(150)
        check("成功 toast 出现", saw_toast)
        page.screenshot(path=str(SHOT_DIR / "2_chip.png"))

        # ---- 发送：让模型读文档 ----
        page.fill("textarea", "我上传的文档第一句话讲了什么？只引用原文回答。")
        page.keyboard.press("Enter")
        check("发送后输入框清空",
              page.wait_for_function(
                  "() => document.querySelector('textarea')?.value === ''",
                  timeout=10_000) is not None)

        # 模型回复断言走后端真源（会话消息 API）：WS 嗅探抓不到回复流
        #（实测回复不经过捕获的那条连接/或为二进制帧），UI 正文断言又
        # 会被历史与用户气泡里的文档原文污染——库里的 assistant 消息
        # 是唯一无歧义证据
        import json as _json
        import urllib.request as _ur

        def _api(path: str):
            with _ur.urlopen(f"{BASE}{path}", timeout=10) as r:
                return _json.loads(r.read().decode("utf-8"))

        reply_ok = False
        reply_sample = ""
        t0 = time.time()
        while time.time() - t0 < 240:
            try:
                sess = (_api("/api/v1/chat/sessions?limit=8")
                        .get("data", {}).get("items", []))
                for s in sess:
                    msgs = (_api(f"/api/v1/chat/sessions/{s['id']}/messages")
                            .get("data"))
                    msgs = (msgs.get("items") if isinstance(msgs, dict)
                            else msgs) or []
                    if not any(m.get("role") == "user"
                               and TXT.name in str(m.get("content", ""))
                               for m in msgs):
                        continue
                    for m in msgs:
                        if (m.get("role") == "assistant"
                                and DOC_KEY in str(m.get("content", ""))):
                            reply_ok = True
                            reply_sample = str(m.get("content", ""))[:120]
                    break
            except Exception:
                pass
            if reply_ok:
                break
            page.wait_for_timeout(2000)
        check("模型回复引用文档内容（会话库取证）", reply_ok,
              f"{int(time.time() - t0)}s 回复:「{reply_sample}」")
        page.screenshot(path=str(SHOT_DIR / "3_reply.png"))

        # ---- WS 帧取证：发送帧必须含 📎 文档块 ----
        sent = [f for f in ws_frames if f.startswith("[S]")]
        has_doc_block = any("📎 文档" in f for f in sent)
        check("WS 发送帧含 📎 文档块", has_doc_block,
              next((f[:220] for f in sent if "📎" in f), f"共 {len(sent)} 帧"))
        check("页面零 JS 报错", len(page_errors) == 0, "; ".join(page_errors[:3]))

        (SHOT_DIR / "ws_frames.txt").write_text(
            "\n".join(ws_frames[-40:]), encoding="utf-8")
        browser.close()

    fails = [r for r in RESULTS if not r[1]]
    print(f"\n===== {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS =====")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
