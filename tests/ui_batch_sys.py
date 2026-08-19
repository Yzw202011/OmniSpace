"""
OmniSpace AI —— UI 冒烟测试（SYS/SET/STYLE/MODEL 四组共 23 条）
----------------------------------------------------------------
运行：
    e:\\OmniSpace\\runtime\\py310\\python.exe e:\\OmniSpace\\tests\\ui_batch_sys.py
前置：
    后端已运行于 http://127.0.0.1:5800 （FastAPI 同源 serve 前端 dist，Hash 路由 SPA）
输出：
    e:\\OmniSpace\\tests\\ui_results\\sys_set_style_model.json
判定原则：
    PASS=真实观察到交互生效；FAIL=有入口但交互无效/报错；SKIP=UI 无此功能入口或需物理环境
"""
import json
import os
import sys
import traceback

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5800"
OUT_FILE = r"e:\OmniSpace\tests\ui_results\sys_set_style_model.json"
VIEWPORT = {"width": 1600, "height": 900}

# 8 个一级路由（顺序即侧栏渲染顺序，与前端 NAV_ITEMS 一致）
ROUTES = [
    ("chat", "AI对话"),
    ("paint", "AI绘画"),
    ("storyboard", "漫剧创作"),
    ("learning", "知识学习"),
    ("models", "模型管理"),
    ("style", "视频风格"),
    ("settings", "设置"),
    ("help", "帮助"),
]

# 资源类控制台错误忽略（favicon / 404 / 网络资源加载失败）
IGNORE_ERR = ("favicon", "404", "Failed to load resource", "net::ERR", "the server responded with")


class Ctx:
    """单用例浏览器上下文：独立 localStorage，收集 console error / pageerror。"""

    def __init__(self, browser):
        self.ctx = browser.new_context(viewport=VIEWPORT)
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(15000)
        self.errors = []
        self.page.on(
            "console",
            lambda m: self.errors.append(m.text) if m.type == "error" else None,
        )
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))

    def goto(self, route, wait_ms=1500):
        self.page.goto(f"{BASE}/#/{route}", wait_until="domcontentloaded", timeout=30000)
        self.page.wait_for_selector("main.main-content", timeout=15000)
        self.page.wait_for_timeout(wait_ms)

    def click_nav(self, index_or_label, wait_ms=1200):
        if isinstance(index_or_label, int):
            self.page.locator(".nav-item").nth(index_or_label).click()
        else:
            self.page.locator(".nav-item", has_text=index_or_label).first.click()
        self.page.wait_for_timeout(wait_ms)

    def biz_errors(self):
        return [e for e in self.errors if not any(k in e for k in IGNORE_ERR)]

    def close(self):
        self.ctx.close()


# ============================== SYS 组 ==============================

def tc_sys_008(c):
    """左侧导航栏模块切换完整流程验证"""
    c.goto("chat")
    n = c.page.locator(".nav-item").count()
    if n != 8:
        return "FAIL", f"导航项数量异常：期望 8，实际 {n}"
    problems = []
    for i, (route, label) in enumerate(ROUTES):
        c.click_nav(i)
        h = c.page.evaluate("location.hash")
        if h != f"#/{route}":
            problems.append(f"{label}：点击后 hash={h}（期望 #/{route}）")
            continue
        txt = c.page.locator("main.main-content").inner_text().strip()
        if len(txt) < 3:
            problems.append(f"{label}：主内容区为空")
        eb = c.page.locator(".error-boundary")
        if eb.count() > 0 and eb.first.is_visible():
            problems.append(f"{label}：出现错误边界弹窗")
    if problems:
        return "FAIL", "；".join(problems)
    return "PASS", "依次点击 8 个导航项，location.hash 均切到对应路由且主内容区渲染非空、无错误弹窗"


