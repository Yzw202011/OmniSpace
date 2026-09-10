"""
OmniSpace AI —— UI 冒烟测试（漫剧创作页 #/storyboard 分镜管理 18 条）
----------------------------------------------------------------
运行：
    e:\\OmniSpace\\runtime\\py310\\python.exe e:\\OmniSpace\\tests\\ui_batch_comic_a.py
前置：
    后端已运行于 http://127.0.0.1:5800 （FastAPI 同源 serve 前端 dist，Hash 路由 SPA）
输出：
    e:\\OmniSpace\\tests\\ui_results\\comic_a.json
判定原则：
    PASS=真实观察到交互生效；FAIL=入口存在但行为错误；SKIP=UI 无入口（detail 注明"前端未实现"）
setup 幂等：
    1) 项目「UI冒烟项目A」先查 list 再建（template=comic_drama 预置 5 行模板分镜）；
    2) 前端 MangaPage 固定挂接 default 项目（无项目选择 UI），故经
       PUT /manga/storyboard/default 全量重置 5 行确定性种子数据，保证可重复运行。
"""
import json
import os
import traceback
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5800"
API = BASE + "/api/v1"
URL = BASE + "/#/storyboard"
OUT_FILE = r"e:\OmniSpace\tests\ui_results\comic_a.json"
VIEWPORT = {"width": 1600, "height": 900}
PROJECT_NAME = "UI冒烟项目A"
UNIQUE_MARK = "唯一标记RP"  # 种子行 1 描述内的唯一标记，用于验证属性面板联动

# ---------------------------------------------------------------- 种子分镜行
SEED_ROWS = [
    {"original_dialogue": "台词：又下雨了", "description": f"主角在天台相遇-{UNIQUE_MARK}",
     "characters": ["角色A"], "scene": "天台", "props": ["雨伞"]},
    {"original_dialogue": "台词：你来了", "description": "角色B 撑伞走近",
     "characters": ["角色B"], "scene": "天台", "props": ["雨伞"]},
    {"original_dialogue": "台词：真相是……", "description": "两人对峙，冲突爆发",
     "characters": ["角色A", "角色B"], "scene": "天台", "props": []},
    {"original_dialogue": "台词：原来如此", "description": "闪回回忆画面",
     "characters": ["角色A"], "scene": "回忆", "props": ["照片"]},
    {"original_dialogue": "台词：再见", "description": "雨停，两人分别",
     "characters": ["角色B"], "scene": "天台", "props": []},
]


# ---------------------------------------------------------------- API 工具
def api(method, path, body=None, timeout=20):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def setup_data():
    """幂等 setup：建项目（先查再建）+ 重置 default 分镜表 5 行种子。"""
    info = {"project_id": "", "seed_rows": 0}
    lst = api("GET", "/comic/project/list")
    items = (lst.get("data") or {}).get("items") or []
    hit = next((p for p in items if p.get("name") == PROJECT_NAME), None)
    if hit:
        info["project_id"] = hit["project_id"]
    else:
        r = api("POST", "/comic/project/create",
                {"name": PROJECT_NAME, "template": "comic_drama",
                 "project_type": "comic"})
        info["project_id"] = (r.get("data") or {}).get("project_id", "")
    rows = [dict(r) for r in SEED_ROWS]
    r = api("PUT", "/manga/storyboard/default", {"rows": rows})
    info["seed_rows"] = (r.get("data") or {}).get("total", 0)
    return info


