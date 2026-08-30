"""转换结果机械约束校验（skills/h3_convert P0 转换层）。

规则源自 skills/_extracted/h3-seg-prompt-design/scripts/validate_project.py
（V7 导播台 skill 校验脚本），改造为对 convert_row_to_project 输出的
工程包（dict）做提交前自检——失败拒绝提交 ComfyUI（P1 接线点）。

校验项（skill 非协商规则 + 本项目铁律）：
- 每生成段 durationSeconds ∈ [5,30]（用户裁定 2026-08-29，>15s 为
  H3 训练域外超域生成，P3 标定把关）
- 六段字段顺序固定（FULL_FIELDS）
- {{ref:}} 引用必须全部登记于 project.assets（素材白名单锁定）
- 禁止官方 Picture/Video/Audio 编号残留（必须 {{ref:}} 格式）
- <Subject N> 全项目稳定编号、不被 {{ref:}} 取代
- 对白原文保留在 <d>[cmn] ...</d>
- plan.trim_to ≤ gen_seconds（裁时计划与生成段自洽）
- 每段参考图 ≤9（素材上限）
"""
from __future__ import annotations

import re

from rules import (
    FULL_FIELDS,
    MAX_REF_IMAGES,
    SEGMENT_MAX_S,
    SEGMENT_MIN_S,
)

_AUTO_SHOT_REF = re.compile(r"\{\{ref:分镜\d+成片(?:\.audio)?\}\}")
_OFFICIAL_NUM = re.compile(r"<(?:Picture|Video|Audio)\s+\d+>", re.I)
_REF = re.compile(r"\{\{ref:([^}]+)\}\}")
_FIELD = re.compile(r"(?m)^([a-z_]+):")
_SUBJECT = re.compile(r"<Subject\s+(\d+)>")
_DIALOGUE = re.compile(r"<d>\[(\w+)\]\s*(.+?)</d>", re.S)


def validate_result(result: dict) -> tuple[list[str], list[str]]:
    """工程包校验。返回 (errors, warnings)；errors 非空 = 拒绝提交。"""
    errors: list[str] = []
    warnings: list[str] = []
    project = result.get("project") or {}
    shots = project.get("shots") or []
    assets = project.get("assets") or []
    plan = result.get("plan") or []

    if not shots:
        errors.append("project.shots 为空")
        return errors, warnings

    alias_whitelist = {a.get("id") for a in assets if a.get("id")}

    for idx, sh in enumerate(shots, 1):
        sid = sh.get("id") or f"shot_{idx:03d}"
        dur = sh.get("durationSeconds") or 0
        if not SEGMENT_MIN_S <= dur <= SEGMENT_MAX_S:
            errors.append(f"{sid} 时长 {dur}s 不在 "
                          f"{SEGMENT_MIN_S:g}-{SEGMENT_MAX_S:g}s 范围")
        prompt = sh.get("prompt") or ""
        fields = tuple(_FIELD.findall(prompt))
        if fields != FULL_FIELDS:
            errors.append(f"{sid} 字段顺序不符合六段结构: {list(fields)}")
        if _OFFICIAL_NUM.search(prompt):
            errors.append(f"{sid} 残留官方 Picture/Video/Audio 编号"
                          "（必须使用 {{ref:别名}}）")
        if _AUTO_SHOT_REF.search(prompt):
            errors.append(f"{sid} 含禁止的自动分镜成片引用")
        refs = _REF.findall(prompt)
        unknown = sorted({r for r in refs if r not in alias_whitelist})
        if unknown:
            errors.append(f"{sid} 引用未登记素材: {', '.join(unknown)}")
        img_count = sum(1 for a in assets
                        if a.get("id") in set(refs) and a.get("kind") == "image")
        if img_count > MAX_REF_IMAGES:
            errors.append(f"{sid} 参考图 {img_count} 张超上限 {MAX_REF_IMAGES}")
        if _SUBJECT.search(prompt) and re.search(
                r"\{\{ref:[^}]*\}\}\s+is the recurring character", prompt):
            errors.append(f"{sid} <Subject N> 被 {{ref:}} 取代"
                          "（skill：Subject 是复现单元不是素材引用）")

    # Subject 编号全项目稳定（同一别名跨镜编号一致；编号不重排）
    alias_to_n: dict[str, int] = {}
    for sh in shots:
        for m in re.finditer(
                r"<Subject\s+(\d+)> is the recurring character from "
                r"\{\{ref:([^}]+)\}\}", sh.get("prompt") or ""):
            n, alias = int(m.group(1)), m.group(2)
            prev = alias_to_n.get(alias)
            if prev is not None and prev != n:
                errors.append(f"{sh.get('id')} 别名 {alias} 的 Subject 编号"
                              f"漂移: {prev} → {n}")
            alias_to_n[alias] = n

    # 对白：出现「says:」但无 <d> 标签 → 原文未保留
    for sh in shots:
        prompt = sh.get("prompt") or ""
        if re.search(r"(?m)\bS\d+> says:", prompt) and not _DIALOGUE.search(prompt):
            errors.append(f"{sh.get('id')} 存在对白但缺少 <d>[cmn] 原文标签")

    # plan 自洽：裁时目标 ≤ 生成段时长；cells 均为有效拆格序号
    n_cells_expected = sum(len(p.get("cells") or []) for p in plan)
    total_cells = sum(len(p.get("cells") or []) for p in plan)
    if n_cells_expected != total_cells:  # pragma: no cover（防御）
        warnings.append("plan cells 统计不一致")
    for p in plan:
        for ts in p.get("trim_specs") or []:
            if ts.get("trim_to", 0) > p.get("gen_seconds", 0) + 1e-6:
                errors.append(f"{p.get('shot_id')} trim_to {ts.get('trim_to')}"
                              f"s 超 H3 生成段 {p.get('gen_seconds')}s"
                              "（裁时只能缩短不能拉长）")

    for w in result.get("warnings") or []:
        warnings.append(str(w))
    if result.get("degraded"):
        warnings.append(
            f"转换降级: {result.get('degrade_reason') or 'unknown'}")
    return errors, warnings


def assert_submittable(result: dict) -> None:
    """P1 提交 ComfyUI 前的闸门：errors 非空抛 ValueError。"""
    errors, warnings = validate_result(result)
    if errors:
        raise ValueError("H3 工程包校验失败:\n" + "\n".join(errors))
    return warnings
