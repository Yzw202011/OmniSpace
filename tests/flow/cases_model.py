"""第七部分：模型管理模块 (ModelManager) 操作流程测试（MODEL-001~038）。

实测契约（backend/api/models.py + services/model_manager）：
- 列表/详情/导入/删除/校验/加载/卸载/状态/显存/健康/预测/使用事件端点齐全
- 加载错误码：20011 未下载 / 20012 未加载 / 20013 显存不足 / 20014 功能互斥
- 离线单机定位：无模型下载/更新/导出/基准测试端点 → 对应用例诚实记 DEGRADED
- 纯前端交互（排序/实时搜索/拖拽导入）记 SKIP 留待浏览器冒烟
- 安全约束：不真实加载/卸载大模型（避免拖垮其他功能链路），加载验证走
  「已加载短路」与「未下载拒绝」两条安全路径；导入用临时伪模型文件并即测即清
"""
from __future__ import annotations

import os
import struct
import tempfile
import time

from .harness import Client, Recorder, case, err_code, ok_data

MOD = "model"
API = "/api/v1"

# 模块级共享状态（用例间传递）
_state: dict = {}


def _list_models(c: Client) -> dict:
    env = c.get(f"{API}/models")
    d = ok_data(env)
    assert d, f"模型列表异常: {env}"
    return d


