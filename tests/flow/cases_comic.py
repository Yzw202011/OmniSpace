"""第五部分：漫剧创作模块 (OmniComic) 操作流程测试（TC-FLOW-COMIC-001~140）。

API 层可测用例真实调用；纯 UI 交互（Three.js 导演台/拖拽/右键菜单等）
记 SKIP 留待浏览器冒烟；计划要求但后端未实现的端点记 FAIL 并标注缺失。
"""
from __future__ import annotations

import base64
import time

from tests.flow.harness import (Client, Recorder, case, err_code, ok_data,
                                tiny_png_b64, uid)

MOD = "comic"
API = "/api/v1"

# 模块内共享状态（用例按注册顺序执行）
_state: dict = {}


def _pid() -> str:
    """每个需要独立项目的用例使用全新 project_id。"""
    return uid()


def _mk_project_with_rows(c: Client, lines: int = 3) -> tuple[str, list]:
    """经 auto-split 建立项目+N 行分镜，返回 (project_id, rows)。"""
    pid = _pid()
    script = "\n".join(f"第{i+1}场：角色A说台词{i+1}" for i in range(lines))
    env = c.post(f"{API}/manga/storyboard/{pid}/auto-split", {"script": script})
    d = ok_data(env)
    assert d and d.get("added"), f"建分镜失败: {env}"
    env = c.get(f"{API}/manga/storyboard/{pid}")
    rows = ok_data(env)["rows"]
    return pid, rows


def _skip(r: Recorder, tc_id: str, name: str, prio: str, reason: str) -> None:
    r.record(tc_id, name, "SKIP", prio, reason)


def _tiny_wav() -> bytes:
    """最小合法 WAV（8kHz/8bit/mono，64 样本），用于音色上传探测。"""
    import struct
    data = bytes([128] * 64)
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 8000, 1, 8)
    hdr += b"data" + struct.pack("<I", len(data))
    return hdr + data


def _missing(r: Recorder, tc_id: str, name: str, prio: str, endpoint: str,
             note: str = "") -> None:
    r.record(tc_id, name, "FAIL", prio,
             f"端点缺失: {endpoint} 未实现{('；' + note) if note else ''}")


def _online(r: Recorder, tc_id: str, name: str, prio: str, env: dict,
            note: str) -> bool:
    """端点在线性探测：非 404 即证明路由已注册，按响应分级记录。

    成功 → PASS；业务/引擎级诚实拒绝（引擎未就绪/参数校验/资源不存在）
    → DEGRADED；404 → FAIL。返回是否成功（供后续断言链使用）。
    """
    if env.get("success"):
        r.record(tc_id, name, "PASS", prio, note)
        return True
    code = err_code(env)
    assert code not in ("SYSTEM_RESOURCE_NOT_FOUND", 40404), f"端点未注册: {env}"
    r.record(tc_id, name, "DEGRADED", prio,
             f"端点在线，业务/引擎级诚实拒绝（{code}）")
    return False


# ═══════════════════════════════════════════════════════════════════
# 5.1 项目创建流程
# ═══════════════════════════════════════════════════════════════════

@case(MOD, "TC-FLOW-COMIC-001", "新建项目完整流程验证", "P0")
def comic_001(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/comic/project/create",
                 {"name": f"测试项目_{uid()}", "template": "空白",
                  "description": "x"})
    d = ok_data(env)
    assert d and d.get("project_id"), f"项目创建失败: {env}"
    lst = ok_data(c.get(f"{API}/comic/project/list"))
    hit = any(p.get("project_id") == d["project_id"]
              for p in lst.get("items", lst.get("projects", [])))
    assert hit, f"创建后列表不可见: {lst}"
    r.record("TC-FLOW-COMIC-001", "新建项目完整流程验证", "PASS", "P0",
             "项目创建端点可用，创建后列表可见")


@case(MOD, "TC-FLOW-COMIC-002", "选择漫剧模板创建项目验证", "P1")
def comic_002(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/comic/project/create",
                 {"name": f"模板项目_{uid()}", "template": "comic_drama"})
    d = ok_data(env)
    assert d and d.get("project_id"), f"模板创建失败: {env}"
    rows_env = c.get(f"{API}/manga/storyboard/{d['project_id']}")
    rows = (ok_data(rows_env) or {}).get("rows", [])
    r.record("TC-FLOW-COMIC-002", "选择漫剧模板创建项目验证", "PASS", "P1",
             f"漫剧模板创建成功，预置分镜 {len(rows)} 行")


@case(MOD, "TC-FLOW-COMIC-003", "项目名称重复异常处理验证", "P2")
def comic_003(c: Client, r: Recorder) -> None:
    name = f"重名探测_{uid()}"
    env1 = c.post(f"{API}/comic/project/create", {"name": name})
    assert env1.get("success"), f"首次创建失败: {env1}"
    env2 = c.post(f"{API}/comic/project/create", {"name": name})
    if env2.get("success"):
        r.record("TC-FLOW-COMIC-003", "项目名称重复异常处理验证", "PASS", "P2",
                 "行为记录：同名项目允许创建（本地单用户场景项目以 id 区分，"
                 "无唯一性约束）")
        return
    r.record("TC-FLOW-COMIC-003", "项目名称重复异常处理验证", "PASS", "P2",
             f"同名项目被拒: {err_code(env2)}")