# ---------------------------------------------------------------- 页面工具
class UI:
    def __init__(self, context, page):
        self.context = context
        self.page = page

    # ---- 基础 ----
    def goto(self):
        self.page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        self.page.wait_for_selector(".st-table tbody tr", timeout=20000)
        self.page.wait_for_timeout(800)

    def rows(self):
        return self.page.locator(".st-table tbody tr").count()

    def wait_rows(self):
        self.page.wait_for_selector(".st-table tbody tr", timeout=10000)

    def toasts_clear(self, timeout=7000):
        try:
            self.page.wait_for_selector(".toast", state="detached", timeout=timeout)
        except Exception:
            pass

    def wait_toast(self, text, timeout=12000):
        """等待含指定文本的 toast 出现，返回其文本；超时返回 None。"""
        try:
            loc = self.page.locator(".toast", has_text=text).first
            loc.wait_for(state="visible", timeout=timeout)
            return loc.inner_text()
        except Exception:
            return None

    def switch_tab(self, label, wait_ms=1000):
        self.page.locator("div.inline-flex button", has_text=label).first.click()
        self.page.wait_for_timeout(wait_ms)

    def click_save(self):
        self.page.locator('.st-toolbar-right button:has-text("保存")').click()
        self.wait_toast("已保存")
        self.wait_rows()
        self.page.wait_for_timeout(400)

    def add_row(self):
        self.page.locator('.st-toolbar button:has-text("添加行")').click()
        self.page.wait_for_timeout(150)

    def row_checkbox(self, idx):
        return self.page.locator(".st-table tbody tr").nth(idx).locator(
            "td.st-col-select input[type=checkbox]")

    def open_split_modal(self):
        self.page.locator('.st-toolbar button:has-text("AI 自动分镜")').click()
        self.page.wait_for_selector("div.fixed.inset-0 textarea", timeout=8000)

    def do_auto_split(self, script, expect_added):
        self.open_split_modal()
        self.page.locator("div.fixed.inset-0 textarea").fill(script)
        # COMIC-014 两段式：生成预览 → 确认导入
        self.page.locator('div.fixed.inset-0 button:has-text("生成预览")').click()
        self.page.locator(
            f'div.fixed.inset-0 button:has-text("确认导入 {expect_added} 行")').click()
        t = self.wait_toast(f"新增 {expect_added} 行", timeout=20000)
        self.wait_rows()
        self.page.wait_for_timeout(500)
        return t

    def close_overlays(self):
        """防级联：关闭可能残留的自动分镜弹窗 / 行右键菜单（各用例异常后由主循环调用）。"""
        try:
            m = self.page.locator("div.fixed.inset-0")
            if m.count() > 0 and m.first.is_visible():
                btn = m.first.locator('button:has-text("取消")')
                if btn.count() > 0:
                    btn.first.click()
                    self.page.wait_for_timeout(300)
        except Exception:
            pass
        try:
            self.page.evaluate("window.dispatchEvent(new MouseEvent('click'))")
        except Exception:
            pass


# ---------------------------------------------------------------- 用例
def tc_020(ui):
    """选中分镜卡片流程验证（P0）—— 复选框高亮 + 镜号点击联动右侧面板"""
    # a) 复选框 → 行高亮（批量选择语义）
    before = ui.page.locator("tr.st-row-selected").count()
    ui.row_checkbox(0).check()
    ui.page.wait_for_timeout(400)
    selected = ui.page.locator("tr.st-row-selected").count()
    ui.row_checkbox(0).uncheck()
    ui.page.wait_for_timeout(200)
    ok_select = selected == before + 1
    # b) 镜号点击 → 行级选中，右侧「选中分镜」面板联动（COMIC-020 修复语义）
    ui.page.locator(".st-table tbody tr td.st-col-shot_number").nth(0).click()
    ui.page.wait_for_timeout(500)
    mark_cnt = ui.page.locator(f"text={UNIQUE_MARK}").count()
    panel_txt = ui.page.locator("aside.right-panel").inner_text()
    linked = "选中分镜" in panel_txt and mark_cnt >= 2
    # 还原：再次点击镜号取消选中
    ui.page.locator(".st-table tbody tr td.st-col-shot_number").nth(0).click()
    ui.page.wait_for_timeout(300)
    if ok_select and linked:
        return ("PASS", f"复选框选中行获得 st-row-selected 高亮；点击镜号后右侧属性面板联动显示该行"
                        f"（唯一标记全页出现 {mark_cnt} 次，含面板 1 次）")
    return ("FAIL", f"复选框高亮={ok_select}（{before}→{selected}），镜号点击后面板联动={linked}"
                    f"（标记文本 {mark_cnt} 次）")


def tc_021(ui):
    """右键菜单操作流程验证（P1）—— 行右键弹出菜单，复制/删除闭环"""
    n0 = ui.rows()
    ui.page.locator(".st-table tbody tr").nth(0).click(button="right")
    ui.page.wait_for_timeout(500)
    menu = ui.page.locator(".st-context-menu")
    if menu.count() == 0:
        return ("FAIL", "行右键后未出现 .st-context-menu 自定义菜单")
    items = menu.locator("button").all_inner_texts()
    # 闭环验证：复制该行 → 行数 +1；再右键复制行 → 删除该行 → 行数还原
    menu.locator("button", has_text="复制该行").click()
    ui.page.wait_for_timeout(400)
    n1 = ui.rows()
    ui.page.locator(".st-table tbody tr").nth(1).click(button="right")
    ui.page.wait_for_timeout(400)
    ui.page.locator(".st-context-menu button", has_text="删除该行").click()
    ui.page.wait_for_timeout(400)
    n2 = ui.rows()
    if n1 == n0 + 1 and n2 == n0:
        return ("PASS", f"行右键弹出菜单（{len(items)} 项：{'/'.join(t.strip() for t in items)}）；"
                        f"复制该行 {n0}→{n1}，再右键删除该行还原 {n1}→{n2}，菜单链路真实生效")
    return ("FAIL", f"菜单项={items}；复制 {n0}→{n1}（期望+1），删除后 {n2}（期望 {n0}）")