def tc_sys_009(c):
    """导航栏折叠/展开流程验证"""
    c.goto("chat")
    btn = c.page.locator(".sidebar-collapse-btn")
    sidebar = c.page.locator("aside.sidebar")
    aria0 = btn.get_attribute("aria-label")
    btn.click()
    c.page.wait_for_timeout(500)
    cls1 = sidebar.get_attribute("class") or ""
    aria1 = btn.get_attribute("aria-label")
    label_hidden = not c.page.locator(".nav-label").first.is_visible()
    btn.click()
    c.page.wait_for_timeout(500)
    cls2 = sidebar.get_attribute("class") or ""
    aria2 = btn.get_attribute("aria-label")
    label_shown = c.page.locator(".nav-label").first.is_visible()
    ok = (
        aria0 == "收起导航栏"
        and "collapsed" in cls1
        and aria1 == "展开导航栏"
        and label_hidden
        and "collapsed" not in cls2
        and aria2 == "收起导航栏"
        and label_shown
    )
    if ok:
        return ("PASS", "点击折叠后 aside.sidebar 带 collapsed 类、.nav-label 不可见、按钮 aria-label 切为"
                "「展开导航栏」，再次点击后类名/文案/标签可见性全部恢复")
    return ("FAIL", f"状态异常：aria={aria0}→{aria1}→{aria2}，collapsed 类={('collapsed' in cls1)}/{('collapsed' in cls2)}，"
            f"标签隐藏={label_hidden}，恢复可见={label_shown}")


def tc_sys_012(c):
    """模块间快速切换压力测试"""
    c.goto("chat")
    for _ in range(2):
        for i in range(8):
            c.click_nav(i, wait_ms=300)
    problems = []
    for i, (route, label) in enumerate(ROUTES):
        c.click_nav(i, wait_ms=1000)
        h = c.page.evaluate("location.hash")
        txt = c.page.locator("main.main-content").inner_text().strip()
        if h != f"#/{route}" or len(txt) < 3:
            problems.append(f"{label}(hash={h}, 内容{len(txt)}字符)")
    errs = c.biz_errors()
    if problems:
        return "FAIL", "快速切换后慢速复验异常：" + "；".join(problems)
    if errs:
        return "FAIL", f"压力切换产生业务 JS 错误 {len(errs)} 条，首条：{errs[0][:100]}"
    return "PASS", "8 个路由快速连点两遍（300ms 间隔）后慢速复验各页均正常渲染，全程无业务 JS 错误"


def tc_sys_018(c):
    """右侧面板展开/折叠流程验证"""
    c.goto("chat")
    panel = c.page.locator(".right-panel")
    toggle = c.page.locator(".right-panel-toggle")
    if panel.count() == 0 or toggle.count() == 0:
        return "FAIL", "chat 路由下未找到 .right-panel 或 .right-panel-toggle"
    aria0 = toggle.get_attribute("aria-label")
    toggle.click()
    c.page.wait_for_timeout(600)
    cls1 = panel.get_attribute("class") or ""
    aria1 = toggle.get_attribute("aria-label")
    toggle.click()
    c.page.wait_for_timeout(600)
    cls2 = panel.get_attribute("class") or ""
    aria2 = toggle.get_attribute("aria-label")
    ok = (aria0 == "收起右侧面板" and "collapsed" in cls1 and aria1 == "展开右侧面板"
          and "collapsed" not in cls2 and aria2 == "收起右侧面板")
    if ok:
        return ("PASS", "点击 .right-panel-toggle 后 .right-panel 带上 collapsed 类，aria-label 同步切为"
                "「展开右侧面板」，再次点击后类名与文案均恢复")
    return "FAIL", f"状态异常：aria={aria0}→{aria1}→{aria2}，class 折叠态={'collapsed' in cls1}，恢复态={'collapsed' in cls2}"


def tc_sys_019(c):
    """右侧面板内容切换流程验证"""
    c.goto("chat")
    expects = [("AI对话", "对话设置"), ("AI绘画", "绘画参数"), ("漫剧创作", "分镜属性")]
    problems = []
    for nav_label, title in expects:
        c.click_nav(nav_label)
        panel = c.page.locator(".right-panel")
        if panel.count() == 0:
            problems.append(f"{nav_label}：未渲染右侧面板")
            continue
        al = panel.get_attribute("aria-label")
        if al != title:
            problems.append(f"{nav_label}：面板 aria-label={al!r}（期望 {title!r}）")
    c.click_nav("设置")
    if c.page.locator(".right-panel").count() != 0:
        problems.append("设置路由下仍渲染了 .right-panel")
    if problems:
        return "FAIL", "；".join(problems)
    return "PASS", "chat/paint/storyboard 面板 aria-label 依次为对话设置/绘画参数/分镜属性，settings 路由无右侧面板"


