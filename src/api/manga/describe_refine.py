"""ai-describe 二遍精修（refine-prompt 六要素方法论落地 · 2026-08-30）。

════════════════════════════════════════════════════════════════════
【撤回锚点】本文件为独立新增模块；storyboard.py 端点内仅 3 行调用
（grep "REFINE-INTEGRATION"）。撤回 = 删本文件 + 删那 3 行。
默认开关关闭（system_settings 无 key 时零行为变化）。
════════════════════════════════════════════════════════════════════

流程（ai-describe 的 _finalize_abc_body 之后）：
1. 六要素体检（temperature=0 评审调用）→ score < threshold 触发重写
2. 重写（只修问题不加戏，长度 0.75~1.5 倍钳制——refine-prompt 铁律）
3. 重写产物复过 _finalize_abc_body（网格标记/首帧保持段/镜头数兜底
   等代码拼装铁律不破坏）+ 复评一次
4. 任一环节异常 → 记日志放行原稿（绝不阻塞主流程）

资产冲突检测：评审只读资产 name/kind 元数据（V49 协议合规，keyframe
侧禁读资产描述词的裁定不受影响——本模块在描述词生成侧，非生图侧）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...data.database import Database

log = logging.getLogger("omnispace.api.manga.describe_refine")

_REFINE_KEY = "manga.describe_refine"
_DEFAULT_THRESHOLD = 75
_ASSET_KIND_ZH = {"character": "角色", "scene": "场景", "prop": "道具"}

# ── 提示词（版本锁定：改动走 git，防止精修模板漂移） ─────────────
_SCORE_PROMPT = """你是漫剧分镜描述词质检员。对下面的 A/B/C 描述词做六项体检，取最低分为总分，并列出具体问题。

六项体检表：
1. 任务：C 段每个镜头的画面动作是否明确可画（谁、在哪、做什么）
2. 画风：是否为网漫/漫画风——出现 3D、PBR、CG、游戏渲染、写实照片等词即违规
3. 格式：B 段是否为高密度世界观构建；C 段是否为时间轴镜头行（[起s-止s]）
4. 资产：描述词与绑定资产清单是否矛盾（如资产名「黑色硬壳行李箱」而描述写「白色行李箱」）；绑定的角色是否在 C 段出场
5. 一致性：B 段外貌/设定描写与 C 段画面是否互相矛盾
6. 边界：单镜时长是否在 2-4s 合理范围、运镜描述是否可实现

只输出 JSON：{{"score": 0到100的整数, "issues": ["问题1", ...]}}
无问题时 issues 为空数组。不要输出任何其他内容。

【绑定资产】
{assets_text}

【待检描述词】
{description}"""

_REWRITE_PROMPT = """你是漫剧分镜描述词修笔师。下面的描述词体检未达标，请修正后重写。

铁律：
- 只修正列出的问题，禁止添加原文没有的剧情、角色、道具
- 重写后总长度必须在原文的 0.75~1.5 倍之间
- 描述词与资产冲突时，以资产名称为准改写
- 输出：首行「氛围：…」，然后「B. 高密度世界观构建：…」，然后 C 段时间轴镜头行
- 不输出 A 段（画风段由系统拼装）、不输出任何解释

【体检问题】
{issues}

【绑定资产】
{assets_text}