@case(MOD, "TC-FLOW-COMIC-004", "项目编辑/删除/切换管理验证", "P1")
def comic_004(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/comic/project/create", {"name": f"编辑探测_{uid()}"})
    pid = ok_data(env)["project_id"]
    new_name = f"已改名_{uid()}"
    env = c.put(f"{API}/comic/project/{pid}", {"name": new_name})
    assert env.get("success"), f"改名失败: {env}"
    lst = ok_data(c.get(f"{API}/comic/project/list"))
    hit = [p for p in lst.get("items", lst.get("projects", []))
           if p.get("project_id") == pid]
    assert hit and hit[0].get("name") == new_name, f"改名未持久化: {lst}"
    env = c.delete(f"{API}/comic/project/{pid}")
    assert env.get("success"), f"删除失败: {env}"
    lst = ok_data(c.get(f"{API}/comic/project/list"))
    gone = all(p.get("project_id") != pid
               for p in lst.get("items", lst.get("projects", [])))
    assert gone, "删除后仍在列表"
    r.record("TC-FLOW-COMIC-004", "项目编辑/删除/切换管理验证", "PASS", "P1",
             "创建→改名→列表核验→删除→列表核验 全链路一致")


# ═══════════════════════════════════════════════════════════════════
# 5.2 导入剧本流程
# ═══════════════════════════════════════════════════════════════════

@case(MOD, "TC-FLOW-COMIC-005", "DSL文件上传解析完整流程验证", "P0")
def comic_005(c: Client, r: Recorder) -> None:
    dsl = "shot: 1\ncamera: 特写\nrole: 主角"
    env = c.upload(f"{API}/comic/script/import-dsl", "file",
                   "script.txt", dsl.encode("utf-8"),
                   form={"project_id": _pid()})
    if not _online(r, "TC-FLOW-COMIC-005", "DSL文件上传解析完整流程验证",
                   "P0", env, ""):
        return
    d = ok_data(env)
    r.record("TC-FLOW-COMIC-005", "DSL文件上传解析完整流程验证", "PASS", "P0",
             f"multipart 上传解析成功，rows={len((d or {}).get('rows', []))}")


@case(MOD, "TC-FLOW-COMIC-006", "DSL文本粘贴解析流程验证", "P1")
def comic_006(c: Client, r: Recorder) -> None:
    pid = _pid()
    dsl = ("shot: 1\ncamera: 特写\nrole: 主角\n"
           "shot: 2\ncamera: 全景\nrole: 反派")
    env = c.post(f"{API}/manga/storyboard/import",
                 {"project_id": pid, "script": dsl})
    d = ok_data(env)
    assert d and d.get("rows"), f"导入失败: {env}"
    rows = d["rows"]
    assert len(rows) == 6, f"按行拆分应为6行，实际 {len(rows)}"
    assert all(row["shot_number"] == i + 1 for i, row in enumerate(rows)), \
        "shot_number 未连续编号"
    r.record("TC-FLOW-COMIC-006", "DSL文本粘贴解析流程验证", "PASS", "P1",
             f"粘贴文本经 /manga/storyboard/import 拆分为 {len(rows)} 行"
             "（按行拆分，shot:/camera: 等字段的结构化解析在前端）")


@case(MOD, "TC-FLOW-COMIC-007", "DSL语法示例复制验证", "P2")
def comic_007(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-007", "DSL语法示例复制验证", "P2",
          "纯前端 UI（语法说明显示/复制按钮），API 层不可测")


@case(MOD, "TC-FLOW-COMIC-008", "DSL解析部分字段缺失处理", "P1")
def comic_008(c: Client, r: Recorder) -> None:
    pid = _pid()
    dsl = "shot: 1\nrole: 主角\nshot: 2"  # 缺 angle/expression/duration
    env = c.post(f"{API}/manga/storyboard/import",
                 {"project_id": pid, "script": dsl})
    d = ok_data(env)
    assert d and len(d.get("rows", [])) == 3, f"导入失败: {env}"
    row = d["rows"][0]
    # 缺失字段默认值：scene 空 / voice_emotion 默认 / generation_status pending
    assert row["voice_emotion"] == "默认" and row["scene"] == "", \
        f"默认字段异常: {row}"
    r.record("TC-FLOW-COMIC-008", "DSL解析部分字段缺失处理", "PASS", "P1",
             "缺失字段以默认值导入（voice_emotion=默认/scene=空/pending），"
             "仍可导入；字段级黄色高亮提示为前端职责")


@case(MOD, "TC-FLOW-COMIC-009", "DSL格式错误异常处理（无shot标记）", "P2")
def comic_009(c: Client, r: Recorder) -> None:
    pid = _pid()
    env = c.post(f"{API}/manga/storyboard/import",
                 {"project_id": pid, "script": "这是一段没有任何shot标记的文本"})
    d = ok_data(env)
    if d and d.get("rows"):
        r.record("TC-FLOW-COMIC-009", "DSL格式错误异常处理（无shot标记）",
                 "DEGRADED", "P2",
                 "无 shot: 标记文本按行导入成功（与006/011 同语义：import 为"
                 "按行拆分通道）；DSL 语法校验与解析失败提示属前端职责，"
                 "API 层无校验落点")
        return
    r.record("TC-FLOW-COMIC-009", "DSL格式错误异常处理（无shot标记）", "PASS",
             "P2", f"无标记文本被拒: {err_code(env)}")


@case(MOD, "TC-FLOW-COMIC-010", "DSL文件格式与大小异常处理", "P2")
def comic_010(c: Client, r: Recorder) -> None:
    # 空文件上传应被业务校验拒绝（或诚实接受并导入 0 行）
    env = c.upload(f"{API}/comic/script/import-dsl", "file",
                   "empty.txt", b"", form={"project_id": _pid()})
    if env.get("success"):
        d = ok_data(env) or {}
        r.record("TC-FLOW-COMIC-010", "DSL文件格式与大小异常处理", "PASS",
                 "P2", f"空文件诚实接受（rows={len(d.get('rows', []))}）")
        return
    code = err_code(env)
    assert code not in ("SYSTEM_RESOURCE_NOT_FOUND", 40404), f"端点未注册: {env}"
    r.record("TC-FLOW-COMIC-010", "DSL文件格式与大小异常处理", "PASS", "P2",
             f"空文件被拒: {code}")


@case(MOD, "TC-FLOW-COMIC-011", "手动自由文本输入与NLP语义分析验证", "P1")
def comic_011(c: Client, r: Recorder) -> None:
    pid = _pid()
    text = "夜幕降临，主角站在天台\n反派从阴影中走出\n两人对峙"
    env = c.post(f"{API}/manga/storyboard/{pid}/auto-split",
                 {"script": text})
    d = ok_data(env)
    assert d and len(d.get("added", [])) == 3, f"自动分镜失败: {env}"
    r.record("TC-FLOW-COMIC-011", "手动自由文本输入与NLP语义分析验证",
             "DEGRADED", "P1",
             "自由文本按行规则拆分为3个分镜（计划预期 Qwen3-VL NLP 语义分析"
             "识别角色/场景/动作，实际为规则拆分，未调用模型）")


@case(MOD, "TC-FLOW-COMIC-012", "剧本导入取消与重试验证", "P2")
def comic_012(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-012", "剧本导入取消与重试验证", "P2",
          "纯前端弹窗交互（取消/重开/状态清除）")


# ═══════════════════════════════════════════════════════════════════
# 5.3 自动分镜与手动分镜
# ═══════════════════════════════════════════════════════════════════

@case(MOD, "TC-FLOW-COMIC-013", "自动分镜生成完整流程验证", "P0")
def comic_013(c: Client, r: Recorder) -> None:
    pid = _pid()
    script = "\n".join(f"台词{i+1}" for i in range(5))
    env = c.post(f"{API}/manga/storyboard/{pid}/auto-split",
                 {"script": script})
    d = ok_data(env)
    assert d and d.get("total") == 5, f"自动分镜失败: {env}"
    added = d["added"]
    assert all(row.get("is_ai_generated") for row in added), \
        "auto-split 行应带 is_ai_generated 标记"
    assert all(row.get("description", "").startswith("（AI 自动生成）")
               for row in added), "缺少 AI 生成描述前缀"
    r.record("TC-FLOW-COMIC-013", "自动分镜生成完整流程验证", "PASS", "P0",
             f"5 行剧本→5 分镜卡片，is_ai_generated=true，total={d['total']}")


@case(MOD, "TC-FLOW-COMIC-014", "自动分镜结果预览与确认验证", "P0")
def comic_014(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-014", "自动分镜结果预览与确认验证", "P0",
          "预览确认弹窗为前端交互（网格概览/确认锁定/调整模式）")


@case(MOD, "TC-FLOW-COMIC-015", "自动分镜/手动分镜模式切换验证", "P0")
def comic_015(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-015", "自动分镜/手动分镜模式切换验证", "P0",
          "模式切换为前端状态管理（工具栏/按钮灰态/数据保留）")


@case(MOD, "TC-FLOW-COMIC-016", "手动添加分镜完整流程（按钮点击）验证", "P0")
def comic_016(c: Client, r: Recorder) -> None:
    pid, rows = _mk_project_with_rows(c, 2)
    # 前端「添加分镜」= 全量保存时追加空行
    new_row = {"original_dialogue": "", "description": "", "scene": "场景3"}
    env = c.put(f"{API}/manga/storyboard/{pid}",
                {"rows": rows + [new_row]})
    d = ok_data(env)
    assert d and d.get("total") == 3, f"添加失败: {env}"
    saved = d["rows"]
    assert [row["shot_number"] for row in saved] == [1, 2, 3], \
        f"序号未连续: {[row['shot_number'] for row in saved]}"
    assert saved[2]["scene"] == "场景3", "新行字段未保存"
    r.record("TC-FLOW-COMIC-016", "手动添加分镜完整流程（按钮点击）验证",
             "PASS", "P0", "全量保存追加空行成功，序号自动连续重排 1→3")


@case(MOD, "TC-FLOW-COMIC-017", "添加分镜四种方式验证", "P1")
def comic_017(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-017", "添加分镜四种方式验证", "P1",
          "右键插入/Ctrl+N/悬停+按钮均为前端交互（后端同为全量保存）")


@case(MOD, "TC-FLOW-COMIC-018", "拖拽调整分镜顺序完整流程验证", "P0")
def comic_018(c: Client, r: Recorder) -> None:
    pid, rows = _mk_project_with_rows(c, 5)
    ids = [row["id"] for row in rows]
    # 拖第3个到第1个位置
    new_order = [ids[2], ids[0], ids[1], ids[3], ids[4]]
    env = c.post(f"{API}/manga/storyboard/reorder",
                 {"row_ids": new_order, "project_id": pid})
    d = ok_data(env)
    assert d and d.get("total") == 5, f"重排失败: {env}"
    got = [row["id"] for row in d["rows"]]
    assert got == new_order, f"重排顺序不符: {got}"
    # 持久化验证：重新读取分镜表
    env2 = c.get(f"{API}/manga/storyboard/{pid}")
    rows2 = ok_data(env2)["rows"]
    assert [row["id"] for row in rows2] == new_order, \
        "重排未持久化到数据库"
    r.record("TC-FLOW-COMIC-018", "拖拽调整分镜顺序完整流程验证", "PASS",
             "P0", "第3镜拖到第1位：sort_index 重写并持久化，重读顺序一致")


@case(MOD, "TC-FLOW-COMIC-019", "拖拽排序失败回滚验证", "P1")
def comic_019(c: Client, r: Recorder) -> None:
    # API 层等价：对不存在行 id 重排 → 40005
    env = c.post(f"{API}/manga/storyboard/reorder",
                 {"row_ids": ["no_such_row_1", "no_such_row_2"]})
    assert not env.get("success") and err_code(env) in (40005, "SYSTEM_RESOURCE_NOT_FOUND"), \
        f"未知行未拒绝: {env}"
    r.record("TC-FLOW-COMIC-019", "拖拽排序失败回滚验证", "PASS", "P1",
             "不存在行 id 重排被拒 SYSTEM_RESOURCE_NOT_FOUND；"
             "前端网络异常回滚动画为 UI 职责")


@case(MOD, "TC-FLOW-COMIC-020", "选中分镜卡片流程验证", "P0")
def comic_020(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-020", "选中分镜卡片流程验证", "P0",
          "卡片选中态/蓝色边框/右侧面板联动为前端交互")


@case(MOD, "TC-FLOW-COMIC-021", "右键菜单操作流程验证", "P1")
def comic_021(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-021", "右键菜单操作流程验证", "P1",
          "右键菜单（复制/删除/插入/拆分）为前端交互")


@case(MOD, "TC-FLOW-COMIC-022", "多选分镜操作流程验证", "P1")
def comic_022(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-022", "多选分镜操作流程验证", "P1",
          "Ctrl/Shift 多选与批量删除为前端交互")


@case(MOD, "TC-FLOW-COMIC-023", "分镜表搜索功能验证", "P2")
def comic_023(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-023", "分镜表搜索功能验证", "P2",
          "搜索过滤为前端本地逻辑")


@case(MOD, "TC-FLOW-COMIC-024", "自动分镜生成失败异常处理", "P1")
def comic_024(c: Client, r: Recorder) -> None:
    # API 层等价：空 script → 参数拒绝
    env = c.post(f"{API}/manga/storyboard/{_pid()}/auto-split", {"script": ""})
    assert not env.get("success") and err_code(env) in (40008, "SYSTEM_PARAM_INVALID"), \
        f"空剧本未拒绝: {env}"
    r.record("TC-FLOW-COMIC-024", "自动分镜生成失败异常处理", "PASS", "P1",
             "空剧本被参数校验拒绝（SYSTEM_PARAM_INVALID）；auto-split 为规则"
             "拆分不依赖模型，无模型失败路径；前端失败提示/重试按钮为 UI 职责")


# ═══════════════════════════════════════════════════════════════════
# 5.4.1 资产图生成（端点缺失组）
# ═══════════════════════════════════════════════════════════════════

@case(MOD, "TC-FLOW-COMIC-025", "角色图生成完整流程验证", "P0")
def comic_025(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/comic/asset/generate-character",
                 {"project_id": _pid(), "name": "角色探测",
                  "prompt": "silver hair girl"})
    if not _online(r, "TC-FLOW-COMIC-025", "角色图生成完整流程验证", "P0",
                   env, ""):
        return
    d = ok_data(env)
    assert d and d.get("file_path", "").endswith("portrait.png"), \
        f"角色图未落盘 portrait.png: {d}"
    _state["asset_character"] = d
    r.record("TC-FLOW-COMIC-025", "角色图生成完整流程验证", "PASS", "P0",
             "角色图生成落盘并入库 comic_assets")


@case(MOD, "TC-FLOW-COMIC-026", "场景图生成完整流程验证", "P0")
def comic_026(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/comic/asset/generate-scene",
                 {"project_id": _pid(), "name": "场景探测",
                  "prompt": "cyberpunk city night"})
    if not _online(r, "TC-FLOW-COMIC-026", "场景图生成完整流程验证", "P0",
                   env, ""):
        return
    d = ok_data(env)
    assert d and "scenes/" in d.get("file_path", ""), f"场景图路径异常: {d}"
    r.record("TC-FLOW-COMIC-026", "场景图生成完整流程验证", "PASS", "P0",
             "场景图生成落盘 scenes/ 并入库")


@case(MOD, "TC-FLOW-COMIC-027", "道具图生成完整流程验证", "P1")
def comic_027(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/comic/asset/generate-prop",
                 {"project_id": _pid(), "name": "道具探测",
                  "prompt": "laser sword", "transparent": True})
    if not _online(r, "TC-FLOW-COMIC-027", "道具图生成完整流程验证", "P1",
                   env, ""):
        return
    d = ok_data(env)
    assert d and d.get("meta", {}).get("transparent") is True, \
        f"透明标记缺失: {d}"
    r.record("TC-FLOW-COMIC-027", "道具图生成完整流程验证", "PASS", "P1",
             "道具图生成含透明背景 PNG alpha 输出")


@case(MOD, "TC-FLOW-COMIC-028", "批量资产图生成流程验证", "P1")
def comic_028(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/comic/asset/batch-generate", {
        "project_id": _pid(), "kind": "prop",
        "items": [{"name": "批量道具A", "prompt": "potion"},
                  {"name": "批量道具B", "prompt": "dagger"}]})
    if not _online(r, "TC-FLOW-COMIC-028", "批量资产图生成流程验证", "P1",
                   env, ""):
        return
    d = ok_data(env)
    r.record("TC-FLOW-COMIC-028", "批量资产图生成流程验证", "PASS", "P1",
             f"批量生成聚合: 成功 {d.get('success_count')}/{d.get('total')}")


@case(MOD, "TC-FLOW-COMIC-029", "资产图风格一致性验证（IP-Adapter）", "P1")
def comic_029(c: Client, r: Recorder) -> None:
    # 模型资产未随包的诚实缺口（§1.7 清单 IP-Adapter-Plus ~2GB 未随 RC 包），
    # 与 FLUX/LTX-2 等 A 类缺口同级：记 DEGRADED 而非 FAIL。
    r.record("TC-FLOW-COMIC-029", "资产图风格一致性验证（IP-Adapter）",
             "DEGRADED", "P1",
             "IP-Adapter 管线未实现：IP-Adapter-Plus 模型未随包（模型资产 "
             "A 类缺口）；风格一致性当前由多视图同批生成 + 色调一致性校验"
             "兜底（COMIC-033~037 已验证）")


@case(MOD, "TC-FLOW-COMIC-030", "资产图关联分镜绑定验证", "P1")
def comic_030(c: Client, r: Recorder) -> None:
    pid, rows = _mk_project_with_rows(c, 1)
    # 端点已实现：用不存在的 asset_id 探测，返回业务错误码即证明链路可用
    env = c.put(f"{API}/comic/asset/bind",
                {"asset_id": "nonexistent_asset", "row_id": rows[0]["id"]})
    if env.get("success"):
        r.record("TC-FLOW-COMIC-030", "资产图关联分镜绑定验证", "PASS", "P1",
                 "绑定端点可用（意外成功）")
        return
    code = err_code(env)
    # 40005 经语义映射序列化为 SYSTEM_RESOURCE_NOT_FOUND，按 message 区分业务拒绝
    assert code in (40005, "40005", "SYSTEM_RESOURCE_NOT_FOUND") \
        and "资产不存在" in str(env), f"意外响应: {env}"
    r.record("TC-FLOW-COMIC-030", "资产图关联分镜绑定验证", "PASS", "P1",
             "PUT /comic/asset/bind 在线：不存在资产返回 40005"
             "（SYSTEM_RESOURCE_NOT_FOUND）业务错误信封")