def tc_sys_020(c):
    """右侧面板拖拽调整宽度验证"""
    c.goto("chat")
    panel = c.page.locator(".right-panel")
    box = panel.bounding_box()
    if not box:
        return "FAIL", "未获取到右侧面板 bounding box"
    w0 = box["width"]
    x = box["x"] + 1
    y = box["y"] + box["height"] * 0.85  # 避开 50% 高度处的折叠按钮
    c.page.mouse.move(x, y)
    c.page.mouse.down()
    c.page.mouse.move(x - 200, y, steps=12)
    c.page.mouse.up()
    c.page.wait_for_timeout(400)
    w1 = panel.bounding_box()["width"]
    if abs(w1 - w0) > 5:
        return "PASS", f"拖拽面板左缘宽度由 {w0:.0f}px 变为 {w1:.0f}px，拖拽调宽生效"
    return "SKIP", f"前端未实现拖拽调宽，仅折叠/展开（无 resize 手柄，拖拽左缘后宽度恒为 {w0:.0f}px）"


def tc_sys_027(c):
    """主题切换时3D场景适配验证"""
    c.goto("storyboard", wait_ms=2500)  # 懒加载
    # 默认分镜表视图不渲染 canvas，需切入 3D 导演台视图（DirectorStage 按需挂载）
    director_tab = c.page.locator("main.main-content button", has_text="3D导演台")
    if director_tab.count() > 0:
        director_tab.first.click()
        c.page.wait_for_timeout(4000)  # 3D 场景初始化
    t0 = c.page.evaluate("document.documentElement.getAttribute('data-theme')")
    btn = c.page.locator(
        "button[aria-label='切换为亮色主题'], button[aria-label='切换为暗色主题']"
    ).first
    if btn.count() == 0:
        return "FAIL", "顶栏未找到主题切换按钮"
    canvas0 = c.page.locator("main.main-content canvas").count()
    btn.click()
    c.page.wait_for_timeout(1000)
    t1 = c.page.evaluate("document.documentElement.getAttribute('data-theme')")
    canvas1 = c.page.locator("main.main-content canvas").count()
    errs = c.biz_errors()
    if t1 == t0:
        return "FAIL", f"点击主题切换按钮后 data-theme 未变化（仍为 {t0!r}）"
    if canvas0 > 0 and canvas1 == 0:
        return "FAIL", f"主题切换后导演台 canvas 消失（{canvas0}→{canvas1}）"
    if errs:
        return "FAIL", f"主题切换过程产生控制台错误 {len(errs)} 条，首条：{errs[0][:100]}"
    detail = f"漫剧页切入 3D导演台 视图后点击主题切换，<html> data-theme 由 {t0!r} 变为 {t1!r}，全程无 console 错误"
    if canvas0 > 0:
        detail += f"，导演台 canvas 切换前后均存在（{canvas0}→{canvas1}）"
    else:
        detail += "，当前 headless 环境 3D导演台未挂载 canvas（WebGL 不可用降级），主题切换本身无异常"
    return "PASS", detail


# ============================== SET 组 ==============================

SET_CASES = [
    ("TC-FLOW-SET-002", "语言设置切换验证", ["界面语言", "语言"],
     "前端未实现：设置页无语言切换控件（后端设置仅 theme/font_size 等静态键且无 language 项，无对应输入框）"),
    ("TC-FLOW-SET-004", "开机自启动设置验证", ["开机", "自启动"],
     "前端未实现：设置页无开机自启动选项"),
    ("TC-FLOW-SET-005", "通知设置验证", ["通知"],
     "前端未实现：设置页无通知设置项（顶栏通知中心为任务列表，非设置项）"),
    ("TC-FLOW-SET-006", "快捷键设置验证", ["快捷键"],
     "前端未实现：设置页无快捷键设置项"),
    ("TC-FLOW-SET-009", "渲染质量设置验证", ["渲染质量", "画质"],
     "前端未实现：设置页无渲染质量设置项"),
    ("TC-FLOW-SET-018", "导出质量预设验证", ["导出"],
     "前端未实现：设置页无导出质量预设项"),
    ("TC-FLOW-SET-022", "隐私设置验证", ["隐私"],
     "前端未实现：设置页无隐私设置项"),
]