【原文】
{description}"""


def read_refine_config(db: Database | None) -> dict:
    """读精修开关（默认关：无 key/读库失败/解析失败 → enabled=False）。"""
    config = {"enabled": False, "threshold": _DEFAULT_THRESHOLD}
    if db is None:
        return config
    try:
        row = db.query_one(
            "SELECT value FROM system_settings WHERE key=?", (_REFINE_KEY,))
        if not row:
            return config
        raw = json.loads(row["value"])
        if isinstance(raw, dict):
            config["enabled"] = bool(raw.get("enabled", False))
            t = int(raw.get("threshold", _DEFAULT_THRESHOLD))
            config["threshold"] = min(max(t, 0), 100)
    except Exception:  # noqa: BLE001 - 配置异常按关闭处理
        log.warning("精修开关读取失败，按关闭处理", exc_info=True)
    return config


def write_refine_config(enabled: bool, threshold: int = _DEFAULT_THRESHOLD) -> None:
    """持久化精修开关（供运维/前端设置页调用）。"""
    from ...data.database import get_db_safe

    db = get_db_safe()
    if db is not None:
        db.sql(
            "INSERT INTO system_settings (key, value, updated_at)"
            " VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET"
            " value=excluded.value, updated_at=excluded.updated_at",
            (_REFINE_KEY,
             json.dumps({"enabled": bool(enabled),
                         "threshold": min(max(int(threshold), 0), 100)}),
             __import__("time").time()))


def _assets_brief(assets: list[dict]) -> str:
    if not assets:
        return "（无绑定资产）"
    lines = [f"- {_ASSET_KIND_ZH.get(a.get('kind'), a.get('kind') or '资产')}"
             f"【{a.get('name') or '未命名'}】" for a in assets]
    return "\n".join(lines)


def _parse_score_json(raw: str) -> tuple[int, list[str]] | None:
    """容忍围栏/前后杂文，提取首个 JSON 对象。失败 → None。"""
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", (raw or "").strip()).strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        score = int(obj.get("score", 0))
        issues = obj.get("issues") or []
        if not isinstance(issues, list):
            issues = [str(issues)]
        return min(max(score, 0), 100), [str(i) for i in issues if str(i).strip()]
    except (ValueError, TypeError, AttributeError):
        return None


async def refine_description(
    engine: Any, description: str, assets: list[dict], db: Database | None,
    finalize: Callable[[str], str],
) -> dict | None:
    """二遍精修入口（ai-describe 端点在持有 dialog 锁的上下文内调用）。

    finalize: _finalize_abc_body 部分应用（style_line/required_shots/
    duration 已绑定）——重写产物复用同一代码拼装管线保证格式铁律。
    返回 None = 开关关/异常放行（外层行为零变化）；
    返回 dict = {"triggered", "score", "rescore", "issues", "description"}。
    """
    from ...services.offload import run_blocking

    config = read_refine_config(db)
    if not config["enabled"]:
        return None
    brief = _assets_brief(assets)
    try:
        raw = await run_blocking(
            engine.chat,
            [{"role": "user", "content": _SCORE_PROMPT.format(
                assets_text=brief, description=description)}],
            temperature=0.0, max_new_tokens=512)
    except Exception:  # noqa: BLE001 - 评审失败放行原稿
        log.warning("精修评审调用失败，放行原稿", exc_info=True)
        return None
    verdict = _parse_score_json(raw)
    if verdict is None:
        log.info("精修评审输出不可解析，放行原稿：%.80s", (raw or "")[:80])
        return None
    score, issues = verdict
    if score >= config["threshold"]:
        return {"triggered": False, "score": score, "rescore": None,
                "issues": issues, "description": description}

    # ── 触发重写 ──
    try:
        rewritten = (await run_blocking(
            engine.chat,
            [{"role": "user", "content": _REWRITE_PROMPT.format(
                issues="\n".join(f"- {i}" for i in issues) or "-（无明细）",
                assets_text=brief, description=description)}],
            temperature=0.4, max_new_tokens=1536)).strip()
    except Exception:  # noqa: BLE001 - 重写失败放行原稿
        log.warning("精修重写调用失败，放行原稿", exc_info=True)
        return {"triggered": False, "score": score, "rescore": None,
                "issues": issues, "description": description}
    final_body = finalize(rewritten)
    if not final_body or len(final_body) < len(description) * 0.4:
        # 重写稿结构不合法（finalize 后为空/过短）→ 放行原稿不静默吞
        log.info("精修重写稿结构不合法，放行原稿（score=%d）", score)
        return {"triggered": False, "score": score, "rescore": None,
                "issues": issues, "description": description}

    # ── 复评（结果回填不阻塞） ──
    rescore = None
    try:
        raw2 = await run_blocking(
            engine.chat,
            [{"role": "user", "content": _SCORE_PROMPT.format(
                assets_text=brief, description=final_body)}],
            temperature=0.0, max_new_tokens=512)
        v2 = _parse_score_json(raw2)
        if v2 is not None:
            rescore = v2[0]
    except Exception:  # noqa: BLE001 - 复评失败仅记日志
        log.info("精修复评失败（不阻塞）", exc_info=True)
    log.info("描述词精修：初评 %d → 复评 %s（问题数 %d）",
             score, rescore, len(issues))
    return {"triggered": True, "score": score, "rescore": rescore,
            "issues": issues, "description": final_body}