def tc_022(ui):
    """多选分镜操作流程验证（P1）"""
    # Ctrl+click 行：源码无该交互，验证不产生多选
    ui.page.locator(".st-table tbody tr td.st-col-shot_number").nth(0).click(modifiers=["Control"])
    ui.page.locator(".st-table tbody tr td.st-col-shot_number").nth(1).click(modifiers=["Control"])
    ui.page.wait_for_timeout(300)
    ctrl_selected = ui.page.locator("tr.st-row-selected").count()
    # 复选框多选 + 批量删除
    n0 = ui.rows()
    ui.row_checkbox(0).check()
    ui.row_checkbox(1).check()
    ui.page.wait_for_timeout(300)
    selected = ui.page.locator("tr.st-row-selected").count()
    del_btn = ui.page.locator('.st-toolbar button:has-text("删除选中")')
    enabled = del_btn.is_enabled()
    del_btn.click()
    ui.page.wait_for_timeout(400)
    n1 = ui.rows()
    ui.click_save()  # 持久化，保证后续用例基数确定
    if ctrl_selected == 0 and selected == 2 and enabled and n1 == n0 - 2:
        return ("PASS", f"复选框多选 2 行高亮生效，「删除选中」批量入口可用（{n0}→{n1} 行）；"
                        "Ctrl+点击不产生多选（该交互未实现，多选经复选框提供）")
    return ("FAIL", f"Ctrl选={ctrl_selected}，复选={selected}，删除按钮可用={enabled}，行数 {n0}→{n1}")


def tc_023(ui):
    """分镜表搜索功能验证（P2）—— 关键词过滤行显示，清空恢复"""
    sb = ui.page.locator(".st-search")
    if sb.count() == 0:
        return ("SKIP", "前端未实现：分镜表工具栏无搜索框")
    n0 = ui.rows()
    sb.first.fill("不存在的关键词XYZ")
    ui.page.wait_for_timeout(400)
    n_miss = ui.rows()
    sb.first.fill("台词")  # 所有种子行台词均含「台词」
    ui.page.wait_for_timeout(400)
    n_hit = ui.rows()
    sb.first.fill("")
    ui.page.wait_for_timeout(400)
    n_back = ui.rows()
    if n_miss == 0 and n_hit == n0 and n_back == n0:
        return ("PASS", f"搜索框过滤生效：无匹配关键词 {n0}→0 行，「台词」命中全部 {n0} 行，"
                        f"清空后恢复 {n_back} 行（仅过滤显示，镜号保持原始编号）")
    return ("FAIL", f"过滤异常：无匹配={n_miss}（期望0），「台词」={n_hit}（期望{n0}），清空={n_back}")


def tc_012(ui):
    """剧本导入取消与重试验证（P2）"""
    n0 = ui.rows()
    ui.open_split_modal()
    ui.page.locator("div.fixed.inset-0 textarea").fill("取消流程验证台词")
    ui.page.locator('div.fixed.inset-0 button:has-text("取消")').click()
    ui.page.wait_for_timeout(500)
    closed = ui.page.locator("div.fixed.inset-0 textarea").count() == 0
    n1 = ui.rows()
    # 重试：再次打开
    ui.open_split_modal()
    reopened = ui.page.locator("div.fixed.inset-0 textarea").is_visible()
    ui.page.locator('div.fixed.inset-0 button:has-text("取消")').click()
    ui.page.wait_for_timeout(400)
    if closed and reopened and n1 == n0:
        return ("PASS", f"弹窗取消按钮可关闭导入弹窗且未产生行变更（{n0}→{n1}），再次打开正常；"
                        "注：导出页「导入剧本」区无独立取消按钮，取消能力由自动分镜弹窗提供")
    return ("FAIL", f"取消后弹窗关闭={closed}，重新打开={reopened}，行数 {n0}→{n1}")


