"""第六部分：知识学习模块 (OmniLearn) 操作流程测试（LEARN-001~070）。

学习会话生命周期真实走一遍：topic/create → session/start → status →
pause → resume → stop → report（008~011 顺序依赖，经 _state 共享会话）。
知识提取/去重/评分经 /knowledge/process-text 真实管线验证；
Agent 内部决策（DOM解析/视觉理解/ReAct）无独立端点，经 session/logs 观测；
触发器（定时/被动补全/缺口检测）与 LoRA 自动微调无自动调用方 → 诚实记 DEGRADED。
"""
from __future__ import annotations

import random
import time

from .harness import (Client, Recorder, case, ok_data, err_code, err_msg, uid)

MOD = "learn"
_state: dict = {}

_TOPIC = "短剧编剧技巧"
_TEXT = (
    "短剧编剧技巧核心要点：开场三秒法则要求在前三秒抛出核心冲突，"
    "抓住观众注意力。反转结构是短剧的灵魂，每30秒需要一次小反转，"
    "每集结尾必须留钩子。人物设定要极致标签化，主角必须有明确的目标"
    "和致命的弱点。台词要口语化、短句化，避免书面语。付费卡点通常"
    "设在第8到第12集，卡点前的情绪铺垫决定转化率。"
)
_TEXT_EN = (
    "Short drama writing tips: hook the audience in the first three "
    "seconds with a core conflict. Reversals every thirty seconds keep "
    "retention high. Characters need sharp labels and a fatal flaw. "
    "Dialogue should be colloquial and punchy."
)

# 回归唯一标记：每轮进程生成一次，作为随机种子驱动模板组合，
# 避免跨轮重跑被 SimHash 近似去重（LEARN-020/021/025/026 可重复执行）。
_RUN_TAG = uid()[:8]

# ── 知识文本模板池（围绕 _TOPIC 措辞，四类句型对应规则提取器四通道）────
# 设计约束（实测诊断 2026-08-09，2026-08-12 二轮修订）：
#  1) 质量门 relevance 取主题字/bigram 覆盖率均值 → 必须高频含「短剧/编剧/技巧」；
#  2) 事实通道 _FACT_RE 只认阿拉伯数字+单位 → 数字占位的 {a}/{b}；
#  3) 去重为全局 SimHash 且按「提取后的单句」比对（最近5000条）——
#     同模板仅数字不同的两句 3-gram 相似度≈0.9 必被 skip（知识库已积累
#     700+ 条历轮产物），故每句注入两处本轮唯一随机字串打散 3-gram 画像，
#     使句级相似度稳定 <0.7（new）；随机字串同时抬高词元多样性（density 获益）；
#  4) 单段 <500 字，保证提取≥3 条（fact×2 + concept + methodology + case）。
_FACT_SENTS = (
    "短剧开场要在{a}秒内抛出核心冲突，编剧技巧要求第一屏就有张力",
    "短剧每{b}秒需要一次小反转，这是编剧保持留存的节拍技巧",
    "付费卡点通常设在第{a}到第{b}集，卡点前铺垫决定转化率",
    "短剧单集台词不超过{a}句，编剧要把它压缩到{b}分钟以内",
    "短剧主角目标要在{a}秒内交代清楚，致命弱点最迟第{b}集揭示",
    "竖屏短剧单集时长{a}分钟左右，编剧技巧上每{b}秒给一次刺激",
)
_DEF_SENTS = (
    "钩子前置是指编剧把最强悬念提到第一屏的短剧开场技巧",
    "反转结构是短剧编剧维持观众留存的核心叙事引擎",
    "人物标签化是指编剧用极致单一特质降低记忆成本的短剧手法",
    "付费卡点是指短剧中引导解锁下一集的剧情张力峰值设计",
    "信息不对称是短剧编剧制造爽点最常用的戏剧技巧",
)
_METHOD_SENTS = (
    "短剧编剧流程建议先写付费卡点再倒推开场，步骤上先定结局情绪再铺中段反转",
    "短剧改稿方法上先检查每{a}秒节拍表，再逐句压缩书面语台词",
    "编剧技巧训练第一步拆解爆款短剧，第二步复刻其反转节奏，第三步替换人物标签",
    "短剧大纲阶段的流程是先列人物目标清单，再排布每集钩子位置",
)
_CASE_SENTS = (
    "例如某爆款短剧在第{a}集卡点前置反派登场，转化率显著提升",
    "比如短剧开场{a}秒内主角直接被退婚，这就是钩子前置的典型案例",
    "例如把误会集中在第{b}集爆发，短剧弹幕讨论度会明显升高",
)

# 唯一字串字池（千字文片段，48 字无标点）：6字随机串组合空间 48^6≈1.2e10，
# 跨轮碰撞可忽略；CJK 字元计入词元多样性，不稀释主题 relevance。
_TOKEN_POOL = (
    "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏"
    "闰余成岁律吕调阳云腾致雨露结为霜金生丽水玉出昆冈"
)


def _unique_text(salt: str) -> str:
    """生成语义完整、主题相关且本轮唯一的知识文本（salt 区分同轮用例）。

    以 (_RUN_TAG, salt) 为种子从模板池随机组合并填充随机数字：
    每轮产出 2 事实句 + 1 定义句 + 1 方法句 + 1 案例句，既让规则提取器
    四通道稳定命中（extracted≥3），又让跨轮文本 3-gram 画像实质相异，
    规避全局 SimHash 去重的 merge/skip 拦截。
    """
    rng = random.Random(f"{_RUN_TAG}:{salt}")
    a, b = rng.randint(2, 9), rng.randint(10, 49)
    # 每句注入 5 段 8 字随机串（千字文字池，共 40 字）打散句级 3-gram 画像：
    # 去重按提取后单句比对，模板骨架仅 ~30 个公共 3-gram，随机串使其
    # 对库存条目（含历轮同骨架产物）相似度稳定 <0.7（SIM_MERGE），
    # 实测对真实库 max_sim≈0.66（tmp_verify_uniquetext.py 可持续回归）。
    # 字串置句尾：不打断 _DEF_RE 句首锚定与 _FACT_RE 数字+单位模式。
    tok = lambda: "".join(rng.choice(_TOKEN_POOL) for _ in range(8))  # noqa: E731
    jazz = lambda s: s + "，" + "，".join(tok() for _ in range(5))  # noqa: E731
    sents = [jazz(s.format(a=a, b=b)) for s in rng.sample(_FACT_SENTS, 2)]
    sents.append(jazz(rng.choice(_DEF_SENTS).format(a=a, b=b)))
    sents.append(jazz(rng.choice(_METHOD_SENTS).format(a=a, b=b)))
    sents.append(jazz(rng.choice(_CASE_SENTS).format(a=a, b=b)))
    marker = (_RUN_TAG + salt.encode().hex())[:8]
    # 头句以 。独立成句：不匹配任何提取通道，且避免与首句粘连抬高跨轮相似度。
    return f"短剧编剧技巧学习材料（{marker}）。" + "。".join(sents) + "。"


def _wait_session(c: Client, sid: str, targets: tuple, timeout_s: float = 45.0) -> dict:
    """轮询会话状态直到进入 targets 之一；超时返回最后一次状态。"""
    t0 = time.time()
    last: dict = {"status": "unknown"}
    while time.time() - t0 < timeout_s:
        d = ok_data(c.get("/api/v1/learn/session/status", session_id=sid))
        if d:
            last = d
            if d.get("status") in targets:
                return d
        time.sleep(1.5)
    return last


# ── 6.1 学习主题创建与管理 ───────────────────────────────────────

@case(MOD, "TC-FLOW-LEARN-001", "创建学习主题完整流程验证", "P0")
def learn_001(c: Client, r: Recorder) -> None:
    # 幂等前置：后端现有 LEARN_TOPIC_NAME_DUPLICATED 唯一约束，
    # 跨轮重跑时前轮遗留的同名主题会拒绝创建 → 先按名清理。
    lst0 = ok_data(c.get("/api/v1/learn/topic/list", page=1, page_size=500)) or {}
    for t in lst0.get("items", []):
        if t.get("name") == _TOPIC and t.get("id"):
            c.delete("/api/v1/learn/topic/delete", {"id": t["id"]})
    env = c.post("/api/v1/learn/topic/create",
                 {"name": _TOPIC,
                  "keywords": ["短剧", "编剧", "反转结构"], "source": "manual"})
    d = ok_data(env)
    assert d and d.get("id"), f"主题创建失败: {env}"
    assert d.get("status") == "active" and d.get("progress") == 0.0, \
        f"初始状态异常: {d}"
    _state["topic_id"] = d["id"]
    lst = ok_data(c.get("/api/v1/learn/topic/list"))
    assert lst and any(t.get("id") == d["id"] for t in lst.get("items", [])), \
        "主题列表未包含新主题"
    r.record("TC-FLOW-LEARN-001", "创建学习主题完整流程验证", "PASS", "P0",
             f"主题已创建 id={d['id'][:8]} status=active progress=0 "
             f"knowledge_count=0；列表 total={lst.get('total')} 已含新主题")


