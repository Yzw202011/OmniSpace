"""推理后资产自动绑定（2026-10-04 用户令：推理出的无图资产桩应自动
绑定到分镜行）。

绑定真源=storyboard_rows.asset_ids（关键帧/视频链只认它）；推理历史
只回填名称列（characters/scene/props 文本），名称→绑定的最后一米
全靠手动——本模块补上。

解析优先级（同名歧义钳制，全局库同名资产真实存在如 林飞雪×3）：
  项目内同名 > 全局唯一同名 > 同优先级多同名=跳过（留用户手动挑）。
只填空 asset_ids（不覆盖手动绑定）；行三列名称按 kind 精确配对。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368

from __future__ import annotations

import logging

from ...data.database import Database, parse_json

log = logging.getLogger("omnispace.api.manga.asset_autobind")


def _split_names(raw: str) -> list[str]:
    """场景/道具等单字符串列的宽容拆分（顿号/逗号分隔均可）。"""
    return [p.strip() for p in str(raw or "").replace("，", ",").replace(
        "、", ",").split(",") if p.strip()]


def _row_names(row: dict) -> list[tuple[str, str]]:
    """行三列 → (kind, name) 有序清单（角色→场景→道具，保持稳定顺序）。"""
    out: list[tuple[str, str]] = []
    chars = parse_json(row.get("characters"), [])
    if isinstance(chars, list):
        out += [("character", str(n).strip()) for n in chars if str(n).strip()]
    out += [("scene", n) for n in _split_names(row.get("scene") or "")]
    props = parse_json(row.get("props"), [])
    if isinstance(props, list):
        out += [("prop", str(n).strip()) for n in props if str(n).strip()]
    return out


def auto_bind_rows(db: Database, project_id: str,
                    rows: list[dict]) -> dict:
    """把行名称列解析为 asset_ids 写入（仅空绑定行）。

    rows 需含 id/characters/scene/props/asset_ids 原始行（调用方回填
    名称列后重查的最新行）。返回 {bound_rows, bound_assets,
    skipped_ambiguous}；db.update 内部把 list 序列化为 JSON（与分镜行
    update 路径同一约定）。
    """
    # 候选索引：(kind, name) -> [(id, priority)]，0=项目内 1=全局
    idx: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for prio, cond, params in (
            (0, "project_id=? AND scope<>'global'", (project_id,)),
            (1, "scope='global'", ())):
        try:
            for r in db.query("SELECT id, kind, name FROM comic_assets"
                              f" WHERE {cond}", params):
                name = str(r.get("name") or "").strip()
                kind = str(r.get("kind") or "")
                if name:
                    idx.setdefault((kind, name), []).append(
                        (str(r["id"]), prio))
        except Exception as exc:  # noqa: BLE001 - 索引失败=本次不绑不阻断
            log.warning("自动绑定候选索引失败（跳过绑定）: %s", exc)
            return {"bound_rows": 0, "bound_assets": 0,
                    "skipped_ambiguous": 0}
    if not idx:
        return {"bound_rows": 0, "bound_assets": 0, "skipped_ambiguous": 0}

    bound_rows = 0
    bound_assets = 0
    skipped = 0
    for row in rows:
        existing = parse_json(row.get("asset_ids"), [])
        if isinstance(existing, list) and existing:
            continue  # 手动绑定不覆盖（同实体列回填哲学）
        ids: list[str] = []
        for kind, name in _row_names(row):
            cands = idx.get((kind, name)) or []
            if not cands:
                continue
            best = min(p for _, p in cands)
            top = [aid for aid, p in cands if p == best]
            if len(top) > 1:
                skipped += 1  # 同优先级同名歧义（如全局×3）留手动
                continue
            if top[0] not in ids:
                ids.append(top[0])
        if ids:
            db.update("storyboard_rows", {"asset_ids": ids},
                      "id=?", (row["id"],))
            bound_rows += 1
            bound_assets += len(ids)
    if bound_rows or skipped:
        log.info("自动绑定: %d 行 / %d 资产 / 歧义跳过 %d 处",
                 bound_rows, bound_assets, skipped)
    return {"bound_rows": bound_rows, "bound_assets": bound_assets,
            "skipped_ambiguous": skipped}