def tc_014(ui):
    """自动分镜结果预览与确认验证（P0）—— 生成预览 → 预览列表 → 确认导入"""
    n0 = ui.rows()
    try:
        ui.open_split_modal()
        ui.page.locator("div.fixed.inset-0 textarea").fill("冒烟剧本第一行\n冒烟剧本第二行")
        ui.page.locator('div.fixed.inset-0 button:has-text("生成预览")').click()
        # 预览中间态：标题「预览确认」+ 逐行列表 + 确认导入按钮
        ui.page.wait_for_selector('div.fixed.inset-0 button:has-text("确认导入 2 行")',
                                  timeout=8000)
        preview_items = ui.page.locator("div.fixed.inset-0 li").count()
        confirm = ui.page.locator('div.fixed.inset-0 button:has-text("确认导入 2 行")')
        confirm.click()
        t = ui.wait_toast("新增 2 行", timeout=20000)
        ui.wait_rows()
        ui.page.wait_for_timeout(500)
        n1 = ui.rows()
        if preview_items == 2 and t and n1 == n0 + 2:
            return ("PASS", f"提交剧本后先生成预览（{preview_items} 行本地预览），点击「确认导入 2 行」"
                            f"后 toast「{t.strip()}」入表（{n0}→{n1} 行），预览确认环节真实存在")
        return ("FAIL", f"预览行数={preview_items}（期望2），toast={t}，行数 {n0}→{n1}")
    finally:
        ui.close_overlays()


def tc_015(ui):
    """自动分镜/手动分镜模式切换验证（P0）"""
    n = ui.page.locator("button:has-text('手动'), button:has-text('模式'), "
                        "[role='switch'], .st-toolbar [role='tablist']").count()
    if n == 0:
        return ("SKIP", "前端未实现：分镜表无自动/手动分镜模式切换控件"
                        "（添加行为手动、AI 自动分镜为弹窗，两者并列无模式开关）")
    return ("FAIL", f"发现疑似模式切换控件 {n} 个，未验证切换行为")


def tc_017(ui):
    """添加分镜四种方式验证（P1）"""
    evidence = []
    n0 = ui.rows()
    # 方式1：末尾添加行
    ui.add_row()
    n1 = ui.rows()
    evidence.append(f"末尾「+ 添加行」{n0}→{n1}")
    ok1 = n1 == n0 + 1
    ui.click_save()
    # 方式2：行内 📋 复制行
    m0 = ui.rows()
    ui.page.locator(".st-table tbody tr").nth(0).locator('button[title="复制行"]').click()
    ui.page.wait_for_timeout(400)
    m1 = ui.rows()
    evidence.append(f"行内📋复制 {m0}→{m1}")
    ok2 = m1 == m0 + 1
    ui.click_save()
    # 方式3：AI 自动分镜
    k0 = ui.rows()
    t = ui.do_auto_split("方式三剧本甲\n方式三剧本乙", 2)
    k1 = ui.rows()
    evidence.append(f"AI自动分镜 {k0}→{k1}")
    ok3 = (t is not None) and k1 == k0 + 2
    # 方式4：导出页「导入剧本」
    ui.toasts_clear()
    ui.switch_tab("导出")
    j0 = k1
    ui.page.wait_for_selector("textarea[placeholder='粘贴剧本文本…']", timeout=8000)
    ui.page.locator("textarea[placeholder='粘贴剧本文本…']").fill("方式四导入的一行剧本")
    ui.page.locator('button:has-text("导入剧本")').click()
    t4 = ui.wait_toast("剧本导入完成", timeout=20000)
    ui.toasts_clear()
    ui.switch_tab("分镜表")
    ui.wait_rows()
    j1 = ui.rows()
    evidence.append(f"剧本导入 {j0}→{j1}")
    ok4 = (t4 is not None) and j1 == j0 + 1
    if ok1 and ok2 and ok3 and ok4:
        return ("PASS", "；".join(evidence) + "；共 4 个添加入口全部生效（无指定位置「插入」入口）")
    return ("FAIL", "；".join(evidence) + f"；各项生效={ok1},{ok2},{ok3},{ok4}")