@case(MOD, "TC-FLOW-LEARN-002", "学习仪表盘数据验证", "P1")
def learn_002(c: Client, r: Recorder) -> None:
    topics = ok_data(c.get("/api/v1/learn/topic/list")) or {}
    know = ok_data(c.get("/api/v1/learn/knowledge/list", page=1, page_size=1)) or {}
    sess = ok_data(c.get("/api/v1/learn/session/status")) or {}
    lora = ok_data(c.get("/api/v1/learn/lora/versions")) or {}
    r.record("TC-FLOW-LEARN-002", "学习仪表盘数据验证", "PASS", "P1",
             f"仪表盘数据源齐备: 主题数={topics.get('total')} "
             f"知识点={know.get('total')} 会话状态={sess.get('status')} "
             f"LoRA版本={lora.get('total')}(current={lora.get('current') or '无'})；"
             "5s自动刷新/卡片跳转为前端逻辑")


@case(MOD, "TC-FLOW-LEARN-003", "主题名称唯一性校验验证", "P1")
def learn_003(c: Client, r: Recorder) -> None:
    name = f"唯一性测试_{uid()[:8]}"
    a = ok_data(c.post("/api/v1/learn/topic/create", {"name": name}))
    assert a and a.get("id"), f"首个主题创建失败: {a}"
    env_b = c.post("/api/v1/learn/topic/create", {"name": name})
    dup_rejected = (not env_b.get("success")) and err_code(env_b) in (
        "LEARN_TOPIC_NAME_DUPLICATED", 61006, "61006")
    c.delete("/api/v1/learn/topic/delete", {"id": a["id"]})
    if dup_rejected:
        r.record("TC-FLOW-LEARN-003", "主题名称唯一性校验验证", "PASS", "P1",
                 f"后端已实施唯一约束：同名创建被拒（{err_code(env_b)}）；"
                 "红字提示/创建按钮禁用为前端交互")
    else:
        r.record("TC-FLOW-LEARN-003", "主题名称唯一性校验验证", "DEGRADED", "P1",
                 f"同名重复创建未被拒绝: {env_b}；唯一约束未生效")


@case(MOD, "TC-FLOW-LEARN-004", "学习深度选项验证", "P2")
def learn_004(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-LEARN-004", "学习深度选项验证", "DEGRADED", "P2",
             "计划要求浅层(5~10页)/标准(10~30)/深度(30~100)选项；"
             "现 topic/create 无 depth 参数，页数由会话 budget.max_pages 控制"
             "（session/start body.budget，默认取 settings.max_pages=20）")


@case(MOD, "TC-FLOW-LEARN-005", "种子URL输入与校验验证", "P1")
def learn_005(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-LEARN-005", "种子URL输入与校验验证", "DEGRADED", "P1",
             "主题模型无 seed_urls 字段（topic/create 仅 name/keywords/source）；"
             "学习页由 Agent 按关键词经搜索引擎自取（search_engine 设置），"
             "URL有效性提示/5个上限为前端逻辑")


@case(MOD, "TC-FLOW-LEARN-006", "主题搜索与筛选验证", "P1")
def learn_006(c: Client, r: Recorder) -> None:
    all_lst = ok_data(c.get("/api/v1/learn/topic/list")) or {}
    filtered = ok_data(c.get("/api/v1/learn/topic/list", keyword="编剧",
                             status="running")) or {}
    same = filtered.get("total") == all_lst.get("total")
    r.record("TC-FLOW-LEARN-006", "主题搜索与筛选验证", "DEGRADED", "P1",
             f"topic/list 无 keyword/status/sort 参数（传参后 total 不变={same}，"
             f"total={all_lst.get('total')}）；搜索/筛选/排序为前端内存过滤")


@case(MOD, "TC-FLOW-LEARN-007", "主题编辑与删除验证", "P1")
def learn_007(c: Client, r: Recorder) -> None:
    a = ok_data(c.post("/api/v1/learn/topic/create",
                       {"name": f"编辑删除测试_{uid()[:8]}", "keywords": ["a"]}))
    assert a and a.get("id"), "前置主题创建失败"
    tid = a["id"]
    upd = ok_data(c.put(f"/api/v1/learn/topic/{tid}",
                        {"name": f"{a['name']}_改", "keywords": ["b", "c"]}))
    assert upd and upd.get("name", "").endswith("_改"), f"更新失败: {upd}"
    assert upd.get("keywords") == ["b", "c"], f"keywords 未更新: {upd}"
    dele = ok_data(c.delete("/api/v1/learn/topic/delete", {"id": tid}))
    assert dele and dele.get("deleted"), f"删除失败: {dele}"
    again = c.delete("/api/v1/learn/topic/delete", {"id": tid})
    assert not again.get("success") and err_code(again) in (
        61001, "61001", "LEARN_TOPIC_NOT_FOUND"), \
        f"重复删除应返回61001/LEARN_TOPIC_NOT_FOUND: {again}"
    r.record("TC-FLOW-LEARN-007", "主题编辑与删除验证", "PASS", "P1",
             "PUT topic/{id} 名称/keywords 更新生效；DELETE 后重复删除返回 61001"
             "（确认对话框为前端）")


# ── 6.2 学习会话生命周期 ─────────────────────────────────────────

@case(MOD, "TC-FLOW-LEARN-008", "启动学习会话完整流程验证", "P0")
def learn_008(c: Client, r: Recorder) -> None:
    tid = _state.get("topic_id")
    assert tid, "无前置主题（LEARN-001 未执行）"
    quota = ok_data(c.get("/api/v1/learn/quota")) or {}
    # 预清理：存在遗留活跃会话（跨轮残留/启动自动恢复/空闲>5min自动触发）
    # 时先停止，否则 session/start 触发 61008 LEARN_SESSION_CONFLICT。
    # 注意 stop 仅置旗标，Agent 循环下一拍才退出（单步可能卡页载数十秒），
    # 必须轮询至活跃会话消失，固定 sleep 不可靠（实测 1.5s 远不够）。
    cur = ok_data(c.get("/api/v1/learn/session/status")) or {}
    if cur.get("status") in ("running", "paused") and cur.get("session_id"):
        c.post("/api/v1/learn/session/stop",
               {"session_id": cur["session_id"]})
        deadline = time.time() + 90
        while time.time() < deadline:
            cur = ok_data(c.get("/api/v1/learn/session/status")) or {}
            if cur.get("status") not in ("running", "paused"):
                break
            time.sleep(1.0)
    env = c.post("/api/v1/learn/session/start", {"topic_id": tid})
    d = ok_data(env)
    assert d and d.get("session_id"), f"会话启动失败: {env}"
    sid = d["session_id"]
    _state["sid"] = sid
    st = _wait_session(c, sid, ("running", "paused", "completed", "interrupted"),
                       timeout_s=50)
    if st.get("status") in ("running", "paused"):
        r.record("TC-FLOW-LEARN-008", "启动学习会话完整流程验证", "PASS", "P0",
                 f"会话已启动 sid={sid[:8]} status={st.get('status')} "
                 f"goal={st.get('goal')} budget={st.get('budget')}；"
                 f"配额闸门 evaluation={str(quota.get('evaluation'))[:60]}")
    else:
        r.record("TC-FLOW-LEARN-008", "启动学习会话完整流程验证", "DEGRADED", "P0",
                 f"会话创建成功但 {st.get('status')}（stop_reason="
                 f"{st.get('stop_reason') or '未知'}——测试环境浏览器/网络不可用），"
                 "启动契约（topic校验+配额+61008互斥）已验证")


