"""模型降级可见性实弹 E2E（2026-09-08）：选 9B（装不下）→发消息→自动降级。

验证用户报的「对话的 AI 跟模型选择的不符合」修复：
① 助手气泡头部显示实际答复模型友好名；② 与所选不一致时带
「（自动降级）」警示与 tooltip 出路；③ 降级当场 toast 说明。
对运行中 5800 实弹（后端为旧代码也可验——前端按引擎≠所选判定）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5800"
SHOT = "E:/OmniSpace/logs/e2e_txt_upload/fallback_badge.png"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name} | {detail}", flush=True)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        console_errs: list[str] = []
        page.on("pageerror", lambda e: console_errs.append(str(e)))

        page.goto(f"{BASE}/#/chat", wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_selector("textarea", timeout=30_000)
        page.wait_for_timeout(3000)  # 等模型清单/状态就绪

        # ---- 在右栏模型下拉里选 Qwen3.5 9B（用户真实操作路径） ----
        sel_value = page.evaluate(
            """() => {
              const s = [...document.querySelectorAll('select')]
                .find(x => [...x.options].some(o => o.text.includes('Qwen3.5')));
              if (!s) return null;
              const o = [...s.options].find(o => o.text.includes('Qwen3.5'));
              return {value: o.value, label: o.text};
            }"""
        )
        if not sel_value:
            check("模型下拉含 Qwen3.5 9B 选项", False, "未找到")
            browser.close()
            return 1
        page.locator("select").nth(
            page.evaluate(
                """() => [...document.querySelectorAll('select')]
                     .findIndex(x => [...x.options].some(o => o.text.includes('Qwen3.5')))"""
            )
        ).select_option(value=sel_value["value"])
        page.wait_for_timeout(800)
        check("已选择 Qwen3.5 9B", True, sel_value["label"])

        # ---- 发消息（触发装载→失败→降级→回复） ----
        page.fill("textarea", "用一句话介绍你自己")
        page.keyboard.press("Enter")
        page.wait_for_timeout(1500)

        # toast：降级说明（自动消失，90s 窗口轮询——降级装载要几秒）
        saw_toast = False
        t0 = time.time()
        while time.time() - t0 < 90:
            if (page.get_by_text("装不下", exact=False).count() > 0
                    or page.get_by_text("自动改用", exact=False).count() > 0):
                saw_toast = True
                break
            page.wait_for_timeout(300)
        check("降级当场 toast 说明", saw_toast)

        # 助手回复完成（等流结束：出现助手气泡模型标注）
        badge = None
        t0 = time.time()
        while time.time() - t0 < 150:
            hits = page.locator("span", has_text="（自动降级）")
            if hits.count() > 0:
                badge = hits.first
                break
            page.wait_for_timeout(500)
        check("气泡显示「（自动降级）」警示", badge is not None,
              badge.inner_text() if badge else "150s 未见")
        if badge:
            # 气泡模型名应为友好名（非裸 id）且非所选 9B
            badge_text = badge.inner_text()
            check("降级标注含实际模型", "4B" in badge_text or "VL" in badge_text,
                  badge_text)
            page.screenshot(path=SHOT)
        check("页面零 JS 报错", len(console_errs) == 0,
              "; ".join(console_errs[:2]))
        browser.close()

    fails = [r for r in RESULTS if not r[1]]
    print(f"\n===== {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS =====")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