def tc_060(ui):
    """角色@提及功能验证（P1）"""
    cell = ui.page.locator(".st-table tbody tr").nth(0).locator("td.st-col-description")
    cell.click()
    ui.page.wait_for_selector(".st-edit-input", timeout=5000)
    ui.page.locator(".st-edit-input").fill("@")
    ui.page.wait_for_timeout(600)
    popup = ui.page.locator("[class*='mention'], [role='listbox'], [class*='autocomplete']").count()
    ui.page.keyboard.press("Escape")  # 取消编辑，不写库
    ui.page.wait_for_timeout(300)
    if popup == 0:
        return ("SKIP", "前端未实现：描述输入框输入 @ 无角色提及弹窗"
                        "（单元格编辑为纯文本 input，无 mention/listbox 组件）")
    return ("PASS", f"输入 @ 后弹出提及列表（{popup} 个候选元素）")


def tc_066(ui):
    """描述词模板选择与复用验证（P2）—— 选中行后套用模板，描述列真实填入"""
    # 前置：刷新页面保证 selectedRowId=null（tc_022 的 Ctrl+点击镜号会遗留行级选中，
    # 全局 store 不随表格重挂载复位，会静默吞掉「未选中」warning 分支）
    ui.page.reload(wait_until="domcontentloaded")
    ui.page.wait_for_selector(".st-table tbody tr", timeout=15000)
    ui.page.wait_for_timeout(800)
    sel = ui.page.locator("select.st-template-select")
    if sel.count() == 0:
        return ("SKIP", "前端未实现：分镜表无描述词模板下拉（描述列为纯文本编辑）")
    # a) 未选中行直接套用 → 如实 warning 提示（不静默写入）
    sel.first.select_option(label="环境空镜")
    t_warn = ui.wait_toast("请先点击镜号选中", timeout=4000)
    # b) 点击镜号选中第 1 行 → 套用「情感特写」→ 描述单元格真实填入模板文本
    ui.page.locator(".st-table tbody tr td.st-col-shot_number").nth(0).click()
    ui.page.wait_for_timeout(400)
    sel.first.select_option(label="情感特写")
    ui.page.wait_for_timeout(400)
    cell_txt = ui.page.locator(
        ".st-table tbody tr").nth(0).locator("td.st-col-description").inner_text()
    filled = "面部特写" in cell_txt
    # 还原：取消行选中（模板仅改本地未保存态，不写库，不影响后续用例行数基数）
    ui.page.locator(".st-table tbody tr td.st-col-shot_number").nth(0).click()
    ui.page.wait_for_timeout(300)
    if t_warn and filled:
        return ("PASS", f"未选中行套用模板时 toast 如实提示「{t_warn.strip()}」；选中第 1 行后套用"
                        f"「情感特写」，描述单元格真实填入「{cell_txt.strip()[:24]}…」，可继续编辑")
    return ("FAIL", f"未选中提示={t_warn!r}，选中套用后描述单元格={cell_txt!r}（期望含「面部特写」）")


def tc_007(ui):
    """DSL语法示例复制验证（P2）—— 复制按钮写入真实剪贴板内容（context 已授剪贴板权限）"""
    ui.switch_tab("导出")
    ui.page.wait_for_selector("textarea[placeholder='粘贴剧本文本…']", timeout=8000)
    pre = ui.page.locator("pre", has_text="shot:").count()
    btn = ui.page.locator("button:has-text('复制示例')")
    if pre == 0 and btn.count() == 0:
        ui.switch_tab("分镜表")
        ui.wait_rows()
        return ("SKIP", "前端未实现：剧本导入区无 DSL 语法示例与复制按钮"
                        "（导入区仅 textarea + 导入按钮；全前端源码亦无 DSL 字样）")
    btn.first.click()
    t = ui.wait_toast("已复制", timeout=5000)
    clip = ui.page.evaluate("navigator.clipboard.readText()")
    ui.switch_tab("分镜表")
    ui.wait_rows()
    ok_clip = isinstance(clip, str) and clip.startswith("shot: 清晨的教室") and clip.count("\n") == 2
    if t and ok_clip:
        return ("PASS", f"导出页 DSL 语法示例区真实存在，点击「复制示例」toast「{t.strip()}」，"
                        f"剪贴板回读 3 行 shot: 示例（首行「{clip.splitlines()[0][:18]}…」）")
    return ("FAIL", f"toast={t!r}，剪贴板内容={clip!r}（期望 3 行 shot: 开头示例）")