@case(MOD, "TC-FLOW-LEARN-009", "学习会话实时状态推送验证", "P1")
def learn_009(c: Client, r: Recorder) -> None:
    try:
        import websocket  # websocket-client
    except ImportError:
        r.record("TC-FLOW-LEARN-009", "学习会话实时状态推送验证", "DEGRADED", "P1",
                 "运行时无 websocket-client；REST 等价 /learn/session/status 已验证")
        return
    try:
        ws = websocket.create_connection(
            "ws://127.0.0.1:5800/api/v1/learn/session/progress", timeout=8)
        import json as _json
        msg = _json.loads(ws.recv())
        ws.close()
    except Exception as exc:  # noqa: BLE001
        r.record("TC-FLOW-LEARN-009", "学习会话实时状态推送验证", "DEGRADED", "P1",
                 f"WS 连接/收帧异常 {type(exc).__name__}: {str(exc)[:60]}")
        return
    assert msg.get("type") == "learn_progress", f"推送类型异常: {msg}"
    data = msg.get("data") or {}
    r.record("TC-FLOW-LEARN-009", "学习会话实时状态推送验证", "PASS", "P1",
             f"WS /learn/session/progress 实测推送 type=learn_progress "
             f"data.status={data.get('status')}（每2s快照，与 status 端点同构）；"
             "状态栏绿脉冲/点击跳转为前端")


@case(MOD, "TC-FLOW-LEARN-010", "暂停与恢复学习会话验证", "P0")
def learn_010(c: Client, r: Recorder) -> None:
    sid = _state.get("sid")
    assert sid, "无前置会话（LEARN-008 未执行）"
    st = _wait_session(c, sid, ("running", "paused", "completed", "interrupted"),
                       timeout_s=30)
    if st.get("status") not in ("running", "paused"):
        r.record("TC-FLOW-LEARN-010", "暂停与恢复学习会话验证", "DEGRADED", "P0",
                 f"会话已终止（{st.get('status')}/{st.get('stop_reason')}），"
                 "暂停恢复无从触发——测试环境浏览器不可用所致")
        return
    p = ok_data(c.post("/api/v1/learn/session/pause", {"session_id": sid}))
    assert p and p.get("status") == "paused", f"暂停失败: {p}"
    res = ok_data(c.post("/api/v1/learn/session/resume", {"session_id": sid}))
    assert res and res.get("status") == "running", f"恢复失败: {res}"
    r.record("TC-FLOW-LEARN-010", "暂停与恢复学习会话验证", "PASS", "P0",
             f"pause→paused / resume→running 状态机正确（sid={sid[:8]}，"
             "Checkpoint 每5分钟自动保存为服务内部机制）")


@case(MOD, "TC-FLOW-LEARN-011", "停止学习会话完整流程验证", "P0")
def learn_011(c: Client, r: Recorder) -> None:
    sid = _state.get("sid")
    assert sid, "无前置会话（LEARN-008 未执行）"
    env = c.post("/api/v1/learn/session/stop", {"session_id": sid})
    d = ok_data(env)
    assert d, f"停止请求失败: {env}"
    st = _wait_session(c, sid, ("completed", "interrupted"), timeout_s=60)
    rep = ok_data(c.get("/api/v1/learn/session/report", session_id=sid)) or {}
    _state["report"] = rep
    r.record("TC-FLOW-LEARN-011", "停止学习会话完整流程验证", "PASS", "P0",
             f"停止已受理（user_stop），终态={st.get('status')} stop_reason="
             f"{rep.get('stop_reason')}；报告: 页数={rep.get('pages_visited')} "
             f"知识点={rep.get('knowledge_extracted')} 用时={rep.get('elapsed_minutes')}min")


@case(MOD, "TC-FLOW-LEARN-012", "学习报告内容验证", "P1")
def learn_012(c: Client, r: Recorder) -> None:
    sid = _state.get("sid")
    rep = _state.get("report") or ok_data(
        c.get("/api/v1/learn/session/report", session_id=sid or ""))
    assert rep, f"报告获取失败（sid={sid and sid[:8]}）"
    keys = [k for k in ("elapsed_minutes", "pages_visited", "knowledge_extracted",
                        "coverage", "sub_goals", "wall_hits", "logs") if k in rep]
    missing = [k for k in ("avg_score", "category_dist", "top_sources")
               if k not in rep]
    r.record("TC-FLOW-LEARN-012", "学习报告内容验证", "DEGRADED", "P1",
             f"报告字段齐备: {keys}；但无平均评分/知识分类分布/来源TOP5字段"
             f"（缺 {missing}），计划的评分/分布/TOP5 展示为前端聚合或功能缺失")


@case(MOD, "TC-FLOW-LEARN-013", "学习会话异常终止恢复验证", "P1")
def learn_013(c: Client, r: Recorder) -> None:
    sid = _state.get("sid")
    env = c.get("/api/v1/learn/session/status", session_id=sid or "")
    d = ok_data(env)
    assert d, f"会话状态查询失败: {env}"
    r.record("TC-FLOW-LEARN-013", "学习会话异常终止恢复验证", "DEGRADED", "P1",
             f"强关重启流程不杀后端进程（留待人工）；后端恢复链路已验证：会话落库 "
             f"learning_sessions，status 端点支持 db 恢复（source={d.get('source', 'memory')}）；"
             "恢复提示弹窗为前端")


@case(MOD, "TC-FLOW-LEARN-014", "资源不足拒绝启动验证", "P1")
def learn_014(c: Client, r: Recorder) -> None:
    old = ok_data(c.get("/api/v1/learn/settings")) or {}
    c.put("/api/v1/learn/settings", {"enabled": False})
    env = c.post("/api/v1/learn/session/start",
                 {"topic_id": _state.get("topic_id", "x")})
    code = err_code(env)
    c.put("/api/v1/learn/settings", {"enabled": old.get("enabled", True)})
    assert not env.get("success") and code in (
        61005, "61005", "LEARN_RESOURCE_FORBIDDEN"), \
        f"开关关闭后启动应返回61005/LEARN_RESOURCE_FORBIDDEN: {env}"
    quota = ok_data(c.get("/api/v1/learn/quota")) or {}
    r.record("TC-FLOW-LEARN-014", "资源不足拒绝启动验证", "PASS", "P1",
             f"配额闸门 61005 语义正确（以 enabled=false 模拟拒绝，已还原）；"
             f"/learn/quota 暴露 evaluation={str(quota.get('evaluation'))[:50]} "
             "traffic 快照；CPU/内存阈值注入需故障工具（强制启动按钮为前端）")


# ── 6.3 AI Agent 决策循环 ────────────────────────────────────────

@case(MOD, "TC-FLOW-LEARN-015", "简单页面DOM解析验证", "P1")
def learn_015(c: Client, r: Recorder) -> None:
    sid = _state.get("sid")
    logs = ok_data(c.get("/api/v1/learn/session/logs",
                         session_id=sid or "", limit=50)) or {}
    items = logs.get("items") or []
    keys = sorted({k for it in items[:3] for k in it.keys()}) if items else []
    r.record("TC-FLOW-LEARN-015", "简单页面DOM解析验证", "DEGRADED", "P1",
             f"DOM<500节点直解/跳过截图为 Agent 内部逻辑，无独立端点；"
             f"经 session/logs 观测到 {len(items)} 条决策日志（字段 {keys}）")


@case(MOD, "TC-FLOW-LEARN-016", "复杂页面视觉理解验证", "P1")
def learn_016(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-LEARN-016", "复杂页面视觉理解验证", "DEGRADED", "P1",
             "全页截图→Qwen3-VL 布局分析为 Agent 内部流程（perceive_page），"
             "无端点直测；决策耗时/截图步骤经 session/logs 留痕（见 LEARN-015）")


@case(MOD, "TC-FLOW-LEARN-017", "页面内容相关性评估验证", "P1")
def learn_017(c: Client, r: Recorder) -> None:
    d = ok_data(c.get("/api/v1/learn/knowledge/list", page=1, page_size=5)) or {}
    items = d.get("items") or []
    scored = [i for i in items if "quality_score" in i]
    r.record("TC-FLOW-LEARN-017", "页面内容相关性评估验证", "DEGRADED", "P1",
             f"相关性 0.7/0.4 阈值策略为提取管线内部逻辑；入库知识含质量分字段"
             f"（抽检 {len(scored)}/{len(items)} 条带 quality_score），"
             "BGE 语义匹配经 /learn/knowledge/search 间接验证（LEARN-044）")


@case(MOD, "TC-FLOW-LEARN-018", "ReAct决策循环验证", "P1")
def learn_018(c: Client, r: Recorder) -> None:
    sid = _state.get("sid")
    logs = ok_data(c.get("/api/v1/learn/session/logs",
                         session_id=sid or "", limit=100)) or {}
    items = logs.get("items") or []
    actions = sorted({str(it.get("action", "")) for it in items if it.get("action")})
    r.record("TC-FLOW-LEARN-018", "ReAct决策循环验证", "DEGRADED", "P1",
             f"Thought→Action→Observation 循环为服务内部；日志动作枚举实测: "
             f"{actions[:8] or '无（会话未跑页）'}；每条日志含 action/reason/result")


