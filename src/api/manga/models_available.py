"""漫剧模型清单端点（2026-09-23 自 video.py 拆出——巨石只拆不涨）。

video.py 已顶到 2000 行硬限（切换诚实性修复 +25 行致 2020），按
「巨石只拆不涨」纪律将非视频逻辑的 models/available 端点独立成模块；
URL 与行为零变化（同路径同响应）。

切换诚实性（2026-09-23 同步落地）：paint 槽在 gen_engine=comfy（默认）
时改由 comfy_paint_engine.COMFY_PAINT_MODELS 真源出——下拉可选 =
引擎白名单 = 物理真身，杜绝「选了 sdxl/2512 实际静默按 klein-9b
出图」的假切换；z-image-turbo 由此首次进漫剧可选清单。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query

from ...middleware.error_handler import ok
from .comic import _SPEED_TABLE, _TASK_CATEGORY_MAP
from .common import _paint_gen_engine_comfy

log = logging.getLogger("omnispace.api.manga.models_available")

router = APIRouter()


def _speed_label(vram_gb: float) -> str:
    for lo, hi, label in _SPEED_TABLE:
        if lo <= vram_gb < hi:
            return label
    return "标准"


@router.get("/manga/models/available")
async def list_available_models(task_type: str = Query("dialog")) -> dict[str, Any]:
    """返回指定任务类型可用的本地模型列表及状态（G2 工序弹窗数据源）。

    task_type: dialog | paint | video
    响应 items: [{id, name, status, vram_gb, speed_label, notes}]
    status: ready=已加载 | not_installed=已下载未加载 | offload=需先卸载其他
    """
    from ...api.models import _merged_models, get_module_model_scope
    from ...services.model_manager import get_model_manager
    mgr = get_model_manager()

    all_models = _merged_models()
    cat_filter = _TASK_CATEGORY_MAP.get(task_type, task_type)

    loaded_ids = {m["model_id"] for m in mgr.get_loaded_models()}
    gpu = mgr.get_gpu_status()
    total_gb = gpu.get("vram_total_gb", 0.0) if gpu.get("available") else 0.0

    comfy_paint_truth = (
        task_type == "paint" and _paint_gen_engine_comfy())
    items: list[dict] = []
    if comfy_paint_truth:
        from ...services.inference.comfy_paint_engine import (
            comfy_paint_models_ready,
        )
        for m in comfy_paint_models_ready():
            if m["status"] != "ready":
                continue  # 只展示已就绪（与下方 downloaded 口径对齐）
            vram = float(m["min_vram_gb"])
            items.append({
                "id": m["id"],
                "name": m["name"],
                "status": "downloaded",
                "vram_gb": round(vram, 1),
                "speed_label": _speed_label(vram),
                "notes": m["notes"],
            })
    for m in (all_models if not comfy_paint_truth else []):
        if m.get("category") != cat_filter:
            continue
        vram = float(m.get("min_vram_gb", 0) or 0)
        mid = m.get("id", "")
        downloaded = m.get("downloaded", False)
        if not downloaded:
            continue  # 只展示已下载的模型
        # 状态语义（2026-08-25 修复：此前"已下载未加载"误报
        # not_installed 且被前端全部禁用，导致模型配置弹窗只能选
        # 恰好驻留显存的模型，手动指定形同虚设）：
        #   ready      已加载，立即可用
        #   downloaded 已下载、需求 ≤ 总显存——可选（生成时后端
        #              ensure_loaded 按需加载，DynamicVRAM/调度器
        #              负责换载腾显存）
        #   offload    需求 > 总显存（物理装不下，如 24GB 模型在
        #              16GB 卡），不可选
        if mid in loaded_ids:
            status = "ready"
        elif vram <= total_gb:
            status = "downloaded"
        else:
            status = "offload"
        items.append({
            "id": mid,
            "name": m.get("name", mid),
            "status": status,
            "vram_gb": round(vram, 1),
            "speed_label": _speed_label(vram),
            "notes": m.get("purpose", "") or "",
        })
    # 模块级选型配置（模型管理 → 功能模块模型配置）：
    # task_type → 漫剧槽映射（dialog→manga-dialog 等，全量槽见
    # _MODULE_MODEL_SLOTS）；白名单过滤 + 默认模型下发。
    slot = f"manga-{task_type}"
    default_model = ""
    try:
        allowed, default_model = get_module_model_scope(slot)
        if allowed is not None:
            items = [m for m in items if m["id"] in allowed]
    except Exception as exc:  # noqa: BLE001 - 配置读取失败不阻断清单
        log.warning("漫剧%s槽白名单过滤跳过: %s", task_type, exc, exc_info=True)
    # 云端可选项追加（2026-10-03 方案一，用户拍板）：对应云端工位已
    # 绑定且启用时，把绑定的云端模型追加为一条选项（id=cloud::
    # prov::model，status=cloud）。未绑定不出现（不制造假选项）。
    # 选中语义：推理=真走云（消费点 ensure_loaded 原生路由 cloud::）；
    # 绘画/视频=意图留痕+可见性（实际路由由工位绑定优先——2026-09-15
    # 拍板 B 不变）；四视图不支持云端（comic_gen 侧明示落本地自动链）。
    _cloud_slot_map = {"dialog": ("text",), "paint": ("asset.image",
                                                     "keyframe.image"),
                       "video": ("video",)}
    _cloud_kind = _cloud_slot_map.get(task_type)
    if _cloud_kind:
        try:
            from ...services.cloud_provider_service import (
                get_image_endpoint,
                get_text_endpoint,
                get_video_endpoint,
            )
            if _cloud_kind == ("text",):
                _eps = [get_text_endpoint("manga.text")]
            elif task_type == "paint":
                _eps = [get_image_endpoint("asset.image"),
                        get_image_endpoint("keyframe.image")]
            else:
                _eps = [get_video_endpoint("manga.video")]
            _seen: set[tuple[str, str]] = set()
            for _ep in (_e for _e in _eps if _e is not None):
                _key = (_ep.provider_id, _ep.model or "")
                if _key in _seen:
                    continue
                _seen.add(_key)
                items.append({
                    "id": (f"cloud::{_ep.provider_id}::"
                           f"{_ep.model or ''}"),
                    "name": (f"云端·{_ep.provider_name}"
                             f"·{_ep.model or '默认模型'}"),
                    "status": "cloud",
                    "vram_gb": 0,
                    "speed_label": "云端",
                    "notes": "按量计费；该环节生成走此云端服务商",
                })
        except Exception as exc:  # noqa: BLE001 - 云端项追加失败不阻断清单
            log.warning("漫剧%s云端可选项追加跳过: %s", task_type, exc)
    # 视频档位下发（2026-10-03 分档具体化）：每项附「时长↔可选分辨率」
    # 精确配对表，前端时长/分辨率下拉按所选模型的档位渲染（单一真源
    # video_caps.py；云端端点 tiers=[] → 前端回退静态兜底清单）
    if task_type == "video":
        from .video_caps import tiers_for
        for it in items:
            it["tiers"] = tiers_for(str(it.get("id") or ""))
    return ok({"items": items, "total": len(items),
               "default_model": default_model or ""})