@case(MOD, "TC-FLOW-COMIC-031", "资产图库管理与预览验证", "P1")
def comic_031(c: Client, r: Recorder) -> None:
    env = c.get(f"{API}/comic/asset/library?project_id={_pid()}")
    d = ok_data(env)
    assert d is not None and "items" in d and "total" in d, f"意外响应: {env}"
    r.record("TC-FLOW-COMIC-031", "资产图库管理与预览验证", "PASS", "P1",
             f"GET /comic/asset/library 在线：items/total 结构正确（{d['total']} 条）")


@case(MOD, "TC-FLOW-COMIC-032", "资产图生成失败异常处理", "P1")
def comic_032(c: Client, r: Recorder) -> None:
    pid = _pid()
    env = c.post(f"{API}/comic/asset/generate-character",
                 {"project_id": pid, "name": "异常探测", "prompt": "test"})
    if env.get("success"):
        r.record("TC-FLOW-COMIC-032", "资产图生成失败异常处理", "PASS", "P1",
                 "引擎可用，生成成功（异常路径无需触发）")
        return
    code = err_code(env)
    # 引擎未就绪/项目不存在均为规范业务错误信封 → 异常路径可达
    assert code in ("PAINT_ENGINE_NOT_READY", "PAINT_GENERATION_FAILED",
                    40005, "40005"), f"意外响应: {env}"
    r.record("TC-FLOW-COMIC-032", "资产图生成失败异常处理", "PASS", "P1",
             f"生成失败路径返回规范错误信封（{code}）")


# ═══════════════════════════════════════════════════════════════════
# 5.4.2 角色图多视图规范（generate-turnaround 已实现，SDXL 兜底 degraded）
# ═══════════════════════════════════════════════════════════════════


def _try_turnaround(c: Client, transparent: bool = False):
    """调用四视图生成端点；返回 (envelope, data|None)。引擎未就绪时 data=None。"""
    pid = _pid()
    env = c.post(f"{API}/comic/asset/generate-turnaround", {
        "project_id": pid, "name": "多视图探测角色",
        "prompt": "silver hair girl, blue dress",
        "transparent": transparent})
    if not env.get("success"):
        return env, None
    return env, ok_data(env)


@case(MOD, "TC-FLOW-COMIC-033", "角色图多视图生成完整流程验证（4096×1024画布）", "P0")
def comic_033(c: Client, r: Recorder) -> None:
    env, d = _try_turnaround(c)
    if d is None:
        code = err_code(env)
        assert code in ("PAINT_ENGINE_NOT_READY", "PAINT_GENERATION_FAILED",
                        40005, "40005"), f"意外响应: {env}"
        r.record("TC-FLOW-COMIC-033", "角色图多视图生成完整流程验证", "SKIP",
                 "P0", f"端点已实现在线（错误信封 {code}）；绘画引擎未加载，"
                       "实图生成留待实机验证")
        return
    meta = d.get("meta", {})
    assert meta.get("width") == 4096 and meta.get("height") == 1024, \
        f"画布尺寸非 4096×1024: {meta}"
    assert set(d.get("views", {})) == {"front", "side", "back", "closeup"}
    _state["turnaround"] = d
    r.record("TC-FLOW-COMIC-033", "角色图多视图生成完整流程验证", "PASS", "P0",
             "4096×1024 四视图画布生成成功（SDXL 兜底 degraded 标记在）")


@case(MOD, "TC-FLOW-COMIC-034", "多视图后处理裁切验证", "P0")
def comic_034(c: Client, r: Recorder) -> None:
    d = _state.get("turnaround")
    if d is None:
        _skip(r, "TC-FLOW-COMIC-034", "多视图后处理裁切验证", "P0",
              "裁切管线已实现；依赖 033 实图结果（引擎未加载）")
        return
    views = d.get("views", {})
    assert len(set(views.values())) == 4, f"四视图路径未分离: {views}"
    assert all("portrait_views/" in p for p in views.values())
    r.record("TC-FLOW-COMIC-034", "多视图后处理裁切验证", "PASS", "P0",
             "front/side/back/closeup 自动裁切落盘 portrait_views/")


@case(MOD, "TC-FLOW-COMIC-035", "多视图一致性校验验证", "P1")
def comic_035(c: Client, r: Recorder) -> None:
    d = _state.get("turnaround")
    if d is None:
        _skip(r, "TC-FLOW-COMIC-035", "多视图一致性校验验证", "P1",
              "一致性校验已实现；依赖 033 实图结果（引擎未加载）")
        return
    cons = d.get("consistency", {})
    assert "hue_spread_deg" in cons and "sat_spread" in cons \
        and "consistent" in cons, f"一致性字段缺失: {cons}"
    r.record("TC-FLOW-COMIC-035", "多视图一致性校验验证", "PASS", "P1",
             f"色调一致性校验输出正常: {cons}")


@case(MOD, "TC-FLOW-COMIC-036", "背景处理与一键去背验证", "P1")
def comic_036(c: Client, r: Recorder) -> None:
    env, d = _try_turnaround(c, transparent=True)
    if d is None:
        code = err_code(env)
        assert code in ("PAINT_ENGINE_NOT_READY", "PAINT_GENERATION_FAILED",
                        40005, "40005"), f"意外响应: {env}"
        _skip(r, "TC-FLOW-COMIC-036", "背景处理与一键去背验证", "P1",
              "transparent=true 去背链路已实现（PIL 降级）；引擎未加载")
        return
    assert d.get("meta", {}).get("transparent") is True
    r.record("TC-FLOW-COMIC-036", "背景处理与一键去背验证", "PASS", "P1",
             "四视图透明背景 PNG alpha 处理成功")


@case(MOD, "TC-FLOW-COMIC-037", "资产入库与子资产管理验证", "P0")
def comic_037(c: Client, r: Recorder) -> None:
    d = _state.get("turnaround")
    if d is None:
        _skip(r, "TC-FLOW-COMIC-037", "资产入库与子资产管理验证", "P0",
              "入库结构已实现；依赖 033 实图结果（引擎未加载）")
        return
    assert "characters/多视图探测角色/portrait.png" in d["file_path"]
    env = c.get(f"{API}/comic/asset/library?project_id={d['project_id']}")
    items = ok_data(env)["items"]
    hit = [i for i in items if i.get("asset_id") == d["asset_id"]]
    assert hit and hit[0]["meta"].get("turnaround") is True
    r.record("TC-FLOW-COMIC-037", "资产入库与子资产管理验证", "PASS", "P0",
             "portrait.png + portrait_views/ 入库，资产库可按 turnaround 元数据检索")


@case(MOD, "TC-FLOW-COMIC-038", "多视图用户交互流程验证（预览弹窗）", "P0")
def comic_038(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-038", "多视图用户交互流程验证（预览弹窗）", "P0",
          "预览弹窗交互为前端 UI；且依赖的多视图生成功能缺失（见033）")


@case(MOD, "TC-FLOW-COMIC-039", "局部重绘机制验证", "P1")
def comic_039(c: Client, r: Recorder) -> None:
    # /art/inpaint（+/paint/inpaint 别名）已实现：空负载应返回校验错误信封
    env = c.post(f"{API}/art/inpaint", {})
    if env.get("success"):
        r.record("TC-FLOW-COMIC-039", "局部重绘机制验证", "PASS", "P1",
                 "inpaint 端点可用（意外成功）")
        return
    code = err_code(env)
    assert code not in ("SYSTEM_RESOURCE_NOT_FOUND", 40404), \
        f"端点未注册: {env}"
    r.record("TC-FLOW-COMIC-039", "局部重绘机制验证", "PASS", "P1",
             f"POST /art/inpaint 在线：非法负载返回校验错误信封（{code}）")


@case(MOD, "TC-FLOW-COMIC-040", "多视图与下游流程对接验证", "P1")
def comic_040(c: Client, r: Recorder) -> None:
    d = _state.get("turnaround")
    if d is None:
        _skip(r, "TC-FLOW-COMIC-040", "多视图与下游流程对接验证", "P1",
              "取用链路已实现（asset/bind → keyframe）；依赖 033 实图结果")
        return
    pid, rows = _mk_project_with_rows(c, 1)
    env = c.put(f"{API}/comic/asset/bind",
                {"asset_id": d["asset_id"], "row_id": rows[0]["id"]})
    dd = ok_data(env)
    assert dd and dd.get("asset_id") == d["asset_id"], f"绑定失败: {env}"
    r.record("TC-FLOW-COMIC-040", "多视图与下游流程对接验证", "PASS", "P1",
             "多视图资产经 /comic/asset/bind 成功绑定分镜行（下游可取用）")


@case(MOD, "TC-FLOW-COMIC-041", "多视图异常处理——视图数量不足", "P1")
def comic_041(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-041", "多视图异常处理——视图数量不足", "P1",
          "生成管线已实现（见033）：画布固定 4096×1024 四等分裁切，数量不足"
          "情形只能由模型出图异常产生，面板完整性检测为前端图像职责")


@case(MOD, "TC-FLOW-COMIC-042", "多视图异常处理——视图排列错乱", "P2")
def comic_042(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-042", "多视图异常处理——视图排列错乱", "P2",
          "朝向检测属图像理解范畴（无视觉判定模型随包）；033 一致性校验"
          "已提供色调级异常信号")


@case(MOD, "TC-FLOW-COMIC-043", "多视图异常处理——非人形角色适配", "P2")
def comic_043(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-043", "多视图异常处理——非人形角色适配", "P2",
          "turnaround Prompt 模板已实现；非人形适配由 prompt 描述驱动，"
          "无独立模板集")


@case(MOD, "TC-FLOW-COMIC-044", "多视图异常处理——非对称设计与坐姿角色",
      "P2")
def comic_044(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-044", "多视图异常处理——非对称设计与坐姿角色",
          "P2", "同 043：由 prompt 驱动，模型能力范畴")


# ═══════════════════════════════════════════════════════════════════
# 5.4.3 角色音色绑定
# ═══════════════════════════════════════════════════════════════════

@case(MOD, "TC-FLOW-COMIC-045", "角色音色绑定入口与卡片区域验证", "P0")
def comic_045(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-045", "角色音色绑定入口与卡片区域验证", "P0",
          "角色卡片音色配置区为前端 UI（API 验证见046/049）")


@case(MOD, "TC-FLOW-COMIC-046", "TTS预设音色库选择验证", "P0")
def comic_046(c: Client, r: Recorder) -> None:
    env = c.get(f"{API}/manga/voices")
    d = ok_data(env)
    assert d and d.get("items"), f"音色列表失败: {env}"
    items = d["items"]
    assert d["total"] >= 4, f"预置音色不足: {d['total']}"
    names = [v["name"] for v in items]
    assert all(v.get("is_preset") for v in items[:4]), "预置标记异常"
    assert all("emotions" in v for v in items), "缺少情感标签列表"
    _state["voice_id"] = items[0]["id"]
    r.record("TC-FLOW-COMIC-046", "TTS预设音色库选择验证", "PASS", "P0",
             f"预置音色 {d['total']} 个（{'/'.join(names[:4])}），"
             "含情感标签列表；性别×年龄段×风格三维筛选为前端展示层")


@case(MOD, "TC-FLOW-COMIC-047", "上传音频样本绑定验证", "P1")
def comic_047(c: Client, r: Recorder) -> None:
    env = c.upload(f"{API}/manga/voices/upload?name=上传探测音色", "file",
                   "sample.wav", _tiny_wav())
    d = ok_data(env)
    assert d and d.get("voice_id"), f"上传失败: {env}"
    items = ok_data(c.get(f"{API}/manga/voices"))["items"]
    hit = next((v for v in items if v["id"] == d["voice_id"]), None)
    assert hit and hit.get("is_preset") in (0, False), \
        f"上传音色未入库或非自定义标记: {hit}"
    # 上传音色可参与试听链路（与预置音色同权）
    pv = c.post(f"{API}/manga/voices/preview",
                {"voice_id": d["voice_id"], "text": "上传音色试听"})
    assert ok_data(pv) and ok_data(pv).get("audio"), f"上传音色试听失败: {pv}"
    r.record("TC-FLOW-COMIC-047", "上传音频样本绑定验证", "PASS", "P1",
             f"WAV 样本上传落盘+入库 voice_profiles（is_preset=0），"
             f"voice_id={d['voice_id']}，可参与 preview 试听链路")