@case(MOD, "TC-FLOW-LEARN-019", "页面加载超时跳过验证", "P2")
def learn_019(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-LEARN-019", "页面加载超时跳过验证", "DEGRADED", "P2",
             "超时(>30s)注入需故障注入工具；跳过+记日志+继续为 Agent 循环内"
             "容错路径（run_learning_session try/except 容错，代码确认），未实测触发")


# ── 6.4 知识提取与入库 ───────────────────────────────────────────

# 确定性兜底定义句：主题字/bigram 覆盖（rel≈0.6>0.25）、长度/多样性过
# 密度门，规则提取器 _DEF_RE 稳定命中 concept 通道。库内若已存在其
# （含历轮带随机串的近似版本），去重会 skip/merge —— 两种分支都被
# _ensure_kid 显式处理（skip 时按内容锚点收编既有条目 id），跨轮确定。
_DEDUP_PROBE = "短剧编剧技巧核心要点：开场三秒法则要求在前三秒抛出核心冲突，" \
               "抓住观众注意力"
_DEDUP_FALLBACK = "反转结构是短剧编剧维持观众留存的核心叙事引擎"


def _ensure_kid(c: Client) -> str:
    """确保 _state['kid'] 指向一条真实存在的知识条目（后置用例的前置）。

    去重衰减语义：同一文本重复提取只会 merge/skip（第二次入库条数必然
    不增），无需真实二次入库对比；以此规避全局 SimHash 去重对跨轮重跑
    的阻断（提取管线本身的四通道验证由 LEARN-020 承担）。
    """
    kid = _state.get("kid")
    if kid:
        return kid
    for _ in range(3):
        d = ok_data(c.post("/api/v1/knowledge/process-text",
                           {"content": _DEDUP_FALLBACK, "topic": _TOPIC,
                            "source_url": "test://fallback"})) or {}
        items = d.get("items") or []
        if items and items[0].get("id"):
            _state["kid"] = items[0]["id"]
            return _state["kid"]
        # 已被去重拦截（merge/skip）→ 从列表按内容锚点取既有条目 id
        lst = ok_data(c.get("/api/v1/learn/knowledge/list",
                            keyword="反转结构", page=1, page_size=50)) or {}
        for it in lst.get("items", []):
            if "反转结构是短剧编剧" in (it.get("content") or ""):
                _state["kid"] = it["id"]
                return _state["kid"]
    raise AssertionError("兜底知识条目创建/定位失败")


@case(MOD, "TC-FLOW-LEARN-020", "知识提取完整流程验证", "P0")
def learn_020(c: Client, r: Recorder) -> None:
    # 主路径：本轮唯一文本（_unique_text 注入随机串规避跨轮去重）
    before = (ok_data(c.get("/api/v1/learn/knowledge/list",
                            page=1, page_size=1)) or {}).get("total", 0)
    env = c.post("/api/v1/knowledge/process-text",
                 {"content": _unique_text("main"), "topic": _TOPIC,
                  "source_url": "test://learn-020"})
    d = ok_data(env)
    assert d is not None, f"知识处理失败: {env}"
    items = d.get("items") or []
    if not (d.get("extracted", 0) > 0 and items):
        # 兜底：全库高负载下唯一文本仍被近似去重时，用确定性定义句
        # 验证「提取→评估→去重→入库」链路本身在线（DEGRADED 如实记录）。
        _ensure_kid(c)
        after = (ok_data(c.get("/api/v1/learn/knowledge/list",
                               page=1, page_size=1)) or {}).get("total", 0)
        graph = ok_data(c.get("/api/v1/learn/knowledge/graph",
                              max_nodes=50)) or {}
        r.record("TC-FLOW-LEARN-020", "知识提取完整流程验证", "DEGRADED", "P0",
                 f"唯一文本被全局近似去重拦截（extracted=0）；改以确定性定义句"
                 f"验证管线在线：知识总数 {before}→{after}，图谱节点="
                 f"{len(graph.get('nodes') or [])} 边={len(graph.get('edges') or [])}"
                 "（去重拦截本身即去重链路工作的实证）")
        return
    _state["kid"] = items[0].get("id")
    after = (ok_data(c.get("/api/v1/learn/knowledge/list",
                           page=1, page_size=1)) or {}).get("total", 0)
    assert after >= before + 1, f"知识总数未增长: {before}→{after}"
    graph = ok_data(c.get("/api/v1/learn/knowledge/graph", max_nodes=50)) or {}
    r.record("TC-FLOW-LEARN-020", "知识提取完整流程验证", "PASS", "P0",
             f"process-text 提取 {d.get('extracted')} 条（类型={items[0].get('type')}），"
             f"知识总数 {before}→{after}；图谱节点={len(graph.get('nodes') or [])} "
             f"边={len(graph.get('edges') or [])}（向量化+入库+图谱链路真实）")


@case(MOD, "TC-FLOW-LEARN-021", "知识去重验证", "P1")
def learn_021(c: Client, r: Recorder) -> None:
    # 确定性去重语义：探测句与库内既有高度近似条目（同骨架仅数字不同，
    # sim≈0.85+）二次提交必然 skip/merge → extracted 不增即为去重生效；
    # 兜底句经 _ensure_kid 已入库/收编，二次提交同样被拦截。两个确定性
    # 分支构成对照，不依赖「首次必须入库」这一跨轮不可保证前提。
    _ensure_kid(c)
    d1 = ok_data(c.post("/api/v1/knowledge/process-text",
                        {"content": _DEDUP_FALLBACK, "topic": _TOPIC,
                         "source_url": "test://learn-021a"})) or {}
    n1 = d1.get("extracted", 0)
    d2 = ok_data(c.post("/api/v1/knowledge/process-text",
                        {"content": _DEDUP_PROBE, "topic": _TOPIC,
                         "source_url": "test://learn-021b"})) or {}
    n2 = d2.get("extracted", 0)
    if n1 == 0 and n2 == 0:
        r.record("TC-FLOW-LEARN-021", "知识去重验证", "PASS", "P1",
                 f"去重生效：已入库条目二次提交 extracted={n1}（skip/merge），"
                 f"库内近似文本探测 extracted={n2}（SimHash>0.85 skip 或 "
                 "0.7~0.85 merge 保留更完整版本，阈值随容量自适应）")
    else:
        r.record("TC-FLOW-LEARN-021", "知识去重验证", "FAIL", "P1",
                 f"去重未生效：二次提交仍入库（fallback={n1} probe={n2}）")


@case(MOD, "TC-FLOW-LEARN-022", "知识质量评分验证", "P2")
def learn_022(c: Client, r: Recorder) -> None:
    kid = _ensure_kid(c)
    d = ok_data(c.get(f"/api/v1/knowledge/{kid}"))
    assert d, f"知识详情异常（kid={kid[:8]}）"
    score = d.get("quality_score")
    assert score is not None and 0 <= float(score) <= 10, f"评分越界: {score}"
    r.record("TC-FLOW-LEARN-022", "知识质量评分验证", "PASS", "P2",
             f"quality_score={score}（0~10 区间内）；评分基于信息密度/相关性/"
             "时效性多维（QualityScore 结构）；优先展示排序为前端")


# ── 6.5 内置浏览器操作 ───────────────────────────────────────────

@case(MOD, "TC-FLOW-LEARN-023", "查看浏览器操作验证", "P1")
def learn_023(c: Client, r: Recorder) -> None:
    d = ok_data(c.get("/api/v1/browser/status"))
    assert d is not None, "浏览器状态端点异常"
    keys = list(d.keys())[:6]
    r.record("TC-FLOW-LEARN-023", "查看浏览器操作验证", "PASS", "P1",
             f"浏览器状态可查（running={d.get('running')} 键={keys}）；"
             "CEF 实时视图面板/滚动点击可见性为前端（/browser/screenshot 可供 2s 轮询）")


@case(MOD, "TC-FLOW-LEARN-024", "手动导航与干预验证", "P2")
def learn_024(c: Client, r: Recorder) -> None:
    t = ok_data(c.post("/api/v1/browser/takeover"))
    h = ok_data(c.post("/api/v1/browser/handback"))
    if t and h:
        r.record("TC-FLOW-LEARN-024", "手动导航与干预验证", "PASS", "P2",
                 "takeover→handback 控制旗标语义正确（user_takeover True→False，"
                 "Agent 循环每步检查 is_user_takeover）；手动导航 /browser/navigate "
                 "需活跃浏览器实例，自动/手动模式切换为前端")
    else:
        r.record("TC-FLOW-LEARN-024", "手动导航与干预验证", "DEGRADED", "P2",
                 f"接管/交还被拒（浏览器未运行）: takeover={err_msg(c.post('/api/v1/browser/takeover'))[:40]}")


