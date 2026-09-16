"""小说模块 MVP 单测（批2，2026-09-05）。

覆盖（实施计划 §2.6）：
- 建表幂等：novel_* 五表存在，_init_schema 复跑不炸；
- 大纲/角色卡 JSON 解析（代码围栏/超量钳制/坏输出拒绝）；
- 章节 prompt 四件套拼装与世界观钳长；
- 正文清洗、伏笔体检、导出拼装（纯函数）；
- 串行队列：FIFO 串行、排队取消出队、运行取消检查点；
- API 冒烟：项目/章节/伏笔/导出全链（tmp 库隔离，不触发 lifespan，
  同 test_api_smoke 策略，绝不触碰 data/omnispace.db）。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database
from src.services.novel_service import (
    MAX_CHAPTERS_PER_VOLUME,
    MAX_OUTLINE_VOLUMES,
    NovelJob,
    NovelJobQueue,
    build_chapter_prompt,
    build_export_text,
    check_foreshadows,
    clean_chapter_text,
    detect_repetition,
    parse_characters_json,
    parse_outline_json,
)

_GOOD_OUTLINE = ('{"title":"书","logline":"主线",'
                 '"volumes":[{"title":"第一卷","summary":"卷概要",'
                 '"chapters":[{"title":"第一章","outline":"细纲一"}]}]}')

_ROOT = Path(__file__).resolve().parents[2]


# ── 建表幂等 ─────────────────────────────────────────────────────

@pytest.mark.schema
def test_novel_tables_created_and_idempotent(tmp_path):
    db = Database(tmp_path / "novel.db")
    conn = db._conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("novel_projects", "novel_outlines", "novel_chapters",
              "novel_characters", "novel_foreshadows"):
        assert t in tables, f"novel 表 {t} 未建"
    # 复跑幂等（重启/迁移重入不炸）
    db._init_schema()
    db._init_schema()


# ── JSON 解析 ────────────────────────────────────────────────────

def test_parse_outline_json_variants():
    d = parse_outline_json(f"```json\n{_GOOD_OUTLINE}\n```")
    assert d["title"] == "书"
    assert d["volumes"][0]["chapters"][0]["title"] == "第一章"
    # 前后混入废话也能抠出 JSON
    d2 = parse_outline_json("好的，以下是大纲：\n" + _GOOD_OUTLINE)
    assert d2["volumes"]
    # 超量钳制（卷≤5、每卷章≤20）
    big = {"title": "t", "logline": "", "volumes": [
        {"title": f"v{i}", "summary": "",
         "chapters": [{"title": f"c{j}", "outline": ""}
                      for j in range(30)]}
        for i in range(9)]}
    d3 = parse_outline_json(json.dumps(big, ensure_ascii=False))
    assert len(d3["volumes"]) == MAX_OUTLINE_VOLUMES
    assert len(d3["volumes"][0]["chapters"]) == MAX_CHAPTERS_PER_VOLUME
    with pytest.raises(ValueError):
        parse_outline_json("这不是 JSON")
    with pytest.raises(ValueError):
        parse_outline_json('{"title":"x"}')  # 缺 volumes


def test_parse_characters_json():
    raw = ('```json\n[{"name":"林川","role":"主角","summary":"坚毅"},'
           '{"name":"","role":"x","summary":"无名被滤"},'
           '{"name":"老魔","role":"反派","summary":"阴鸷"}]\n```')
    items = parse_characters_json(raw)
    assert [i["name"] for i in items] == ["林川", "老魔"]
    with pytest.raises(ValueError):
        parse_characters_json('[{"role":"主角"}]')  # 全部无名 → 空 → 拒


def test_repair_json_prefix_truncated_output():
    """截断 JSON 抢救（实弹 18:55 事故回归）：输出撞 token 上限拦腰断，
    修复器应砍残尾补闭合，抢救出已生成的卷/章。"""
    big = {"title": "书", "logline": "l", "volumes": [
        {"title": f"v{i}", "summary": "s",
         "chapters": [{"title": f"c{j}", "outline": "细" * 60}
                      for j in range(6)]}
        for i in range(3)]}
    full = json.dumps(big, ensure_ascii=False)
    # 拦腰截断（最后一个完整章节对象之后、数组未闭合处）
    cut_at = full.rfind("}", 0, int(len(full) * 0.6))
    truncated = full[:cut_at + 1]
    data = parse_outline_json(truncated)
    assert data["title"] == "书"
    assert 1 <= len(data["volumes"]) <= 3
    assert all(len(v["chapters"]) > 0 for v in data["volumes"])
    # 完整 JSON 原样通过（不破坏）
    assert parse_outline_json(full)["volumes"][0]["title"] == "v0"
    # 字符串未闭合的残尾：回退到最后一个完整对象边界抢救
    bad = ('{"title":"书","logline":"l","volumes":[{"title":"v0",'
           '"summary":"s","chapters":[{"title":"c0","outline":"o"},'
           '{"title":"残')
    data2 = parse_outline_json(bad)
    assert data2["volumes"][0]["chapters"][0]["title"] == "c0"


# ── prompt 拼装 / 正文清洗 / 伏笔 / 导出（纯函数）────────────────

def _mk_prompt(**kw):
    return build_chapter_prompt(
        project_name=kw.get("project_name", "测试书"),
        genre=kw.get("genre", "玄幻"),
        style_notes=kw.get("style_notes", ""),
        chapter_title=kw.get("chapter_title", "初入宗门"),
        outline_text=kw.get("outline_text", "主角拜师。"),
        prev_summaries=kw.get("prev_summaries", []) or [],
        prev_tail=kw.get("prev_tail", ""),
        characters=kw.get("characters", []) or [],
        worldview=kw.get("worldview", ""),
    )


def test_build_chapter_prompt_sections():
    prompt = _mk_prompt(
        prev_summaries=[(1, "开局"), (2, "遇袭")],
        prev_tail="他握紧了剑。",
        characters=[{"name": "林川", "role": "主角", "summary": "坚毅"}],
        worldview="灵气复苏设定。")
    for seg in ("【前情摘要】", "第2章", "【上一章结尾】", "【本章题目】初入宗门",
                "【本章细纲】主角拜师", "【主要角色】", "林川", "【世界观资料】"):
        assert seg in prompt, f"prompt 缺段落: {seg}"


def test_build_chapter_prompt_caps_and_omissions():
    wv = "设" * 5000
    prompt = _mk_prompt(worldview=wv)
    # 世界观钳到 RAG_MAX_CHARS=2000（2000 字上下，不精确等差）
    assert prompt.count("设") <= 2010
    # 空段落整段省略（按「段头+换行」判定，避开写作要求尾注的字样）
    assert "【主要角色】\n" not in prompt
    assert "【前情摘要】\n" not in prompt
    assert "【上一章结尾】\n" not in prompt


def test_clean_chapter_text():
    raw = "第一章 初入宗门\n他推开门。\n\n以下是正文：\n雪落了下来。"
    out = clean_chapter_text(raw)
    assert out.startswith("他推开门。")
    assert "雪落了下来。" in out


def test_detect_repetition():
    """复读检测（2026-09-05 用户实测事故：同 500 字段连抄 3 遍）。"""
    para = ("暴雨砸在城市上空，守夜人盯着积水里的新闻倒影，这是伪造的。"
            "新闻发布时间是凌晨三点，而气象数据的波动发生在两点五十分，"
            "时间差五分钟，这不可能是巧合。有人在操控新闻。他咬紧牙关，"
            "把城市停电、气象数据异常、新闻发布时间三个关键词依次敲进"
            "检索框，屏幕上跳出一条标题：暴雨致城市停电，气象局紧急"
            "通报无异常。")
    assert len(para) > 140  # 与实弹事故同量级（400 字免检线之上）
    looped = "\n\n".join([para] * 3)  # 用户实测形态：同段连抄
    frag = detect_repetition(looped)
    assert frag is not None, "复读文本未被检出"
    # 干净长文不误伤（递进式剧情，无 80 字级重复）
    clean = "".join(
        f"第{i}幕：林远走进{['地铁站','档案馆','气象局','旧报馆'][i % 4]}，"
        f"发现编号{i}的雨滴档案，线索指向第{i + 1}层真相，他继续深入。"
        for i in range(40))
    assert detect_repetition(clean) is None
    # 超短文本免检
    assert detect_repetition("太短。") is None


def test_chapter_prompt_forbids_repetition():
    prompt = _mk_prompt()
    assert "严禁复读" in prompt


def test_anti_repeat_param_plumbing_sentinel():
    """extra_params 通道必须全链贯通（engine→backend→vllm_service payload）。"""
    for rel in ("src/engines/vllm_service.py",
                "src/services/inference/dialog_engine.py",
                "src/services/inference/backends/vllm_backend.py"):
        src = (_ROOT / rel).read_text(encoding="utf-8")
        assert "extra_params" in src, f"{rel} 缺 extra_params 透传通道"
    src = (_ROOT / "src" / "engines" / "vllm_service.py").read_text(
        encoding="utf-8")
    assert "payload.update(extra_params)" in src, "vLLM 请求体未消费扩展参数"


def test_check_foreshadows():
    chapters = [{"id": "c1", "chapter_index": 1, "title": "t"},
                {"id": "c2", "chapter_index": 2, "title": "t"}]
    rows = [
        {"id": "f1", "status": "planted", "description": "玉佩来历",
         "planted_chapter_id": "c1", "payoff_chapter_id": ""},
        {"id": "f2", "status": "payoff", "description": "旧约",
         "planted_chapter_id": "c1", "payoff_chapter_id": "ghost"},
        {"id": "f3", "status": "dropped", "description": "弃",
         "planted_chapter_id": ""},
    ]
    r = check_foreshadows(rows, chapters)
    assert (r["planted_total"], r["payoff_total"],
            r["dropped_total"]) == (1, 1, 1)
    assert r["unrecalled"][0]["planted_chapter_index"] == 1
    assert r["dangling_payoff"][0]["id"] == "f2"


def test_build_export_text():
    project = {"name": "书A", "description": "desc"}
    chapters = [{"chapter_index": 2, "title": "乙", "content": "内容二"},
                {"chapter_index": 1, "title": "甲", "content": "内容一"}]
    txt = build_export_text(project, chapters, "txt")
    assert txt.index("第1章 甲") < txt.index("第2章 乙")  # 按章序
    md = build_export_text(project, chapters, "md")
    assert md.startswith("# 书A") and "### 第1章 甲" in md


# ── 串行队列 ─────────────────────────────────────────────────────

def test_queue_fifo_serial_and_cancel():
    q = NovelJobQueue()  # 裸实例（不碰全局单例）
    order: list[str] = []

    async def run_a(job, qq):
        order.append("a-start")
        await asyncio.sleep(0.01)
        order.append("a-end")

    async def run_cancel_self(job, qq):
        order.append("b-start")
        qq.request_cancel(job.task_id)        # 运行中置旗标
        qq.raise_if_cancelled(job.task_id)    # 检查点收割
        order.append("b-end")                 # 不应到达

    async def run_c(job, qq):
        order.append("c")

    async def main():
        await q.submit(NovelJob(task_id="a", kind="chapter", run=run_a))
        await q.submit(NovelJob(task_id="b", kind="chapter",
                                run=run_cancel_self))
        await q.submit(NovelJob(task_id="c", kind="chapter", run=run_c))
        assert q.position("c") == 3
        assert q.request_cancel("c") == "queued"  # 排队取消=直接出队
        assert q.position("c") is None
        for _ in range(100):
            await asyncio.sleep(0.05)
            if q._worker is None or q._worker.done():
                break

    asyncio.run(main())
    assert order == ["a-start", "a-end", "b-start"], (
        f"队列串行/取消语义破坏: {order}")


# ── API 冒烟（tmp 库隔离，不触发 lifespan）─────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    test_db = Database(tmp_path / "novel_api.db")
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1")


def test_novel_api_full_chain(client):
    # 建项目 + 重名拒绝
    r = client.post("/api/v1/novel/project/create", json={
        "name": "测试书", "genre": "玄幻", "description": "一个灵感"})
    assert r.status_code == 200 and r.json()["success"]
    pid = r.json()["data"]["project_id"]
    dup = client.post("/api/v1/novel/project/create", json={"name": "测试书"})
    assert dup.json()["success"] is False
    # 手工建章（带细纲）→ 人工保存正文 → 详情字数
    rc = client.post("/api/v1/novel/chapter/create", json={
        "project_id": pid, "title": "第一章", "outline": "主角登场"})
    assert rc.json()["success"]
    cid = rc.json()["data"]["chapter_id"]
    ru = client.put(f"/api/v1/novel/chapter/{cid}",
                    json={"content": "他推开门，风雪扑面。"})
    assert ru.json()["success"]
    rd = client.get(f"/api/v1/novel/chapter/{cid}")
    assert rd.json()["data"]["word_count"] > 0
    assert rd.json()["data"]["status"] == "done"
    # 伏笔登记 + 体检
    client.post("/api/v1/novel/foreshadows/create", json={
        "project_id": pid, "description": "玉佩的来历",
        "planted_chapter_id": cid})
    rf = client.get(f"/api/v1/novel/foreshadows/{pid}")
    assert rf.json()["data"]["check"]["unrecalled"][0][
        "description"] == "玉佩的来历"
    # 世界观入库（过质检闸：太短拒绝）
    rw_short = client.post("/api/v1/novel/worldbuilding/import", json={
        "project_id": pid, "content": "太短"})
    assert rw_short.json()["success"] is False
    # 导出（md）
    re_ = client.post("/api/v1/novel/export", json={
        "project_id": pid, "fmt": "md"})
    assert "第一章" in re_.json()["data"]["content"]
    # 列表统计
    rl = client.get("/api/v1/novel/project/list")
    item = rl.json()["data"]["items"][0]
    assert item["chapter_total"] == 1 and item["chapter_done"] == 1
    # 级联删除（章随项目没）
    client.delete(f"/api/v1/novel/project/{pid}")
    assert client.get(f"/api/v1/novel/chapter/{cid}").json()["success"] is False


def test_novel_generate_progress_idle(client):
    r = client.get("/api/v1/novel/generate/progress")
    assert r.json()["success"]
    assert r.json()["data"]["active"] is False


def test_novel_batch_skip_completed(client):
    """批量生成 skip_completed（用户迭代项 2026-09-07）：done 章跳过、
    pending/error 照常入队；缺省 False 保持全量重跑兼容语义。"""
    r = client.post("/api/v1/novel/project/create", json={"name": "跳过测"})
    pid = r.json()["data"]["project_id"]
    # 建 3 章：第1章 done、第2章 pending、第3章 error
    for title, content, status in [
            ("一", "正文一", "done"), ("二", "", "pending"),
            ("三", "", "error")]:
        rc = client.post("/api/v1/novel/chapter/create",
                         json={"project_id": pid, "title": title})
        cid = rc.json()["data"]["chapter_id"]
        if content or status != "pending":
            client.put(f"/api/v1/novel/chapter/{cid}",
                       json={"content": content, "status": status})
    # 章节手工建时 status=pending；直接改库把三章状态摆对
    from src.data.database import get_db
    db = get_db()
    rows = db.query("SELECT id, chapter_index FROM novel_chapters "
                    "WHERE project_id=? ORDER BY chapter_index", (pid,))
    db.update("novel_chapters", {"status": "done"},
              "id=?", (rows[0]["id"],))
    db.update("novel_chapters", {"status": "error", "error": "旧错"},
              "id=?", (rows[2]["id"],))
    # ① skip_completed=True：只入队 pending+error 两章
    r1 = client.post("/api/v1/novel/chapters/generate",
                     json={"project_id": pid, "skip_completed": True})
    d1 = r1.json()["data"]
    assert d1["queued"] == 2 and d1["skipped_completed"] == 1
    # 入队侧把两章置回 pending（worker 不在测试里跑）——复位后测全量语义
    db.update("novel_chapters", {"status": "pending", "progress": 0.0, "error": ""},
              "project_id=?", (pid,))
    # ② 缺省：全量（3 章全部重做，兼容旧语义）
    r2 = client.post("/api/v1/novel/chapters/generate",
                     json={"project_id": pid})
    d2 = r2.json()["data"]
    assert d2["queued"] == 3 and d2["skipped_completed"] == 0
# 本项目仅供学习使用，商业授权请+Q 3559331368
