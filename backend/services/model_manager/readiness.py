"""模型就绪总检（2026-09-03 体验流 #3：绿灯/缺件指路）。

按 models_manifest.json 契约逐条核对盘上存在性，归并成功能模块级
绿/灰判定——「拖入→启动→首启激活→即用」最后一步"完美使用"的门面：
缺什么、缺几个、去哪补，一眼看全。

口径（与 docs/封装发行计划.md §2.4 对齐）：
  - 存在性＝models/<path>/ 是目录且含至少一个 ≥100MB 的权重文件
    （防「目录在而文件是空壳」的 16.5G 空壳失真旧案；不逐文件 du，
    命中即返回，260G 目录也秒回；扫描条目封顶防病态目录拖死请求）；
  - 模块归并：对话←dialog；绘画与漫剧关键帧←image_gen；漫剧视频←
    video_gen；知识学习←embedding；语音转写←asr；一致性度量←
    segmentation+auxiliary；
  - builtin 类型（embedding/asr/segmentation/auxiliary）随软件包内置
    发行：缺了只记 detail 警告不翻灰——包损坏不该被误读为「用户没
    放模型」；非内置缺失才是用户的「拖入大模型包」待办。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("omnispace.models.readiness")

# 内置件类型（随 ≤50G 软件包发行，见 make_dist 四辅助目录）
BUILTIN_TYPES = frozenset({"embedding", "asr", "segmentation", "auxiliary"})

# 功能模块 → 消费的 manifest 类型（display 顺序即输出顺序）
MODULE_DEFS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("dialog", "AI 对话", frozenset({"dialog"})),
    ("paint", "AI 绘画", frozenset({"image_gen"})),
    ("manga_keyframe", "漫剧关键帧", frozenset({"image_gen"})),
    ("manga_video", "漫剧视频", frozenset({"video_gen"})),
    ("knowledge", "知识学习", frozenset({"embedding"})),
    ("voice", "语音转写", frozenset({"asr"})),
    ("consistency", "一致性度量", frozenset({"segmentation", "auxiliary"})),
)

# 存在性判据（体积感知，2026-09-03 实弹修正）：
#   - 单文件 ≥50MB 立即在位（whisper-tiny 72MB / dinov2 86MB 这类小内置件，
#     旧「单文件 ≥100MB」判据会把它们冤枉成缺失——开发机 dinov2 实弹翻案）；
#   - 否则累计目录总量 ≥ max(50MB, 声称体积×0.5) 才算在位
#     （防 16.5G 空壳旧案：目录在、只有 config 碎文件）。
_BIG_FILE_BYTES = 50 * 1024 * 1024
# 病态目录保险丝：最多检查这么多个条目就下「不在位」结论
_SCAN_ENTRY_CAP = 4000


def _has_weight_file(entry_dir: Path, size_gb: float = 0.0) -> bool:
    """目录是否真有权重在位（命中即返回，封顶防拖死）。"""
    need_total = max(_BIG_FILE_BYTES, int((size_gb or 0.0) * 1e9 * 0.5))
    total = 0
    seen = 0
    try:
        for p in entry_dir.rglob("*"):
            seen += 1
            if seen > _SCAN_ENTRY_CAP:
                return False
            if not p.is_file():
                continue
            try:
                sz = p.stat().st_size
            except OSError:
                continue
            if sz >= _BIG_FILE_BYTES:
                return True
            total += sz
            if total >= need_total:
                return True
    except OSError:
        return False
    return False


def compute_readiness(models_root: Path, manifest_path: Path) -> dict:
    """模块级就绪总检（纯同步轻量计算；端点侧走 offload 不堵事件循环）。"""
    root = Path(models_root)
    result: dict = {
        "manifest_found": manifest_path.is_file(),
        "models_root": str(root),
        "modules": [],
        "missing_count": 0,
        "all_ready": False,
        "notes": [],
    }
    if not result["manifest_found"]:
        # 无清单＝空包首启（理论不应发生：清单随包 ESSENTIALS 哨兵）
        result["notes"].append("未找到 models_manifest.json（软件包可能不完整）")
        result["modules"] = [
            {"key": k, "label": lbl, "ready": False, "builtin": False,
             "present": [], "missing": []}
            for k, lbl, _ in MODULE_DEFS
        ]
        return result

    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
        entries: dict = manifest.get("models") or {}
    except (OSError, ValueError) as exc:
        result["notes"].append(f"清单不可读：{exc}")
        return result

    # 逐条核对盘上存在性（一次遍历，模块归并复用结果）
    present_by_type: dict[str, list[str]] = {}
    missing_by_type: dict[str, list[str]] = {}
    for mid, meta in entries.items():
        if not isinstance(meta, dict):
            continue
        mtype = str(meta.get("type") or "")
        rel = str(meta.get("path") or mid)
        if _has_weight_file(root / rel, float(meta.get("size_gb") or 0.0)):
            present_by_type.setdefault(mtype, []).append(mid)
        else:
            missing_by_type.setdefault(mtype, []).append(mid)

    builtin_missing_notes: list[str] = []
    for key, label, types in MODULE_DEFS:
        present = sorted(n for t in types for n in present_by_type.get(t, []))
        missing = sorted(n for t in types for n in missing_by_type.get(t, []))
        # 内置类型缺件不翻灰（包内置），但记录告警供诊断
        builtin = types <= BUILTIN_TYPES
        ready = bool(present) if not builtin else True
        if builtin and missing:
            builtin_missing_notes.append(
                f"{label}内置件缺失：{', '.join(missing)}（软件包可能损坏）")
        result["modules"].append({
            "key": key, "label": label, "ready": ready, "builtin": builtin,
            "present": present, "missing": missing,
        })
    result["notes"].extend(builtin_missing_notes)
    # 用户视角的缺件数＝非内置缺失「条目」数（按类型去重——image_gen 同
    # 时喂绘画/漫剧关键帧两个模块，逐模块累加会把 1 件模型数成 2）；
    # all_ready 跟「模块全绿」走（可选件缺席不算功能缺失，只提示补齐）
    consumed_types = {t for _k, _l, types in MODULE_DEFS
                      for t in types if t not in BUILTIN_TYPES}
    result["missing_count"] = sum(
        len(missing_by_type.get(t, [])) for t in consumed_types)
    result["all_ready"] = all(m["ready"] for m in result["modules"])
    return result