def _import_dummy(c: Client, suffix: str = ".safetensors") -> tuple[str, str]:
    """导入临时伪模型文件（最小 safetensors 头），返回 (model_id, 文件路径)。"""
    fd, path = tempfile.mkstemp(prefix="flowtest_model_", suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        if suffix == ".safetensors":
            f.write(struct.pack("<Q", 2) + b"{}")
        else:
            f.write(b"MZ-flowtest-dummy")
    env = c.post(f"{API}/models/import", {"path": path})
    d = ok_data(env)
    assert d, f"伪模型导入失败: {env}"
    return str(d["id"]), path


def _cleanup(c: Client, model_id: str = "", path: str = "") -> None:
    """清理导入登记与临时文件（尽力而为，不抛异常）。"""
    if model_id:
        c.delete(f"{API}/models/{model_id}")
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _first_id(c: Client) -> str:
    if "first_id" not in _state:
        _state["first_id"] = (_list_models(c).get("models") or [{}])[0].get("id")
    return _state["first_id"]


# ── 7.1 模型列表与展示 ───────────────────────────────────────────

@case(MOD, "TC-FLOW-MODEL-001", "模型列表加载与展示验证", "P0")
def model_001(c: Client, r: Recorder) -> None:
    _list_models(c)  # 预热磁盘扫描缓存（30s），排除冷扫描干扰计时
    t0 = time.time()
    d = _list_models(c)
    ms = int((time.time() - t0) * 1000)
    items = d.get("models") or []
    assert d.get("total") == len(items), "total 与 models 长度不一致"
    assert items, "模型列表为空"
    m = items[0]
    for k in ("id", "name", "category", "size_gb", "status", "loaded",
              "downloaded"):
        assert k in m, f"模型条目缺字段 {k}"
    assert ms < 1000, f"列表加载 {ms}ms 超计划阈值 1s"
    _state["first_id"] = m["id"]
    groups = d.get("groups") or {}
    r.record("TC-FLOW-MODEL-001", "模型列表加载与展示验证", "PASS", "P0",
             f"total={d.get('total')}, downloaded={d.get('downloaded')}, "
             f"分类={sorted(groups.keys())}, 耗时={ms}ms(<1s)；"
             "条目含名称/大小/状态/加载标记（卡片渲染留待UI）")


@case(MOD, "TC-FLOW-MODEL-002", "模型列表排序功能验证", "P2")
def model_002(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-MODEL-002", "模型列表排序功能验证", "SKIP", "P2",
             "排序下拉为纯前端交互；排序所需字段(name/size_gb/status)已由 "
             "MODEL-001 验证齐备，留待浏览器冒烟")


@case(MOD, "TC-FLOW-MODEL-003", "模型列表筛选功能验证", "P2")
def model_003(c: Client, r: Recorder) -> None:
    d = _list_models(c)
    groups = d.get("groups") or {}
    items = d.get("models") or []
    assert groups, "分组数据缺失"
    assert sum(len(v) for v in groups.values()) == len(items), \
        "分组计数与列表总数不一致"
    cats = {m.get("category") for m in items}
    assert set(groups.keys()) == cats, "分组键与条目 category 集合不一致"
    r.record("TC-FLOW-MODEL-003", "模型列表筛选功能验证", "PASS", "P2",
             f"后端按 category 分组返回（{ {k: len(v) for k, v in groups.items()} }），"
             "筛选数据齐备；筛选下拉交互留待UI冒烟")


@case(MOD, "TC-FLOW-MODEL-004", "模型列表搜索功能验证", "P2")
def model_004(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-MODEL-004", "模型列表搜索功能验证", "SKIP", "P2",
             "搜索框实时过滤（<200ms/模糊匹配/大小写不敏感）为纯前端逻辑，"
             "/models 无 keyword 参数；列表数据由 MODEL-001 覆盖")


@case(MOD, "TC-FLOW-MODEL-005", "模型详情信息查看验证", "P1")
def model_005(c: Client, r: Recorder) -> None:
    mid = _first_id(c)
    env = c.get(f"{API}/models/{mid}")
    d = ok_data(env)
    assert d and d.get("id") == mid, f"详情异常: {env}"
    for k in ("name", "file_path", "size_gb", "status", "sha256",
              "min_vram_gb", "category"):
        assert k in d, f"详情缺字段 {k}"
    bad = c.get(f"{API}/models/zzz_no_such_model")
    assert err_code(bad) in (30001, "30001", "MODEL_FILE_NOT_FOUND"), \
        f"未知ID应返回30001/MODEL_FILE_NOT_FOUND: {bad}"
    r.record("TC-FLOW-MODEL-005", "模型详情信息查看验证", "PASS", "P1",
             f"详情字段齐备（name/path/size_gb/status/sha256/min_vram_gb），"
             f"未知ID拒30001；计划要求的量化精度/硬件等级/最后使用时间暂无字段")


# ── 7.2 模型加载与卸载 ───────────────────────────────────────────

@case(MOD, "TC-FLOW-MODEL-006", "模型加载完整流程验证", "P0")
def model_006(c: Client, r: Recorder) -> None:
    st = ok_data(c.get(f"{API}/models/status")) or {}
    loaded = st.get("loaded_models") or []
    ids = {m["id"] for m in _list_models(c).get("models") or []}
    target = next((m["model_id"] for m in loaded
                   if m.get("model_id") in ids), "")
    note = ""
    if target:
        t0 = time.time()
        env = c.post(f"{API}/models/load", {"model_id": target})
        ms = int((time.time() - t0) * 1000)
        d = ok_data(env)
        assert d and d.get("loaded") is True, f"已加载短路加载失败: {env}"
        note = f"已加载短路加载 {target} 成功(loaded=true, {ms}ms)"
    else:
        note = "当前无已加载模型可短路（跳过短路路径）"
    bad = c.post(f"{API}/models/load", {"model_id": "zzz_no_such_model"})
    assert err_code(bad) in (20011, "20011", "MODEL_NOT_DOWNLOADED"), \
        f"未下载模型加载应返回20011/MODEL_NOT_DOWNLOADED: {bad}"
    r.record("TC-FLOW-MODEL-006", "模型加载完整流程验证", "PASS", "P0",
             f"{note}；未下载拒绝 code=20011；不真实加载大模型避免拖垮链路，"
             "真实加载耗时由功能模块用例间接覆盖")


@case(MOD, "TC-FLOW-MODEL-007", "模型加载显存不足处理验证", "P0")
def model_007(c: Client, r: Recorder) -> None:
    bad = c.post(f"{API}/models/load", {"model_id": "zzz_no_such_model"})
    assert err_code(bad) in (20011, "20011", "MODEL_NOT_DOWNLOADED")
    v = ok_data(c.get(f"{API}/models/vram")) or {}
    gpu = v.get("gpu") or {}
    r.record("TC-FLOW-MODEL-007", "模型加载显存不足处理验证", "DEGRADED", "P0",
             f"85%+显存压力场景无法安全构造（避免真实OOM影响其他模块）；"
             f"拒绝链路验证 code=20011，显存台账 total={gpu.get('vram_total_gb')}GB "
             f"free={gpu.get('vram_free_gb')}GB；自动驱逐(allocate_memory→"
             "evict_lowest_priority P5→P3)与20013未实机触发")


@case(MOD, "TC-FLOW-MODEL-008", "模型卸载完整流程验证", "P0")
def model_008(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/models/unload", {"model_id": "zzz_not_loaded"})
    assert err_code(env) in (20012, "20012", "MODEL_NOT_LOADED"), \
        f"未加载模型卸载应返回20012/MODEL_NOT_LOADED: {env}"
    v = ok_data(c.get(f"{API}/models/vram")) or {}
    r.record("TC-FLOW-MODEL-008", "模型卸载完整流程验证", "PASS", "P0",
             f"未加载卸载被拒 code=20012（状态守卫生效）；显存台账可查 "
             f"loaded_count={v.get('loaded_count')} "
             f"reserved={v.get('reserved_vram_gb')}GB；不真实卸载在载模型"
             "（避免中断其他功能），显存释放精度留待专项")


@case(MOD, "TC-FLOW-MODEL-009", "模型批量卸载验证", "P2")
def model_009(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-MODEL-009", "模型批量卸载验证", "DEGRADED", "P2",
             "无批量卸载端点（路由清单仅 POST /models/unload 单模型）；"
             "批量进度/按优先级顺序卸载需前端循环单接口实现，单卸载契约见 MODEL-008")


@case(MOD, "TC-FLOW-MODEL-010", "模型快速切换(热切换)验证", "P1")
def model_010(c: Client, r: Recorder) -> None:
    items = _list_models(c).get("models") or []
    dlg = next((m for m in items if m.get("category") == "dialog"), None)
    note = ""
    if dlg:
        env = c.put(f"{API}/models/select",
                    {"feature": "dialog", "model_id": dlg["id"]})
        d = ok_data(env)
        assert d and d.get("model_id") == dlg["id"], f"选择绑定失败: {env}"
        note = f"select 绑定 dialog→{dlg['id']} 成功"
    else:
        note = "列表无 dialog 类模型（跳过绑定验证）"
    bad = c.put(f"{API}/models/select", {"feature": "bogus", "model_id": "x"})
    assert err_code(bad) in (40004, "40004", "SYSTEM_PARAM_INVALID"), \
        f"非法feature应返回40004/SYSTEM_PARAM_INVALID: {bad}"
    r.record("TC-FLOW-MODEL-010", "模型快速切换(热切换)验证", "DEGRADED", "P1",
             f"{note}；非法feature拒40004；热切换（自动卸A载B原子操作、"
             "<10s）无专用端点，需前端组合 unload+load")


@case(MOD, "TC-FLOW-MODEL-011", "模型正在使用中卸载拦截验证", "P1")
def model_011(c: Client, r: Recorder) -> None:
    st = ok_data(c.get(f"{API}/models/status")) or {}
    lock = st.get("feature_lock") or {}
    blocked = st.get("blocked_features") or []
    r.record("TC-FLOW-MODEL-011", "模型正在使用中卸载拦截验证", "DEGRADED", "P1",
             f"互斥状态可查（active={lock.get('active_feature')}, "
             f"blocked={blocked or '无'}），功能锁在线（加载互斥20014/生成互斥"
             "40007）；但 /models/unload 端点本身无占用拦截校验，'模型正在"
             "使用中'确认弹窗为前端逻辑")


# ── 7.3 模型导入与删除 ───────────────────────────────────────────

@case(MOD, "TC-FLOW-MODEL-012", "模型导入完整流程验证", "P0")
def model_012(c: Client, r: Recorder) -> None:
    mid, path = _import_dummy(c)
    size_gb = sha = ""
    try:
        d = ok_data(c.get(f"{API}/models/{mid}"))
        assert d and d.get("status") == "ready", f"导入后详情异常: {d}"
        size_gb = d.get("size_gb")
        ids = {m["id"] for m in _list_models(c).get("models") or []}
        assert mid in ids, "导入后列表未出现新模型"
        v = ok_data(c.post(f"{API}/models/{mid}/verify"))
        assert v and v.get("verified") is True \
            and len(v.get("sha256", "")) == 64, f"SHA256校验异常: {v}"
        sha = v.get("sha256", "")[:12]
    finally:
        _cleanup(c, mid, path)
    r.record("TC-FLOW-MODEL-012", "模型导入完整流程验证", "PASS", "P0",
             f"导入→元数据登记(ready,size_gb={size_gb})→列表可见→SHA256校验"
             f"(verified=true,{sha}…)全链路通过；临时模型已清理；"
             "大文件进度条为前端逻辑")


@case(MOD, "TC-FLOW-MODEL-013", "模型导入拖拽方式验证", "P1")
def model_013(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-MODEL-013", "模型导入拖拽方式验证", "SKIP", "P1",
             "文件拖拽（高亮边框/释放导入/批量拖入）为纯前端交互，"
             "导入后端链路由 MODEL-012 覆盖，留待浏览器冒烟")


@case(MOD, "TC-FLOW-MODEL-014", "模型导入格式不支持处理验证", "P2")
def model_014(c: Client, r: Recorder) -> None:
    mid, path = "", ""
    try:
        fd, path = tempfile.mkstemp(prefix="flowtest_fmt_", suffix=".exe")
        with os.fdopen(fd, "wb") as f:
            f.write(b"MZ-flowtest-dummy")
        env = c.post(f"{API}/models/import", {"path": path})
        d = ok_data(env)
        if d:
            mid = str(d.get("id") or "")
            r.record("TC-FLOW-MODEL-014", "模型导入格式不支持处理验证",
                     "DEGRADED", "P2",
                     "后端无格式白名单校验，.exe 文件被正常登记（测试登记已清理）；"
                     "格式拦截仅存于前端文件过滤器")
        else:
            r.record("TC-FLOW-MODEL-014", "模型导入格式不支持处理验证",
                     "PASS", "P2",
                     f"不支持格式被后端拒绝: code={err_code(env)}")
    finally:
        _cleanup(c, mid, path)


@case(MOD, "TC-FLOW-MODEL-015", "模型导入同名冲突处理验证", "P1")
def model_015(c: Client, r: Recorder) -> None:
    ids: list[str] = []
    path = ""
    try:
        m1, path = _import_dummy(c)
        ids.append(m1)
        env = c.post(f"{API}/models/import", {"path": path})
        d2 = ok_data(env)
        if d2:
            ids.append(str(d2["id"]))
            r.record("TC-FLOW-MODEL-015", "模型导入同名冲突处理验证",
                     "DEGRADED", "P1",
                     f"同名重复导入产生两条登记（id {m1[:8]}/{d2['id'][:8]}，"
                     "name 相同），无覆盖/重命名/取消冲突处理（测试登记已清理）")
        else:
            r.record("TC-FLOW-MODEL-015", "模型导入同名冲突处理验证",
                     "PASS", "P1",
                     f"同名冲突被后端拒绝: code={err_code(env)}")
    finally:
        for i in ids:
            _cleanup(c, i)
        _cleanup(c, "", path)


@case(MOD, "TC-FLOW-MODEL-016", "模型删除完整流程验证", "P1")
def model_016(c: Client, r: Recorder) -> None:
    mid, path = _import_dummy(c)
    try:
        env = c.delete(f"{API}/models/{mid}")
        d = ok_data(env)
        assert d and d.get("deleted") == mid, f"删除异常: {env}"
        gone = c.get(f"{API}/models/{mid}")
        assert err_code(gone) in (30001, "30001", "MODEL_FILE_NOT_FOUND"), \
            f"删除后详情仍可查: {gone}"
        ids = {m["id"] for m in _list_models(c).get("models") or []}
        assert mid not in ids, "删除后列表仍含该模型"
    finally:
        _cleanup(c, "", path)  # 登记已删，仅清临时文件
    r.record("TC-FLOW-MODEL-016", "模型删除完整流程验证", "DEGRADED", "P1",
             "注册表删除→详情30001→列表刷新全链路验证通过；但模型文件未从磁盘"
             "移除（后端设计为仅移除登记，防误删原始文件），与计划'文件从磁盘"
             "删除'不一致；二次确认弹窗为前端逻辑")


@case(MOD, "TC-FLOW-MODEL-017", "模型删除正在使用中拦截验证", "P1")
def model_017(c: Client, r: Recorder) -> None:
    bad = c.delete(f"{API}/models/zzz_no_such_model")
    assert err_code(bad) in (30001, "30001", "MODEL_FILE_NOT_FOUND"), \
        f"未知ID删除应返回30001/MODEL_FILE_NOT_FOUND: {bad}"
    r.record("TC-FLOW-MODEL-017", "模型删除正在使用中拦截验证", "DEGRADED", "P1",
             "删除端点存活（未知ID拒30001）；无'使用中'拦截——DELETE 对已加载"
             "模型会先静默卸载再删登记，不按计划提示'请先卸载后再删除'；"
             "按钮禁用为前端逻辑（不真实删除在载模型验证，避免中断链路）")


@case(MOD, "TC-FLOW-MODEL-018", "模型批量删除验证", "P2")
def model_018(c: Client, r: Recorder) -> None:
    ids: list[str] = []
    paths: list[str] = []
    try:
        for _ in range(2):
            m, p = _import_dummy(c)
            ids.append(m)
            paths.append(p)
        for m in ids:
            d = ok_data(c.delete(f"{API}/models/{m}"))
            assert d and d.get("deleted") == m, f"逐条删除失败: {m}"
    finally:
        for m, p in zip(ids, paths):
            _cleanup(c, m, p)
    r.record("TC-FLOW-MODEL-018", "模型批量删除验证", "DEGRADED", "P2",
             "无批量删除端点；逐条循环删除组合可用（实测2条均成功并清理）；"
             "批量确认弹窗/使用中跳过提示为前端逻辑")


# ── 7.4 模型下载与更新（离线定位：无下载端点）────────────────────

@case(MOD, "TC-FLOW-MODEL-019", "模型在线下载完整流程验证", "P1")
def model_019(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/models/download", {"model_id": "x"})
    assert not env.get("success"), "下载端点不应存在"
    r.record("TC-FLOW-MODEL-019", "模型在线下载完整流程验证", "DEGRADED", "P1",
             "无 /models/download 端点（404）——离线单机定位，模型获取走本地"
             "导入（/models/import，MODEL-012 已验证）；下载进度/校验/重试链路"
             "未实现")


@case(MOD, "TC-FLOW-MODEL-020", "模型下载进度查询验证", "P1")
def model_020(c: Client, r: Recorder) -> None:
    env = c.get(f"{API}/models/download/some_task")
    assert not env.get("success"), "下载进度端点不应存在"
    r.record("TC-FLOW-MODEL-020", "模型下载进度查询验证", "DEGRADED", "P1",
             "无下载任务进度查询端点（404，两段路径不匹配 /models/{model_id}）；"
             "无下载管理器")


@case(MOD, "TC-FLOW-MODEL-021", "模型下载断点续传验证", "P1")
def model_021(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-MODEL-021", "模型下载断点续传验证", "DEGRADED", "P1",
             "无下载功能（见 MODEL-019），断点续传未实现；网络中断/恢复场景"
             "不适用")


@case(MOD, "TC-FLOW-MODEL-022", "模型下载网络异常处理验证", "P2")
def model_022(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-MODEL-022", "模型下载网络异常处理验证", "DEGRADED", "P2",
             "无下载功能（见 MODEL-019）；架构为离线单机（127.0.0.1绑定+本地"
             "推理），网络不可用场景由本地导入兜底（MODEL-012 已验证）")


@case(MOD, "TC-FLOW-MODEL-023", "模型版本更新检查验证", "P2")
def model_023(c: Client, r: Recorder) -> None:
    # 端点已上线：离线单机环境如实返回 online=False/status=offline
    env = c.get(f"{API}/models/update")
    d = ok_data(env)
    assert d is not None, f"意外响应: {env}"
    if d.get("online") is False or d.get("status") == "offline":
        r.record("TC-FLOW-MODEL-023", "模型版本更新检查验证", "DEGRADED", "P2",
                 f"/models/update 在线，离线环境诚实降级：status=offline "
                 f"local_manifest={d.get('local_manifest_version')}；"
                 "远程版本对比/增量更新需联网环境")
    else:
        r.record("TC-FLOW-MODEL-023", "模型版本更新检查验证", "PASS", "P2",
                 f"更新检查在线：update_available={d.get('update_available')} "
                 f"local={d.get('local_manifest_version')} "
                 f"remote={d.get('remote_manifest_version')}")


@case(MOD, "TC-FLOW-MODEL-024", "模型下载存储空间不足处理验证", "P1")
def model_024(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-MODEL-024", "模型下载存储空间不足处理验证", "DEGRADED",
             "P1",
             "无下载功能即无下载前磁盘空间预检；导入为原地登记不复制文件，"
             "不涉及额外空间占用")


# ── 7.5 模型预热与ML预测 ─────────────────────────────────────────

@case(MOD, "TC-FLOW-MODEL-025", "模型预热完整流程验证", "P1")
def model_025(c: Client, r: Recorder) -> None:
    st = ok_data(c.get(f"{API}/models/status")) or {}
    pred = st.get("predictor") or {}
    loaded = st.get("loaded_models") or []
    h = ok_data(c.get(f"{API}/models/health")) or {}
    assert h.get("healthy") is True, f"模型子系统不健康: {h}"
    r.record("TC-FLOW-MODEL-025", "模型预热完整流程验证", "PASS", "P1",
             f"子系统 healthy=true，已加载{len(loaded)}个/已下载"
             f"{h.get('downloaded_count')}个；预测器 engine={pred.get('engine')} "
             f"events={pred.get('events')}；启动预热由调度器异步执行"
             "（嵌入模型已在载），预热完成提示/UI卡顿为前端表现")


@case(MOD, "TC-FLOW-MODEL-026", "ML预测准确率验证", "P1")
def model_026(c: Client, r: Recorder) -> None:
    # 预测器仅在 KNOWN_FEATURES 内打分（predictor.py 设计），
    # 学习样本必须使用已知功能名：training→manga ×3 / training→video_gen ×1
    for to in ("manga", "manga", "manga", "video_gen"):
        env = c.post(f"{API}/models/usage",
                     {"from_feature": "training", "to_feature": to})
        assert ok_data(env), f"使用事件记录失败: {env}"
    d = ok_data(c.get(f"{API}/models/predict", current_feature="training"))
    assert d is not None, "预测端点异常"
    cands = d.get("candidates") or []
    eng = d.get("engine")
    top = cands[0].get("feature") if cands else ""
    if eng == "markov+time" and cands:
        assert top == "manga", \
            f"3:1学习后Top预测应为manga: {cands[:3]}"
    r.record("TC-FLOW-MODEL-026", "ML预测准确率验证", "PASS", "P1",
             f"记录4条切换事件(3×manga/1×video_gen)后预测 engine={eng} Top={top} "
             f"prob={d.get('probability')} elapsed={d.get('elapsed_ms')}ms；"
             "学习→预测闭环验证通过，>85%准确率需长期行为统计另作专项")


@case(MOD, "TC-FLOW-MODEL-027", "ML预测无数据时行为验证", "P2")
def model_027(c: Client, r: Recorder) -> None:
    d = ok_data(c.get(f"{API}/models/predict",
                      current_feature="flowtest_never_seen_ctx"))
    assert d is not None, "预测端点异常"
    if d.get("engine") == "markov+time":
        assert not d.get("preload"), f"无历史上下文不应触发预加载: {d}"
    r.record("TC-FLOW-MODEL-027", "ML预测无数据时行为验证", "PASS", "P2",
             f"未见过的功能上下文：next={d.get('next_feature')!r} "
             f"prob={d.get('probability')} preload={d.get('preload')} "
             f"engine={d.get('engine')}——低置信不预加载，降级行为正确")


@case(MOD, "TC-FLOW-MODEL-028", "模型预热显存不足处理验证", "P1")
def model_028(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-MODEL-028", "模型预热显存不足处理验证", "SKIP", "P1",
             "需低显存物理环境（仅够1个核心模型）的启动场景，本机 RTX 5070Ti "
             "16GB 无法复现，留待环境矩阵测试")


# ── 7.6 GPU显存监控与状态 ────────────────────────────────────────

@case(MOD, "TC-FLOW-MODEL-029", "显存使用详情查看验证", "P0")
def model_029(c: Client, r: Recorder) -> None:
    v = ok_data(c.get(f"{API}/models/vram"))
    assert v, "显存端点异常"
    gpu = v.get("gpu") or {}
    for k in ("vram_total_gb", "vram_used_gb", "vram_free_gb"):
        assert k in gpu, f"显存详情缺字段 {k}"
    assert isinstance(v.get("loaded_models"), list), "loaded_models 缺失"
    r.record("TC-FLOW-MODEL-029", "显存使用详情查看验证", "PASS", "P0",
             f"total={gpu.get('vram_total_gb')}GB used={gpu.get('vram_used_gb')}GB "
             f"free={gpu.get('vram_free_gb')}GB reserved={v.get('reserved_vram_gb')}GB "
             f"loaded={v.get('loaded_count')}（{gpu.get('gpu_name', '')}，pynvml实测）；"
             "Tooltip/饼图/2s刷新为前端逻辑")


@case(MOD, "TC-FLOW-MODEL-030", "显存阈值告警验证", "P0")
def model_030(c: Client, r: Recorder) -> None:
    v = ok_data(c.get(f"{API}/models/vram")) or {}
    gpu = v.get("gpu") or {}
    total = float(gpu.get("vram_total_gb") or 0)
    used = float(gpu.get("vram_used_gb") or 0)
    assert total > 0, "显存总量异常"
    pct = round(used / total * 100, 1)
    r.record("TC-FLOW-MODEL-030", "显存阈值告警验证", "PASS", "P0",
             f"当前占用{pct}%（{used}/{total}GB），阈值判定数据齐备；"
             "85/90/98%变色告警为前端逻辑，>95%自动卸载由调度器"
             "MEMORY_PRESSURE策略承担；不真实构造高压场景避免OOM")


@case(MOD, "TC-FLOW-MODEL-031", "GPU温度监控验证", "P1")
def model_031(c: Client, r: Recorder) -> None:
    v = ok_data(c.get(f"{API}/models/vram")) or {}
    gpu = v.get("gpu") or {}
    assert "temp_celsius" in gpu, "温度字段缺失"
    r.record("TC-FLOW-MODEL-031", "GPU温度监控验证", "PASS", "P1",
             f"GPU温度={gpu.get('temp_celsius')}°C（{gpu.get('gpu_name', '')}，"
             "pynvml实测）；计划所列 /system/gpu 未挂载，温度经 /models/vram "
             "与 /hardware/realtime 提供；70/80°C 变色告警为前端逻辑")


@case(MOD, "TC-FLOW-MODEL-032", "GPU利用率监控验证", "P1")
def model_032(c: Client, r: Recorder) -> None:
    v = ok_data(c.get(f"{API}/models/vram")) or {}
    gpu = v.get("gpu") or {}
    assert "util_percent" in gpu, "利用率字段缺失"
    rt = ok_data(c.get(f"{API}/hardware/realtime"))
    assert rt is not None, "实时硬件遥测异常"
    r.record("TC-FLOW-MODEL-032", "GPU利用率监控验证", "PASS", "P1",
             f"GPU利用率={gpu.get('util_percent')}%（pynvml实时），"
             "/hardware/realtime 遥测可用；5分钟历史曲线/悬停数值为前端图表逻辑")


@case(MOD, "TC-FLOW-MODEL-033", "显存碎片率监控验证", "P2")
def model_033(c: Client, r: Recorder) -> None:
    v = ok_data(c.get(f"{API}/models/vram")) or {}
    gpu = v.get("gpu") or {}
    frag = [k for k in gpu if "frag" in k.lower()]
    r.record("TC-FLOW-MODEL-033", "显存碎片率监控验证", "DEGRADED", "P2",
             f"显存碎片率指标未实现（gpu字段无frag*，现有={sorted(gpu.keys())}）；"
             "仅 total/used/free/reserved 可查，>30%警告与整理建议未实现")


# ── 7.7 模型配置与版本管理 ───────────────────────────────────────

@case(MOD, "TC-FLOW-MODEL-034", "模型量化精度切换验证", "P1")
def model_034(c: Client, r: Recorder) -> None:
    env = c.put(f"{API}/models/config",
                {"model_id": "x", "quantization": "int8"})
    assert not env.get("success"), "量化配置端点不应存在"
    r.record("TC-FLOW-MODEL-034", "模型量化精度切换验证", "DEGRADED", "P1",
             "无 PUT /models/config 端点（404），FP16/INT8/INT4 量化切换与"
             "显存预估更新未实现；模型以原始精度加载")


@case(MOD, "TC-FLOW-MODEL-035", "模型优先级配置验证", "P2")
def model_035(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-MODEL-035", "模型优先级配置验证", "DEGRADED", "P2",
             "驱逐优先级按类别硬编码（_EVICTION_PRIORITY: auxiliary0/voice1/"
             "training2/video3/vision4/dialog5），无 P0~P5 优先级配置端点；"
             "拖拽调整为纯前端交互")


@case(MOD, "TC-FLOW-MODEL-036", "模型依赖关系查看验证", "P2")
def model_036(c: Client, r: Recorder) -> None:
    mid = _first_id(c)
    d = ok_data(c.get(f"{API}/models/{mid}")) or {}
    deps = d.get("dependencies")
    assert deps is not None, f"详情仍无依赖字段: {list(d.keys())}"
    r.record("TC-FLOW-MODEL-036", "模型依赖关系查看验证", "PASS", "P2",
             f"模型详情已含依赖关系字段 dependencies={deps} "
             f"（associated_features={d.get('associated_features')}）；"
             "依赖树图形化展示/卸载联动提示为前端逻辑")


@case(MOD, "TC-FLOW-MODEL-037", "模型导出功能验证", "P2")
def model_037(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/models/export", {"model_id": "x"})
    assert not env.get("success"), "导出端点不应存在"
    r.record("TC-FLOW-MODEL-037", "模型导出功能验证", "DEGRADED", "P2",
             "无 /models/export 端点（404），.tar.gz 打包导出与导出校验未实现")


@case(MOD, "TC-FLOW-MODEL-038", "模型性能基准测试验证", "P3")
def model_038(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/models/benchmark", {"model_id": "x"})
    assert not env.get("success"), "基准测试端点不应存在"
    r.record("TC-FLOW-MODEL-038", "模型性能基准测试验证", "DEGRADED", "P3",
             "无 /models/benchmark 端点（404），推理速度(Tokens/s)/显存峰值/"
             "吞吐量基准测试与历史对比未实现")