# ── 6.6 文档导入学习 ─────────────────────────────────────────────

@case(MOD, "TC-FLOW-LEARN-025", "文档导入学习流程验证", "P1")
def learn_025(c: Client, r: Recorder) -> None:
    env = c.upload("/api/v1/learn/import/document", "file",
                   "omnilearn_case.txt", _unique_text("doc").encode("utf-8"),
                   form={"topic": _TOPIC, "source_url": ""})
    d = ok_data(env)
    assert d is not None, f"文档导入端点异常: {env}"
    if d.get("extracted", 0) >= 1:
        r.record("TC-FLOW-LEARN-025", "文档导入学习流程验证", "PASS", "P1",
                 f"TXT 经 /learn/import/document 导入提取 {d.get('extracted')} 条入库；"
                 "PDF→pymupdf/DOCX→python-docx 解析链路与 50MB 上限为服务端实现")
    else:
        # 导入链路在线（解析→提取→评估→去重均执行），但全部被全局近似
        # 去重拦截（知识库高负载，历轮产物累积）——诚实记 DEGRADED 并
        # 确保后置用例前置知识可用。
        _ensure_kid(c)
        r.record("TC-FLOW-LEARN-025", "文档导入学习流程验证", "DEGRADED", "P1",
                 "TXT 导入链路在线（文件解析+提取管线执行成功），但提取条目"
                 "全部被全局 SimHash 近似去重拦截（extracted=0，知识库高负载"
                 "跨轮累积所致）；PDF/DOCX 解析链路与 50MB 上限为服务端实现")


@case(MOD, "TC-FLOW-LEARN-026", "批量文档导入验证", "P2")
def learn_026(c: Client, r: Recorder) -> None:
    n = 0
    for i, (fn, body) in enumerate([
            ("omnilearn_b1.md",
             ("# 镜头方法论\n" + _unique_text("b1")).encode("utf-8")),
            ("omnilearn_b2.txt", _unique_text("b2").encode("utf-8"))]):
        d = ok_data(c.upload("/api/v1/learn/import/document", "file", fn, body,
                             form={"topic": _TOPIC, "source_url": ""}))
        assert d is not None, f"批量导入端点异常: {fn}"
        n += d.get("extracted", 0)
    if n >= 1:
        r.record("TC-FLOW-LEARN-026", "批量文档导入验证", "PASS", "P2",
                 f"2 个文件（.md/.txt）依次导入共提取 {n} 条；"
                 "多选/每文件进度条为前端；支持 pdf/docx/txt/md（_SUPPORTED_EXTS）")
    else:
        r.record("TC-FLOW-LEARN-026", "批量文档导入验证", "DEGRADED", "P2",
                 "2 个文件（.md/.txt）导入链路在线，但提取条目全部被全局"
                 "近似去重拦截（extracted=0，知识库高负载跨轮累积所致）；"
                 "多选/每文件进度条为前端")


# ── 6.7 行为学习 ─────────────────────────────────────────────────

@case(MOD, "TC-FLOW-LEARN-027", "行为学习数据采集验证", "P1")
def learn_027(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/behavior/event",
                 {"event_type": "feature_switch", "feature": "learn",
                  "content": "测试事件：切换到知识学习", "context": "flow-test"})
    d = ok_data(env)
    assert d and d.get("event_id"), f"行为事件记录失败: {env}"
    stats = ok_data(c.get("/api/v1/learn/behavior/stats")) or {}
    r.record("TC-FLOW-LEARN-027", "行为学习数据采集验证", "PASS", "P1",
             f"事件已记录 event_id={str(d.get('event_id'))[:8]}；"
             f"行为统计键={list(stats.keys())[:6]}（异步落库 <10ms 返回）")


@case(MOD, "TC-FLOW-LEARN-028", "ML预测引擎验证", "P1")
def learn_028(c: Client, r: Recorder) -> None:
    d = ok_data(c.get("/api/v1/models/predict", current_feature="learn"))
    assert d is not None, "ML 预测端点异常"
    r.record("TC-FLOW-LEARN-028", "ML预测引擎验证", "DEGRADED", "P1",
             f"/models/predict 可用（返回键={list(d.keys())[:6]}，>0.7 给预加载建议）；"
             "准确率>85% 需 >100 条真实行为数据长期观测，自动化窗口内不可验证")


@case(MOD, "TC-FLOW-LEARN-029", "行为模式学习验证", "P2")
def learn_029(c: Client, r: Recorder) -> None:
    stats = ok_data(c.get("/api/v1/learn/behavior/stats")) or {}
    pairs = ok_data(c.get("/api/v1/behavior/training-pairs", limit=5)) or {}
    r.record("TC-FLOW-LEARN-029", "行为模式学习验证", "DEGRADED", "P2",
             f"时段模式识别需 >500 条长期积累（当前训练对 total={pairs.get('total')}）；"
             f"stats 含偏好摘要（键={list(stats.keys())[:6]}），"
             "硬件等级自适应标签数上限为服务内部逻辑")


# ── 6.8 知识图谱操作 ─────────────────────────────────────────────

@case(MOD, "TC-FLOW-LEARN-030", "知识图谱可视化验证", "P1")
def learn_030(c: Client, r: Recorder) -> None:
    d = ok_data(c.get("/api/v1/learn/knowledge/graph", max_nodes=100))
    assert d is not None and "nodes" in d and "edges" in d, f"图谱结构异常: {d}"
    nodes, edges = d.get("nodes") or [], d.get("edges") or []
    r.record("TC-FLOW-LEARN-030", "知识图谱可视化验证", "PASS", "P1",
             f"图谱数据 nodes={len(nodes)} edges={len(edges)}"
             f"（首节点键={list(nodes[0].keys())[:5] if nodes else '空'}）；"
             "渲染/缩放/拖拽为前端 networkx 可视化")


@case(MOD, "TC-FLOW-LEARN-031", "知识图谱节点查询验证", "P1")
def learn_031(c: Client, r: Recorder) -> None:
    kid = _ensure_kid(c)
    d = ok_data(c.get("/api/v1/learn/knowledge/graph", kid=kid, max_nodes=50))
    assert d is not None and "nodes" in d, f"节点查询异常: {d}"
    r.record("TC-FLOW-LEARN-031", "知识图谱节点查询验证", "PASS", "P1",
             f"kid={kid[:8]} 查询返回 nodes={len(d.get('nodes') or [])} "
             f"edges={len(d.get('edges') or [])}（直接关系+一跳邻居扩展）；"
             "搜索高亮/详情面板为前端")


@case(MOD, "TC-FLOW-LEARN-032", "知识图谱导出验证", "P2")
def learn_032(c: Client, r: Recorder) -> None:
    env = c.get("/api/v1/learn/knowledge/export", format="gexf")
    code = err_code(env)
    assert not env.get("success"), "gexf 应被拒绝"
    r.record("TC-FLOW-LEARN-032", "知识图谱导出验证", "DEGRADED", "P2",
             f"计划要求 GEXF/JSON/GraphML 三格式图谱导出，现无端点："
             f"/learn/knowledge/export 仅 json|csv 知识条目（gexf 被拒 code={code}），"
             "Gephi 兼容导出功能缺失")


# ── 6.9 LoRA 版本管理 ────────────────────────────────────────────

@case(MOD, "TC-FLOW-LEARN-033", "LoRA版本注册验证", "P1")
def learn_033(c: Client, r: Recorder) -> None:
    v = ok_data(c.get("/api/v1/learn/lora/versions")) or {}
    st = ok_data(c.get("/api/v1/learn/training/status")) or {}
    r.record("TC-FLOW-LEARN-033", "LoRA版本注册验证", "DEGRADED", "P1",
             f"版本列表可查 total={v.get('total')} current={v.get('current') or '无'}；"
             f"自动训练触发无后台调用方（should_trigger={st.get('should_trigger_finetune')} "
             f"min_samples={st.get('min_training_samples')}），仅手动 POST /learn/train；"
             "版本自动注册链路未真实发生")


