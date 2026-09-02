"""风格包关键词 lint（2026-09-02 词序坑治理·护栏工具）。

新增/修改风格包后必跑：python tools/style_pack_lint.py

检查项（exit 1 才算失败）：
  1) PACK_PRIORITY 登记表与 STYLE_PACKS 逐一对应（漏登记=失败）；
  2) 内置包 keywords 非空且无空白项/包内重复（失败）；
  3) 跨包重复关键词（平局由优先级裁决——列出胜者，供人工确认）；
  4) 包含关系对（「国漫」是「国漫悬疑」的碎片——同位遮蔽下无害，
     列出供 awareness；若碎片词包优先级更低且无遮蔽关系才需警惕）；
  5) 优先级总表打印。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services.inference.gen_router import PACK_PRIORITY, STYLE_PACKS  # noqa: E402


def main() -> int:
    errors: list[str] = []

    sids = [p.sid for p in STYLE_PACKS]
    if len(sids) != len(set(sids)):
        errors.append("存在重复 sid")
    if set(PACK_PRIORITY) != set(sids):
        errors.append(f"优先级表与包集不一致: "
                      f"缺 {set(sids) - set(PACK_PRIORITY)} / "
                      f"多 {set(PACK_PRIORITY) - set(sids)}")

    for p in STYLE_PACKS:
        if not p.keywords:
            errors.append(f"{p.sid}: keywords 为空（内置包必须参与嗅探）")
        if any(not k.strip() for k in p.keywords):
            errors.append(f"{p.sid}: 存在空白关键词")
        if len(set(p.keywords)) != len(p.keywords):
            errors.append(f"{p.sid}: 包内关键词重复")

    # 跨包重复关键词：平局由优先级裁决，列出胜者
    owner: dict[str, str] = {}
    for p in sorted(STYLE_PACKS, key=lambda x: PACK_PRIORITY[x.sid]):
        for k in p.keywords:
            owner.setdefault(k, p.sid)
    dups = [(k, s) for k, s in owner.items()
            if sum(k in p.keywords for p in STYLE_PACKS) > 1]
    print("── 跨包重复关键词（平局→优先级小者胜）──")
    for k, winner in sorted(dups):
        holders = [p.sid for p in STYLE_PACKS if k in p.keywords]
        print(f"  「{k}」出现在 {holders}，胜者 {winner}")

    # 包含关系对：碎片词 ⊂ 复合词（同位遮蔽下复合词结构性胜出）
    print("── 包含关系对（碎片词 → 复合词，遮蔽规则下复合词胜）──")
    for p in STYLE_PACKS:
        for k in p.keywords:
            for q in STYLE_PACKS:
                if q is p:
                    continue
                for k2 in q.keywords:
                    if len(k2) > len(k) and k in k2:
                        print(f"  「{k}」({p.sid}) ⊂ 「{k2}」({q.sid})"
                              f" → {q.sid} 遮蔽胜")

    print("── 优先级总表 ──")
    for p in sorted(STYLE_PACKS, key=lambda x: PACK_PRIORITY[x.sid]):
        print(f"  {PACK_PRIORITY[p.sid]:>4}  {p.sid:<12} {p.label}")

    if errors:
        print("\n*** 失败项 ***", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