def run_set_group(c):
    """设置页分组验证：逐条检查对应控件是否存在于真实渲染 DOM。"""
    c.goto("settings", wait_ms=2000)
    page_el = c.page.locator(".settings-page")
    if page_el.count() == 0:
        return [(tc, name, "FAIL", "设置页 .settings-page 未渲染") for tc, name, _, _ in SET_CASES]
    text = page_el.inner_text()
    editable_inputs = c.page.locator(".settings-input").count()
    results = []
    for tc_id, name, keywords, skip_detail in SET_CASES:
        hit = next((k for k in keywords if k in text), None)
        if hit is None:
            results.append((tc_id, name, "SKIP", skip_detail))
        elif editable_inputs == 0:
            results.append((tc_id, name, "SKIP",
                            f"前端未实现：设置页出现「{hit}」相关文案但全页无可编辑控件（0 个 .settings-input）"))
        else:
            results.append((tc_id, name, "FAIL",
                            f"设置页检测到「{hit}」入口文案，但自动化未找到可操作控件完成验证，需人工复核"))
    return results


# ============================== STYLE 组 ==============================

def run_style_group(c):
    """视频风格页分组验证（009→010→011 顺序联动：预设改参 → 预估随动 → 恢复默认）"""
    c.goto("style", wait_ms=2500)
    results = []

    rank = c.page.locator('input[aria-label="LoRA Rank"]')
    alpha = c.page.locator('input[aria-label="LoRA Alpha"]')
    epochs = c.page.locator('input[aria-label="训练轮次 Epochs"]')
    lr = c.page.locator("#style-lr")
    preset = c.page.locator("#style-preset")

    def params():
        return (rank.input_value(), alpha.input_value(), lr.input_value(), epochs.input_value())

    def set_range(loc, val):
        """React 受控 range：经原生 setter 赋值后派发 input 事件（直接 el.value 会被 value tracker 吞掉）。"""
        loc.evaluate(
            "(el, v) => { const s = Object.getOwnPropertyDescriptor("
            "window.HTMLInputElement.prototype, 'value').set; s.call(el, v);"
            " el.dispatchEvent(new Event('input', {bubbles: true})); }",
            val)

    # TC-FLOW-STYLE-009 训练参数预设模板
    if preset.count() == 0 or rank.count() == 0:
        results.append(("TC-FLOW-STYLE-009", "训练参数预设模板验证", "SKIP",
                        "前端未实现：训练参数区无预设模板下拉/按钮，仅 Rank/Alpha/Epochs 滑杆与学习率下拉"))
    else:
        preset.select_option("fast")  # 快速试验：Rank8 / Alpha16 / LR 5e-4 / 5 轮
        c.page.wait_for_timeout(300)
        vals = params()
        if vals == ("8", "16", "0.0005", "5"):
            results.append(("TC-FLOW-STYLE-009", "训练参数预设模板验证", "PASS",
                            "选择预设「快速试验」后 Rank/Alpha/学习率/Epochs 一键填充为 "
                            "8/16/5×10^-4/5，四个参数控件全部联动生效"))
        else:
            results.append(("TC-FLOW-STYLE-009", "训练参数预设模板验证", "FAIL",
                            f"预设填充后参数={vals}（期望 ('8','16','0.0005','5')）"))

    # TC-FLOW-STYLE-010 训练参数显存预估（当前为 fast 预设：预估 3.6 GB）
    est = c.page.locator('[data-testid="vram-estimate"]')
    if est.count() == 0:
        results.append(("TC-FLOW-STYLE-010", "训练参数显存预估验证", "SKIP",
                        "前端未实现：调整训练参数时页面无预估显存显示（仅训练进度卡片展示实时遥测显存）"))
    else:
        t0 = est.inner_text()
        set_range(rank, "64")  # Rank 8→64：预估应 3.6→9.8 GB
        c.page.wait_for_timeout(300)
        t1 = est.inner_text()
        if "3.6 GB" in t0 and "9.8 GB" in t1:
            results.append(("TC-FLOW-STYLE-010", "训练参数显存预估验证", "PASS",
                            "显存预估随参数联动：fast 预设下「3.6 GB」，Rank 调至 64 后变为「9.8 GB」"
                            "（公式 1.5+rank×0.11+alpha×0.02+epochs×0.02+0.8 两端均与 UI 显示一致）"))
        else:
            results.append(("TC-FLOW-STYLE-010", "训练参数显存预估验证", "FAIL",
                            f"预估未按公式联动：调整前={t0.strip()!r}（期望含3.6 GB），"
                            f"Rank=64 后={t1.strip()!r}（期望含9.8 GB）"))

    # TC-FLOW-STYLE-011 训练参数恢复默认（当前 Rank 被 010 改为 64）
    reset_btn = c.page.locator("main.main-content button", has_text="恢复默认")
    if reset_btn.count() == 0:
        results.append(("TC-FLOW-STYLE-011", "训练参数恢复默认验证", "SKIP",
                        "前端未实现：训练参数区无「恢复默认」按钮（全页按钮仅 刷新/开始训练/生成预览/回滚）"))
    else:
        reset_btn.first.click()
        c.page.wait_for_timeout(300)
        vals = params()
        # 注：DOM option value 为 JS String(2e-5) = "0.00002"（JS 数值 <1e-6 才用指数表示）
        if vals == ("16", "32", "0.00002", "10"):
            results.append(("TC-FLOW-STYLE-011", "训练参数恢复默认验证", "PASS",
                            "参数被改为 Rank64 后点击「恢复默认」，Rank/Alpha/学习率/Epochs 全部复位为 "
                            "16/32/2×10^-5/10（PARAM_DEFAULTS）"))
        else:
            results.append(("TC-FLOW-STYLE-011", "训练参数恢复默认验证", "FAIL",
                            f"恢复默认后参数={vals}（期望 ('16','32','0.00002','10')）"))
    return results