def tc_038(ui):
    """多视图用户交互流程验证（预览弹窗）（P0）"""
    hits = 0
    for tab in ("分镜表", "3D导演台"):
        ui.switch_tab(tab, wait_ms=2500 if tab == "3D导演台" else 800)
        hits += ui.page.locator("button:has-text('预览'), button:has-text('四视图'), "
                                "button:has-text('多视图'), [class*='preview-modal']").count()
    ui.switch_tab("分镜表")
    ui.wait_rows()
    if hits == 0:
        return ("SKIP", "前端未实现：分镜表与 3D 导演台均无角色/场景资产多视图（四视图）预览弹窗入口")
    return ("FAIL", f"发现疑似预览入口 {hits} 个，未验证打开/切换/关闭")


def tc_056(ui):
    """音频时长>分镜时长异常（P2）"""
    header = ui.page.locator(".st-table th", has_text="时长").count()
    ui.switch_tab("时间线")
    hint = ui.page.locator("text=暂无时长字段").count()
    dur_input = ui.page.locator("input[placeholder*='时长'], input[type='number']").count()
    ui.switch_tab("分镜表")
    ui.wait_rows()
    if header == 0 and dur_input == 0:
        evi = "（时间线页如实标注「分镜行暂无时长字段」）" if hint else ""
        return ("SKIP", f"前端未实现：无音频/分镜时长设置入口，UI 无法构造时长冲突场景{evi}")
    return ("FAIL", f"时长表头={header}，时长输入={dur_input}，未验证告警")


def tc_045(ui):
    """角色音色绑定入口与卡片区域验证（P0）"""
    ui.switch_tab("音色绑定", wait_ms=2000)
    char_btn = ui.page.locator("button", has_text="角色A").first
    if char_btn.count() == 0:
        ui.switch_tab("分镜表")
        return ("FAIL", "音色绑定页左栏无角色卡片（角色列表为空）")
    char_btn.click()
    ui.page.wait_for_timeout(500)
    cls = char_btn.get_attribute("class") or ""
    active = "border-[var(--color-primary)]" in cls
    bind_btn = ui.page.locator("div.space-y-2 button").filter(has_text="绑定").first
    enabled = bind_btn.is_enabled() if bind_btn.count() else False
    bind_btn.click()
    t = ui.wait_toast("已绑定到", timeout=15000)
    ui.page.wait_for_timeout(600)
    card_txt = char_btn.inner_text()
    ui.toasts_clear()
    if active and enabled and t and "已绑定：" in card_txt:
        return ("PASS", f"角色卡片点击后高亮（active 边框），右侧音色卡「绑定」按钮可用，"
                        f"点击后 toast「{t.strip()}」，角色卡状态变为「已绑定：」")
    ui.switch_tab("分镜表")
    return ("FAIL", f"卡片高亮={active}，绑定按钮可用={enabled}，toast={t}，卡片文本={card_txt!r}")


def tc_052(ui):
    """音色异常处理——角色未绑定音色（P2）"""
    # 当前已在音色绑定页（tc_045 后）
    card_b = ui.page.locator("button", has_text="角色B").first
    unbound = card_b.count() > 0 and "未绑定音色" in card_b.inner_text()
    synth_entry = ui.page.locator("button:has-text('合成'), button:has-text('生成语音')").count()
    if unbound and synth_entry == 0:
        return ("SKIP", "前端未实现：角色「角色B」显示「未绑定音色」，但页面无角色级语音合成/试听触发入口"
                        "（试听按钮作用于音色本身、无需绑定角色），无法构造未绑定触发场景")
    if synth_entry > 0:
        return ("FAIL", f"存在合成入口 {synth_entry} 个，未验证未绑定错误提示")
    return ("FAIL", f"角色B 未绑定状态确认失败（unbound={unbound}）")