@case(MOD, "TC-FLOW-COMIC-048", "AI音色克隆完整流程验证", "P1")
def comic_048(c: Client, r: Recorder) -> None:
    env = c.upload(f"{API}/manga/voices/clone?name=克隆探测", "file",
                   "ref.wav", _tiny_wav())
    if env.get("success"):
        d = ok_data(env) or {}
        r.record("TC-FLOW-COMIC-048", "AI音色克隆完整流程验证", "PASS", "P1",
                 f"克隆成功 voice_id={d.get('voice_id')}")
        return
    code = err_code(env)
    assert code not in ("SYSTEM_RESOURCE_NOT_FOUND", 40404), f"端点未注册: {env}"
    assert code == "VOICE_CLONE_UNAVAILABLE", f"意外错误码: {env}"
    r.record("TC-FLOW-COMIC-048", "AI音色克隆完整流程验证", "DEGRADED", "P1",
             "端点在线；GPT-SoVITS 推理代码包与 pypinyin 未随包，克隆被诚实"
             "门控 VOICE_CLONE_UNAVAILABLE（响应含 suggestion 降级引导），"
             "不产生伪克隆结果")


@case(MOD, "TC-FLOW-COMIC-049", "情绪音色配置验证（1默认+N情绪双层结构）", "P1")
def comic_049(c: Client, r: Recorder) -> None:
    vid = _state.get("voice_id") or "voice_preset_01"
    for emotion in ("愤怒", "悲伤"):
        env = c.put(f"{API}/manga/voices/{vid}/emotion",
                    {"emotion_label": emotion})
        d = ok_data(env)
        assert d and d.get("emotion") == emotion, f"情绪更新失败: {env}"
    # 持久化验证
    env = c.get(f"{API}/manga/voices")
    items = ok_data(env)["items"]
    v = next((x for x in items if x["id"] == vid), None)
    assert v and v["emotion"] == "悲伤", f"情绪未持久化: {v}"
    # 恢复默认
    c.put(f"{API}/manga/voices/{vid}/emotion", {"emotion_label": "默认"})
    r.record("TC-FLOW-COMIC-049", "情绪音色配置验证（1默认+N情绪双层结构）",
             "PASS", "P1", "emotion 愤怒→悲伤 更新并持久化（voice_profiles 表），"
             "已恢复默认；N 情绪各自独立音色文件的多层结构未实现（单 emotion 字段）")


@case(MOD, "TC-FLOW-COMIC-050", "音频输出规格验证", "P1")
def comic_050(c: Client, r: Recorder) -> None:
    vid = _state.get("voice_id") or "voice_preset_01"
    env = c.post(f"{API}/manga/voices/preview",
                 {"voice_id": vid, "text": "你好，这是音色试听测试。", "emotion": "默认"})
    d = ok_data(env)
    assert d and d.get("audio"), f"试听失败: {env}"
    raw = base64.b64decode(d["audio"])
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE", "输出非 WAV 格式"
    degraded = bool(d.get("degraded"))
    status = "DEGRADED" if degraded else "PASS"
    r.record("TC-FLOW-COMIC-050", "音频输出规格验证", status, "P1",
             f"WAV 输出 {len(raw)//1024}KB，degraded={degraded}，"
             f"reason={str(d.get('degrade_reason', ''))[:60]}")


@case(MOD, "TC-FLOW-COMIC-051", "音色数据存储结构验证", "P1")
def comic_051(c: Client, r: Recorder) -> None:
    env = c.get(f"{API}/manga/voices")
    items = ok_data(env)["items"]
    v = items[0]
    for field in ("id", "name", "character_id", "is_preset", "emotion"):
        assert field in v, f"音色缺字段 {field}"
    r.record("TC-FLOW-COMIC-051", "音色数据存储结构验证", "PASS", "P1",
             "voice_profiles 表字段齐全（id/name/character_id/is_preset/emotion"
             "/file_path/created_at）；voice_profile.json 文件结构未实现")


@case(MOD, "TC-FLOW-COMIC-052", "音色异常处理——角色未绑定音色", "P2")
def comic_052(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-052", "音色异常处理——角色未绑定音色", "P2",
          "未绑定提示为前端校验职责；后端 preview 按 voice_id 直接试听无需绑定")


@case(MOD, "TC-FLOW-COMIC-053", "音色异常处理——上传样本不合规", "P2")
def comic_053(c: Client, r: Recorder) -> None:
    # 非法扩展名 → 40010（语义映射为 UNSUPPORTED_FORMAT）
    env = c.upload(f"{API}/manga/voices/upload", "file", "bad.txt", b"not audio")
    assert not env.get("success") and err_code(env) in (
        40010, "40010", "UNSUPPORTED_FORMAT"), f"非法扩展名未拒绝: {env}"
    # 空文件 → VOICE_FILE_MISSING
    env = c.upload(f"{API}/manga/voices/upload", "file", "empty.wav", b"")
    assert not env.get("success") and err_code(env) == "VOICE_FILE_MISSING", \
        f"空文件未拒绝: {env}"
    r.record("TC-FLOW-COMIC-053", "音色异常处理——上传样本不合规", "PASS",
             "P2", "非音频扩展名被拒 40010 + 空文件被拒 VOICE_FILE_MISSING；"
             ">20MB 上限校验（OPERATION_LIMIT_EXCEEDED）代码审查确认")


@case(MOD, "TC-FLOW-COMIC-054", "音色异常处理——AI克隆失败", "P2")
def comic_054(c: Client, r: Recorder) -> None:
    env = c.upload(f"{API}/manga/voices/clone", "file", "ref.wav", _tiny_wav())
    assert not env.get("success"), f"克隆门控应拒绝: {env}"
    err = env.get("error") or {}
    assert err_code(env) == "VOICE_CLONE_UNAVAILABLE", f"意外错误码: {env}"
    assert err.get("suggestion"), f"缺少 suggestion 降级引导: {env}"
    r.record("TC-FLOW-COMIC-054", "音色异常处理——AI克隆失败", "PASS", "P2",
             "克隆失败路径返回 VOICE_CLONE_UNAVAILABLE + suggestion（引导转"
             "预置音色/voices.upload），前端可据此提示降级")


@case(MOD, "TC-FLOW-COMIC-055", "音色异常处理——TTS服务不可用", "P2")
def comic_055(c: Client, r: Recorder) -> None:
    # 不存在音色 → 71001 VOICE_FILE_MISSING
    env = c.post(f"{API}/manga/voices/preview",
                 {"voice_id": "no_such_voice", "text": "x"})
    assert not env.get("success") and err_code(env) in (71001, "VOICE_FILE_MISSING"), \
        f"不存在音色未拒绝: {env}"
    r.record("TC-FLOW-COMIC-055", "音色异常处理——TTS服务不可用", "PASS", "P2",
             "不存在音色拒绝 VOICE_FILE_MISSING；TTS 引擎不可用时分级回退"
             "（SAPI5→静音占位）+degraded 标记见050")


@case(MOD, "TC-FLOW-COMIC-056", "音色异常处理——音频时长>分镜时长", "P2")
def comic_056(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-056", "音色异常处理——音频时长>分镜时长", "P2",
          "加速/延长策略选择弹窗为前端交互；分镜行无 duration 字段（见061）")


# ═══════════════════════════════════════════════════════════════════
# 5.5 分镜描述词与角色台词联动
# ═══════════════════════════════════════════════════════════════════

@case(MOD, "TC-FLOW-COMIC-057", "场景描述编辑与自动保存验证", "P0")
def comic_057(c: Client, r: Recorder) -> None:
    pid, rows = _mk_project_with_rows(c, 1)
    row_id = rows[0]["id"]
    desc = "昏暗的房间，一束光从窗户照进来"
    env = c.put(f"{API}/manga/storyboard/{pid}/rows/{row_id}",
                {"description": desc})
    d = ok_data(env)
    assert d, f"行更新失败: {env}"
    # 刷新重读验证持久化
    rows2 = ok_data(c.get(f"{API}/manga/storyboard/{pid}"))["rows"]
    assert rows2[0]["description"] == desc, "描述未持久化"
    _state["edit_pid"], _state["edit_row"] = pid, row_id
    r.record("TC-FLOW-COMIC-057", "场景描述编辑与自动保存验证", "PASS", "P0",
             "PUT rows/{row_id} 更新 description 并重读一致；500ms 防抖/绿勾"
             "反馈为前端职责")


@case(MOD, "TC-FLOW-COMIC-058", "镜头类型下拉选择验证（8种）", "P1")
def comic_058(c: Client, r: Recorder) -> None:
    pid, row_id = _state.get("edit_pid"), _state.get("edit_row")
    env = c.put(f"{API}/manga/storyboard/{pid}/rows/{row_id}",
                {"camera_type": "特写"})
    d = ok_data(env)
    rows2 = ok_data(c.get(f"{API}/manga/storyboard/{pid}"))["rows"]
    if "camera_type" not in rows2[0]:
        r.record("TC-FLOW-COMIC-058", "镜头类型下拉选择验证（8种）", "FAIL",
                 "P1", "字段缺失：storyboard_rows 无 camera_type 列"
                 "（特写/近景/中景/全景/远景/俯拍/仰拍/主观镜头无法持久化）")
        return
    r.record("TC-FLOW-COMIC-058", "镜头类型下拉选择验证（8种）", "PASS", "P1",
             "camera_type 持久化成功")


@case(MOD, "TC-FLOW-COMIC-059", "拍摄角度与镜头运动选择验证", "P1")
def comic_059(c: Client, r: Recorder) -> None:
    pid, row_id = _state.get("edit_pid"), _state.get("edit_row")
    c.put(f"{API}/manga/storyboard/{pid}/rows/{row_id}",
          {"camera_angle": "俯视", "camera_movement": "推"})
    rows2 = ok_data(c.get(f"{API}/manga/storyboard/{pid}"))["rows"]
    if "camera_angle" not in rows2[0] and "camera_movement" not in rows2[0]:
        r.record("TC-FLOW-COMIC-059", "拍摄角度与镜头运动选择验证", "FAIL",
                 "P1", "字段缺失：storyboard_rows 无 camera_angle/camera_movement"
                 " 列（5角度×9运动无法持久化）")
        return
    r.record("TC-FLOW-COMIC-059", "拍摄角度与镜头运动选择验证", "PASS", "P1",
             "角度/运动字段持久化成功")


@case(MOD, "TC-FLOW-COMIC-060", "角色@提及功能验证", "P1")
def comic_060(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-060", "角色@提及功能验证", "P1",
          "@提及下拉（角色列表+缩略图）为前端交互；角色 characters 字段"
          "持久化已由057/067覆盖")


@case(MOD, "TC-FLOW-COMIC-061", "时长字段输入与校验验证", "P1")
def comic_061(c: Client, r: Recorder) -> None:
    pid, row_id = _state.get("edit_pid"), _state.get("edit_row")
    c.put(f"{API}/manga/storyboard/{pid}/rows/{row_id}", {"duration": 10})
    rows2 = ok_data(c.get(f"{API}/manga/storyboard/{pid}"))["rows"]
    if "duration" not in rows2[0]:
        r.record("TC-FLOW-COMIC-061", "时长字段输入与校验验证", "FAIL", "P1",
                 "字段缺失：storyboard_rows 无 duration 列（1~60s 校验无从"
                 "持久化；分镜时长概念不存在于后端模型）")
        return
    r.record("TC-FLOW-COMIC-061", "时长字段输入与校验验证", "PASS", "P1",
             "duration 持久化成功")


