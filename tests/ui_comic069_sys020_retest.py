# -*- coding: utf-8 -*-
"""
OmniSpace AI —— 批 7g 复测（COMIC-069 语速/音量滑块 + SYS-020 面板拖拽调宽）
----------------------------------------------------------------
运行：
    e:\\OmniSpace\\runtime\\py310\\python.exe e:\\OmniSpace\\tests\\ui_comic069_sys020_retest.py
前置：
    后端已运行于 http://127.0.0.1:5800 ，前端 dist 已含批 7g 构建产物
覆盖：
    TC-FLOW-COMIC-069 语速/音量滑块控制验证（P1）
      - UI：选中分镜行 → 右侧面板滑块方向键步进 → 松手/键起提交 PUT 行更新
      - API：GET 分镜行回读确认落库；越界值（speed=3.0 / volume=-20）→ 40008
    TC-FLOW-SYS-020 右侧面板拖拽调整宽度验证（P3）
      - UI：拖拽左缘手柄宽度变化；localStorage 持久化；刷新后宽度保持
判定原则：
    PASS=真实观察到交互生效且数据落库/持久化；FAIL=入口存在但行为错误；SKIP=UI 无入口
"""
import json
import os
import sys
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5800"
API = BASE + "/api/v1"
OUT_FILE = r"e:\OmniSpace\tests\ui_results\comic069_sys020_retest.json"
VIEWPORT = {"width": 1600, "height": 900}

SEED_ROWS = [
    {"original_dialogue": "台词：批7g验证", "description": "语速音量滑块验证行",
     "characters": ["角色G"], "scene": "天台", "props": []},
    {"original_dialogue": "台词：第二行", "description": "对照行",
     "characters": ["角色H"], "scene": "教室", "props": []},
]


def api(method, path, body=None, timeout=20):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def get_row0():
    _, r = api("GET", "/manga/storyboard/default")
    rows = (r.get("data") or {}).get("rows") or []
    return rows[0] if rows else None


