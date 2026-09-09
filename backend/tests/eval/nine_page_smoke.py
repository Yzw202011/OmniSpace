"""九页浏览器冒烟（V8 验收②，2026-09-09）。

真浏览器（Playwright Chromium）逐页走访 9 个一级路由（Hash Router），
收集：控制台错误 / 页面未捕获异常 / DOM 渲染哨兵（#root 有内容），
每页截图留证 → logs/eval/nine_page_smoke_<时间戳>/。

判定：任一页 console error 或页面异常或空渲染 = FAIL（白屏/JS 崩）。
用法：runtime/py310/python.exe backend/tests/eval/nine_page_smoke.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5800"
ROUTES = ["/chat", "/paint", "/storyboard", "/learning", "/models",
          "/style", "/settings", "/logs", "/help"]
OUT_DIR = Path("E:/OmniSpace/logs/eval")


def main() -> int:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    shot_dir = OUT_DIR / f"nine_page_smoke_{stamp}"
    shot_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    failures = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        for route in ROUTES:
            console_errors: list[str] = []
            page_errors: list[str] = []
            page.on("console",  # type: ignore[call-overload]
                    lambda m, acc=console_errors: acc.append(m.text)
                    if m.type == "error" else None)
            page.on("pageerror",  # type: ignore[call-overload]
                    lambda e, acc=page_errors: acc.append(str(e)))
            try:
                page.goto(f"{BASE}/#{route}", wait_until="domcontentloaded",
                          timeout=20000)
                page.wait_for_timeout(3500)  # 首屏异步渲染（数据/WS/预热）
                root_children = page.evaluate(
                    "document.getElementById('root')"
                    ".querySelectorAll(':scope > *').length")
                body_text_len = page.evaluate(
                    "document.body.innerText.trim().length")
                blank = root_children == 0 or body_text_len < 20
                shot = f"{route.strip('/')}.png"
                page.screenshot(path=str(shot_dir / shot))
                ok = (not console_errors and not page_errors and not blank)
                if not ok:
                    failures += 1
                results.append({
                    "route": route, "ok": ok,
                    "root_children": root_children,
                    "body_text_len": body_text_len,
                    "console_errors": console_errors[:5],
                    "page_errors": page_errors[:3],
                    "screenshot": shot,
                })
                print(f"  {route}: {'PASS' if ok else 'FAIL'}"
                      f" (root={root_children} text={body_text_len}"
                      f" cerr={len(console_errors)} perr={len(page_errors)})",
                      flush=True)
            except Exception as exc:  # noqa: BLE001 - 单页失败不终止巡检
                failures += 1
                results.append({"route": route, "ok": False,
                                "error": str(exc)[:200]})
                print(f"  {route}: EXCEPTION {str(exc)[:120]}", flush=True)
        browser.close()

    report = {"bench": "nine_page_smoke", "ts": time.strftime("%F %T"),
              "base": BASE, "failures": failures, "pages": results}
    (shot_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] {9 - failures}/9 PASS，截图+报告 → {shot_dir}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