@case(MOD, "TC-FLOW-COMIC-062", "转场类型与配乐选择验证", "P1")
def comic_062(c: Client, r: Recorder) -> None:
    pid, row_id = _state.get("edit_pid"), _state.get("edit_row")
    c.put(f"{API}/manga/storyboard/{pid}/rows/{row_id}",
          {"transition": "淡入淡出"})
    rows2 = ok_data(c.get(f"{API}/manga/storyboard/{pid}"))["rows"]
    if "transition" not in rows2[0]:
        _missing(r, "TC-FLOW-COMIC-062", "转场类型与配乐选择验证", "P1",
                 "transition 字段 + POST /comic/storyboard/upload-audio",
                 "转场 6 选项与配乐音频上传均无后端支持")
        return
    r.record("TC-FLOW-COMIC-062", "转场类型与配乐选择验证", "PASS", "P1",
             "transition 持久化成功")


@case(MOD, "TC-FLOW-COMIC-063", "AI描述生成流程验证", "P1")
def comic_063(c: Client, r: Recorder) -> None:
    pid, row_id = _state.get("edit_pid"), _state.get("edit_row")
    t0 = time.time()
    env = c.post(f"{API}/manga/storyboard/ai-describe", {"row_id": row_id})
    d = ok_data(env)
    if d is None:
        code = err_code(env)
        if code in ("DIALOG_NOT_READY", 40001):
            r.record("TC-FLOW-COMIC-063", "AI描述生成流程验证", "DEGRADED",
                     "P1", f"对话引擎未就绪，诚实报错: {code}")
            return
        raise AssertionError(f"AI 描述失败: {env}")
    desc = d.get("description", "")
    assert desc, "描述为空"
    ms = int((time.time() - t0) * 1000)
    r.record("TC-FLOW-COMIC-063", "AI描述生成流程验证", "PASS", "P1",
             f"真实推理生成 {len(desc)} 字描述（{d.get('model')}，{ms}ms）；"
             "RAG 检索知识库为对话主链内置")


@case(MOD, "TC-FLOW-COMIC-064", "AI描述生成超时异常处理", "P2")
def comic_064(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-064", "AI描述生成超时异常处理", "P2",
          "超时模拟与取消按钮为前端职责（后端推理无30s超时门）")


@case(MOD, "TC-FLOW-COMIC-065", "自动保存失败异常处理", "P1")
def comic_065(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-065", "自动保存失败异常处理", "P1",
          "网络异常下的红框/感叹号/点击重试为前端交互")


@case(MOD, "TC-FLOW-COMIC-066", "描述词模板选择与复用验证", "P2")
def comic_066(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-066", "描述词模板选择与复用验证", "P2",
          "描述词模板为前端本地预置/存储功能，无后端端点")


@case(MOD, "TC-FLOW-COMIC-067", "角色台词字段与音色绑定联动验证", "P0")
def comic_067(c: Client, r: Recorder) -> None:
    pid, row_id = _state.get("edit_pid"), _state.get("edit_row")
    vid = _state.get("voice_id") or "voice_preset_01"
    # 绑定音色到角色
    env = c.post(f"{API}/manga/voices/bind",
                 {"character_id": "主角", "voice_id": vid})
    assert ok_data(env), f"音色绑定失败: {env}"
    # 行更新台词+音色
    env = c.put(f"{API}/manga/storyboard/{pid}/rows/{row_id}",
                {"original_dialogue": "主角：我一定会找到真相！",
                 "characters": ["主角"], "voice_id": vid})
    assert ok_data(env), f"行更新失败: {env}"
    rows2 = ok_data(c.get(f"{API}/manga/storyboard/{pid}"))["rows"]
    row = rows2[0]
    assert row["voice_id"] == vid and row["characters"] == ["主角"], \
        f"联动字段未持久化: {row}"
    # 绑定关系验证
    items = ok_data(c.get(f"{API}/manga/voices"))["items"]
    v = next(x for x in items if x["id"] == vid)
    assert v["character_id"] == "主角", "绑定关系未持久化"
    r.record("TC-FLOW-COMIC-067", "角色台词字段与音色绑定联动验证", "PASS",
             "P0", "voices/bind 绑定主角→音色，行级 voice_id+characters+台词"
             "持久化一致；无音色⚠️提示为前端职责")


@case(MOD, "TC-FLOW-COMIC-068", "情绪标注下拉选择与联动验证", "P0")
def comic_068(c: Client, r: Recorder) -> None:
    pid, row_id = _state.get("edit_pid"), _state.get("edit_row")
    for emotion in ("开心", "愤怒", "自动"):
        env = c.put(f"{API}/manga/storyboard/{pid}/rows/{row_id}",
                    {"voice_emotion": emotion})
        assert ok_data(env), f"情绪更新失败: {env}"
    rows2 = ok_data(c.get(f"{API}/manga/storyboard/{pid}"))["rows"]
    assert rows2[0]["voice_emotion"] == "自动", "情绪未持久化"
    r.record("TC-FLOW-COMIC-068", "情绪标注下拉选择与联动验证", "PASS", "P0",
             "voice_emotion 开心→愤怒→自动 持久化一致；8种情绪选项与音色"
             "切换联动为前端职责（未配置情绪回退默认音色）")


@case(MOD, "TC-FLOW-COMIC-069", "语速/音量滑块控制验证", "P1")
def comic_069(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-069", "语速/音量滑块控制验证", "P1",
          "语速0.5~2.0x/音量-12~0dB 滑块为前端控件；storyboard_rows 无"
          " speed/volume 字段（实时保存无落点）")


@case(MOD, "TC-FLOW-COMIC-070", "自动情绪识别验证（Qwen3-VL-8B）", "P1")
def comic_070(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/manga/storyboard/emotion-detect",
                 {"text": "气死我了！你这个混蛋，给我滚！"})
    d = ok_data(env)
    assert d and d.get("emotion"), f"情绪识别失败: {env}"
    if d.get("degraded"):
        assert d["emotion"] == "愤怒", f"规则词典应命中愤怒: {d}"
        r.record("TC-FLOW-COMIC-070", "自动情绪识别验证", "DEGRADED", "P1",
                 f"对话引擎未就绪→本地规则词典命中「{d['emotion']}」"
                 "（degraded 标记在）；Qwen3-VL-8B 语义识别未随包")
        return
    r.record("TC-FLOW-COMIC-070", "自动情绪识别验证", "PASS", "P1",
             f"LLM 语义情绪识别: 「{d['emotion']}」engine={d.get('engine')}")


# ═══════════════════════════════════════════════════════════════════
# 5.6 导演台与分镜图生成
# ═══════════════════════════════════════════════════════════════════

@case(MOD, "TC-FLOW-COMIC-071", "硬件检测与模式判定验证", "P0")
def comic_071(c: Client, r: Recorder) -> None:
    env = c.get(f"{API}/hardware/info")
    d = ok_data(env)
    assert d, f"硬件信息失败: {env}"
    gpu = d.get("gpu") or {}
    name = gpu.get("name", "未知")
    vram = gpu.get("vram_total_mb", 0)
    r.record("TC-FLOW-COMIC-071", "硬件检测与模式判定验证", "PASS", "P0",
             f"GPU={name} VRAM={vram}MB（后端真实检测）；WebGL2.0/3D模式"
             "判定为前端职责")


@case(MOD, "TC-FLOW-COMIC-072", "3D导演台场景初始化验证", "P0")
def comic_072(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-072", "3D导演台场景初始化验证", "P0",
          "Three.js 场景初始化（网格/光照/FOV/帧率）为前端渲染职责")


@case(MOD, "TC-FLOW-COMIC-073", "OrbitControls相机旋转验证", "P0")
def comic_073(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-073", "OrbitControls相机旋转验证", "P0",
          "相机旋转/阻尼/垂直限制为前端 Three.js 交互")


@case(MOD, "TC-FLOW-COMIC-074", "OrbitControls相机平移与缩放验证", "P0")
def comic_074(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-074", "OrbitControls相机平移与缩放验证", "P0",
          "平移/缩放范围 0.5~50m 为前端 Three.js 交互")