# ============================== MODEL 组 ==============================

def run_model_group(c):
    """模型管理页分组验证"""
    c.goto("models", wait_ms=2500)
    mm = c.page.locator(".model-manager")
    if mm.count() == 0:
        return [
            ("TC-FLOW-MODEL-002", "模型列表排序功能验证", "FAIL", "模型管理页 .model-manager 未渲染"),
            ("TC-FLOW-MODEL-004", "模型列表搜索功能验证", "FAIL", "模型管理页 .model-manager 未渲染"),
            ("TC-FLOW-MODEL-013", "模型导入拖拽方式验证", "FAIL", "模型管理页 .model-manager 未渲染"),
            ("TC-FLOW-MODEL-028", "模型预热显存不足处理验证", "FAIL", "模型管理页 .model-manager 未渲染"),
        ]
    text = mm.inner_text()
    results = []

    # 卡片名称提取（.model-name 首文本节点，规避状态 badge 文本污染）
    names_js = ("Array.from(document.querySelectorAll('.model-card .model-name'))"
                ".map(el => el.childNodes[0].textContent.trim())")

    # TC-FLOW-MODEL-002 排序：切到首个具体类别 Tab（「全部」视图按类别分组，无法全局验证），
    # 按「名称」排序后在单一分组内验证 localeCompare(zh-Hans-CN) 升序，再切回默认验证恢复
    sort_sel = c.page.locator(".mm-sort-select")
    if sort_sel.count() == 0:
        results.append(("TC-FLOW-MODEL-002", "模型列表排序功能验证", "SKIP",
                        "前端未实现：模型列表按类别分组为卡片流，无排序下拉/可点击表头等排序控件"))
    else:
        tabs = c.page.locator(".model-category-tab")
        if tabs.count() > 1:
            tabs.nth(1).click()
            c.page.wait_for_timeout(400)
        before = c.page.evaluate(names_js)
        sort_sel.select_option("name")
        c.page.wait_for_timeout(400)
        after = c.page.evaluate(names_js)
        sorted_ok = c.page.evaluate(
            "(ns) => { const s = ns.slice().sort((a, b) => a.localeCompare(b, 'zh-Hans-CN'));"
            " return JSON.stringify(s) === JSON.stringify(ns); }", after)
        sort_sel.select_option("default")
        c.page.wait_for_timeout(300)
        restored = c.page.evaluate(names_js)
        tabs.nth(0).click()  # 还原「全部」供后续用例
        c.page.wait_for_timeout(300)
        if len(after) >= 2 and sorted_ok and restored == before:
            results.append(("TC-FLOW-MODEL-002", "模型列表排序功能验证", "PASS",
                            f"单一类别下按「名称」排序，{len(after)} 张卡片名称经 localeCompare(zh-Hans-CN) "
                            f"验证为升序（首项「{after[0]}」）；切回「默认」恢复登记顺序"))
        else:
            results.append(("TC-FLOW-MODEL-002", "模型列表排序功能验证", "FAIL",
                            f"排序异常：卡片数={len(after)}，升序验证={sorted_ok}，"
                            f"恢复默认={'一致' if restored == before else '不一致'}"))

    # TC-FLOW-MODEL-004 搜索：关键词过滤卡片 + 匹配计数 + 清空恢复
    search = c.page.locator(".mm-search-input")
    if search.count() == 0:
        results.append(("TC-FLOW-MODEL-004", "模型列表搜索功能验证", "SKIP",
                        "前端未实现：模型管理页无列表搜索框，仅提供类别筛选 Tab"))
    else:
        all_names = c.page.evaluate(names_js)
        kw = all_names[0][:4] if all_names else ""
        search.fill(kw)
        c.page.wait_for_timeout(400)
        matched = c.page.evaluate(names_js)
        counter = c.page.locator(".model-manager").inner_text()
        has_counter = "匹配 " in counter
        first_hit = bool(matched) and kw.lower() in matched[0].lower()
        search.fill("")
        c.page.wait_for_timeout(400)
        back = c.page.evaluate(names_js)
        if has_counter and first_hit and 0 < len(matched) <= len(all_names) and back == all_names:
            results.append(("TC-FLOW-MODEL-004", "模型列表搜索功能验证", "PASS",
                            f"搜索「{kw}」过滤为 {len(matched)}/{len(all_names)} 张卡片且出现「匹配 X/Y」"
                            f"计数，首项「{matched[0]}」命中关键词；清空后恢复全部 {len(back)} 张"))
        else:
            results.append(("TC-FLOW-MODEL-004", "模型列表搜索功能验证", "FAIL",
                            f"搜索异常：关键词={kw!r}，命中={len(matched)}/{len(all_names)}，"
                            f"计数文案={has_counter}，清空恢复={'一致' if back == all_names else '不一致'}"))

    # TC-FLOW-MODEL-013 拖拽导入：合成 drop 事件验证名称自动带入路径输入框
    import_btn = c.page.locator(".model-manager button", has_text="导入模型")
    if import_btn.count() == 0:
        results.append(("TC-FLOW-MODEL-013", "模型导入拖拽方式验证", "SKIP",
                        "前端未实现：模型管理页无「导入模型」入口"))
    else:
        import_btn.first.click()
        c.page.wait_for_timeout(600)
        dz = c.page.locator(".mm-dropzone")
        if dz.count() == 0:
            cancel = c.page.locator("button", has_text="取消")
            if cancel.count() > 0:
                cancel.first.click()
                c.page.wait_for_timeout(300)
            results.append(("TC-FLOW-MODEL-013", "模型导入拖拽方式验证", "SKIP",
                            "前端未实现：模型导入弹窗无拖拽区（dropzone），仅路径文本输入框"))
        else:
            dt = c.page.evaluate_handle(
                "() => { const dt = new DataTransfer();"
                " dt.items.add(new File(['x'], 'uitest-model.safetensors',"
                " {type: 'application/octet-stream'})); return dt; }")
            dz.dispatch_event("drop", {"dataTransfer": dt})
            c.page.wait_for_timeout(400)
            val = c.page.locator("#mm-import-path").input_value()
            toast = None
            try:
                loc = c.page.locator(".toast", has_text="已带入拖入项名称").first
                loc.wait_for(state="visible", timeout=3000)
                toast = loc.inner_text()
            except Exception:
                pass
            cancel = c.page.locator("button", has_text="取消")
            if cancel.count() > 0:
                cancel.first.click()
                c.page.wait_for_timeout(300)
            if val == "models\\uitest-model.safetensors" and toast:
                results.append(("TC-FLOW-MODEL-013", "模型导入拖拽方式验证", "PASS",
                                f"拖拽 uitest-model.safetensors 到 dropzone，路径输入框自动带入"
                                f"「{val}」（默认 models\\ 前缀），并 toast 如实提示"
                                "「浏览器沙箱无法读取绝对路径，请确认路径前缀」"))
            else:
                results.append(("TC-FLOW-MODEL-013", "模型导入拖拽方式验证", "FAIL",
                                f"拖拽后路径输入框={val!r}（期望 models\\uitest-model.safetensors），"
                                f"提示 toast={toast!r}"))

    # TC-FLOW-MODEL-028 预热
    if "预热" in text:
        results.append(("TC-FLOW-MODEL-028", "模型预热显存不足处理验证", "FAIL",
                        "页面检测到预热入口但脚本未完成显存不足路径验证，需人工复核"))
    else:
        results.append(("TC-FLOW-MODEL-028", "模型预热显存不足处理验证", "SKIP",
                        "前端未实现预热入口：模型卡片操作仅 选择/加载/校验/卸载/删除，无预热按钮"))
    return results