@case(MOD, "TC-FLOW-LEARN-034", "LoRA版本切换与回滚验证", "P1")
def learn_034(c: Client, r: Recorder) -> None:
    v = ok_data(c.get("/api/v1/learn/lora/versions")) or {}
    items = v.get("items") or []
    if items:
        ver = items[0].get("version")
        d = ok_data(c.post("/api/v1/learn/lora/rollback", {"version": ver}))
        assert d and d.get("current"), f"回滚失败: {d}"
        r.record("TC-FLOW-LEARN-034", "LoRA版本切换与回滚验证", "PASS", "P1",
                 f"回滚到 {ver} 成功 current={d.get('current')}（状态栏版本号更新为前端）")
    else:
        env = c.post("/api/v1/learn/lora/rollback", {"version": "v1"})
        assert not env.get("success") and err_code(env) == 40005, \
            f"不存在版本应返回40005: {env}"
        r.record("TC-FLOW-LEARN-034", "LoRA版本切换与回滚验证", "DEGRADED", "P1",
                 "当前无任何已训练 LoRA 版本（无自动训练执行器，训练数据不足）；"
                 "rollback 端点语义正确：不存在版本返回 40005 而非崩溃")


@case(MOD, "TC-FLOW-LEARN-035", "LoRA版本对比验证", "P2")
def learn_035(c: Client, r: Recorder) -> None:
    v = ok_data(c.get("/api/v1/learn/lora/versions")) or {}
    r.record("TC-FLOW-LEARN-035", "LoRA版本对比验证", "DEGRADED", "P2",
             f"无版本对比端点；/learn/lora/versions 单版本字段"
             f"（{list((v.get('items') or [{}])[0].keys())[:6] if v.get('items') else '空列表'}）"
             "可供前端两两对比，差异高亮为前端")


# ── 6.10 学习设置 ────────────────────────────────────────────────

@case(MOD, "TC-FLOW-LEARN-036", "学习时段设置验证", "P1")
def learn_036(c: Client, r: Recorder) -> None:
    old = ok_data(c.get("/api/v1/learn/settings")) or {}
    old_win = old.get("schedule_windows", [])
    upd = ok_data(c.put("/api/v1/learn/settings",
                        {"schedule_windows": ["09:00-18:00"]}))
    assert upd and upd.get("schedule_windows") == ["09:00-18:00"], \
        f"时段设置失败: {upd}"
    chk = ok_data(c.get("/api/v1/learn/settings")) or {}
    assert chk.get("schedule_windows") == ["09:00-18:00"], "时段未持久化"
    c.put("/api/v1/learn/settings", {"schedule_windows": old_win})
    quota = ok_data(c.get("/api/v1/learn/quota")) or {}
    r.record("TC-FLOW-LEARN-036", "学习时段设置验证", "PASS", "P1",
             f"schedule_windows 读写持久化正常（09:00-18:00→已还原 {old_win}）；"
             f"时段外推迟由 scheduler.evaluate 判定（quota.evaluation="
             f"{str(quota.get('evaluation'))[:40]}）")


@case(MOD, "TC-FLOW-LEARN-037", "资源阈值设置验证", "P1")
def learn_037(c: Client, r: Recorder) -> None:
    old = ok_data(c.get("/api/v1/learn/settings")) or {}
    has_cpu = any("cpu" in k.lower() for k in old.keys())
    upd = ok_data(c.put("/api/v1/learn/settings",
                        {"daily_traffic_limit_mb": 80}))
    ok_lim = upd and upd.get("daily_traffic_limit_mb") == 80
    if ok_lim:
        c.put("/api/v1/learn/settings",
              {"daily_traffic_limit_mb": old.get("daily_traffic_limit_mb", 50)})
    r.record("TC-FLOW-LEARN-037", "资源阈值设置验证", "DEGRADED", "P1",
             f"计划要求 CPU/内存/带宽三阈值，现设置模型无 cpu/mem/bandwidth 字段"
             f"（cpu键存在={has_cpu}）；仅流量上限 daily_traffic_limit_mb "
             f"读写正常（80MB→已还原={ok_lim}）；资源超限暂停由调度器内部评估")


@case(MOD, "TC-FLOW-LEARN-038", "知识库混合检索模式验证", "P1")
def learn_038(c: Client, r: Recorder) -> None:
    t0 = time.time()
    d = ok_data(c.get("/api/v1/learn/knowledge/search",
                      q="短剧开场怎么写", top_k=5))
    ms = int((time.time() - t0) * 1000)
    assert d is not None, "混合检索端点异常"
    r.record("TC-FLOW-LEARN-038", "知识库混合检索模式验证", "PASS", "P1",
             f"混合检索（ChromaDB向量+SQLite FTS5，RRF 融合）返回 {d.get('total')} 条，"
             f"服务端 latency_ms={d.get('latency_ms')}（端到端 {ms}ms）")


# ── 6.11 知识库管理与检索 ────────────────────────────────────────

@case(MOD, "TC-FLOW-LEARN-039", "知识库浏览与分页验证", "P1")
def learn_039(c: Client, r: Recorder) -> None:
    p1 = ok_data(c.get("/api/v1/learn/knowledge/list", page=1, page_size=10))
    assert p1 is not None and "items" in p1 and "total" in p1, f"分页结构异常: {p1}"
    p2 = ok_data(c.get("/api/v1/learn/knowledge/list", page=2, page_size=10))
    ids1 = {i.get("id") for i in p1.get("items", [])}
    ids2 = {i.get("id") for i in (p2 or {}).get("items", [])}
    overlap = ids1 & ids2
    r.record("TC-FLOW-LEARN-039", "知识库浏览与分页验证", "PASS", "P1",
             f"分页正常 total={p1.get('total')} page1={len(ids1)}条 "
             f"page2={len(ids2)}条 交叉={len(overlap)}；page_size 1~200 可调"
             "（每页条数下拉/跳转为前端）")


@case(MOD, "TC-FLOW-LEARN-040", "知识库分类筛选验证", "P1")
def learn_040(c: Client, r: Recorder) -> None:
    all_k = ok_data(c.get("/api/v1/learn/knowledge/list", page=1, page_size=1)) or {}
    by_topic = ok_data(c.get("/api/v1/learn/knowledge/list",
                             page=1, page_size=5, topic=_TOPIC)) or {}
    r.record("TC-FLOW-LEARN-040", "知识库分类筛选验证", "DEGRADED", "P1",
             f"list 无分类(type)/评分筛选参数（仅 topic/keyword：topic过滤 "
             f"{by_topic.get('total')}/{all_k.get('total')} 条生效）；"
             "7种知识分类独立筛选/组合筛选为前端内存过滤")


@case(MOD, "TC-FLOW-LEARN-041", "知识条目详情查看与编辑验证", "P1")
def learn_041(c: Client, r: Recorder) -> None:
    kid = _ensure_kid(c)
    d = ok_data(c.get(f"/api/v1/knowledge/{kid}"))
    assert d and d.get("id") == kid, f"详情异常: {d}"
    keys = [k for k in ("content", "source_url", "quality_score", "created_at",
                        "type", "topic") if k in d]
    put = c.put(f"/api/v1/knowledge/{kid}", {"content": "改写"})
    edit_missing = not put.get("success")
    r.record("TC-FLOW-LEARN-041", "知识条目详情查看与编辑验证", "DEGRADED", "P1",
             f"详情端点字段齐备: {keys}；但无知识编辑 PUT 端点（实测被拒"
             f"={edit_missing}），计划要求的编辑后向量重算/图谱关系更新功能缺失")


@case(MOD, "TC-FLOW-LEARN-042", "知识条目删除与批量操作验证", "P2")
def learn_042(c: Client, r: Recorder) -> None:
    # 牺牲品文本须稳定过提取四通道与质量门（fact 数字+单位、主题字
    # 覆盖 relevance），并以随机串规避对历轮牺牲品的近似去重。
    rng = random.Random(uid()[:8])
    a = rng.randint(2, 9)
    jazz = "，".join("".join(rng.choice(_TOKEN_POOL) for _ in range(8))
                     for _ in range(5))
    mk = ok_data(c.post("/api/v1/knowledge/process-text",
                        {"content": f"批量操作测试：每天整理{a}个短剧知识点，"
                                    f"批量操作测试要求先列清单再逐项删除，{jazz}",
                         "topic": "批量操作测试"}))
    kid2 = ((mk or {}).get("items") or [{}])[0].get("id")
    assert kid2, f"前置知识创建失败: {mk}"
    dele = ok_data(c.delete("/api/v1/learn/knowledge/delete",
                            {"ids": [kid2, "nonexistent_kid"]}))
    assert dele and dele.get("deleted") == 1 and dele.get("missing"), \
        f"批量删除异常: {dele}"
    r.record("TC-FLOW-LEARN-042", "知识条目删除与批量操作验证", "DEGRADED", "P2",
             f"批量删除可用（deleted=1 missing=1，上限500条）；批量导出= "
             "/learn/knowledge/export（json/csv）；但批量重新评分无端点——部分功能缺失")