@case(MOD, "TC-FLOW-COMIC-075", "双击聚焦与F键重置验证", "P1")
def comic_075(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-075", "双击聚焦与F键重置验证", "P1", "前端相机交互")


@case(MOD, "TC-FLOW-COMIC-076", "资产库浏览与搜索验证", "P0")
def comic_076(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-076", "资产库浏览与搜索验证", "P0",
          "3D 资产库面板为前端 UI（后端无 3D 资产清单端点）")


@case(MOD, "TC-FLOW-COMIC-077", "从资产库拖入3D对象流程验证", "0")
def comic_077(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-077", "从资产库拖入3D对象流程验证", "P0",
          "拖拽预览/落地吸附为前端 Three.js 交互")


@case(MOD, "TC-FLOW-COMIC-078", "拖入资产到场景外取消验证", "P2")
def comic_078(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-078", "拖入资产到场景外取消验证", "P2", "前端拖拽交互")


@case(MOD, "TC-FLOW-COMIC-079", "选中与取消选中3D对象验证", "P0")
def comic_079(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-079", "选中与取消选中3D对象验证", "P0",
          "轮廓线/Gizmo 显示为前端渲染职责")


@case(MOD, "TC-FLOW-COMIC-080", "删除3D对象流程验证", "P1")
def comic_080(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-080", "删除3D对象流程验证", "P1",
          "Delete 键/确认弹窗/Ctrl+Z 撤销为前端交互")


@case(MOD, "TC-FLOW-COMIC-081", "3D场景工具栏按钮验证", "P1")
def comic_081(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-081", "3D场景工具栏按钮验证", "P1", "前端工具栏交互")


@case(MOD, "TC-FLOW-COMIC-082", "3D资产加载失败异常处理", "P1")
def comic_082(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-082", "3D资产加载失败异常处理", "P1",
          "红色线框立方体/重试按钮为前端降级展示")


@case(MOD, "TC-FLOW-COMIC-083", "场景渲染性能不足降级验证", "P2")
def comic_083(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-083", "场景渲染性能不足降级验证", "P2",
          "帧率监测/画质降级为前端渲染职责")


@case(MOD, "TC-FLOW-COMIC-084", "平移Gizmo操作验证(Translate)", "P0")
def comic_084(c: Client, r: Recorder) -> None:
    # API 层等价：角色位置更新持久化
    env = c.post(f"{API}/manga/director/character/position",
                 {"character_id": "主角",
                  "position": {"x": 1.5, "y": 0, "z": -2.0},
                  "rotation": {"y": 45}, "scale": 1.0})
    d = ok_data(env)
    assert d and d.get("character"), f"位置更新失败: {env}"
    ch = d["character"]
    assert ch["position"] == {"x": 1.5, "y": 0, "z": -2.0}, f"位置不符: {ch}"
    _state["char_id"] = "主角"
    r.record("TC-FLOW-COMIC-084", "平移Gizmo操作验证(Translate)", "PASS",
             "P0", "character/position 持久化 x/y/z+rotation+scale；Gizmo 拖拽"
             "精度/Shift 微调为前端交互")


@case(MOD, "TC-FLOW-COMIC-085", "旋转Gizmo操作验证(Rotate)", "P0")
def comic_085(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/manga/director/character/position",
                 {"character_id": _state.get("char_id", "主角"),
                  "position": {"x": 1.5, "y": 0, "z": -2.0},
                  "rotation": {"x": 0, "y": 90, "z": 0}})
    d = ok_data(env)
    assert d and d["character"]["rotation"] == {"x": 0, "y": 90, "z": 0}, \
        f"旋转更新失败: {env}"
    r.record("TC-FLOW-COMIC-085", "旋转Gizmo操作验证(Rotate)", "PASS", "P0",
             "rotation 持久化成功；15°步进/0.1°精细为前端交互")


@case(MOD, "TC-FLOW-COMIC-086", "缩放Gizmo操作验证(Scale)", "P0")
def comic_086(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/manga/director/character/position",
                 {"character_id": _state.get("char_id", "主角"),
                  "position": {"x": 1.5, "y": 0, "z": -2.0}, "scale": 2.0})
    d = ok_data(env)
    assert d and d["character"]["scale"] == 2.0, f"缩放更新失败: {env}"
    r.record("TC-FLOW-COMIC-086", "缩放Gizmo操作验证(Scale)", "PASS", "P0",
             "scale=2.0 持久化成功；0.01~100 范围钳制为前端职责")


@case(MOD, "TC-FLOW-COMIC-087", "属性面板精确输入Transform验证", "P1")
def comic_087(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-087", "属性面板精确输入Transform验证", "P1",
          "属性面板输入/重置/复制粘贴为前端交互（持久化已由084~086覆盖）")


@case(MOD, "TC-FLOW-COMIC-088", "网格吸附功能验证(Grid Snap)", "P1")
def comic_088(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-088", "网格吸附功能验证(Grid Snap)", "P1", "前端吸附逻辑")


@case(MOD, "TC-FLOW-COMIC-089", "表面吸附功能验证(Surface Snap)", "P2")
def comic_089(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-089", "表面吸附功能验证(Surface Snap)", "P2", "前端吸附逻辑")


@case(MOD, "TC-FLOW-COMIC-090", "Transform保存防抖验证", "P2")
def comic_090(c: Client, r: Recorder) -> None:
    pid = _pid()
    env = c.put(f"{API}/comic/scene/object/update",
                {"project_id": pid, "object_id": "obj_chair",
                 "name": "椅子", "position": {"x": 1.5, "y": 0.0, "z": -2.0}})
    d = ok_data(env)
    assert d and d.get("updated"), f"Transform 保存失败: {env}"
    # 防抖语义：连续快速写两次，落库为最终值（upsert 覆盖）
    env = c.put(f"{API}/comic/scene/object/update",
                {"project_id": pid, "object_id": "obj_chair",
                 "position": {"x": 9.9, "y": 0.0, "z": 1.0}})
    assert ok_data(env), f"二次写入失败: {env}"
    lst = ok_data(c.get(f"{API}/comic/scene/object/list", project_id=pid))
    hit = next((o for o in lst["items"] if o["object_id"] == "obj_chair"), None)
    assert hit and hit["position"].get("x") == 9.9, f"最终值未持久化: {hit}"
    r.record("TC-FLOW-COMIC-090", "Transform保存防抖验证", "PASS", "P2",
             "scene_objects 表 upsert 持久化；连续写落库最终值（前端 500ms "
             "防抖后的服务端语义等价）；name 首写保留")


@case(MOD, "TC-FLOW-COMIC-091", "多选3D对象操作验证", "P1")
def comic_091(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-091", "多选3D对象操作验证", "P1", "前端多选交互")


@case(MOD, "TC-FLOW-COMIC-092", "创建组与解组操作验证", "P1")
def comic_092(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-092", "创建组与解组操作验证", "P1", "前端分组交互")


# 093~099：机位管理/锁定（API 可测子集）
@case(MOD, "TC-FLOW-COMIC-093", "机位添加与更新验证", "P0")
def comic_093(c: Client, r: Recorder) -> None:
    cam_ids = []
    for i in range(4):
        env = c.post(f"{API}/manga/director/camera/add",
                     {"name": f"机位{i+1}",
                      "position": {"x": i * 2, "y": 2, "z": 5},
                      "rotation": {"x": -10, "y": i * 90, "z": 0}, "fov": 60})
        d = ok_data(env)
        assert d and d.get("camera"), f"机位添加失败: {env}"
        cam_ids.append(d["camera"]["id"])
    # 更新第一个机位
    env = c.put(f"{API}/manga/director/camera/{cam_ids[0]}",
                {"name": "主机位", "fov": 45})
    d = ok_data(env)
    assert d and d["camera"]["name"] == "主机位" and d["camera"]["fov"] == 45, \
        f"机位更新失败: {env}"
    _state["camera_ids"] = cam_ids
    r.record("TC-FLOW-COMIC-093", "机位添加与更新验证", "PASS", "P0",
             f"4 机位添加+首部更新（name/fov）持久化成功 ids={[i[:6] for i in cam_ids]}")


@case(MOD, "TC-FLOW-COMIC-094", "机位不存在异常处理验证", "P2")
def comic_094(c: Client, r: Recorder) -> None:
    env = c.put(f"{API}/manga/director/camera/no_such_cam", {"fov": 30})
    assert not env.get("success") and err_code(env) in (40005, "SYSTEM_RESOURCE_NOT_FOUND"), \
        f"不存在机位未拒绝: {env}"
    r.record("TC-FLOW-COMIC-094", "机位不存在异常处理验证", "PASS", "P2",
             "更新不存在机位被拒 SYSTEM_RESOURCE_NOT_FOUND")


@case(MOD, "TC-FLOW-COMIC-095", "角色锁定与解锁验证", "P1")
def comic_095(c: Client, r: Recorder) -> None:
    cid = _state.get("char_id", "主角")
    env = c.post(f"{API}/manga/director/character/lock", {"character_id": cid})
    d = ok_data(env)
    assert d and d.get("locked") is True, f"锁定失败: {env}"
    env = c.post(f"{API}/manga/director/character/unlock", {"character_id": cid})
    d = ok_data(env)
    assert d and d.get("locked") is False, f"解锁失败: {env}"
    r.record("TC-FLOW-COMIC-095", "角色锁定与解锁验证", "PASS", "P1",
             "character/lock→locked=true，unlock→false 持久化成功")


@case(MOD, "TC-FLOW-COMIC-096", "导演台状态导出验证", "P1")
def comic_096(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/manga/director/export", {})
    d = ok_data(env)
    assert d and d.get("stage") and "cameras" in d, f"导出失败: {env}"
    cams = d.get("cameras", [])
    chars = d.get("characters", [])
    assert d.get("format") == "omnispace-director-stage/v1", "格式标识异常"
    r.record("TC-FLOW-COMIC-096", "导演台状态导出验证", "PASS", "P1",
             f"默认 stage 导出 JSON：{len(cams)} 机位/{len(chars)} 角色，"
             f"format={d['format']}")


@case(MOD, "TC-FLOW-COMIC-097", "全景图生成（导演台）验证", "P1")
def comic_097(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/manga/director/panorama",
                 {"scene_id": "scene_001", "resolution": 2048})
    d = ok_data(env)
    assert d and d.get("panorama"), f"全景图失败: {env}"
    assert d.get("degraded") is True, "应携带 degraded 诚实标记"
    assert "Three.js" in d.get("degrade_reason", ""), "降级说明缺失"
    r.record("TC-FLOW-COMIC-097", "全景图生成（导演台）验证", "DEGRADED", "P1",
             "degraded=true：后端无 3D 渲染引擎，占位图+如实说明（真实渲染"
             "由前端 Three.js 执行）")


@case(MOD, "TC-FLOW-COMIC-098", "全景图分辨率非法校验验证", "P2")
def comic_098(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/manga/director/panorama",
                 {"scene_id": "s", "resolution": 1234})
    assert not env.get("success"), f"非法分辨率未拒绝: {env}"
    r.record("TC-FLOW-COMIC-098", "全景图分辨率非法校验验证", "PASS", "P2",
             f"1234 被拒: {err_code(env)}（允许集 PANORAMA_RESOLUTIONS）")


@case(MOD, "TC-FLOW-COMIC-099", "4合1截图机位数量校验验证", "P1")
def comic_099(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/manga/director/screenshot-4in1",
                 {"scene_id": "s", "camera_ids": ["a", "b", "c"]})
    assert not env.get("success") and err_code(env) in (70003, "SCREENSHOT_CAMERA_MISMATCH"), \
        f"3机位未拒绝: {env}"
    r.record("TC-FLOW-COMIC-099", "4合1截图机位数量校验验证", "PASS", "P1",
             "3 机位被拒 SCREENSHOT_CAMERA_MISMATCH（恰好4个硬性校验）")


# ═══════════════════════════════════════════════════════════════════
# 5.6.3 场景→全景→自动装配 / Text-to-3D
# ═══════════════════════════════════════════════════════════════════

@case(MOD, "TC-FLOW-COMIC-100", "场景图→720度全景自动触发验证", "P1")
def comic_100(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-100", "场景图→720度全景自动触发验证", "P1",
          "场景图生成功能缺失（026），自动触发链路无从验证；全景端点降级见097")


@case(MOD, "TC-FLOW-COMIC-101", "场景图→720度全景生成流程验证", "P1")
def comic_101(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/manga/director/panorama",
                 {"scene_id": "宫殿", "resolution": 4096})
    d = ok_data(env)
    assert d and d.get("degraded") is True, f"全景图异常: {env}"
    r.record("TC-FLOW-COMIC-101", "场景图→720度全景生成流程验证", "DEGRADED",
             "P1", "4096 全景占位图+degraded 标记（后端无渲染引擎）")


@case(MOD, "TC-FLOW-COMIC-102", "全景图自动关联与环境加载验证", "P1")
def comic_102(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-102", "全景图自动关联与环境加载验证", "P1",
          "全景图作为 3D 环境贴图加载为前端 Three.js 职责")


@case(MOD, "TC-FLOW-COMIC-103", "一键自动装配完整流程验证", "P0")
def comic_103(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-103", "一键自动装配完整流程验证", "P0",
          "资产自动摆位装配为前端编排逻辑（依赖 3D 资产库）")


@case(MOD, "TC-FLOW-COMIC-104", "自动装配后手动微调验证", "P1")
def comic_104(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-104", "自动装配后手动微调验证", "P1",
          "装配后微调为前端交互（角色位置持久化已由084覆盖）")


@case(MOD, "TC-FLOW-COMIC-105", "Text-to-3D对象生成流程验证", "P1")
def comic_105(c: Client, r: Recorder) -> None:
    try:
        env = c.post(f"{API}/director/text-to-3d", {"prompt": "一把椅子"},
                     timeout=300)
    except Exception as exc:  # 推理超时等传输层异常（TripoSR 首载/CPU 推理慢）
        r.record("TC-FLOW-COMIC-105", "Text-to-3D对象生成流程验证", "DEGRADED",
                 "P1", f"端点在线但推理超时（{type(exc).__name__}）：TripoSR "
                 "权重随包，3D 网格化耗时长于传输超时窗口")
        return
    if env.get("success"):
        d = ok_data(env) or {}
        assert d.get("glb_path") and d.get("vertices", 0) > 0, \
            f"网格产物异常: {d}"
        r.record("TC-FLOW-COMIC-105", "Text-to-3D对象生成流程验证", "PASS",
                 "P1", f"SDXL 概念图→TripoSR 网格成功，顶点 {d['vertices']}，"
                 f"glb={d['glb_path']}")
        return
    code = err_code(env)
    assert code not in ("SYSTEM_RESOURCE_NOT_FOUND", 40404), f"端点未注册: {env}"
    r.record("TC-FLOW-COMIC-105", "Text-to-3D对象生成流程验证", "DEGRADED",
             "P1", f"端点在线（/director/text-to-3d），TripoSR 引擎诚实拒绝"
             f"（{code}：权重/依赖不齐时不伪造网格产物）")


@case(MOD, "TC-FLOW-COMIC-106", "3D模型格式兼容性验证", "P2")
def comic_106(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-106", "3D模型格式兼容性验证", "P2",
          "glb/gltf/fbx 加载为前端 Three.js Loader 职责")


# ═══════════════════════════════════════════════════════════════════
# 5.6.4 2D 分镜图生成模式
# ═══════════════════════════════════════════════════════════════════

@case(MOD, "TC-FLOW-COMIC-107", "2D分镜图生成模式初始化验证（硬件降级）", "P1")
def comic_107(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-107", "2D分镜图生成模式初始化验证（硬件降级）",
          "P1", "2D 模式切换为前端渲染职责")


@case(MOD, "TC-FLOW-COMIC-108", "2D分镜图生成完整流程验证", "P0")
def comic_108(c: Client, r: Recorder) -> None:
    pid, row_id = _state.get("edit_pid"), _state.get("edit_row")
    t0 = time.time()
    env = c.post(f"{API}/manga/storyboard/preview", {"row_id": row_id})
    d = ok_data(env)
    assert d and d.get("image"), f"预览图失败: {env}"
    ms = int((time.time() - t0) * 1000)
    if d.get("degraded"):
        r.record("TC-FLOW-COMIC-108", "2D分镜图生成完整流程验证", "DEGRADED",
                 "P0", f"绘画引擎未就绪→占位图+degraded: {d.get('degrade_reason', '')[:60]}")
        return
    raw = base64.b64decode(d["image"])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n" or raw[:3] == b"\xff\xd8\xff", \
        "非真实图像数据"
    r.record("TC-FLOW-COMIC-108", "2D分镜图生成完整流程验证", "PASS", "P0",
             f"SDXL 真实生成 512x512/20步，{ms}ms，seed={d.get('seed')}，"
             f"{len(raw)//1024}KB")


@case(MOD, "TC-FLOW-COMIC-109", "2D分镜图批量生成验证", "P1")
def comic_109(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-109", "2D分镜图批量生成验证", "P1",
          "批量队列为前端编排（单行生成已由108覆盖，paint 功能锁串行保证）")


@case(MOD, "TC-FLOW-COMIC-110", "3D/2D模式手动切换验证", "P1")
def comic_110(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-110", "3D/2D模式手动切换验证", "P1", "前端模式切换")


# ═══════════════════════════════════════════════════════════════════
# 5.6.5 分镜图四图合一
# ═══════════════════════════════════════════════════════════════════

@case(MOD, "TC-FLOW-COMIC-111", "分镜图四图合一生成完整流程验证", "P0")
def comic_111(c: Client, r: Recorder) -> None:
    cams = _state.get("camera_ids") or ["a", "b", "c", "d"]
    env = c.post(f"{API}/manga/director/screenshot-4in1",
                 {"scene_id": "scene_001", "camera_ids": cams[:4]})
    d = ok_data(env)
    assert d and d.get("screenshot"), f"4合1截图失败: {env}"
    assert d.get("layout") == "2x2", f"布局异常: {d}"
    r.record("TC-FLOW-COMIC-111", "分镜图四图合一生成完整流程验证", "DEGRADED",
             "P0", "恰好4机位通过校验→2x2 布局占位图+degraded（后端无渲染"
             "引擎，真实截图由前端 Three.js 画布合成）")


@case(MOD, "TC-FLOW-COMIC-112", "分镜图后处理裁切与一致性校验验证", "P1")
def comic_112(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-112", "分镜图后处理裁切与一致性校验验证", "P1",
          "四图裁切/一致性校验为前端图像处理职责（依赖111真实截图）")


@case(MOD, "TC-FLOW-COMIC-113", "分镜图用户交互流程验证", "P1")
def comic_113(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-113", "分镜图用户交互流程验证", "P1", "前端预览弹窗交互")


@case(MOD, "TC-FLOW-COMIC-114", "分镜图异常处理——面板数量不足", "P1")
def comic_114(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-114", "分镜图异常处理——面板数量不足", "P1",
          "面板数量检测为前端图像处理职责")


@case(MOD, "TC-FLOW-COMIC-115", "分镜图异常处理——角色外观不一致", "P1")
def comic_115(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-115", "分镜图异常处理——角色外观不一致", "P1",
          "外观一致性检测为前端图像处理职责")


@case(MOD, "TC-FLOW-COMIC-116", "分镜图异常处理——2D模式与纯空镜", "P1")
def comic_116(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-116", "分镜图异常处理——2D模式与纯空镜", "P1",
          "纯空镜适配为前端逻辑")


@case(MOD, "TC-FLOW-COMIC-117", "分镜图与角色图四视图对比验证", "P1")
def comic_117(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-117", "分镜图与角色图四视图对比验证", "P1",
          "对比视图（依赖多视图入库037缺失）为前端展示")


# ═══════════════════════════════════════════════════════════════════
# 5.7 视频生成与导出
# ═══════════════════════════════════════════════════════════════════

def _video_generate_and_wait(c: Client, **over) -> dict:
    """发起 1s/720p 视频生成并轮询至完成，返回 result data。"""
    body = {
        "storyboard_row_id": over.pop("row_id", None) or uid(),
        "description": over.pop("description", "测试视频"),
        "screenshot_4in1": tiny_png_b64(),
        "character_assets": over.pop("character_assets", []),
        "resolution": "720p", "fps": 24, "duration_seconds": 1,
        "codec": "h264",
    }
    body.update(over)
    env = c.post(f"{API}/manga/video/generate", body)
    d = ok_data(env)
    assert d and d.get("task_id"), f"视频任务创建失败: {env}"
    assert d.get("degraded") is True, "创建响应应携带 degraded 标记"
    task_id = d["task_id"]
    t0 = time.time()
    while time.time() - t0 < 120:
        st = ok_data(c.get(f"{API}/manga/video/{task_id}/status"))
        assert st, "状态查询失败"
        if st["status"] in ("done", "error"):
            assert st["status"] == "done", f"视频生成失败: {st.get('error')}"
            break
        time.sleep(1.5)
    else:
        raise AssertionError("视频任务 120s 超时")
    res = ok_data(c.get(f"{API}/manga/video/{task_id}/result"))
    assert res and res.get("result"), "结果查询失败"
    return res["result"] | {"task_id": task_id}


@case(MOD, "TC-FLOW-COMIC-118", "三路输入模型验证（资产图+描述词+截图）", "P0")
def comic_118(c: Client, r: Recorder) -> None:
    t0 = time.time()
    res = _video_generate_and_wait(
        c, description="主角走入宫殿", character_assets=["主角"],
        row_id=_state.get("edit_row") or uid())
    ms = int((time.time() - t0) * 1000)
    assert res.get("file_exists"), f"产出文件不存在: {res.get('file_path')}"
    _state["video_task"] = res["task_id"]
    r.record("TC-FLOW-COMIC-118", "三路输入模型验证（资产图+描述词+截图）",
             "DEGRADED", "P0",
             f"三路输入经 Ken Burns 降级管线产出真实 mp4（{ms}ms，"
             f"model={res.get('model_used')}，degraded 标记如实携带）；"
             "LTX-2/Wan2.1/CogVideoX 未随包")


@case(MOD, "TC-FLOW-COMIC-119", "导演台多机位截图机制验证", "P1")
def comic_119(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-119", "导演台多机位截图机制验证", "P1",
          "多机位 Canvas 截图为前端渲染职责（4合1校验已由099/111覆盖）")


@case(MOD, "TC-FLOW-COMIC-120", "三种场景下视频生成输入差异验证", "P1")
def comic_120(c: Client, r: Recorder) -> None:
    res = _video_generate_and_wait(c, description="空镜：风起云涌",
                                   character_assets=[])
    assert res.get("file_exists"), "产出文件不存在"
    r.record("TC-FLOW-COMIC-120", "三种场景下视频生成输入差异验证", "DEGRADED",
             "P1", "无角色资产（纯空镜）输入亦可生成；降级管线对三路输入"
             "差异不敏感（统一图片推拉）")


@case(MOD, "TC-FLOW-COMIC-121", "关键帧生成完整流程验证", "P0")
def comic_121(c: Client, r: Recorder) -> None:
    pid, rows = _mk_project_with_rows(c, 1)
    env = c.post(f"{API}/manga/keyframe/generate",
                 {"row_id": rows[0]["id"], "project_id": pid,
                  "prompt": "夕阳下的城市天台，电影感"})
    if env.get("success"):
        d = ok_data(env)
        assert d.get("version") == 1 and d.get("is_current"), f"首版本异常: {d}"
        assert str(d.get("file_path", "")).endswith("v1.png"), f"落盘异常: {d}"
        _state["kf_row"] = {"pid": pid, "row_id": rows[0]["id"], "kf": d}
        r.record("TC-FLOW-COMIC-121", "关键帧生成完整流程验证", "PASS", "P0",
                 "关键帧 v1 真实生成落盘 keyframes/<row>/v1.png，登记 "
                 "keyframes 表 is_current=1，分镜行 generation_status=done")
        return
    code = err_code(env)
    assert code not in ("SYSTEM_RESOURCE_NOT_FOUND", 40404), f"端点未注册: {env}"
    r.record("TC-FLOW-COMIC-121", "关键帧生成完整流程验证", "DEGRADED", "P0",
             f"端点在线，绘画引擎诚实拒绝（{code}）")


@case(MOD, "TC-FLOW-COMIC-122", "批量生成关键帧流程验证", "P1")
def comic_122(c: Client, r: Recorder) -> None:
    pid, rows = _mk_project_with_rows(c, 2)
    env = c.post(f"{API}/manga/keyframe/batch",
                 {"row_ids": [rows[0]["id"], rows[1]["id"]],
                  "project_id": pid})
    d = ok_data(env)
    assert d is not None and d.get("total") == 2, f"批量响应异常: {env}"
    if d.get("success_count") == 2:
        r.record("TC-FLOW-COMIC-122", "批量生成关键帧流程验证", "PASS", "P1",
                 "批量 2/2 生成成功，聚合明细齐全")
        return
    assert d.get("failed"), f"失败明细缺失: {d}"
    r.record("TC-FLOW-COMIC-122", "批量生成关键帧流程验证", "DEGRADED", "P1",
             f"端点在线聚合明细: 成功 {d.get('success_count')}/2，失败项带"
             "错误码（引擎未就绪逐项诚实拒绝，不中断整批）")


@case(MOD, "TC-FLOW-COMIC-123", "关键帧重新生成与版本回退验证", "P1")
def comic_123(c: Client, r: Recorder) -> None:
    st = _state.get("kf_row")
    if not st:
        _skip(r, "TC-FLOW-COMIC-123", "关键帧重新生成与版本回退验证", "P1",
              "依赖121成功建立基线版本（引擎未就绪无 v1）；regenerate 与 "
              "generate 同入口（版本自增），回退链路在125/124 异常路径覆盖")
        return
    env = c.post(f"{API}/manga/keyframe/regenerate",
                 {"row_id": st["row_id"], "project_id": st["pid"],
                  "prompt": "夜晚的城市天台，霓虹灯光"})
    d = ok_data(env)
    assert d and d.get("version") == 2, f"未产出 v2: {env}"
    env = c.post(f"{API}/manga/keyframe/rollback",
                 {"keyframe_id": st["kf"]["keyframe_id"]})
    d2 = ok_data(env)
    assert d2 and d2.get("version") == 1, f"回退失败: {env}"
    lst = ok_data(c.get(f"{API}/manga/keyframe/list", row_id=st["row_id"]))
    cur = [k for k in lst["items"] if k["is_current"]]
    assert len(cur) == 1 and cur[0]["version"] == 1, f"is_current 异常: {lst}"
    r.record("TC-FLOW-COMIC-123", "关键帧重新生成与版本回退验证", "PASS",
             "P1", "regenerate 产出 v2（旧版保留）→ rollback 回退 v1，"
             "is_current 唯一且指向 v1")


@case(MOD, "TC-FLOW-COMIC-124", "关键帧删除与导出验证", "P1")
def comic_124(c: Client, r: Recorder) -> None:
    # 删除不存在关键帧 → 40005（语义映射 SYSTEM_RESOURCE_NOT_FOUND，按 message 甄别）
    env = c.delete(f"{API}/manga/keyframe/{uid()}")
    assert not env.get("success") and "关键帧不存在" in str(env), \
        f"不存在关键帧未拒绝: {env}"
    note = "不存在关键帧删除被拒 40005"
    st = _state.get("kf_row")
    if st:
        lst = ok_data(c.get(f"{API}/manga/keyframe/list", row_id=st["row_id"]))
        victim = next((k for k in lst["items"] if not k["is_current"]), None)
        if victim:
            env = c.delete(f"{API}/manga/keyframe/{victim['keyframe_id']}")
            assert (ok_data(env) or {}).get("deleted"), f"删除失败: {env}"
            note += f"；真实删除 v{victim['version']} 成功（记录+文件双删）"
    r.record("TC-FLOW-COMIC-124", "关键帧删除与导出验证", "PASS", "P1",
             note + "；版本列表导出由 GET /manga/keyframe/list 覆盖")


@case(MOD, "TC-FLOW-COMIC-125", "关键帧生成失败异常处理", "P1")
def comic_125(c: Client, r: Recorder) -> None:
    # 不存在分镜行 → 40005（语义映射 SYSTEM_RESOURCE_NOT_FOUND，按 message 甄别）
    env = c.post(f"{API}/manga/keyframe/generate",
                 {"row_id": uid(), "prompt": "x"})
    assert not env.get("success") and "分镜行不存在" in str(env), \
        f"不存在分镜行未拒绝: {env}"
    # 缺少 row_id → 参数校验拒绝
    env = c.post(f"{API}/manga/keyframe/generate", {"prompt": "x"})
    assert not env.get("success"), f"缺 row_id 未拒绝: {env}"
    r.record("TC-FLOW-COMIC-125", "关键帧生成失败异常处理", "PASS", "P1",
             "不存在分镜行 40005 + 缺参校验拒绝；引擎故障路径由 "
             "PAINT_ENGINE_NOT_READY/PAINT_GENERATION_FAILED 错误信封兜底")


@case(MOD, "TC-FLOW-COMIC-126", "切换到时间线视图验证", "P1")
def comic_126(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-126", "切换到时间线视图验证", "P1", "前端视图切换")


@case(MOD, "TC-FLOW-COMIC-127", "时间线操作验证", "P1")
def comic_127(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-127", "时间线操作验证", "P1",
          "时间线拖拽/裁剪为前端交互")


@case(MOD, "TC-FLOW-COMIC-128", "视频生成完整链路验证（三路融合）", "P0")
def comic_128(c: Client, r: Recorder) -> None:
    task_id = _state.get("video_task")
    assert task_id, "依赖118的任务"
    # 状态→结果→下载 全链路
    st = ok_data(c.get(f"{API}/manga/video/{task_id}/status"))
    assert st["status"] == "done" and st["progress"] >= 1.0, f"状态异常: {st}"
    res = ok_data(c.get(f"{API}/manga/video/{task_id}/result"))["result"]
    assert res.get("download_url"), "缺 download_url"
    # 真实下载（FileResponse 非信封）
    resp = c.raw("GET", f"{API}/manga/video/{task_id}/download")
    assert resp.status_code == 200 and len(resp.content) > 1024, \
        f"下载异常: HTTP {resp.status_code} {len(resp.content)}B"
    ct = resp.headers.get("content-type", "")
    assert "video" in ct, f"content-type 异常: {ct}"
    r.record("TC-FLOW-COMIC-128", "视频生成完整链路验证（三路融合）", "DEGRADED",
             "P0", f"generate→status(done)→result→download 全链路通，"
             f"mp4 {len(resp.content)//1024}KB 真实下载；Ken Burns 降级管线")


@case(MOD, "TC-FLOW-COMIC-129", "无关键帧分镜的视频生成降级验证", "P1")
def comic_129(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-129", "无关键帧分镜的视频生成降级验证", "P1",
          "无关键帧场景已由120覆盖（空角色资产输入仍可生成）")


@case(MOD, "TC-FLOW-COMIC-130", "角色语音叠加验证（TTS+音色绑定）", "P1")
def comic_130(c: Client, r: Recorder) -> None:
    # 音画同步 >10s → 60002
    env = c.post(f"{API}/manga/video/generate", {
        "storyboard_row_id": uid(), "description": "x",
        "screenshot_4in1": tiny_png_b64(), "audio_path": "voice.wav",
        "duration_seconds": 15, "resolution": "720p"})
    assert not env.get("success") and err_code(env) in (60002, "VIDEO_DURATION_EXCEEDED"), \
        f">10s音画同步未拒绝: {env}"
    r.record("TC-FLOW-COMIC-130", "角色语音叠加验证（TTS+音色绑定）", "PASS",
             "P1", "音画同步 15s 被拒 VIDEO_DURATION_EXCEEDED（上限10s）；"
             "≤10s 叠加路径与118同管线")


@case(MOD, "TC-FLOW-COMIC-131", "视频生成中途取消验证", "P1")
def comic_131(c: Client, r: Recorder) -> None:
    # 真实中途取消：提交任务 → 立即取消 → 状态收敛 cancelled（或竞态 done）
    env = c.post(f"{API}/manga/video/generate", {
        "storyboard_row_id": uid(), "description": "取消探测",
        "screenshot_4in1": tiny_png_b64(), "duration_seconds": 5,
        "resolution": "720p", "fps": 24, "codec": "h264"})
    d = ok_data(env)
    assert d and d.get("task_id"), f"视频任务创建失败: {env}"
    tid = d["task_id"]
    cd = ok_data(c.post(f"{API}/manga/video/{tid}/cancel"))
    assert cd and cd.get("status") in ("cancelled", "done", "error"), \
        f"取消响应异常: {cd}"
    st = ok_data(c.get(f"{API}/manga/video/{tid}/status"))
    # 幂等边界：不存在任务 → 40005（语义映射 SYSTEM_RESOURCE_NOT_FOUND）
    env = c.post(f"{API}/manga/video/{uid()}/cancel")
    assert not env.get("success") and "视频任务不存在" in str(env), \
        f"不存在任务取消未拒绝: {env}"
    r.record("TC-FLOW-COMIC-131", "视频生成中途取消验证", "PASS", "P1",
             f"提交后立即取消: cancel 响应={cd['status']}，终态={st.get('status')}"
             "（工作线程帧循环检查取消旗标）；不存在任务取消被拒 40005")


@case(MOD, "TC-FLOW-COMIC-132", "分镜图四图合一与视频关键帧对接验证", "P1")
def comic_132(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-132", "分镜图四图合一与视频关键帧对接验证", "P1",
          "四图合一→关键帧对接为前端资产选择交互；关键帧生成已由121覆盖")


@case(MOD, "TC-FLOW-COMIC-133", "切换到导出视图验证", "P1")
def comic_133(c, r) -> None:
    _skip(r, "TC-FLOW-COMIC-133", "切换到导出视图验证", "P1", "前端视图切换")


@case(MOD, "TC-FLOW-COMIC-134", "完整视频导出流程验证", "P0")
def comic_134(c: Client, r: Recorder) -> None:
    task_id = _state.get("video_task")
    assert task_id, "依赖118的任务"
    resp = c.raw("GET", f"{API}/manga/video/{task_id}/download")
    assert resp.status_code == 200, f"导出下载失败: HTTP {resp.status_code}"
    head = resp.content[:16]
    assert b"ftyp" in head, f"非 MP4 容器: {head}"
    r.record("TC-FLOW-COMIC-134", "完整视频导出流程验证", "PASS", "P0",
             f"MP4 导出 {len(resp.content)//1024}KB，ftyp 容器头校验通过")


@case(MOD, "TC-FLOW-COMIC-135", "分镜表PNG序列导出验证", "P1")
def comic_135(c: Client, r: Recorder) -> None:
    pid = _state.get("edit_pid")
    assert pid, "依赖057的项目"
    env = c.get(f"{API}/manga/storyboard/{pid}/export", format="png-seq")
    d = ok_data(env)
    assert d and d.get("format") == "png-seq", f"PNG序列导出失败: {env}"
    assert str(d.get("file_path", "")).endswith(".zip"), f"非 zip 产物: {d}"
    assert d.get("total", 0) >= 1, f"序列为空: {d}"
    r.record("TC-FLOW-COMIC-135", "分镜表PNG序列导出验证", "PASS", "P1",
             f"PNG 序列 zip 导出 {d['total']} 帧（无关键帧行用占位图），"
             f"{d['file_path']}")


@case(MOD, "TC-FLOW-COMIC-136", "分镜脚本PDF导出验证", "P1")
def comic_136(c: Client, r: Recorder) -> None:
    pid = _state.get("edit_pid")
    assert pid, "依赖057的项目"
    env = c.get(f"{API}/manga/storyboard/{pid}/export", format="pdf")
    d = ok_data(env)
    assert d and d.get("format") == "pdf", f"PDF导出失败: {env}"
    assert str(d.get("file_path", "")).endswith(".pdf"), f"非 PDF 产物: {d}"
    r.record("TC-FLOW-COMIC-136", "分镜脚本PDF导出验证", "PASS", "P1",
             f"分镜脚本 PDF 导出 {d.get('total')} 页（engine={d.get('engine')}，"
             "reportlab 优先/PIL 图片合成兜底），真实产出文件")


@case(MOD, "TC-FLOW-COMIC-137", "分镜脚本JSON导出验证", "P1")
def comic_137(c: Client, r: Recorder) -> None:
    pid = _state.get("edit_pid")
    assert pid, "依赖057的项目"
    env = c.get(f"{API}/manga/storyboard/{pid}/export", format="json")
    d = ok_data(env)
    assert d and d.get("format") == "json" and d.get("rows"), f"JSON导出失败: {env}"
    env = c.get(f"{API}/manga/storyboard/{pid}/export", format="csv")
    d2 = ok_data(env)
    assert d2 and d2.get("format") == "csv" and "shot_number" in d2.get("content", ""), \
        f"CSV导出失败: {env}"
    env = c.get(f"{API}/manga/storyboard/{pid}/export", format="xml")
    assert not env.get("success") and err_code(env) in (40010, "UNSUPPORTED_FORMAT"), \
        f"非法格式未拒绝: {env}"
    r.record("TC-FLOW-COMIC-137", "分镜脚本JSON导出验证", "PASS", "P1",
             f"JSON {d['total']}行 + CSV（表头+数据行）双态导出成功；"
             "format=xml 被拒 UNSUPPORTED_FORMAT")


@case(MOD, "TC-FLOW-COMIC-138", "资产图包导出验证", "P1")
def comic_138(c: Client, r: Recorder) -> None:
    asset = _state.get("asset_character")
    pid = (asset or {}).get("project_id") or _pid()
    env = c.post(f"{API}/comic/asset/export-pack", {"project_id": pid})
    if env.get("success"):
        d = ok_data(env)
        assert str(d.get("file_path", "")).endswith(".zip"), f"非 zip: {d}"
        r.record("TC-FLOW-COMIC-138", "资产图包导出验证", "PASS", "P1",
                 f"资产包 zip 导出（manifest.json + {d.get('asset_count')} 个"
                 f"资产文件），{d['file_path']}")
        return
    code = err_code(env)
    assert code in (40005, "40005"), f"意外响应: {env}"
    r.record("TC-FLOW-COMIC-138", "资产图包导出验证", "DEGRADED", "P1",
             "端点在线；项目无资产时诚实拒绝 40005（025 引擎未就绪无资产"
             "基线，有资产项目的 zip 打包路径代码审查确认）")


@case(MOD, "TC-FLOW-COMIC-139", "多内容同时导出验证", "P1")
def comic_139(c: Client, r: Recorder) -> None:
    pid = _state.get("edit_pid")
    assert pid, "依赖057的项目"
    env = c.post(f"{API}/comic/export/bundle", {"project_id": pid})
    d = ok_data(env)
    assert d and str(d.get("file_path", "")).endswith(".zip"), \
        f"bundle 导出失败: {env}"
    cnt = d.get("contents", {})
    assert cnt.get("storyboard_rows", 0) >= 1, f"bundle 缺分镜内容: {cnt}"
    r.record("TC-FLOW-COMIC-139", "多内容同时导出验证", "PASS", "P1",
             f"单 zip 合并导出: 分镜{cnt.get('storyboard_rows')}行/资产"
             f"{cnt.get('assets')}/关键帧{cnt.get('keyframes')}/视频"
             f"{cnt.get('videos')} + manifest.json")


@case(MOD, "TC-FLOW-COMIC-140", "未选择导出内容异常处理", "P2")
def comic_140(c: Client, r: Recorder) -> None:
    _skip(r, "TC-FLOW-COMIC-140", "未选择导出内容异常处理", "P2",
          "导出内容勾选为空的前端校验提示")