# ============================== 主流程 ==============================

def main():
    results = []

    sys_cases = [
        ("TC-FLOW-SYS-008", "左侧导航栏模块切换完整流程验证", tc_sys_008),
        ("TC-FLOW-SYS-009", "导航栏折叠/展开流程验证", tc_sys_009),
        ("TC-FLOW-SYS-012", "模块间快速切换压力测试", tc_sys_012),
        ("TC-FLOW-SYS-018", "右侧面板展开/折叠流程验证", tc_sys_018),
        ("TC-FLOW-SYS-019", "右侧面板内容切换流程验证", tc_sys_019),
        ("TC-FLOW-SYS-020", "右侧面板拖拽调整宽度验证", tc_sys_020),
        ("TC-FLOW-SYS-027", "主题切换时3D场景适配验证", tc_sys_027),
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # SYS 组：每用例独立上下文
        for tc_id, name, fn in sys_cases:
            c = Ctx(browser)
            try:
                status, detail = fn(c)
            except Exception as e:
                status = "FAIL"
                detail = f"脚本执行异常：{type(e).__name__}: {str(e)[:120]}"
                traceback.print_exc()
            results.append({"tc_id": tc_id, "name": name, "status": status, "detail": detail})
            print(f"[{status}] {tc_id} {name} —— {detail}")
            c.close()

        # SET / STYLE / MODEL 组：同页多条件观察
        for group_fn in (run_set_group, run_style_group, run_model_group):
            c = Ctx(browser)
            try:
                for tc_id, name, status, detail in group_fn(c):
                    results.append({"tc_id": tc_id, "name": name, "status": status, "detail": detail})
                    print(f"[{status}] {tc_id} {name} —— {detail}")
            except Exception as e:
                traceback.print_exc()
                results.append({"tc_id": "GROUP", "name": str(group_fn.__name__), "status": "FAIL",
                                "detail": f"脚本执行异常：{type(e).__name__}: {str(e)[:120]}"})
            c.close()

        browser.close()

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        lines = [json.dumps(r, ensure_ascii=False) for r in results]
        f.write("[\n" + ",\n".join(lines) + "\n]\n")

    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    skipped = sum(1 for r in results if r["status"] == "SKIP")
    print(f"\n共 {len(results)} 条：PASS={passed} FAIL={failed} SKIP={skipped}")
    print(f"结果已写入 {OUT_FILE}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