def tc_069(ui):
    """语速/音量滑块控制验证（P1）—— 「选中分镜」面板滑块步进提交落库 + 越界 40008"""
    ui.switch_tab("分镜表")
    ui.wait_rows()
    ui.page.locator(".st-table tbody tr td.st-col-shot_number").nth(0).click()
    ui.page.wait_for_timeout(600)
    speed = ui.page.locator("#rp-row-speed")
    volume = ui.page.locator("#rp-row-volume")
    if speed.count() == 0 or volume.count() == 0:
        return ("FAIL", "选中分镜行后右侧「选中分镜」面板未出现 #rp-row-speed/#rp-row-volume 滑块")

    def get_row0():
        r = api("GET", "/manga/storyboard/default")
        rows = (r.get("data") or {}).get("rows") or []
        return rows[0] if rows else None

    def put_row(rid, patch):
        """返回 (ok, error_code)；后端错误为 200 信封（success:false + error.code），
        故成功路径同样解析 body 取码；HTTP 4xx 兜底解析。"""
        req = urllib.request.Request(
            API + f"/manga/storyboard/default/rows/{rid}", method="PUT",
            data=json.dumps(patch).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = json.loads(e.read().decode("utf-8"))
        code = (body.get("error") or {}).get("code") or body.get("code")
        return bool(body.get("success")), code

    # 语速 ArrowRight 步进 0.05（1.0→1.05），keyup 提交 PUT；API 回读确认落库
    speed.focus()
    ui.page.keyboard.press("ArrowRight")
    ui.page.wait_for_timeout(1200)
    row = get_row0()
    spd = row.get("speed") if row else None
    # 音量 ArrowLeft 步进 0.5（0.0→-0.5）
    volume.focus()
    ui.page.keyboard.press("ArrowLeft")
    ui.page.wait_for_timeout(1200)
    row = get_row0()
    vol = row.get("volume") if row else None
    rid = row["id"] if row else ""
    # 越界：speed=3.0 / volume=-20 均须被后端 SYSTEM_PARAM_INVALID(40008) 拒绝
    _, c1 = put_row(rid, {"speed": 3.0})
    _, c2 = put_row(rid, {"volume": -20})
    bound_ok = (c1 in (40008, "SYSTEM_PARAM_INVALID") and c2 in (40008, "SYSTEM_PARAM_INVALID"))
    ui_ok = (spd is not None and abs(spd - 1.05) < 1e-6
             and vol is not None and abs(vol - (-0.5)) < 1e-6)
    # 还原：落库值复位 + 取消行选中
    put_row(rid, {"speed": 1.0, "volume": 0.0})
    ui.page.locator(".st-table tbody tr td.st-col-shot_number").nth(0).click()
    ui.page.wait_for_timeout(300)
    if ui_ok and bound_ok:
        return ("PASS", "选中行后右侧面板渲染语速/音量滑块；ArrowRight 语速 1.0→1.05、"
                        "ArrowLeft 音量 0.0→-0.5，keyup 提交 PUT 行更新并经 API 回读确认落库；"
                        "越界 speed=3.0 / volume=-20 均被后端 40008 拒绝")
    return ("FAIL", f"滑块步进后回读 speed={spd}（期望1.05）、volume={vol}（期望-0.5）；"
                    f"越界校验 code={c1}/{c2}（期望 SYSTEM_PARAM_INVALID）")


def tc_064(ui):
    """AI描述生成超时异常处理（P2）"""
    n = ui.page.locator("button:has-text('描述生成'), button:has-text('生成描述'), "
                        "button:has-text('AI 描述')").count()
    if n == 0:
        return ("SKIP", "前端未实现：无 AI 描述生成按钮（仅 AI 自动分镜弹窗）；源码层面 api.ts "
                        "timeout 默认 0（不超时）且无该链路的超时降级提示 UI，无法真实触发")
    return ("FAIL", f"发现描述生成按钮 {n} 个，未验证超时处理")


def tc_065(ui):
    """自动保存失败异常处理（P1）"""
    def fail_save(route):
        if route.request.method == "PUT":
            route.fulfill(status=500, content_type="application/json",
                          body=json.dumps({"success": False,
                                           "error": {"code": "SYS_MOCK", "message": "模拟保存失败：服务器500"},
                                           "data": None}))
        else:
            route.continue_()

    ui.page.route("**/api/v1/manga/storyboard/default", fail_save)
    toast_txt = None
    try:
        for _ in range(20):  # 自动保存阈值=20 次操作（源码 AUTOSAVE_OP_THRESHOLD）
            ui.add_row()
        try:
            loc = ui.page.locator(".toast.error").first
            loc.wait_for(state="visible", timeout=10000)
            toast_txt = loc.inner_text()
        except Exception:
            toast_txt = None
    finally:
        ui.page.unroute("**/api/v1/manga/storyboard/default")
    # 清理：API 重置种子，保持可重复运行
    api("PUT", "/manga/storyboard/default", {"rows": [dict(r) for r in SEED_ROWS]})
    if toast_txt and "模拟保存失败" in toast_txt:
        return ("PASS", f"page.route 拦截保存 API 返回 500，连续 20 次「添加行」触发阈值自动保存，"
                        f"观察到错误 toast：「{toast_txt.strip()}」（自动保存失败提示链路真实生效）")
    return ("FAIL", f"20 次操作后未观察到错误 toast（toast={toast_txt!r}）")


# ---------------------------------------------------------------- 主流程
CANONICAL = [
    ("TC-FLOW-COMIC-007", "DSL语法示例复制验证", tc_007),
    ("TC-FLOW-COMIC-012", "剧本导入取消与重试验证", tc_012),
    ("TC-FLOW-COMIC-014", "自动分镜结果预览与确认验证", tc_014),
    ("TC-FLOW-COMIC-015", "自动分镜/手动分镜模式切换验证", tc_015),
    ("TC-FLOW-COMIC-017", "添加分镜四种方式验证", tc_017),
    ("TC-FLOW-COMIC-020", "选中分镜卡片流程验证", tc_020),
    ("TC-FLOW-COMIC-021", "右键菜单操作流程验证", tc_021),
    ("TC-FLOW-COMIC-022", "多选分镜操作流程验证", tc_022),
    ("TC-FLOW-COMIC-023", "分镜表搜索功能验证", tc_023),
    ("TC-FLOW-COMIC-038", "多视图用户交互流程验证（预览弹窗）", tc_038),
    ("TC-FLOW-COMIC-045", "角色音色绑定入口与卡片区域验证", tc_045),
    ("TC-FLOW-COMIC-052", "音色异常处理——角色未绑定音色", tc_052),
    ("TC-FLOW-COMIC-056", "音色异常处理——音频时长>分镜时长", tc_056),
    ("TC-FLOW-COMIC-060", "角色@提及功能验证", tc_060),
    ("TC-FLOW-COMIC-064", "AI描述生成超时异常处理", tc_064),
    ("TC-FLOW-COMIC-065", "自动保存失败异常处理", tc_065),
    ("TC-FLOW-COMIC-066", "描述词模板选择与复用验证", tc_066),
    ("TC-FLOW-COMIC-069", "语速/音量滑块控制验证", tc_069),
]

# 执行顺序（状态连续：先无破坏性用例，再增删行用例，音色组连续，065 收尾并复位数据）
EXEC_ORDER = [
    "TC-FLOW-COMIC-020", "TC-FLOW-COMIC-021", "TC-FLOW-COMIC-022", "TC-FLOW-COMIC-023",
    "TC-FLOW-COMIC-012", "TC-FLOW-COMIC-014", "TC-FLOW-COMIC-015", "TC-FLOW-COMIC-017",
    "TC-FLOW-COMIC-060", "TC-FLOW-COMIC-066", "TC-FLOW-COMIC-007", "TC-FLOW-COMIC-038",
    "TC-FLOW-COMIC-056", "TC-FLOW-COMIC-045", "TC-FLOW-COMIC-052", "TC-FLOW-COMIC-069",
    "TC-FLOW-COMIC-064", "TC-FLOW-COMIC-065",
]


def main():
    print("[setup] 幂等准备测试数据 …")
    info = setup_data()
    print(f"[setup] 项目「{PROJECT_NAME}」project_id={info['project_id']}，"
          f"default 分镜表已重置为 {info['seed_rows']} 行种子")

    results = {}
    fn_map = {tid: fn for tid, _, fn in CANONICAL}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport=VIEWPORT,
            permissions=["clipboard-read", "clipboard-write"],
        )
        page = context.new_page()
        page.set_default_timeout(15000)
        ui = UI(context, page)
        ui.goto()
        print(f"[setup] 页面加载完成，当前分镜行数={ui.rows()}")

        for tid in EXEC_ORDER:
            fn = fn_map[tid]
            try:
                status, detail = fn(ui)
            except Exception as exc:  # noqa: BLE001
                status, detail = "FAIL", f"执行异常：{exc.__class__.__name__}: {exc}"
                traceback.print_exc()
            results[tid] = {"status": status, "detail": detail}
            print(f"  [{status:4s}] {tid}  {detail[:60]}")

        context.close()
        browser.close()

    out = [{"tc_id": tid, "name": name,
            "status": results[tid]["status"], "detail": results[tid]["detail"]}
           for tid, name, _ in CANONICAL]
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    stat = {}
    for r in out:
        stat[r["status"]] = stat.get(r["status"], 0) + 1
    print(f"\n[done] 共 {len(out)} 条：{stat} → {OUT_FILE}")


if __name__ == "__main__":
    main()
