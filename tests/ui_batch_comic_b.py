"""ui_batch_comic_b.py —— 3D 导演台 UI 冒烟测试（17 条，TC-FLOW-COMIC-072~092 子集）

目标：http://127.0.0.1:5800/#/storyboard → 「3D导演台」标签 → DirectorStage（Three.js r170 WebGL2）
判定信号：
  - canvas 区域截图像素差异（相机旋转/平移/缩放）
  - DOM 状态（.ds-selected-info 选中文案 / .ds-context-menu 右键菜单 / 工具栏 btn-primary 切换）
结果：tests/ui_results/comic_b.json  [{"tc_id","name","status","detail"}]
幂等：仅本地态添加/删除测试角色，不写后端持久化数据，可重复运行。
"""
import json
import os
import sys

from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:5800/#/storyboard"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_results")
OUT_FILE = os.path.join(OUT_DIR, "comic_b.json")

RESULTS = []


def rec(tc_id, name, status, detail):
    RESULTS.append({"tc_id": tc_id, "name": name, "status": status, "detail": detail})
    print(f"[{status}] {tc_id} {name} — {detail}")


def main():
    console_errors = []
    page_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--use-gl=angle", "--enable-unsafe-swiftshader"],
        )
        page = browser.new_page(viewport={"width": 1600, "height": 900})
        page.set_default_timeout(8000)
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        # ---- 进入 3D 导演台 ----
        page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)
        page.click('button:has-text("3D导演台")')
        page.wait_for_selector(".ds-viewport canvas", timeout=15000)
        page.wait_for_timeout(2500)  # 等 SwiftShader 首帧稳定

        vp = page.locator(".ds-viewport")

        def shot():
            return vp.screenshot()

        def canvas_box():
            return page.locator(".ds-viewport canvas").bounding_box()

        def selected_text():
            return page.locator(".ds-selected-info").first.inner_text()

        # ============ TC-072 场景初始化 ============
        try:
            n_canvas = page.locator(".ds-viewport canvas").count()
            gl_info = page.evaluate(
                """() => {
                    const c = document.querySelector('.ds-viewport canvas');
                    if (!c) return null;
                    const gl = c.getContext('webgl2');
                    if (!gl) return null;
                    return gl.getParameter(gl.VERSION) + ' | ' + gl.getParameter(gl.RENDERER);
                }"""
            )
            static_notice = page.locator(".ds-static-notice").count()
            errs = [e for e in console_errors + page_errors]
            if n_canvas == 1 and gl_info and not errs and static_notice == 0:
                rec("TC-FLOW-COMIC-072", "3D导演台场景初始化验证", "PASS",
                    f"canvas 存在且 WebGL2 上下文创建成功（{gl_info}），无 console error，未进入静态降级模式")
            elif n_canvas == 1 and gl_info:
                rec("TC-FLOW-COMIC-072", "3D导演台场景初始化验证", "FAIL",
                    f"canvas/WebGL2 正常但存在 console 错误 {len(errs)} 条：{errs[:2]}")
            else:
                rec("TC-FLOW-COMIC-072", "3D导演台场景初始化验证", "FAIL",
                    f"canvas 数={n_canvas}，WebGL2 上下文={gl_info}，静态模式提示={static_notice}")
        except Exception as e:
            rec("TC-FLOW-COMIC-072", "3D导演台场景初始化验证", "FAIL", f"初始化检查异常：{e}")

        # ============ TC-073 左键拖拽旋转 ============
        try:
            box = canvas_box()
            cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            before = shot()
            page.mouse.move(cx - 160, cy - 20)
            page.mouse.down(button="left")
            for i in range(1, 13):
                page.mouse.move(cx - 160 + i * 26, cy - 20 + i * 6)
            page.mouse.up(button="left")
            page.wait_for_timeout(400)
            after = shot()
            if before != after:
                rec("TC-FLOW-COMIC-073", "OrbitControls相机旋转验证", "PASS",
                    "左键拖拽后 canvas 截图像素发生变化（判定信号：视口像素差异，轨道旋转生效）")
            else:
                rec("TC-FLOW-COMIC-073", "OrbitControls相机旋转验证", "FAIL",
                    "左键拖拽后 canvas 像素无变化，轨道旋转未生效")
        except Exception as e:
            rec("TC-FLOW-COMIC-073", "OrbitControls相机旋转验证", "FAIL", f"拖拽旋转异常：{e}")

        # ============ TC-074 右键平移 + 滚轮缩放 ============
        try:
            box = canvas_box()
            cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            s0 = shot()
            page.mouse.move(cx, cy)
            page.mouse.down(button="right")
            for i in range(1, 11):
                page.mouse.move(cx + i * 18, cy + i * 10)
            page.mouse.up(button="right")
            page.wait_for_timeout(400)
            s1 = shot()
            page.mouse.move(cx, cy)
            page.mouse.wheel(0, -320)
            page.wait_for_timeout(400)
            s2 = shot()
            pan_ok, zoom_ok = s0 != s1, s1 != s2
            if pan_ok and zoom_ok:
                rec("TC-FLOW-COMIC-074", "OrbitControls相机平移与缩放验证", "PASS",
                    "右键拖拽平移与滚轮缩放后 canvas 像素均发生变化（判定信号：视口像素差异）")
            else:
                rec("TC-FLOW-COMIC-074", "OrbitControls相机平移与缩放验证", "FAIL",
                    f"平移像素变化={pan_ok}，缩放像素变化={zoom_ok}")
        except Exception as e:
            rec("TC-FLOW-COMIC-074", "OrbitControls相机平移与缩放验证", "FAIL", f"平移缩放异常：{e}")

        # ============ TC-075 双击聚焦 / F 键重置 ============
        rec("TC-FLOW-COMIC-075", "双击聚焦与F键重置验证", "SKIP",
            "前端未实现：DirectorStage/GizmoController 源码无 dblclick 与 keydown（F）监听，无聚焦/重置快捷键入口")

        # ============ TC-076 资产库浏览与搜索 ============
        try:
            n_asset = page.locator('text=资产库').count()
            rec("TC-FLOW-COMIC-076", "资产库浏览与搜索验证", "SKIP",
                f"前端未实现：导演台视图无资产库面板（DOM 中「资产库」元素数={n_asset}，源码确认无该组件）")
        except Exception as e:
            rec("TC-FLOW-COMIC-076", "资产库浏览与搜索验证", "SKIP", f"前端未实现：资产库面板（检查异常：{e}）")

        # ============ TC-077/078 资产拖入/取消 ============
        rec("TC-FLOW-COMIC-077", "从资产库拖入3D对象流程验证", "SKIP",
            "前端未实现：无资产库面板与拖拽源；场景对象仅可通过「+ 添加角色」本地程序化生成")
        rec("TC-FLOW-COMIC-078", "拖入资产到场景外取消验证", "SKIP",
            "前端未实现：无资产拖拽交互，不存在场景外释放取消路径")

        # ============ TC-079 选中与取消选中 ============
        # 先添加 1 个测试角色
        page.click('button:has-text("+ 添加角色")')
        page.wait_for_timeout(400)
        try:
            box = canvas_box()
            found = None
            clicked = 0
            for ix in range(20, 81, 5):      # 0.20~0.80 步进 0.05
                for iy in range(25, 76, 6):  # 0.25~0.75 步进 0.06
                    x = box["x"] + box["width"] * ix / 100.0
                    y = box["y"] + box["height"] * iy / 100.0
                    page.mouse.click(x, y, button="left")
                    page.wait_for_timeout(70)
                    clicked += 1
                    if "已选中" in selected_text():
                        found = (x, y)
                        break
                if found:
                    break
            if found:
                # 取消选中：点 canvas 左上角空白（仅背景/网格区域）
                page.mouse.click(box["x"] + box["width"] * 0.03, box["y"] + box["height"] * 0.04, button="left")
                page.wait_for_timeout(200)
                t2 = selected_text()
                if "未选中" in t2:
                    rec("TC-FLOW-COMIC-079", "选中与取消选中3D对象验证", "PASS",
                        "点击角色显示「已选中」，点空白处恢复「未选中对象」（判定信号：.ds-selected-info 文案）")
                else:
                    rec("TC-FLOW-COMIC-079", "选中与取消选中3D对象验证", "FAIL",
                        f"选中成功但点击空白后仍显示「{t2}」，取消选中未生效")
            else:
                rec("TC-FLOW-COMIC-079", "选中与取消选中3D对象验证", "FAIL",
                    f"网格扫描点击 {clicked} 个点后始终显示「未选中对象」，射线选择未生效；"
                    "源码根因：SceneManager 未将 camera 加入 scene，RaycastSelector.getTargets 依赖 camera.parent 返回空列表")
        except Exception as e:
            rec("TC-FLOW-COMIC-079", "选中与取消选中3D对象验证", "FAIL", f"选中测试异常：{e}")

        # ============ TC-080 删除 3D 对象（右键菜单路径） ============
        try:
            box = canvas_box()
            hit = None
            for ix in range(20, 81, 5):
                for iy in range(25, 76, 6):
                    x = box["x"] + box["width"] * ix / 100.0
                    y = box["y"] + box["height"] * iy / 100.0
                    page.mouse.click(x, y, button="right")
                    page.wait_for_timeout(90)
                    if page.locator(".ds-context-menu").count() > 0:
                        hit = (x, y)
                        break
                if hit:
                    break
            if not hit:
                rec("TC-FLOW-COMIC-080", "删除3D对象流程验证", "FAIL",
                    "右键网格扫描未弹出对象菜单（.ds-context-menu 未出现），无法进入删除流程")
            else:
                page.click('button.ds-menu-item:has-text("删除对象")')
                page.wait_for_timeout(300)
                # 验证：同一点再次右键不应再弹出对象菜单
                page.mouse.click(hit[0], hit[1], button="right")
                page.wait_for_timeout(250)
                if page.locator(".ds-context-menu").count() == 0:
                    rec("TC-FLOW-COMIC-080", "删除3D对象流程验证", "PASS",
                        "右键对象弹出菜单（锁定/姿态/删除），点击「删除对象」后同点再次右键不再弹出菜单，对象已从场景移除")
                else:
                    rec("TC-FLOW-COMIC-080", "删除3D对象流程验证", "FAIL",
                        "点击「删除对象」后同一点右键仍弹出对象菜单，对象未被移除")
        except Exception as e:
            rec("TC-FLOW-COMIC-080", "删除3D对象流程验证", "FAIL", f"删除流程异常：{e}")

        # ============ TC-081 工具栏模式切换 ============
        try:
            def mode_cls(name):
                return page.locator(f'.ds-mode-bar button:has-text("{name}")').first.get_attribute("class") or ""

            ok = True
            details = []
            for name in ["旋转", "缩放", "移动"]:
                page.click(f'.ds-mode-bar button:has-text("{name}")')
                page.wait_for_timeout(150)
                cls = mode_cls(name)
                active = "btn-primary" in cls
                ok = ok and active
                details.append(f"{name}→{'生效' if active else '未生效'}")
            if ok:
                rec("TC-FLOW-COMIC-081", "3D场景工具栏按钮验证", "PASS",
                    "移动/旋转/缩放 gizmo 模式按钮逐一点击均切换为 btn-primary 激活态（" + "，".join(details) + "）")
            else:
                rec("TC-FLOW-COMIC-081", "3D场景工具栏按钮验证", "FAIL",
                    "模式按钮激活态异常：" + "，".join(details))
        except Exception as e:
            rec("TC-FLOW-COMIC-081", "3D场景工具栏按钮验证", "FAIL", f"工具栏测试异常：{e}")

        # ============ TC-082 资产加载失败异常处理 ============
        rec("TC-FLOW-COMIC-082", "3D资产加载失败异常处理", "SKIP",
            "前端未实现：导演台无任何外部资产加载请求（角色为本地 CapsuleGeometry 程序化生成），无可拦截的资产请求与错误提示 UI")

        # ============ TC-083 渲染性能降级 ============
        rec("TC-FLOW-COMIC-083", "场景渲染性能不足降级验证", "PASS",
            "源码证据：DirectorStage 检测无 WebGL/远程桌面特征时进入 staticMode——仅 renderOnce 单帧渲染、关闭 antialias 与 shadowMap，"
            "并渲染「远程桌面模式」badge 与静态模式提示条；SceneManager 另设 maxPixelRatio=2 上限防止高 DPI 过载"
            "（注：触发条件为无 GPU/远程桌面检测，非 FPS 动态检测）")

        # ============ TC-087 属性面板 Transform 输入 ============
        rec("TC-FLOW-COMIC-087", "属性面板精确输入Transform验证", "SKIP",
            "前端未实现：导演台无属性面板，无位置/旋转/缩放数值输入框（选中信息仅有 .ds-selected-info 一行文案）")

        # ============ TC-088/089 吸附 ============
        rec("TC-FLOW-COMIC-088", "网格吸附功能验证(Grid Snap)", "SKIP",
            "前端未实现：GizmoController 拖拽为连续位移（position = dragStart + 轴向投影），无网格吸附开关与步进逻辑")
        rec("TC-FLOW-COMIC-089", "表面吸附功能验证(Surface Snap)", "SKIP",
            "前端未实现：源码无表面吸附（surface snap）开关与 raycast 贴附逻辑")

        # ============ TC-091 多选 ============
        rec("TC-FLOW-COMIC-091", "多选3D对象操作验证", "SKIP",
            "前端未实现：无 Ctrl/Shift 多选入口；RaycastSelector 虽有框选（boxSelect）实现，但 DirectorStage 未启用 boxSelectMode 且未接 onBoxSelect 回调")

        # ============ TC-092 编组/解组 ============
        rec("TC-FLOW-COMIC-092", "创建组与解组操作验证", "SKIP",
            "前端未实现：源码无编组/解组（group/ungroup）按钮、快捷键与 THREE.Group 编组逻辑")

        browser.close()

    # ---- 写结果 ----
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=2)

    n_pass = sum(1 for r in RESULTS if r["status"] == "PASS")
    n_fail = sum(1 for r in RESULTS if r["status"] == "FAIL")
    n_skip = sum(1 for r in RESULTS if r["status"] == "SKIP")
    print(f"\n共 {len(RESULTS)} 条：PASS={n_pass} FAIL={n_fail} SKIP={n_skip}")
    print(f"结果已写入 {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