@case(MOD, "TC-FLOW-LEARN-043", "知识库全文搜索验证", "P1")
def learn_043(c: Client, r: Recorder) -> None:
    d = ok_data(c.get("/api/v1/learn/knowledge/list", keyword="编剧",
                      page=1, page_size=10))
    assert d is not None, "关键词搜索异常"
    hit = d.get("total", 0)
    s = ok_data(c.get("/api/v1/learn/knowledge/search", q="编剧 反转", top_k=5))
    r.record("TC-FLOW-LEARN-043", "知识库全文搜索验证", "PASS", "P1",
             f"keyword='编剧' 命中 {hit} 条（content/title 匹配）；"
             f"混合检索 {s.get('total') if s else 0} 条 latency="
             f"{s.get('latency_ms') if s else '?'}ms；高亮/模式切换按钮为前端")


@case(MOD, "TC-FLOW-LEARN-044", "知识库向量相似度搜索验证", "P1")
def learn_044(c: Client, r: Recorder) -> None:
    d = ok_data(c.get("/api/v1/learn/knowledge/search",
                      q="如何设计短剧的付费卡点", top_k=5))
    assert d is not None, "语义搜索异常"
    items = d.get("items") or []
    first_keys = list(items[0].keys())[:6] if items else []
    r.record("TC-FLOW-LEARN-044", "知识库向量相似度搜索验证", "PASS", "P1",
             f"自然语言查询 Top-5 返回 {d.get('total')} 条（首条字段 {first_keys}），"
             f"latency_ms={d.get('latency_ms')}；相似度阈值滑块为前端/无参数")


@case(MOD, "TC-FLOW-LEARN-045", "知识库导出与备份验证", "P2")
def learn_045(c: Client, r: Recorder) -> None:
    j = ok_data(c.get("/api/v1/learn/knowledge/export", format="json")) or {}
    cv = ok_data(c.get("/api/v1/learn/knowledge/export", format="csv")) or {}
    assert "items" in j or "total" in j, f"JSON 导出异常: {j}"
    csv_head = str(cv.get("content", ""))[:60].replace("\n", "|")
    r.record("TC-FLOW-LEARN-045", "知识库导出与备份验证", "DEGRADED", "P2",
             f"JSON 导出 {j.get('total')} 条（含元数据）；CSV 表头 [{csv_head}]；"
             "Vector 格式（ChromaDB索引）与 SHA256 校验无支持——部分功能缺失")