def main():
    results = []

    # ---------- setup：重置 default 分镜表 2 行种子 ----------
    _, r = api("PUT", "/manga/storyboard/default", {"rows": SEED_ROWS})
    assert (r.get("data") or {}).get("total") == 2, "种子行写入失败"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # ============================== COMIC-069 ==============================
        ctx = browser.new_context(viewport=VIEWPORT)
        page = ctx.new_page()
        page.set_default_timeout(15000)
        try:
            page.goto(BASE + "/#/storyboard", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector(".st-table tbody tr", timeout=20000)
            page.wait_for_timeout(1000)

            # 1) 点击镜号选中第 1 行 → 右侧面板出现语速/音量滑块
            page.locator(".st-table tbody tr td.st-col-shot_number").nth(0).click()
            page.wait_for_timeout(800)
            speed = page.locator("#rp-row-speed")
            volume = page.locator("#rp-row-volume")
            if speed.count() == 0 or volume.count() == 0:
                results.append({
                    "tc_id": "TC-FLOW-COMIC-069", "name": "语速/音量滑块控制验证",
                    "status": "FAIL",
                    "detail": "选中分镜行后右侧面板未出现 #rp-row-speed / #rp-row-volume 滑块"})
            else:
                # 2) 语速：focus + ArrowRight（步进 0.05 → 1.05），keyup 触发提交
                speed.focus()
                page.keyboard.press("ArrowRight")  # keydown 改值 → keyup 提交
                page.wait_for_timeout(1200)
                row = get_row0()
                spd = row.get("speed") if row else None

                # 3) 音量：ArrowLeft（步进 0.5 → -0.5）
                volume.focus()
                page.keyboard.press("ArrowLeft")
                page.wait_for_timeout(1200)
                row = get_row0()
                vol = row.get("volume") if row else None

                # 4) 边界：越界值须被后端 40008 拒绝
                rid = row["id"] if row else ""
                _, r1 = api("PUT", f"/manga/storyboard/default/rows/{rid}", {"speed": 3.0})
                _, r2 = api("PUT", f"/manga/storyboard/default/rows/{rid}", {"volume": -20})
                c1 = (r1.get("error") or {}).get("code") or r1.get("code")
                c2 = (r2.get("error") or {}).get("code") or r2.get("code")
                bound_ok = (c1 in (40008, "SYSTEM_PARAM_INVALID")
                            and c2 in (40008, "SYSTEM_PARAM_INVALID"))

                ui_ok = abs((spd or 0) - 1.05) < 1e-6 and abs((vol or 0) - (-0.5)) < 1e-6
                # 还原现场
                api("PUT", f"/manga/storyboard/default/rows/{rid}", {"speed": 1.0, "volume": 0.0})

                if ui_ok and bound_ok:
                    results.append({
                        "tc_id": "TC-FLOW-COMIC-069", "name": "语速/音量滑块控制验证",
                        "status": "PASS",
                        "detail": ("选中行后右侧面板渲染语速/音量滑块；ArrowRight 语速 1.0→1.05、"
                                   "ArrowLeft 音量 0.0→-0.5，keyup 提交 PUT 行更新并经 API 回读确认落库；"
                                   "越界 speed=3.0 / volume=-20 均被后端 40008 拒绝")})
                else:
                    results.append({
                        "tc_id": "TC-FLOW-COMIC-069", "name": "语速/音量滑块控制验证",
                        "status": "FAIL",
                        "detail": (f"滑块步进后 API 回读 speed={spd}（期望 1.05）、volume={vol}（期望 -0.5）；"
                                   f"越界校验 code={c1}/{c2}（期望 SYSTEM_PARAM_INVALID）")})
        except Exception as e:
            results.append({
                "tc_id": "TC-FLOW-COMIC-069", "name": "语速/音量滑块控制验证",
                "status": "FAIL", "detail": f"脚本执行异常：{type(e).__name__}: {str(e)[:150]}"})
        ctx.close()

        # ============================== SYS-020 ==============================
        ctx = browser.new_context(viewport=VIEWPORT)
        page = ctx.new_page()
        page.set_default_timeout(15000)
        try:
            page.goto(BASE + "/#/chat", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector("aside.right-panel", timeout=15000)
            page.wait_for_timeout(800)

            panel = page.locator("aside.right-panel")
            grip = page.locator(".right-panel-resize-grip")
            if grip.count() == 0:
                results.append({
                    "tc_id": "TC-FLOW-SYS-020", "name": "右侧面板拖拽调整宽度验证",
                    "status": "FAIL", "detail": "未渲染 .right-panel-resize-grip 拖拽手柄"})
            else:
                w0 = panel.bounding_box()["width"]
                gb = grip.bounding_box()
                cx = gb["x"] + gb["width"] / 2
                cy = gb["y"] + gb["height"] * 0.85  # 避开 50% 处折叠按钮
                page.mouse.move(cx, cy)
                page.mouse.down()
                page.mouse.move(cx - 200, cy, steps=12)  # 左拖 200px → 增宽
                page.mouse.up()
                page.wait_for_timeout(500)
                w1 = panel.bounding_box()["width"]
                stored = page.evaluate(
                    "localStorage.getItem('omnispace.layout.rightPanelWidth')")

                # 刷新后宽度应保持（持久化生效）
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector("aside.right-panel", timeout=15000)
                page.wait_for_timeout(800)
                w2 = page.locator("aside.right-panel").bounding_box()["width"]

                drag_ok = abs(w1 - w0) > 5
                persist_ok = stored is not None and abs(float(stored) - w1) < 2
                reload_ok = abs(w2 - w1) < 2
                if drag_ok and persist_ok and reload_ok:
                    results.append({
                        "tc_id": "TC-FLOW-SYS-020", "name": "右侧面板拖拽调整宽度验证",
                        "status": "PASS",
                        "detail": (f"拖拽左缘手柄面板宽度 {w0:.0f}px→{w1:.0f}px；"
                                   f"localStorage 持久化值={stored}；刷新后宽度保持 {w2:.0f}px")})
                else:
                    results.append({
                        "tc_id": "TC-FLOW-SYS-020", "name": "右侧面板拖拽调整宽度验证",
                        "status": "FAIL",
                        "detail": (f"拖拽 {w0:.0f}→{w1:.0f}px（变化={'是' if drag_ok else '否'}），"
                                   f"localStorage={stored!r}，刷新后 {w2:.0f}px")})
        except Exception as e:
            results.append({
                "tc_id": "TC-FLOW-SYS-020", "name": "右侧面板拖拽调整宽度验证",
                "status": "FAIL", "detail": f"脚本执行异常：{type(e).__name__}: {str(e)[:150]}"})
        ctx.close()

        browser.close()

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write("[\n" + ",\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n]\n")

    for r in results:
        print(f"[{r['status']}] {r['tc_id']} {r['name']} —— {r['detail']}")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    print(f"\n共 {len(results)} 条：PASS={sum(1 for r in results if r['status'] == 'PASS')} FAIL={failed}")
    print(f"结果已写入 {OUT_FILE}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