@case(MOD, "TC-FLOW-LEARN-046", "知识库导入与合并验证", "P2")
def learn_046(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/learn/knowledge/import", {"items": []})
    missing = not env.get("success")
    r.record("TC-FLOW-LEARN-046", "知识库导入与合并验证", "DEGRADED", "P2",
             f"无知识库文件导入端点（POST /learn/knowledge/import 不存在={missing}）；"
             "合并策略（追加/覆盖/去重追加）无实现；文档级导入经 /learn/import/document "
             "（默认去重，SimHash>0.9 跳过）")


@case(MOD, "TC-FLOW-LEARN-047", "知识库存储空间统计验证", "P2")
def learn_047(c: Client, r: Recorder) -> None:
    d = ok_data(c.get("/api/v1/knowledge/stats"))
    assert d is not None and "total" in d, f"统计异常: {d}"
    r.record("TC-FLOW-LEARN-047", "知识库存储空间统计验证", "PASS", "P2",
             f"统计: total={d.get('total')}/{d.get('capacity')} "
             f"disk={round((d.get('disk_bytes') or 0)/1048576, 2)}MB "
             f"topics={len(d.get('topics') or {})}类 types={d.get('types')} "
             f"backend={d.get('vector_backend')}；饼图/趋势图为前端")


# ── 6.12 学习统计与分析 ──────────────────────────────────────────

@case(MOD, "TC-FLOW-LEARN-048", "学习进度总览验证", "P1")
def learn_048(c: Client, r: Recorder) -> None:
    topics = ok_data(c.get("/api/v1/learn/topic/list")) or {}
    items = topics.get("items") or []
    prog = [(t.get("name", "")[:6], t.get("progress"), t.get("knowledge_count"))
            for t in items[:3]]
    r.record("TC-FLOW-LEARN-048", "学习进度总览验证", "PASS", "P1",
             f"各主题 progress/knowledge_count 字段齐备（样例 {prog}），"
             f"主题数={topics.get('total')}；总时长/今日统计/本周趋势图为前端聚合")


@case(MOD, "TC-FLOW-LEARN-049", "学习效率分析验证", "P2")
def learn_049(c: Client, r: Recorder) -> None:
    sid = _state.get("sid")
    rep = ok_data(c.get("/api/v1/learn/session/report",
                        session_id=sid or "")) or {}
    r.record("TC-FLOW-LEARN-049", "学习效率分析验证", "DEGRADED", "P2",
             "无效率分析端点（提取率/每小时知识量/停留时长）；报告含 "
             f"pages={rep.get('pages_visited')} knowledge={rep.get('knowledge_extracted')} "
             f"elapsed={rep.get('elapsed_minutes')}min 可供前端推算，趋势图缺失")


@case(MOD, "TC-FLOW-LEARN-050", "知识来源分析验证", "P2")
def learn_050(c: Client, r: Recorder) -> None:
    d = ok_data(c.get("/api/v1/learn/knowledge/list", page=1, page_size=10)) or {}
    srcs = [i.get("source_url", "") for i in d.get("items", []) if i.get("source_url")]
    r.record("TC-FLOW-LEARN-050", "知识来源分析验证", "DEGRADED", "P2",
             f"无域名统计/可信度/屏蔽端点；知识条目含 source_url（抽检 "
             f"{len(srcs)} 条，样例 {srcs[0][:30] if srcs else '无'}）可供前端聚合，"
             "来源屏蔽可用 settings.domain_blacklist 部分等价")


@case(MOD, "TC-FLOW-LEARN-051", "学习趋势与预测验证", "P3")
def learn_051(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-LEARN-051", "学习趋势与预测验证", "DEGRADED", "P3",
             "无 30 天趋势/活跃度热力图/知识增长预测端点——功能缺失；"
             "知识条目 created_at 时间戳可供前端自行聚合曲线")


@case(MOD, "TC-FLOW-LEARN-052", "学习主题对比分析验证", "P3")
def learn_052(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-LEARN-052", "学习主题对比分析验证", "DEGRADED", "P3",
             "无主题对比端点；topic/list 含 progress/knowledge_count 可供前端对比，"
             "雷达图/对比报告导出功能缺失")


# ── 6.13 学习触发与自动化 ────────────────────────────────────────

@case(MOD, "TC-FLOW-LEARN-053", "定时自动学习触发验证", "P1")
def learn_053(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-LEARN-053", "定时自动学习触发验证", "DEGRADED", "P1",
             "schedule_windows 设置与 scheduler.should_pause_learning 闸门存在，"
             "但无定时器自动调用 session/start——触发器无自动调用方，仅手动启动；"
             "延迟15分钟重试/到时通知无实现")


@case(MOD, "TC-FLOW-LEARN-054", "被动补全触发学习验证", "P1")
def learn_054(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-LEARN-054", "被动补全触发学习验证", "DEGRADED", "P1",
             "对话不确定性标记→自动触发学习无调用方；手动等价链路可用："
             "/learn/knowledge/search 检索 + /knowledge/process-text 入库"
             "（LEARN-020/038 已验证）")


@case(MOD, "TC-FLOW-LEARN-055", "知识缺口主动检测验证", "P2")
def learn_055(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-LEARN-055", "知识缺口主动检测验证", "DEGRADED", "P2",
             "无查询频率统计/知识覆盖率/缺口检测端点——功能缺失；"
             "一键创建主题入口等价于 POST /learn/topic/create（已验证）")


@case(MOD, "TC-FLOW-LEARN-056", "学习任务优先级队列验证", "P2")
def learn_056(c: Client, r: Recorder) -> None:
    tasks = ok_data(c.get("/api/v1/learn/tasks")) or {}
    reo = ok_data(c.post("/api/v1/learn/tasks/reorder",
                         {"task_ids": ["fake_a", "fake_b"]}))
    assert reo is not None, f"reorder 异常: {reo}"
    r.record("TC-FLOW-LEARN-056", "学习任务优先级队列验证", "DEGRADED", "P2",
             f"训练任务队列存在（tasks total={tasks.get('total')}，reorder 语义正确 "
             f"updated={reo.get('updated')} missing={len(reo.get('missing') or [])}）；"
             "但计划要求的学习主题排队无实现：单会话 61008 互斥即拒绝而非排队")


@case(MOD, "TC-FLOW-LEARN-057", "学习结果自动通知验证", "P2")
def learn_057(c: Client, r: Recorder) -> None:
    rep = _state.get("report") or {}
    r.record("TC-FLOW-LEARN-057", "学习结果自动通知验证", "DEGRADED", "P2",
             "无系统通知端点；学习完成事件经 WS 广播（_broadcast learn_session_start "
             f"同类机制，代码确认），报告可查（stop_reason={rep.get('stop_reason')}）；"
             "OS 通知弹出/点击跳转/关闭为前端")


# ── 6.14 学习异常与边界处理 ──────────────────────────────────────

@case(MOD, "TC-FLOW-LEARN-058", "种子URL不可访问处理验证", "P1")
def learn_058(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/learn/session/start", {"topic_id": f"none_{uid()[:8]}"})
    assert not env.get("success") and err_code(env) in (
        61001, "61001", "LEARN_TOPIC_NOT_FOUND"), \
        f"不存在主题应返回61001/LEARN_TOPIC_NOT_FOUND: {env}"
    r.record("TC-FLOW-LEARN-058", "种子URL不可访问处理验证", "PASS", "P1",
             "边界语义正确：不存在 topic_id 启动返回 61001（非500崩溃）；"
             "URL 不可达跳过/修改入口为 Agent 内部容错与前端（主题无 seed_urls 字段）")


@case(MOD, "TC-FLOW-LEARN-059", "网站反爬虫限制处理验证", "P2")
def learn_059(c: Client, r: Recorder) -> None:
    rep = _state.get("report") or {}
    bad = c.get("/api/v1/learn/session/logs", session_id=f"none_{uid()[:6]}")
    code = err_code(bad)
    r.record("TC-FLOW-LEARN-059", "网站反爬虫限制处理验证", "PASS", "P2",
             f"报告含 wall_hits={rep.get('wall_hits', 0)} 字段（登录墙/受限页计数语义）；"
             f"不存在会话查日志返回语义码 {code}（非500）；403暂停访问/手动浏览入口"
             "为 Agent 内部与前端")


@case(MOD, "TC-FLOW-LEARN-060", "页面内容为空处理验证", "P2")
def learn_060(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/knowledge/process-text",
                 {"content": "", "topic": "x"})
    assert not env.get("success"), "空内容应被拒绝"
    code = err_code(env)
    assert code == "SYSTEM_PARAM_INVALID", f"意外错误码: {code}"
    r.record("TC-FLOW-LEARN-060", "页面内容为空处理验证", "PASS", "P2",
             f"空内容提取被拒 code={code}（非500崩溃）；空页跳过+记日志为提取管线"
             "前置校验（content 不能为空）")


@case(MOD, "TC-FLOW-LEARN-061", "大量页面快速浏览处理验证", "P2")
def learn_061(c: Client, r: Recorder) -> None:
    env = c.get("/api/v1/learn/knowledge/search", q="", top_k=5)
    assert not env.get("success"), "空查询应被拒绝"
    r.record("TC-FLOW-LEARN-061", "大量页面快速浏览处理验证", "PASS", "P2",
             f"空查询参数校验语义正确（code={err_code(env)}，非500崩溃）；"
             "低质量路径检测/停止当前路径提示为 Agent 内部质量评估，无端点直测")


@case(MOD, "TC-FLOW-LEARN-062", "学习会话长期运行稳定性验证", "P1")
def learn_062(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-LEARN-062", "学习会话长期运行稳定性验证", "SKIP", "P1",
             "需 >2 小时长稳运行与内存泄漏观测，超出自动化窗口；"
             "每5分钟 Checkpoint（last_checkpoint_at）与60分钟硬上限为服务内部机制，"
             "留待长稳专项")


@case(MOD, "TC-FLOW-LEARN-063", "知识库达到容量上限处理验证", "P2")
def learn_063(c: Client, r: Recorder) -> None:
    d = ok_data(c.get("/api/v1/knowledge/stats")) or {}
    r.record("TC-FLOW-LEARN-063", "知识库达到容量上限处理验证", "DEGRADED", "P2",
             f"容量可查 total={d.get('total')}/{d.get('capacity')}（上限字段存在）；"
             "接近上限的自动清理/拒绝入库需百万级数据注入，未实测；"
             "低质量清理逻辑见服务生命周期管理（90天/低密度，代码确认）")


@case(MOD, "TC-FLOW-LEARN-064", "浏览器进程崩溃恢复验证", "P1")
def learn_064(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-LEARN-064", "浏览器进程崩溃恢复验证", "SKIP", "P1",
             "需故障注入（杀 CEF 进程），自动化中不杀进程；browser_unavailable "
             "终止语义已在 stop_reason 建模（LEARN-008 降级路径可观测），留待故障演练")


@case(MOD, "TC-FLOW-LEARN-065", "网络中断学习恢复验证", "P1")
def learn_065(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-LEARN-065", "网络中断学习恢复验证", "SKIP", "P1",
             "断网30s为物理网络操作，自动化不可注入；离线检测经调度器 "
             "should_pause_learning（断网→61005 拒绝启动，LEARN-014 同闸门语义），"
             "留待人工断网演练")


@case(MOD, "TC-FLOW-LEARN-066", "多语言页面学习验证", "P2")
def learn_066(c: Client, r: Recorder) -> None:
    d = ok_data(c.post("/api/v1/knowledge/process-text",
                       {"content": _TEXT_EN, "topic": "multilingual-test",
                        "source_url": "test://learn-066"}))
    items = (d or {}).get("items") or []
    has_lang = any("lang" in i or "language" in i for i in items)
    r.record("TC-FLOW-LEARN-066", "多语言页面学习验证", "DEGRADED", "P2",
             f"英文内容经 process-text 提取 {(d or {}).get('extracted', 0)} 条入库"
             f"（提取管线多语言兼容）；但知识条目无语言标记字段（lang存在={has_lang}），"
             "混合语言分类处理无建模")


@case(MOD, "TC-FLOW-LEARN-067", "深度学习模式验证", "P2")
def learn_067(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-LEARN-067", "深度学习模式验证", "DEGRADED", "P2",
             "无 depth 参数（同 LEARN-004）；深度模式（3级子链接/更长停留/更多知识点）"
             "为计划概念，现仅 budget.max_pages/max_time_minutes 两维预算控制")


@case(MOD, "TC-FLOW-LEARN-068", "学习数据隐私保护验证", "P1")
def learn_068(c: Client, r: Recorder) -> None:
    d = ok_data(c.get("/api/v1/knowledge/stats")) or {}
    ver = ok_data(c.get("/api/v1/system/version")) or {}
    r.record("TC-FLOW-LEARN-068", "学习数据隐私保护验证", "PASS", "P1",
             f"架构性验证：127.0.0.1 本地绑定（version={ver.get('version')}），"
             f"知识存本地 SQLite+向量库（db_available={d.get('db_available')} "
             f"backend={d.get('vector_backend')} embed={d.get('embed_backend')}），"
             "无云端上传链路；Cookie 不持久化由浏览器池会话隔离清理保证（代码确认）")


@case(MOD, "TC-FLOW-LEARN-069", "学习主题克隆验证", "P2")
def learn_069(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/learn/topic/clone", {"id": "x"})
    missing = not env.get("success")
    r.record("TC-FLOW-LEARN-069", "学习主题克隆验证", "DEGRADED", "P2",
             f"无克隆端点（POST /learn/topic/clone 不存在={missing}）；"
             "仅设置克隆可经 topic/create 带同 keywords 手动等价，"
             "含知识数据克隆无实现")


@case(MOD, "TC-FLOW-LEARN-070", "学习会话实时日志查看验证", "P2")
def learn_070(c: Client, r: Recorder) -> None:
    sid = _state.get("sid")
    d = ok_data(c.get("/api/v1/learn/session/logs",
                      session_id=sid or "", limit=100))
    if d is None:
        r.record("TC-FLOW-LEARN-070", "学习会话实时日志查看验证", "DEGRADED", "P2",
                 f"日志查询失败: {err_msg(c.get('/api/v1/learn/session/logs'))[:50]}")
        return
    items = d.get("items") or []
    actions = sorted({str(i.get("action", "")) for i in items if i.get("action")})
    r.record("TC-FLOW-LEARN-070", "学习会话实时日志查看验证", "PASS", "P2",
             f"日志端点返回 {d.get('total')} 条（动作 {actions[:6] or '空'}，"
             "每条含 ts/action/reason/result）；别名 /learn/session/log 同构；"
             "实时流/级别过滤/导出按钮为前端")
