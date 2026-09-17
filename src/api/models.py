"""模型管理 API 路由（规格 §4.5 模型管理 API + TASK-011 运行时接线）。

端点清单（/v1 前缀由 main.py 挂载）：
- GET    /models                模型列表（路由表 ∪ 磁盘扫描，downloaded 标记）
- GET    /models/readiness      模型就绪总检（体验流 #3：模块级绿/灰+缺件清单）
- GET    /models/{model_id}     模型详情
- POST   /models/import         导入模型（ModelImportRequest）
- POST   /models/{model_id}/verify  SHA256 校验
- DELETE /models/{model_id}     从注册表移除模型
- PUT    /models/select         手动选择模型（ModelSelectRequest；P1 附带 prefetch 预热点火）
- POST   /models/load           加载模型到 GPU（ensure_loaded 接线；dialog/vision 类别内部改道切换引擎）
- POST   /models/unload         从 GPU 卸载模型
- POST   /models/switch         提交模型切换任务（ModelSwitchEngine P0）
- GET    /models/switch/list    最近切换任务列表
- GET    /models/switch/{id}    切换任务状态（plan/进度/回滚）
- POST   /models/switch/{id}/cancel  取消切换任务（P1：force=true 强制终止）
- GET    /models/status         GPU/已加载/互斥/预测器 全景状态
- GET    /models/predict        ML 预测下一功能（>0.7 给预加载建议）
- POST   /models/usage          记录功能切换事件（供 ML 预测学习）
- GET    /models/config         量化精度偏好读取（MODEL-034）
- PUT    /models/config         量化精度偏好持久化（下次加载生效）
- GET    /models/update         版本更新检查（MODEL-023，离线如实 offline）
- POST   /models/export         模型 tar.gz 导出 + SHA256（MODEL-037）
- POST   /models/benchmark      已加载模型基准测试（MODEL-038，落库）
- GET    /models/benchmark/history  基准历史对比
- POST   /models/download       在线下载（MODEL-019，离线诚实门控）
- /models/vram 附带 fragmentation 碎片率字段（MODEL-033）
- /models/{id} 详情附带 dependencies 依赖关系字段（MODEL-036）

约定：router 不带 prefix；成功 ok(data)；错误抛 ApiError。
错误码段：既有端点保持 30001/40004 不变（前端兼容）；
新增运行时接线端点使用 20000~29999 段（TASK-011 约定）：
  20010 模型加载失败 / 20011 模型未下载 / 20012 模型未处于已加载状态
  20013 显存不足且无可驱逐模型 / 20014 功能互斥，当前无法加载
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..config import API_PREFIX, DATA_DIR, ROOT_DIR
from ..data.database import Database, get_db_safe, parse_json
from ..data.models import (
    DIALOG_ROUTING_TABLE,
    PAINT_ROUTING_TABLE,
    VIDEO_ROUTING_TABLE,
    ModelCategory,
    ModelImportRequest,
    ModelSelectRequest,
    ModelStatus,
)
from ..middleware.error_handler import ApiError, ok
from ..middleware.feature_lock import get_feature_lock
from ..services.model_manager import get_model_manager
from ..services.offload import run_blocking

router = APIRouter()
log = logging.getLogger("omnispace.api.models")

# ── 内存态模型注册表（数据库不可用时的兜底数据源）──────────────────────
_models: dict[str, dict] = {}
# 手动选择记录：feature -> model_id（内存态，单机本地运行）
_selections: dict[str, str] = {}

# 磁盘额外登记但不在路由表中的已知模型（磁盘扫描时补充进列表）
_EXTRA_KNOWN_MODELS = {
    "qwen2-vl-2b": {"category": ModelCategory.DIALOG.value, "purpose": "对话/多模态理解",
                    "min_vram_gb": 4.0},
    "sdxl-base-1.0": {"category": ModelCategory.VISION.value, "purpose": "绘画",
                      "min_vram_gb": 8.0},
    "bge-large-zh": {"category": ModelCategory.LANGUAGE.value, "purpose": "文本嵌入",
                     "min_vram_gb": 0.0},
    # 视觉语音全模态（omni 类，2026-08-21 新增）：
    # qwen2.5-omni-7b / -int4 已入 DIALOG_ROUTING_TABLE，此处兜底
    # ModelScope 镜像目录名变体，防止磁盘扫描漏登记
    "qwen2.5-omni-7b": {"category": ModelCategory.OMNI.value,
                        "purpose": "视觉语音对话（文/图/视/音输入，文+语音输出）",
                        "min_vram_gb": 16.0},
    "qwen2.5-omni-7b-int4": {"category": ModelCategory.OMNI.value,
                             "purpose": "视觉语音对话（int4 量化，12GB 档）",
                             "min_vram_gb": 6.0},
    # MiniMax H3 33B（ComfyUI 子进程管线，2026-08-25 接入）：
    # ComfyUI 单文件权重布局（非 diffusers），注册表侧 DB 已播种不再
    # 重插，靠本条目 + 磁盘扫描 hints 供模型管理页/漫剧 G2 弹窗发现
    "minimax-h3": {"category": ModelCategory.VIDEO.value,
                   "purpose": "H3 音画联合视频生成（ComfyUI NVFP4 管线）",
                   "min_vram_gb": 13.0},
    # FLUX.2 Klein 9B（2026-08-25 接入）：quanto float8 量化布局，
    # 16GB 卡顶格质量绘画底座（4B 的质量升级档）
    "flux2-klein-9b": {"category": ModelCategory.VISION.value,
                       "purpose": "绘画（float8 量化，9B 高质量底座）",
                       "min_vram_gb": 10.5},
    # DeepSeek-R1-Distill-Qwen-14B（2026-08-25 接入）：W4A16 GPTQ
    # compressed-tensors → vLLM 子进程推理，R1 推理模型（<think> 段
    # 由 reasoning parser 剥离），纯文本
    "deepseek-r1-14b-w4a16": {"category": ModelCategory.DIALOG.value,
                              "purpose": "深度推理对话（R1，vLLM W4A16）",
                              "min_vram_gb": 11.5},
    # Z-Image-Turbo（Z1 2026-09-15 接入）：ComfyUI 单文件布局（bf16
    # unet + z_image_ae，TE 复用 qwen_3_4b），8步/cfg1.0 蒸馏档——
    # 绘画直连与分张四视图 zviews 管线底座。审计修复（同日）：补此
    # 条目+分类器关键词，否则归 AUXILIARY 进不了 paint 槽候选（死锁）
    "z-image-turbo": {"category": ModelCategory.VISION.value,
                      "purpose": "绘画（Z-Image 极速档/四视图分张底座，comfy 槽）",
                      "min_vram_gb": 12.5},
}


class ModelLoadRequest(BaseModel):
    model_id: str
    category: str = ""          # 缺省时从注册表/磁盘扫描推断


class ModelUnloadRequest(BaseModel):
    model_id: str


class ModelUsageEvent(BaseModel):
    from_feature: str = ""
    to_feature: str


def _register(model_id: str, name: str, category: ModelCategory,
              purpose: str, min_vram_gb: float, size_gb: float,
              status: ModelStatus = ModelStatus.READY) -> dict:
    """登记一个模型到内存注册表。"""
    info = {
        "id": model_id,
        "name": name,
        "category": category.value,
        "purpose": purpose,
        "size_gb": size_gb,
        "params": "",
        "min_vram_gb": min_vram_gb,
        "associated_features": [],
        "status": status.value,
        "file_path": "",
        "sha256": "",
    }
    _models[model_id] = info
    return info


def _row_to_model(row: dict) -> dict:
    """models 表行 -> ModelInfo dict。"""
    return {
        "id": row["id"],
        "name": row["name"],
        "category": row["category"],
        "purpose": row.get("purpose", ""),
        "size_gb": row.get("size_gb", 0.0),
        "params": row.get("params", ""),
        "min_vram_gb": row.get("min_vram_gb", 0.0),
        "associated_features": parse_json(row.get("associated_features"), []),
        "status": row.get("status", ModelStatus.NOT_INSTALLED.value),
        "file_path": row.get("file_path", ""),
        "sha256": row.get("sha256", ""),
    }


def _dialog_entry_category(mid: str) -> tuple[str, str]:
    """DIALOG_ROUTING_TABLE 条目 id → (category, purpose)。

    视觉语音全模态（omni）模型独立归类（2026-08-21 用户裁定），
    其余对话模型保持 dialog。
    """
    if "omni" in mid.lower():
        return (ModelCategory.OMNI.value, "视觉语音对话（文/图/视/音输入，文+语音输出）")
    return (ModelCategory.DIALOG.value, "对话")


def _seed_db(db: Database) -> None:
    """表为空时从路由表播种初始模型到数据库。"""
    if db.count("models") > 0:
        return
    for m in DIALOG_ROUTING_TABLE:
        mid = m["model"]
        cat, purpose = _dialog_entry_category(mid)
        db.insert("models", {
            "id": mid, "name": mid,
            "category": cat, "purpose": purpose,
            "size_gb": 4.0, "min_vram_gb": float(m["min_vram_gb"]),
            "associated_features": [], "status": ModelStatus.READY.value,
            "file_path": "", "sha256": "",
        })
    for m in PAINT_ROUTING_TABLE:
        db.insert("models", {
            "id": m["model"], "name": m["model"],
            "category": ModelCategory.VISION.value, "purpose": "绘画",
            "size_gb": 6.5, "min_vram_gb": float(m["min_vram_gb"]),
            "associated_features": [], "status": ModelStatus.READY.value,
            "file_path": "", "sha256": "",
        })
    for m in VIDEO_ROUTING_TABLE:
        mid = m["model"].value if hasattr(m["model"], "value") else m["model"]
        db.insert("models", {
            "id": mid, "name": mid,
            "category": ModelCategory.VIDEO.value, "purpose": "视频生成",
            "size_gb": 5.0, "min_vram_gb": float(m["min_vram_gb"]),
            "associated_features": [], "status": ModelStatus.READY.value,
            "file_path": "", "sha256": "",
        })


def _seed_memory() -> None:
    """数据库不可用时，播种到内存 _models。"""
    if _models:
        return
    for m in DIALOG_ROUTING_TABLE:
        cat, purpose = _dialog_entry_category(m["model"])
        _register(m["model"], m["model"], ModelCategory(cat), purpose,
                  float(m["min_vram_gb"]), 4.0)
    for m in PAINT_ROUTING_TABLE:
        _register(m["model"], m["model"], ModelCategory.VISION, "绘画",
                  float(m["min_vram_gb"]), 6.5)
    for m in VIDEO_ROUTING_TABLE:
        mid = m["model"].value if hasattr(m["model"], "value") else m["model"]
        _register(mid, mid, ModelCategory.VIDEO, "视频生成",
                  float(m["min_vram_gb"]), 5.0)


def _seed_registry() -> None:
    """从路由表播种初始模型注册表（优先数据库，不可用降级内存）。"""
    db = get_db_safe()
    if db is not None:
        try:
            _seed_db(db)
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库播种失败，降级内存存储: %s", exc)
    _seed_memory()


def _registry_models() -> list[dict]:
    """取注册表全量模型（DB 优先，内存兜底）。"""
    _seed_registry()
    db = get_db_safe()
    if db is not None:
        try:
            return [_row_to_model(r) for r in db.query(
                "SELECT id, name, category, purpose, size_gb, params,"
                " min_vram_gb, associated_features, status, file_path, sha256"
                " FROM models")]
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库查询失败，降级内存存储: %s", exc)
    return list(_models.values())


def _merged_models() -> list[dict]:
    """注册表（路由表）∪ 磁盘扫描合并视图。

    磁盘实际存在的模型标记 downloaded=true、写入真实 file_path/size_gb；
    仅存在于磁盘但未登记的已知模型（qwen2-vl-2b 等）补充进列表；
    loaded 标记来自 ModelManager 运行时状态。
    """
    mgr = get_model_manager()
    scanned = mgr.scan_downloaded_models()
    loaded_ids = {m["model_id"] for m in mgr.get_loaded_models()}

    items: dict[str, dict] = {}
    for m in _registry_models():
        item = dict(m)
        mid = item["id"]
        hit = scanned.get(mid)
        item["downloaded"] = bool(hit)
        item["loaded"] = mid in loaded_ids
        if hit:
            item["file_path"] = hit["path"]
            if hit["size_gb"] > 0:
                item["size_gb"] = hit["size_gb"]
            if item.get("status") in (ModelStatus.NOT_INSTALLED.value, ""):
                item["status"] = ModelStatus.READY.value
        elif item.get("file_path") and Path(item["file_path"]).exists():
            # 用户导入的外部路径（models/ 之外扫描层看不到）：文件在盘
            # 即视为已下载，导入后立即可见；文件被删则回落未下载
            item["downloaded"] = True
            if item.get("status") in (ModelStatus.NOT_INSTALLED.value, ""):
                item["status"] = ModelStatus.READY.value
        else:
            item["status"] = item.get("status") or ModelStatus.NOT_INSTALLED.value
        items[mid] = item

    # 磁盘有但路由表未登记的已知模型
    for mid, meta in _EXTRA_KNOWN_MODELS.items():
        if mid in items or mid not in scanned:
            continue
        hit = scanned[mid]
        items[mid] = {
            "id": mid, "name": mid, "category": meta["category"],
            "purpose": meta["purpose"], "size_gb": hit["size_gb"],
            "params": "", "min_vram_gb": meta["min_vram_gb"],
            "associated_features": [], "status": ModelStatus.READY.value,
            "file_path": hit["path"], "sha256": "",
            "downloaded": True, "loaded": mid in loaded_ids,
        }

    # 导入即可见（模型全维度对接）：磁盘扫描到的其余模型自动分类入列表，
    # 用户把模型目录/GGUF 放入 models/ 后无需手工登记即可在模型管理页看到
    classifier = mgr.classifier
    _purpose_map = {
        ModelCategory.DIALOG.value: "对话/文本生成（自动发现）",
        ModelCategory.OMNI.value: "视觉语音对话（自动发现）",
        ModelCategory.VOICE.value: "语音识别/合成（自动发现）",
        ModelCategory.VIDEO.value: "视频生成（自动发现）",
        ModelCategory.VISION.value: "绘画/视觉（自动发现）",
        ModelCategory.THREE_D.value: "3D 生成（自动发现）",
        ModelCategory.LANGUAGE.value: "语言/嵌入（自动发现）",
        ModelCategory.AUXILIARY.value: "辅助模型（自动发现）",
    }
    for mid, hit in scanned.items():
        if mid in items:
            continue
        try:
            category = classifier.classify(hit["path"]).value
        except Exception:  # noqa: BLE001
            category = ModelCategory.AUXILIARY.value
        items[mid] = {
            "id": mid, "name": mid, "category": category,
            "purpose": _purpose_map.get(category, "自动发现"),
            "size_gb": hit["size_gb"], "params": "",
            "min_vram_gb": round(mgr.estimate_vram_gb(mid, category), 2),
            "associated_features": [], "status": ModelStatus.READY.value,
            "file_path": hit["path"], "sha256": "",
            "downloaded": True, "loaded": mid in loaded_ids,
            "auto_discovered": True,
        }
    return list(items.values())


def _find_model(model_id: str) -> dict | None:
    """按 ID 查模型（合并视图）。"""
    for m in _merged_models():
        if m["id"] == model_id:
            return m
    return None


# ═══════════════════════════════════════════════════════════════════
#  批 6 共享辅助：KV 持久化 / 显存碎片 / 依赖关系 / 基准落库
# ═══════════════════════════════════════════════════════════════════

_MODELS_CONFIG_KEY = "models.config"
_VALID_PRECISIONS = ("bf16", "fp16", "fp32", "int8", "int4")
_DEFAULT_MODELS_CONFIG = {"precision": "bf16"}

_BENCH_DDL = """
CREATE TABLE IF NOT EXISTS model_benchmarks (
    id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    engine TEXT DEFAULT '',
    runs INTEGER DEFAULT 0,
    tokens_per_s REAL DEFAULT 0,
    first_token_ms REAL DEFAULT 0,
    total_ms REAL DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    vram_peak_gb REAL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_model_benchmarks_mid
    ON model_benchmarks(model_id, created_at DESC);
"""

# 已知模型依赖关系（MODEL-036；manifest 带 dependencies 字段时优先）
_MODEL_DEPENDENCIES: dict[str, list[dict]] = {
    "sdxl-base-1.0": [
        {"id": "sdxl-vae", "name": "SDXL VAE (fp16)", "bundled": True,
         "note": "模型目录 vae/ 组件"},
        {"id": "clip-vit-l", "name": "CLIP ViT-L 文本编码器", "bundled": True,
         "note": "模型目录 text_encoder/ 组件"},
        {"id": "openclip-vit-bigG", "name": "OpenCLIP ViT-bigG 文本编码器",
         "bundled": True, "note": "模型目录 text_encoder_2/ 组件"},
    ],
    "animatelcm": [
        {"id": "sd15-base", "name": "Stable Diffusion 1.5 底座",
         "bundled": True, "note": "运动模块挂载于 SD1.5 权重"},
    ],
    "triposr": [
        {"id": "dino-vitb16", "name": "DINO ViT-B/16 图像分词器",
         "bundled": True,
         "note": "结构配置本地缓存，权重包含于 model.ckpt"},
    ],
    "gpt-sovits": [
        {"id": "chinese-hubert-base", "name": "HuBERT 内容表征",
         "bundled": True, "note": "模型目录内 hubert/"},
        {"id": "chinese-roberta-wwm-ext-large", "name": "RoBERTa 文本前端",
         "bundled": True, "note": "模型目录内 roberta/"},
    ],
}


def _kv_get(key: str, default: Any = None) -> Any:
    """读 system_settings 表（JSON 值）；异常/缺失返回 default。"""
    db = get_db_safe()
    if db is None:
        return default
    try:
        row = db.query_one(
            "SELECT value FROM system_settings WHERE key=?", (key,))
        if row:
            return json.loads(row["value"])
    except Exception as exc:  # noqa: BLE001
        log.debug("system_settings 读取失败 %s: %s", key, exc)
    return default


def _kv_set(key: str, value: Any) -> None:
    """写 system_settings 表（JSON 值，UPSERT）。"""
    db = get_db_safe()
    if db is None:
        return
    try:
        db.sql(
            "INSERT INTO system_settings (key, value, updated_at)"
            " VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET"
            " value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), time.time()))
    except Exception as exc:  # noqa: BLE001
        log.warning("system_settings 写入失败 %s: %s", key, exc)


def _models_config() -> dict:
    """模型加载配置（MODEL-034）：持久化值叠加默认值。"""
    cfg = dict(_DEFAULT_MODELS_CONFIG)
    persisted = _kv_get(_MODELS_CONFIG_KEY)
    if isinstance(persisted, dict):
        cfg.update({k: v for k, v in persisted.items() if k in cfg})
    return cfg


# ═══════════════════════════════════════════════════════════════════
#  功能模块级模型选型配置（2026-08-27）：模块白名单 + 默认模型
# ------------------------------------------------------------------------
#  模型管理层面的分模块精细管控：
#  - allowed（白名单）：该模块可选用的大模型范围；空 = 不限制（全量
#    开放，兼容存量行为）。业务侧清单 API（/dialog/models、
#    /draw/models、/manga/models/available）按此过滤，模型选择 UI
#    只能看到白名单内模型；
#  - default（默认模型）：该模块「系统默认」时实际调用的模型；空 =
#    维持系统原有自动选择行为。对话 WS / 绘画生成链路在未显式指定
#    模型时优先采用，漫剧三槽经清单响应下发供前端展示与传参。
#  持久化 system_settings[models.module_config]（JSON，SQLite kv，
#  不新增表——符合 8 表约束）。
# ═══════════════════════════════════════════════════════════════════

_MODULE_MODEL_SLOTS: dict[str, dict] = {
    "dialog": {"label": "AI 对话", "category": "dialog",
               "desc": "文字 + 图片理解、深度思考问答",
               "hint": "清单 /dialog/models；生成链路：对话 WS + prewarm"},
    "paint": {"label": "AI 绘画", "category": "vision",
              "desc": "文生图 / 图生图",
              "hint": "清单 /draw/models；生成链路：绘画 generate"},
    "manga-dialog": {"label": "漫剧 · 文字", "category": "dialog",
                     "desc": "剧本 / 分镜文案生成",
                     "hint": "清单 /manga/models/available?task_type=dialog"},
    "manga-paint": {"label": "漫剧 · 图片", "category": "vision",
                    "desc": "角色 / 分镜生图",
                    "hint": "清单 /manga/models/available?task_type=paint"},
    "manga-video": {"label": "漫剧 · 视频", "category": "video",
                    "desc": "图生视频",
                    "hint": "清单 /manga/models/available?task_type=video"},
}

_MODULE_MODEL_CONFIG_KEY = "models.module_config"


def _module_model_config() -> dict[str, dict]:
    """读模块级选型配置：slot → {allowed: list[str], default: str}。

    损坏/缺失的槽配置回退空态（allowed 空=不限制，default 空=系统
    原行为）；未知槽丢弃（防 kv 手改残留注入前端）。
    """
    persisted = _kv_get(_MODULE_MODEL_CONFIG_KEY)
    out: dict[str, dict] = {}
    for slot in _MODULE_MODEL_SLOTS:
        entry = (persisted or {}).get(slot) if isinstance(persisted, dict) \
            else None
        allowed: list[str] = []
        if isinstance(entry, dict) and isinstance(entry.get("allowed"), list):
            allowed = [str(a) for a in entry["allowed"] if a]
        default = str(entry.get("default") or "") \
            if isinstance(entry, dict) else ""
        # 防御：默认不在白名单内视为未配置默认（自相矛盾的 kv 残留）
        if default and default not in allowed:
            default = ""
        out[slot] = {"allowed": allowed, "default": default}
    return out


def get_module_model_scope(slot: str) -> tuple[set[str] | None, str]:
    """模块白名单与默认模型（供业务清单/生成链路消费）。

    Returns:
        (allowed_set, default)：allowed_set None = 未配置白名单
        （不限制）；default '' = 未配置默认。
    """
    entry = (_module_model_config().get(slot) or {})
    allowed = entry.get("allowed") or []
    return (set(allowed) if allowed else None), (entry.get("default") or "")


def module_default_model(slot: str) -> str:
    """模块默认模型 id（'' = 未配置，维持系统自动选择）。"""
    return get_module_model_scope(slot)[1]


def module_model_allowed(slot: str, model_id: str) -> bool:
    """模型是否在模块白名单内（未配置白名单 = 全量放行）。"""
    allowed, _ = get_module_model_scope(slot)
    return allowed is None or model_id in allowed


def _slot_candidate_ids(slot: str) -> set[str]:
    """槽候选模型全集（注册表合并视图 ∪ 领域补充，含未下载模型）。

    与各业务清单同源（/dialog/models 的候选表+动态发现、
    PAINT_ROUTING_TABLE 全表、_merged_models 分类视图），保证配置
    界面勾选范围与业务可选范围一致。允许配置未下载模型（管控声明
    先行，模型下载/导入后生效——下载即受控）。
    """
    meta = _MODULE_MODEL_SLOTS.get(slot) or {}
    cat = meta.get("category")
    ids: set[str] = set()
    for m in _merged_models():
        if m.get("category") == cat:
            ids.add(m.get("id", ""))
    if slot == "dialog":
        # 与 dialog_list_models 同源：候选表 + 高档位 + 动态发现
        try:
            from ..services.inference.dialog_engine import (
                _HIGH_TIER_DIALOG_CANDIDATE,
                DIALOG_MODEL_CANDIDATES,
                discover_dialog_models,
            )
            ids.add(_HIGH_TIER_DIALOG_CANDIDATE[0])
            ids.update(c[0] for c in DIALOG_MODEL_CANDIDATES)
            ids.update(discover_dialog_models())
        except Exception as exc:  # noqa: BLE001
            log.debug("对话候选补充失败（不阻断配置）: %s", exc)
    if slot == "paint":
        # PAINT_ROUTING_TABLE 全表（含未下载路由候选）。
        # 2026-08-29 模型裁剪：sdxl-base-1.0 退出绘画槽候选
        # （保留引擎候选供 LoRA 训练底座使用）。
        ids.update(m.get("model") for m in PAINT_ROUTING_TABLE
                   if m.get("model"))
    ids.discard("")
    return ids


def _slot_candidates_meta(slot: str) -> list[dict]:
    """槽候选清单（带展示元数据，供配置界面渲染）。

    排序：已下载在前；未下载候选保留可勾选（标注未安装）。
    """
    meta_by_id: dict[str, dict] = {m.get("id", ""): m
                                   for m in _merged_models()}
    out: dict[str, dict] = {}
    for mid in _slot_candidate_ids(slot):
        m = meta_by_id.get(mid)
        out[mid] = {
            "id": mid,
            "name": (m or {}).get("name") or mid,
            "category": (m or {}).get("category")
            or (_MODULE_MODEL_SLOTS.get(slot) or {}).get("category", ""),
            "downloaded": bool((m or {}).get("downloaded")),
            "loaded": bool((m or {}).get("loaded")),
            "min_vram_gb": float((m or {}).get("min_vram_gb", 0) or 0),
            "purpose": (m or {}).get("purpose", ""),
        }
    return sorted(out.values(),
                  key=lambda x: (not x["downloaded"], x["id"]))


def _module_config_payload() -> dict:
    """GET/PUT 共用的响应载荷：槽定义 + 配置 + 候选 + 未知项标注。"""
    cfg = _module_model_config()
    slots = []
    for slot, meta in _MODULE_MODEL_SLOTS.items():
        entry = cfg.get(slot) or {"allowed": [], "default": ""}
        candidates = _slot_candidates_meta(slot)
        cand_ids = {c["id"] for c in candidates}
        slots.append({
            "slot": slot,
            "label": meta["label"],
            "desc": meta["desc"],
            "hint": meta["hint"],
            "allowed": entry["allowed"],
            "default": entry["default"],
            "restricted": bool(entry["allowed"]),
            "candidates": candidates,
            # 白名单中不在候选集的 id（可能已删除的模型）：保留配置
            # 但前端标注「未知」，提醒管理员清理
            "unknown_allowed": [a for a in entry["allowed"]
                                if a not in cand_ids],
        })
    return {"slots": slots, "config": cfg,
            "slot_keys": list(_MODULE_MODEL_SLOTS)}


class ModuleModelConfigRequest(BaseModel):
    """模块级选型配置写入体：slot → {allowed, default}。"""
    configs: dict[str, dict]


@router.get("/models/module-config")
def models_module_config_get() -> dict[str, Any]:
    """功能模块级模型选型配置读取（模型管理页配置面板数据源）。"""
    return ok(_module_config_payload())


@router.put("/models/module-config")
def models_module_config_put(req: ModuleModelConfigRequest) -> dict[str, Any]:
    """保存模块级选型配置（白名单 + 默认模型，即时持久化）。

    校验（诚实语义）：
    - slot 必须是已定义槽位；
    - allowed 内未知模型 id 保留（声明式管控，标注 unknown）；
    - default 非空时必须在 allowed 且真实存在于候选集（保证默认
      可生效——默认指向不存在模型是配置错误，如实拒绝）。
    清单 API 即时生效（下次拉取即过滤）；对话/绘画生成链路的默认
    模型即时生效（每次请求实时读配置）。
    """
    if not isinstance(req.configs, dict) or not req.configs:
        raise ApiError(40004, "configs 不能为空（slot → 配置映射）")
    unknown_slots = [s for s in req.configs if s not in _MODULE_MODEL_SLOTS]
    if unknown_slots:
        raise ApiError(40004, "未知功能模块槽位",
                       detail={"unknown_slots": unknown_slots,
                               "valid": list(_MODULE_MODEL_SLOTS)})

    merged = _module_model_config()
    warnings: list[str] = []
    for slot, raw in req.configs.items():
        if not isinstance(raw, dict):
            raise ApiError(40004, f"槽 {slot} 配置必须是对象",
                           detail={"slot": slot})
        allowed_raw = raw.get("allowed")
        if allowed_raw is None:
            allowed_raw = []
        if not isinstance(allowed_raw, list) \
                or not all(isinstance(a, str) for a in allowed_raw):
            raise ApiError(40004, f"槽 {slot} 的 allowed 必须是字符串数组",
                           detail={"slot": slot})
        # 去重保序
        allowed = list(dict.fromkeys(a for a in allowed_raw if a))
        default = str(raw.get("default") or "")
        cand_ids = _slot_candidate_ids(slot)
        if default:
            if default not in allowed:
                raise ApiError(
                    40004, f"槽 {slot} 的默认模型必须在可选范围内",
                    detail={"slot": slot, "default": default})
            if default not in cand_ids:
                raise ApiError(
                    40004,
                    f"槽 {slot} 的默认模型 {default} 不存在（未注册/"
                    "未下载），无法设为默认",
                    detail={"slot": slot, "default": default})
        if slot == "dialog" and any(
                k in a.lower() for a in allowed for k in
                ("tts", "voice", "speech", "asr", "audio")):
            warnings.append(
                "AI 对话白名单含语音类模型（对话清单不会展示它们）")
        unknown = [a for a in allowed if a not in cand_ids]
        if unknown:
            warnings.append(
                f"槽 {slot} 白名单含未安装/未知模型: {', '.join(unknown)}"
                "（保留配置，下载后生效）")
        merged[slot] = {"allowed": allowed, "default": default}

    _kv_set(_MODULE_MODEL_CONFIG_KEY, merged)
    log.info("模块级模型选型配置已保存: %s",
             {s: {"n": len(v["allowed"]), "d": v["default"]}
              for s, v in merged.items()})
    return ok({**_module_config_payload(), "warnings": warnings},
              message="模块模型配置已保存")


def _vram_fragmentation() -> dict:
    """显存碎片率（MODEL-033）：1 - 已分配/已预留（缓存分配器级）。

    PyTorch CUDA caching allocator 按段预留显存，预留中零散未用部分
    即内部碎片；>30% 给整理建议。无 CUDA 时 available=false。
    """
    out = {"available": False, "fragmentation": 0.0,
           "reserved_gb": 0.0, "allocated_gb": 0.0,
           "warning": False, "suggestion": ""}
    try:
        import torch  # type: ignore
        if not torch.cuda.is_available():
            return out
        reserved = float(torch.cuda.memory_reserved(0))
        allocated = float(torch.cuda.memory_allocated(0))
        frag = (reserved - allocated) / reserved if reserved > 0 else 0.0
        out.update({
            "available": True,
            "fragmentation": round(frag, 4),
            "reserved_gb": round(reserved / (1024 ** 3), 2),
            "allocated_gb": round(allocated / (1024 ** 3), 2),
            "warning": frag > 0.3,
        })
        if out["warning"]:
            out["suggestion"] = (
                "显存碎片率超过 30%，建议卸载闲置模型（/models/unload）"
                "触发缓存整理")
    except Exception:  # noqa: BLE001
        pass
    return out


def _manifest_entry(model_id: str) -> dict:
    """读 models_manifest.json 中指定模型的条目（无则空 dict）。

    ADR-003 P1：读取收敛至 data.model_registry（mtime 缓存单一读者），
    返回契约与旧直读实现一致。
    """
    from ..data.model_registry import entry as _registry_entry
    return _registry_entry(model_id)


def _model_dependencies(model: dict) -> list[dict]:
    """模型依赖关系（MODEL-036）：manifest dependencies 优先，内置映射兜底。"""
    mid = model.get("id", "")
    deps = _manifest_entry(mid).get("dependencies")
    if isinstance(deps, list):
        return deps
    for key, val in _MODEL_DEPENDENCIES.items():
        if key.lower() == mid.lower():
            return val
    return []



# ═══════════════════════════════════════════════════════════════════
#  既有端点（路径与语义保持不变）
# ═══════════════════════════════════════════════════════════════════

@router.get("/models")
def models_list() -> dict[str, Any]:
    """模型列表（规格 §4.5）。按类别分组，合并磁盘扫描 downloaded 标记。"""
    items = _merged_models()
    grouped: dict[str, list[dict]] = {}
    for m in items:
        grouped.setdefault(m["category"], []).append(m)
    return ok({"groups": grouped, "models": items, "total": len(items),
               "downloaded": sum(1 for m in items if m.get("downloaded"))})


@router.get("/models/readiness")
def models_readiness() -> dict[str, Any]:
    """模型就绪总检（2026-09-03 体验流 #3：绿灯/缺件指路）。

    按 models_manifest.json 契约核对盘上存在性，输出功能模块级
    ready/missing——「拖入→启动→首启激活→即用」的验收门面。
    注意：须声明在 /models/{model_id} 之前，否则被路径参数吞掉。
    """
    from ..config import MODELS_DIR
    from ..services.model_manager.readiness import compute_readiness
    return ok(compute_readiness(
        MODELS_DIR, MODELS_DIR / "models_manifest.json"))


@router.get("/models/status")
def models_status() -> dict[str, Any]:
    """模型管理全景状态（TASK-011）：GPU + 已加载 + 互斥 + 预测器。"""
    mgr = get_model_manager()
    return ok(mgr.get_status())


@router.get("/models/predict")
def models_predict(current_feature: str = Query("", description="当前功能名")) -> dict[str, Any]:
    """ML 预测下一功能（TASK-011）：概率 > 0.7 时返回预加载建议。"""
    mgr = get_model_manager()
    result = mgr.predict_next_feature(current_feature or None)
    return ok(result)


# ── 规格 §7.1 契约别名（审计 P2-4/P2-6）─────────────────────────────
# 规格 §7.1.4 模型管理核心端点：/list, /load, /unload, /health。
# 注意（审计 P2-4）：以下命名路由必须声明在 /models/{model_id} 之前，
# 否则 "list"/"health"/"vram" 会被参数化路由吞掉当作 model_id。

@router.get("/models/vram")
def models_vram() -> dict[str, Any]:
    """规格 §7.1 契约端点：显存全景（GPU 状态 + 逻辑预留 + 已加载占用）。

    批5（2026-09-10）：并入统一账本供需全景（budget=可批额度、
    external=账外占用对账口径）与忙碌登记簿（本地/云任务上榜）——
    前端状态栏从此可见「系统真实在忙什么」（云任务此前隐身）。
    """
    mgr = get_model_manager()
    gpu = mgr.get_gpu_status()
    loaded = mgr.get_loaded_models()
    # 统一账本供需快照（账本失明/异常降级为空对象，不阻断端点）
    budget: dict[str, Any] = {}
    busy_list: list[dict[str, Any]] = []
    try:
        from ..services.inference.gpu_budget import (
            get_busy_registry,
            get_gpu_budget,
        )

        snap = get_gpu_budget().snapshot(int(gpu.get("device", 0) or 0))
        budget = {
            "available": snap.available,
            "free_gb": snap.free_gb,
            "budget_gb": snap.budget_gb,
            "ledger_gb": snap.ledger_gb,
            "reserved_gb": snap.reserved_gb,
            "external_gb": snap.external_gb,
        }
        busy_list = [
            {"feature": e.feature, "kind": e.kind, "note": e.note}
            for e in get_busy_registry().entries()
        ]
    except Exception:  # noqa: BLE001 - 账本不可用保持旧契约字段
        pass
    return ok({
        "gpu": gpu,
        "reserved_vram_gb": gpu.get("reserved_vram_gb", 0.0),
        "fragmentation": _vram_fragmentation(),
        "loaded_count": len(loaded),
        "loaded_models": [
            {"model_id": m["model_id"], "vram_gb": m.get("vram_gb", 0.0),
             "category": m.get("category", "")}
            for m in loaded
        ],
        "budget": budget,
        "busy_registry": busy_list,
    })


@router.get("/models/health")
def models_health() -> dict[str, Any]:
    """规格 §7.1 契约端点：模型子系统健康检查。

    汇总：GPU 可用性 / 已加载模型数 / 已下载模型数 / 功能锁状态。
    healthy=true 表示模型管理链路可用（无 GPU 时为 degraded=true）。
    """
    mgr = get_model_manager()
    gpu = mgr.get_gpu_status()
    loaded = mgr.get_loaded_models()
    downloaded = mgr.scan_downloaded_models()
    return ok({
        "healthy": True,
        "degraded": not gpu.get("available", False),
        "gpu_available": gpu.get("available", False),
        "gpu_name": gpu.get("gpu_name", ""),
        "loaded_count": len(loaded),
        "downloaded_count": len(downloaded),
        "last_error": mgr.last_error or "",
    })


@router.post("/models/usage")
def models_usage(req: ModelUsageEvent) -> dict[str, Any]:
    """记录一次功能切换事件（TASK-011 ML 预测学习数据源）。"""
    mgr = get_model_manager()
    mgr.record_feature_switch(req.from_feature, req.to_feature)
    return ok({"recorded": True, "events": mgr.predictor.event_count})


@router.get("/models/config")
def models_config_get() -> dict[str, Any]:
    """模型加载配置读取（MODEL-034）：量化精度偏好。"""
    cfg = _models_config()
    return ok({**cfg,
               "valid_precisions": list(_VALID_PRECISIONS),
               "effective": "下次模型加载时生效，不切换在途模型精度",
               "quantization_note": "int8/int4 需 bitsandbytes，未安装时"
                                    "回退 bf16 并记日志（绝不伪造量化）"})


@router.get("/models/update")
def models_update_check() -> dict[str, Any]:
    """模型版本更新检查（MODEL-023）。

    离线单机定位：默认无远程版本源，如实返回 offline；若在
    system_settings 配置 models.update_url 则短超时探测远程
    manifest 并对比 version 字段。
    """
    local_ver = ""
    try:
        from ..data.model_registry import load_manifest
        local_ver = str(load_manifest().get("version") or "")
    except Exception:  # noqa: BLE001
        pass

    update_url = _kv_get("models.update_url", "")
    if not update_url:
        return ok({
            "online": False, "status": "offline",
            "local_manifest_version": local_ver,
            "remote_manifest_version": "",
            "update_available": None,
            "message": "离线单机定位：未配置远程版本源，模型更新请经"
                       " /models/import 本地导入新权重",
        })

    import urllib.request
    try:
        with urllib.request.urlopen(str(update_url), timeout=3) as resp:
            remote = json.loads(resp.read().decode())
        remote_ver = str(remote.get("version") or "")
        return ok({
            "online": True, "status": "checked",
            "local_manifest_version": local_ver,
            "remote_manifest_version": remote_ver,
            "update_available": bool(remote_ver and remote_ver != local_ver),
            "message": "远程版本检查完成",
        })
    except Exception as exc:  # noqa: BLE001
        return ok({
            "online": False, "status": "unreachable",
            "local_manifest_version": local_ver,
            "remote_manifest_version": "",
            "update_available": None,
            "message": f"远程版本源不可达: {exc}",
        })


@router.get("/models/benchmark/history")
def models_benchmark_history(model_id: str = Query(default=""),
                             limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    """基准测试历史（MODEL-038）：按模型过滤，时间倒序。"""
    db = get_db_safe()
    if db is None:
        return ok({"items": [], "total": 0, "persisted": False})
    try:
        db.executescript(_BENCH_DDL)
        if model_id:
            rows = db.query(
                "SELECT * FROM model_benchmarks WHERE model_id=?"
                " ORDER BY created_at DESC LIMIT ?", (model_id, limit))
        else:
            rows = db.query(
                "SELECT * FROM model_benchmarks"
                " ORDER BY created_at DESC LIMIT ?", (limit,))
        items = [dict(r) for r in rows]
        return ok({"items": items, "total": len(items), "persisted": True})
    except Exception as exc:  # noqa: BLE001
        log.warning("基准历史查询失败: %s", exc)
        return ok({"items": [], "total": 0, "persisted": False})


@router.get("/models/{model_id}")
def models_detail(model_id: str) -> dict[str, Any]:
    """模型详情（规格 §4.5 + MODEL-036 依赖关系字段）。"""
    model = _find_model(model_id)
    if model is None:
        raise ApiError(30001, "模型文件未找到，请导入模型",
                       detail={"model_id": model_id})
    model = dict(model)
    model["dependencies"] = _model_dependencies(model)
    return ok(model)


@router.post("/models/import")
async def models_import(req: ModelImportRequest) -> dict[str, Any]:
    """导入模型（规格 §4.5；2026-09-01 完整接入：自动识别 + 即刻可见 + 可加载）。

    经 ModelImporter 自动识别类别（config.json 元数据 → 文件名关键词 →
    目录特征 → 扩展名 → safetensors header），递归统计目录体积（修复旧
    实现目录导入 size 恒 0 的缺陷），最低显存按磁盘体积 ×1.2 估算；
    大文件（>1GB）跳过导入时 SHA256（防数十 GB 哈希把请求卡住数分钟）。

    登记条目对运行时立即可用：_merged_models 按 file_path 存在性判
    downloaded（models/ 外部路径同样可见），ModelManager.resolve_model_path
    以登记表兜底解析路径。

    幂等与命名：同一路径重复导入直接返回既有记录；models/ 内路径沿用
    目录名/文件 stem 作 id（与磁盘扫描同一命名空间，预置行借此回填
    file_path 而不产生重复条目，身份字段 category/purpose 保持原值）。
    """
    from ..config import MODELS_DIR

    raw = Path(req.path)
    if not raw.is_absolute():
        raw = ROOT_DIR / raw
    raw = raw.resolve()
    if not raw.exists():
        raise ApiError(30001, "模型文件未找到，请导入模型",
                       detail={"path": req.path})
    resolved = str(raw)

    # B0（2026-09-13）：导入路径闸——端点设计上接受任意路径（外接盘
    # 权重），但与加载链（trust_remote_code）组合即成恶意「模型分享包」
    # RCE 链。封禁系统敏感目录与在库关键目录（向这些位置导入权重
    # 永非合法场景；data/ 不封——隔离区观察期满回补登记走该处）。
    # 闸为 best-effort，治本在加载侧沙箱（长期项）。
    def _forbidden(prefix: Path) -> bool:
        try:
            return raw.is_relative_to(prefix.resolve())
        except Exception:  # noqa: BLE001 - 解析失败逐项跳过
            return False

    for _bp in (
        Path(os.environ.get("SystemRoot", r"C:\Windows")),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        Path(os.environ.get("ProgramData", r"C:\ProgramData")),
    ):
        if _forbidden(_bp):
            raise ApiError("MODEL_IMPORT_PATH_FORBIDDEN",
                           "该路径不允许导入（系统敏感目录）",
                           detail={"path": req.path},
                           suggestion="请把模型权重放在独立目录后再导入")
    for _rel in ("runtime", "pydeps", "logs"):
        if _forbidden(ROOT_DIR / _rel):
            raise ApiError("MODEL_IMPORT_PATH_FORBIDDEN",
                           f"该路径不允许导入（在库 {_rel}/ 目录）",
                           detail={"path": req.path},
                           suggestion="模型权重应放 models/ 或外部专用目录")

    db = get_db_safe()

    def _find_by_path() -> dict | None:
        """按 file_path 查既有登记（幂等判据）。"""
        if db is not None:
            try:
                row = db.query_one(
                    "SELECT id, name, category, purpose, size_gb, params,"
                    " min_vram_gb, associated_features, status, file_path, sha256"
                    " FROM models WHERE file_path = ?", (resolved,))
                if row:
                    return _row_to_model(row)
            except Exception as exc:  # noqa: BLE001
                log.warning("导入查重失败: %s", exc)
        for m in _models.values():
            if m.get("file_path") == resolved:
                return dict(m)
        return None

    existing = _find_by_path()
    if existing is not None:
        return ok(existing, message="该路径已导入，返回既有登记")

    mgr = get_model_manager()
    result = await run_blocking(mgr.importer.import_model, resolved)

    # id 命名空间：models/ 内沿用目录名/文件 stem（与扫描 key 一致）；
    # 外部路径用导入器语义化 id
    model_id = result.model_id
    try:
        in_models_dir = raw.is_relative_to(MODELS_DIR.resolve())
    except (OSError, ValueError):
        in_models_dir = False
    if in_models_dir:
        model_id = raw.stem if raw.is_file() else raw.name

    info = {
        "id": model_id,
        "name": result.name,
        "category": result.category.value,
        "purpose": "用户导入",
        "size_gb": result.size_gb,
        "params": "",
        "min_vram_gb": round(result.size_gb * 1.2, 2) if result.size_gb > 0 else 0.0,
        "associated_features": [],
        "status": ModelStatus.READY.value,
        "file_path": result.file_path,
        "sha256": result.sha256,
    }
    if result.warnings:
        log.info("导入 %s 提示: %s", result.name, "; ".join(result.warnings))

    if db is not None:
        try:
            row = await run_blocking(lambda: db.query_one(
                "SELECT id, file_path FROM models WHERE id = ?", (model_id,)))
            if row is not None and row.get("file_path") not in ("", None, resolved):
                # 同 id 已被其他路径占用：语义化 id 兜底防撞
                model_id = result.model_id
                info["id"] = model_id
                row = await run_blocking(lambda: db.query_one(
                    "SELECT id FROM models WHERE id = ?", (model_id,)))
            if row is not None:
                # 预置/既有行撞名（models/ 内导入常见）：只回填物理字段，
                # category/purpose 等身份字段保持登记原值
                await run_blocking(lambda: db.update("models", {
                    "size_gb": info["size_gb"],
                    "min_vram_gb": info["min_vram_gb"],
                    "status": info["status"],
                    "file_path": info["file_path"],
                    "sha256": info["sha256"],
                }, "id = ?", (model_id,)))
                full = await run_blocking(lambda: db.query_one(
                    "SELECT id, name, category, purpose, size_gb, params,"
                    " min_vram_gb, associated_features, status, file_path, sha256"
                    " FROM models WHERE id = ?", (model_id,)))
                info = _row_to_model(full) if full else info
            else:
                await run_blocking(lambda: db.insert("models", info))
            info["imported"] = True
            return ok(info, message="模型已导入")
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库写入失败，降级内存存储: %s", exc)

    _models[model_id] = info
    info["imported"] = True
    return ok(info, message="模型已导入")


@router.post("/models/load")
async def models_load(req: ModelLoadRequest) -> dict[str, Any]:
    """加载模型到 GPU（TASK-011 接线 ModelManager.ensure_loaded）。

    错误码（20xxx 段）：20011 未下载 / 20014 互斥阻断 / 20013 显存不足 /
    20010 其他加载失败。

    审计 P0-5 并发安全：按模型 category 映射获取对应功能锁后再加载——
    加载窗口内禁止冲突功能的生成任务启动（反之亦然），消除「加载中
    被另一功能抢占显存导致双方 OOM」的竞态。同功能锁可重入（规格
    §6.1），不阻断当前活跃功能自身的模型加载。
    """
    mgr = get_model_manager()
    model = _find_model(req.model_id)
    if model is None:
        raise ApiError(20011, f"模型未下载: {req.model_id}",
                       detail={"model_id": req.model_id})

    category = req.category or model.get("category", "")
    feature = _category_to_feature(category)

    # 互斥检查（规格 §2.1：目标功能被当前活跃功能阻断时拒绝加载）
    if feature:
        blocked, reason = mgr.is_feature_blocked(feature)
        if blocked:
            raise ApiError(20014, f"功能互斥，当前无法加载：{reason}",
                           detail={"feature": feature,
                                   "blocked_features": mgr.get_blocked_features()})

    # 按 category 映射的功能锁保护加载窗口（无映射类别不加锁）
    lock_mgr = get_feature_lock()
    acquired = False
    if feature:
        acquired = await lock_mgr.acquire(
            feature, task_id=f"models_load:{req.model_id}")
        if not acquired:
            reason = lock_mgr.get_block_reason(feature) or "功能互斥"
            raise ApiError(20014, f"功能互斥，当前无法加载：{reason}",
                           detail={"feature": feature,
                                   "active_feature": lock_mgr.active_feature})

    # 统一切换引擎改道（P0 2026-08-25）：进度可见 + 失败尽力回滚，
    # 同步语义兼容（阻塞至终态）。锁移交给任务线程（submit 成功后
    # 由任务终态释放，handler 不再重复 release）；submit 抛异常时
    # 锁未移交，此处兜底释放——除非 acquire 是对既有切换任务锁的
    # 重入（was_switch_held，让位协议），此时锁仍归原任务不得清。
    from ..services.switch_engine import (
        ModelSwitchEngine,
        SwitchBusyError,
        get_switch_engine,
    )
    if category in ModelSwitchEngine.VALID_CATEGORIES:
        st_pre = lock_mgr.status()
        was_switch_held = (
            feature and st_pre.get("active_feature") == feature
            and str(st_pre.get("task_id") or "").startswith("switch:"))
        try:
            result = await run_blocking(
                get_switch_engine().submit_and_wait,
                category, req.model_id, 900.0,
                feature=feature if acquired else None)
        except SwitchBusyError as e:
            if acquired and not was_switch_held:
                await lock_mgr.release(feature)
            raise ApiError(20010, str(e),
                           detail={"active_task_id": e.active_task_id,
                                   "model_id": req.model_id}) from e
        except ValueError as e:
            if acquired and not was_switch_held:
                await lock_mgr.release(feature)
            raise ApiError(20011, str(e), detail={"model_id": req.model_id}) from e
        status = result.get("status", "")
        if status == "done":
            mgr.note_user_load(req.model_id)  # #8：用户显式装载打免回收钉
            return ok({"model_id": req.model_id, "category": category,
                       "loaded": True, "loaded_models": mgr.get_loaded_models(),
                       "switch_task": result},
                      message="模型已加载")
        reason = result.get("error") or result.get("message") or "加载失败"
        code = 20013 if "显存不足" in reason else (
            20011 if "未下载" in reason else 20010)
        raise ApiError(code, f"模型加载失败：{reason}",
                       detail={"model_id": req.model_id, "category": category,
                               "switch_task": result})

    try:
        # ensure_loaded 是数秒级阻塞调用，经 run_blocking 卸载避免卡住事件循环
        loaded = await run_blocking(
            mgr.ensure_loaded, category, req.model_id)
    finally:
        if acquired:
            await lock_mgr.release(feature)

    if not loaded:
        reason = mgr.last_error or "加载失败"
        code = 20013 if "显存不足" in reason else (
            20011 if "未下载" in reason else 20010)
        raise ApiError(code, f"模型加载失败：{reason}",
                       detail={"model_id": req.model_id, "category": category})
    mgr.note_user_load(req.model_id)  # #8：用户显式装载打免回收钉
    return ok({"model_id": req.model_id, "category": category, "loaded": True,
               "loaded_models": mgr.get_loaded_models()},
              message="模型已加载")


@router.post("/models/unload")
def models_unload(req: ModelUnloadRequest) -> dict[str, Any]:
    """从 GPU 卸载模型（TASK-011 接线 ModelManager.unload_model）。"""
    mgr = get_model_manager()
    if not mgr.unload_model(req.model_id):
        raise ApiError(20012, f"模型未处于已加载状态: {req.model_id}",
                       detail={"model_id": req.model_id})
    return ok({"model_id": req.model_id, "loaded": False,
               "loaded_models": mgr.get_loaded_models()},
              message="模型已卸载")


# ═══════════════════════════════════════════════════════════════════
#  统一模型切换引擎（ModelSwitchEngine，P0 2026-08-25）
# ═══════════════════════════════════════════════════════════════════

class ModelSwitchRequest(BaseModel):
    """提交模型切换（异步任务，返回 task_id 供进度轮询/WS 订阅）。"""
    model_id: str
    # 缺省取模型注册表类别（dialog/vision/video）
    category: str | None = None
    # 加载失败时尽力回滚至切换前模型（vLLM 回滚=全量重载，耗时同首次加载）
    rollback: bool = True


async def _acquire_switch_lock(feature: str, model_id: str) -> None:
    """切换任务的功能锁获取（严格持锁检查版）。

    与 /models/load 的可重入语义不同：切换窗口长（vLLM 实测 177s），
    若对"该功能正被真实任务持有（如对话流式）"的锁重入成功，任务
    线程结束时 release 会把活跃任务持有的锁误清——因此提交切换前
    必须确认该功能当前空闲（用户裁定：切换需等活跃任务结束）。

    P1 让位放宽：持锁者 task_id 以 "switch:" 开头（即另一个切换/
    预热任务）时放行——引擎 submit() 内部会取消低优 prefetch 任务
    并接管锁（lock_handover）；用户级任务仍互斥（SwitchBusyError）。

    必须 async/await（lock_mgr.acquire 是协程——同步裸调用返回
    coroutine 对象恒 truthy，锁实际未获取，2026-08-25 e2e 日志
    实测踩坑：/models/switch 提交全程无锁保护）。
    """
    mgr = get_model_manager()
    lock_mgr = get_feature_lock()
    blocked, reason = mgr.is_feature_blocked(feature)
    if blocked:
        raise ApiError(20014, f"功能互斥，当前无法切换：{reason}",
                       detail={"feature": feature,
                               "blocked_features": mgr.get_blocked_features()})
    st = lock_mgr.status()
    if st.get("active_feature") == feature and st.get("task_id"):
        holder_tid = str(st.get("task_id") or "")
        if not holder_tid.startswith("switch:"):
            raise ApiError(
                20014, f"{feature} 功能任务进行中，无法切换模型（请等待完成）",
                detail={"feature": feature, "holder_task_id": st.get("task_id"),
                        "held_seconds": st.get("held_seconds", 0)})
    if not await lock_mgr.acquire(feature, task_id=f"switch:{model_id}"):
        reason = lock_mgr.get_block_reason(feature) or "功能互斥"
        raise ApiError(20014, f"功能互斥，当前无法切换：{reason}",
                       detail={"feature": feature,
                               "active_feature": lock_mgr.active_feature})


class ModuleReleaseRequest(BaseModel):
    """模块切换资源释放请求（用户裁定 2026-08-21）。"""
    module: str                # 目标模块功能名（dialog/paint/video_gen/training）
    timeout_ms: int = 3000     # 释放时间预算（默认 3s）


@router.post("/models/release-for-module")
async def models_release_for_module(req: ModuleReleaseRequest) -> dict[str, Any]:
    """模块切换资源调度：其他模块 3 秒内释放显存/内存，优先供应目标模块。

    前端导航切换模块/进入漫剧项目时调用。时间预算内尽力卸载其他
    模块已加载模型；超预算部分释放返回 completed=False（部分完成）。
    运行中任务（功能锁持有）的模型跳过，共享小模型保留。
    vLLM 独立子进程（AWQ 对话模型）同样纳入释放：目标模块非对话时
    杀进程秒级回收显存（2026-08-21 vLLM 集成扩展；功能锁持有中
    跳过——2026-08-22 竞态修复）。
    """
    mgr = get_model_manager()
    result = await run_blocking(
        mgr.release_for_module, req.module, req.timeout_ms / 1000.0)
    return ok(result)


class ModuleWarmupRequest(BaseModel):
    """模块常驻模型预热请求（2026-08-22 思考过长事故）。"""
    feature: str              # 目标功能名（目前支持 dialog）
    model_id: str | None = None  # 对话模型（前端 localStorage 持久化的选择）


# 预热去重（防重复点击/快速路由抖动起多线程排队等引擎锁）
_warmup_inflight: set[str] = set()


@router.post("/models/warmup")
async def models_warmup(req: ModuleWarmupRequest) -> dict[str, Any]:
    """后台预热模块常驻模型（fire-and-forget，立即返回 started）。

    对话模块主用（2026-08-22 思考过长事故）：vLLM 冷启动 ~157s，
    用户切入对话页即点火，打字/阅读时间即加载时间，发消息时已
    就绪。model_id 透传前端持久化选择（localStorage）——引擎可能
    已加载默认 4b 而用户选择 8b-awq，发消息才热切换即二次冷启动；
    预热直接以用户选择为目标。刻意不持功能锁——用户切走时
    release_for_module 可正常终止预热中的 vLLM（不阻塞互斥功能）；
    与对话请求的并发安全由 dialog_engine 内部引擎锁串行化。
    """
    import threading

    feature = (req.feature or "").strip().lower()

    # 绘画模块预热（2026-08-31，用户需求「跟 AI 对话一样的冷启动弹窗」）：
    # 本地 diffusers 管线冷启动约 10~60s，进页面即点火把加载摊进浏览
    # 时间；就绪信号 = /draw/status loaded（PaintEngine state=ready）
    if feature == "paint":
        # W3-C（2026-09-13）：gen_engine=comfy 时预热=拉起 ComfyUI 进程
        # （klein 权重由工作流流式装载，无常驻预载概念）；legacy 走
        # 原 diffusers 预热链不动
        try:
            from ..config import get_config
            _comfy_mode = str((get_config().get("paint") or {}).get(
                "gen_engine", "legacy")).strip().lower() == "comfy"
        except Exception:  # noqa: BLE001
            _comfy_mode = False
        if _comfy_mode:
            from ..services.inference.comfy_paint_engine import (
                comfy_paint_available,
                get_comfy_paint_engine,
            )
            _warmup_inflight.discard("paint")
            if not comfy_paint_available():
                return ok({"feature": "paint", "started": False,
                           "reason": "unavailable",
                           "model": "comfy-klein-9b-fp8"},
                          message="ComfyUI klein 出图栈不可用"
                                  "（便携版或权重缺失）")
            threading.Thread(
                target=get_comfy_paint_engine().ensure_running,
                daemon=True, name="paint-warmup-comfy").start()
            return ok({"feature": "paint", "started": True,
                       "model_id": "comfy-klein-9b-fp8"},
                      message="ComfyUI klein 预热已启动（冷启动约 40 秒）")

        from ..services.inference.paint_engine import get_paint_engine
        engine = get_paint_engine()
        status = engine.get_status()
        want_model = (req.model_id or "").strip() or None
        if status.get("loaded"):
            # 已就绪即达标，不按目标匹配热切换拆台（2026-09-09 反向
            # 互踩根修，与 /dialog/prewarm 同语义：换模型=显式动作）
            _warmup_inflight.discard("paint")
            return ok({"feature": "paint", "started": False,
                       "reason": "already_ready",
                       "model": status.get("model", "")},
                      message="绘画模型已就绪")
        if "paint" in _warmup_inflight:
            return ok({"feature": "paint", "started": False,
                       "reason": "inflight"}, message="绘画模型预热中")

        _warmup_inflight.add("paint")

        def _bg_warmup_paint() -> None:
            try:
                engine.ensure_loaded(want_model)
            except Exception:  # noqa: BLE001 - 预热失败静默（生成时如实报错）
                pass
            finally:
                _warmup_inflight.discard("paint")

        threading.Thread(target=_bg_warmup_paint, daemon=True,
                         name="paint-warmup").start()
        return ok({"feature": "paint", "started": True,
                   "model_id": want_model or "auto"},
                  message="绘画模型预热已启动（冷启动约 0.5-1 分钟）")

    if feature != "dialog":
        return ok({"feature": feature, "started": False,
                   "reason": "unsupported"},
                  message="该模块无需预热")

    # 云端对话无需本地预热（2026-09-10 竞态根修）：dialog.text 槽位绑定
    # /旧远程配置生效时，本地 vLLM 预热纯属空拉显存——预热线程还可能
    # 跨越显存等待窗口，在用户绑定云端后走完本地降级链（误导横幅 +
    # 无谓 vLLM 启动）。云端就绪由发送路径的健康探测自证，这里直接短路。
    from ..services.inference.backends.remote_backend import is_remote_dialog_enabled
    if is_remote_dialog_enabled():
        return ok({"feature": "dialog", "started": False,
                   "reason": "cloud_bound"},
                  message="对话已由云端承载，无需本地预热")

    from ..services.inference.dialog_engine import get_dialog_engine
    engine = get_dialog_engine()
    status = engine.get_status()
    # 已就绪即达标：不按目标匹配热切换拆台（2026-09-09 反向互踩
    # 根修——换模型走 /models/load 或发送带 model 的显式动作）
    want_model = (req.model_id or "").strip() or None
    if status.get("state") == "ready":
        _warmup_inflight.discard("dialog")
        return ok({"feature": "dialog", "started": False,
                   "reason": "already_ready",
                   "model": status.get("model", "")},
                  message="对话模型已就绪")
    if "dialog" in _warmup_inflight:
        return ok({"feature": "dialog", "started": False,
                   "reason": "inflight"}, message="对话模型预热中")

    _warmup_inflight.add("dialog")

    def _bg_warmup() -> None:
        try:
            engine.ensure_loaded(want_model)
        except Exception:  # noqa: BLE001 - 预热失败静默（发消息时如实报错）
            pass
        finally:
            _warmup_inflight.discard("dialog")

    threading.Thread(target=_bg_warmup, daemon=True,
                     name="dialog-warmup").start()
    return ok({"feature": "dialog", "started": True,
               "model_id": want_model or "auto"},
              message="对话模型预热已启动（冷启动约 2-3 分钟）")


@router.get("/models/vllm/status")
def vllm_status() -> dict[str, Any]:
    """vLLM 推理服务状态（runtime 安装/进程存活/健康/PID/运行时长）。"""
    from ..engines.vllm_service import get_vllm_service
    return ok(get_vllm_service().status())


@router.post("/models/vllm/stop")
async def vllm_stop() -> dict[str, Any]:
    """停止 vLLM 子进程并回收显存（用户操作最高权限，强制终止）。"""
    from ..engines.vllm_service import get_vllm_service
    svc = get_vllm_service()
    stopped = await run_blocking(svc.stop)
    if not stopped:
        raise ApiError(20020, "vLLM 子进程终止失败（详见 logs/vllm-server.log）")
    return ok({"running": False}, message="vLLM 服务已停止，显存已回收")


def _category_to_feature(category: str) -> str:
    """模型类别 -> 功能锁功能名（用于互斥检查）。

    omni（视觉语音全模态）归 dialog 功能：语音/视频对话仍是对话
    模块的形态，与 dialog 共用功能锁与模块资源调度保留集。
    """
    return {
        "dialog": "dialog", "language": "dialog", "omni": "dialog",
        "vision": "paint", "video": "video_gen",
    }.get((category or "").strip().lower(), "")


def _compute_model_fingerprint(file_path: str) -> str | None:
    """同步计算模型文件 SHA256 指纹（经 run_blocking 卸载的同步核心）。

    文件模型：全量流式哈希；目录模型：对特征文件做组合指纹
    （避免全量哈希数 GB 权重）。无本地文件时返回 None。
    """
    if file_path and os.path.isfile(file_path):
        sha = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                sha.update(chunk)
        return sha.hexdigest()
    if file_path and os.path.isdir(file_path):
        sha = hashlib.sha256()
        for sig in ("config.json", "model_index.json",
                    "model.safetensors.index.json"):
            fp = os.path.join(file_path, sig)
            if os.path.isfile(fp):
                with open(fp, "rb") as f:
                    sha.update(f.read())
        return sha.hexdigest()
    return None


@router.post("/models/{model_id}/verify")
async def models_verify(model_id: str) -> dict[str, Any]:
    """SHA256 校验（规格 §4.5）。计算模型文件指纹。

    审计 BK-044 诚实行为：模型无本地文件时不再伪造模拟指纹，
    返回 verified:false + skipped:true + reason 中文字段
    （与 draw/manga degrade_reason 诚实降级风格一致），保持 API 可用。
    """
    model = _find_model(model_id)
    if model is None:
        raise ApiError(30001, "模型文件未找到，请导入模型",
                       detail={"model_id": model_id})

    # 审计 R1-05：大文件哈希为秒级阻塞计算，经 run_blocking 卸载避免卡住事件循环
    digest = await run_blocking(
        _compute_model_fingerprint, model.get("file_path") or "")
    if digest is None:
        return ok({"model_id": model_id, "verified": False,
                   "skipped": True,
                   "reason": "本地模型文件不存在，已跳过校验"})

    # 持久化指纹
    db = get_db_safe()
    if db is not None:
        try:
            await run_blocking(lambda: db.update("models", {"sha256": digest}, "id=?", (model_id,)))
        except Exception as exc:  # noqa: BLE001
            log.warning("指纹持久化失败: %s", exc)
    if model_id in _models:
        _models[model_id]["sha256"] = digest
    return ok({"model_id": model_id, "sha256": digest, "verified": True})


@router.delete("/models/{model_id}")
def models_delete(model_id: str) -> dict[str, Any]:
    """从注册表移除模型（规格 §4.5）。已加载模型先卸载。"""
    mgr = get_model_manager()
    mgr.unload_model(model_id)  # 未加载时为 no-op

    db = get_db_safe()
    deleted = False
    if db is not None:
        try:
            n = db.delete("models", "id=?", (model_id,))
            deleted = n > 0
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库删除失败，降级内存存储: %s", exc)
    if not deleted:
        if model_id in _models:
            _models.pop(model_id, None)
            deleted = True
        else:
            raise ApiError(30001, "模型文件未找到，请导入模型",
                           detail={"model_id": model_id})
    # 清理手动选择引用
    for feat, mid in list(_selections.items()):
        if mid == model_id:
            _selections.pop(feat, None)
    return ok({"deleted": model_id})


@router.delete("/models/{model_id}/files")
def models_purge_files(model_id: str) -> dict[str, Any]:
    """彻底删除模型磁盘文件（卸载+注销+删盘；前端「彻底删除」按钮）。

    与 DELETE /models/{id}（仅移除注册）相对：连盘上文件一起清。
    2026-09-16 补路由：此前前端 modelApi.purgeModelFiles 调用必 404。
    安全闸：只删注册表登记的 file_path，且要求解析后位于 MODELS_DIR
    之内或为登记的绝对外部路径（导入功能写入），路径深度必须 >2 层
    （挡盘根/一级目录误删）；硬链接架构下删除 models/ 侧名称不影响
    ComfyUI 侧链接（同 inode 多名，删一名不断链）。
    """
    model = _find_model(model_id)
    if model is None:
        raise ApiError(30001, "模型文件未找到，请导入模型",
                       detail={"model_id": model_id})
    raw_path = str(model.get("file_path") or "").strip()
    if not raw_path:
        raise ApiError(30002, "该模型无磁盘路径（仅注册条目），请用普通删除",
                       detail={"model_id": model_id})
    from ..config import MODELS_DIR
    resolved = Path(raw_path).resolve()
    models_root = MODELS_DIR.resolve()
    in_models_dir = resolved == models_root or models_root in resolved.parents
    external_ok = Path(raw_path).is_absolute() and not raw_path.startswith("\\\\")
    if not (in_models_dir or external_ok) or len(resolved.parts) <= 2:
        raise ApiError(30003, "路径安全闸拒绝删除（不在模型目录且非登记外部路径）",
                       detail={"path": raw_path})

    def _dir_size(p: Path) -> int:
        if p.is_file():
            try:
                return p.stat().st_size
            except OSError:
                return 0
        total = 0
        for f in p.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                continue
        return total

    freed = _dir_size(resolved) if resolved.exists() else 0
    mgr = get_model_manager()
    mgr.unload_model(model_id)  # 已加载先卸载（未加载时 no-op）
    if resolved.is_dir():
        shutil.rmtree(resolved)
    elif resolved.exists():
        resolved.unlink()
    # 注销（复用 models_delete 语义：db 删除 → 内存兜底 → 清手动选择）
    db = get_db_safe()
    if db is not None:
        try:
            db.delete("models", "id=?", (model_id,))
        except Exception as exc:  # noqa: BLE001
            log.warning("purge 后注册表删除失败: %s", exc)
    _models.pop(model_id, None)
    for feat, mid in list(_selections.items()):
        if mid == model_id:
            _selections.pop(feat, None)
    return ok({"deleted": model_id, "path": raw_path,
               "freed_gb": round(freed / 1024 ** 3, 2)}, message="已彻底删除模型文件")


@router.put("/models/select")
async def models_select(req: ModelSelectRequest) -> dict[str, Any]:
    """手动选择模型（规格 §4.5）。feature -> model_id 绑定。

    P1 prefetch 预热：feature ∈ {dialog, paint, video} 且功能锁空闲
    时，后台低优提交切换任务预热目标模型（保存配置即预载，下次
    使用免等）；忙碌（真实任务/另一切换进行中）时静默跳过——预热
    是尽力优化，绝不能阻塞或打断用户当前工作。P2 起 video 预热
    对 minimax-h3 为 ComfyUI 子进程冷启动（~20s，免下个任务等待）。
    """
    valid_features = ("dialog", "paint", "video", "voice")
    if req.feature not in valid_features:
        raise ApiError(40004, "feature 必须是 dialog/paint/video/voice",
                       detail={"feature": req.feature})
    if _find_model(req.model_id) is None:
        raise ApiError(30001, "模型文件未找到，请导入模型",
                       detail={"model_id": req.model_id})
    _selections[req.feature] = req.model_id

    # prefetch 预热点火（fire-and-forget，不影响选择响应）
    prefetch = "skipped"
    if req.feature in ("dialog", "paint", "video"):
        prefetch = await _try_prefetch(req.feature, req.model_id)
    return ok({"feature": req.feature, "model_id": req.model_id,
               "selections": dict(_selections),
               "prefetch": prefetch},
              message="模型选择已保存"
                      + ("，后台预热已点火" if prefetch == "started" else ""))


async def _try_prefetch(feature: str, model_id: str) -> str:
    """保存模型配置后的后台预热（P1；P2 扩展 video）。

    语义（尽力而为，任何不满足条件都静默跳过）：
    - 引擎已持有目标模型 → 无需预热（幂等；video 经 holds_model
      特化——diffusers 装载目录名 / H3 台账登记态）
    - 功能锁被真实任务/其他功能持有 → 跳过（不打扰用户）
    - 功能锁空闲 → acquire（task_id=switch: 前缀）→ 引擎 submit
      priority=prefetch；提交失败兜底释放锁
    - 活跃切换任务是 prefetch → submit 让位协议自然处理（新配置
      覆盖旧预热）；活跃任务是用户级切换 → SwitchBusyError 跳过

    Returns:
        "started" 已点火 / "skipped" 跳过（附原因语义见日志）
    """
    from ..services.switch_engine import get_switch_engine
    # feature 名 → 切换类目（mgr/注册表词汇；vision=绘画引擎）
    category = {"dialog": "dialog", "paint": "vision",
                "video": "video"}.get(feature)
    if category is None:
        return "skipped"
    # 锁词汇对齐（2026-08-25 e2e E1 实测）：feature_lock 四锁是
    # dialog/paint/video_gen/training——video 功能的锁名是 video_gen，
    # 裸用 feature 名 acquire 会 40010 且任务线程 release 失配
    # （与 models_switch._category_to_feature 同口径）
    lock_feature = {"dialog": "dialog", "paint": "paint",
                    "video": "video_gen"}[feature]
    engine = get_switch_engine()
    lock_mgr = get_feature_lock()

    # 引擎已持有目标 → 幂等跳过
    if engine.holds_model(category, model_id):
        return "skipped"

    # 本功能有活跃切换任务：user 级一律跳过（不能干扰显式操作）；
    # prefetch 仅在可让位阶段（planning/unloading）放行——loading 中
    # 不可让位，重入提交必 SwitchBusyError，兜底 release 会误清其锁
    active_info = engine.get_active_task(category)
    if active_info is not None:
        if active_info.get("priority") != "prefetch":
            return "skipped"
        if str(active_info.get("status", "")) in ("loading", "verifying"):
            return "skipped"

    # 锁忙判定：被其他功能持有 / 本功能被真实任务（非 switch: 前缀）持有
    st = lock_mgr.status()
    active = st.get("active_feature")
    if active and active != lock_feature:
        return "skipped"
    if active == lock_feature:
        holder_tid = str(st.get("task_id") or "")
        if holder_tid and not holder_tid.startswith("switch:"):
            return "skipped"  # 真实生成任务进行中，不打扰

    if not await lock_mgr.acquire(lock_feature,
                                  task_id=f"switch:{model_id}"):
        return "skipped"
    try:
        engine.submit(category, model_id, priority="prefetch",
                      feature=lock_feature)
        log.info("prefetch 预热已点火: %s → %s (%s)", feature, model_id,
                 category)
        return "started"
    except Exception as exc:  # noqa: BLE001 - 预热失败不影响选择
        log.info("prefetch 预热未点火（%s）: %s", type(exc).__name__, exc)
        try:
            await lock_mgr.release(lock_feature)
        except Exception:  # noqa: BLE001
            pass
        return "skipped"


# ═══════════════════════════════════════════════════════════════════
#  批 6 新增端点：配置 / 导出 / 基准 / 下载（诚实语义）
# ═══════════════════════════════════════════════════════════════════

class ModelConfigRequest(BaseModel):
    precision: str


class ModelExportRequest(BaseModel):
    model_id: str


class ModelBenchmarkRequest(BaseModel):
    model_id: str = ""        # 缺省 = 当前已加载对话模型
    runs: int = 3
    max_new_tokens: int = 32


class ModelDownloadRequest(BaseModel):
    model_id: str = ""
    url: str = ""


@router.put("/models/config")
def models_config_put(req: ModelConfigRequest) -> dict[str, Any]:
    """量化精度偏好持久化（MODEL-034）。

    写 system_settings[models.config]；对话/多模态引擎加载时读取
    （bf16/fp16/fp32 直接映射 dtype；int8/int4 需 bitsandbytes，
    未安装回退 bf16 并记日志）。诚实标注：不在途切换。
    """
    p = (req.precision or "").strip().lower()
    if p not in _VALID_PRECISIONS:
        raise ApiError(
            40004,
            f"precision 必须是 {'/'.join(_VALID_PRECISIONS)}",
            detail={"precision": req.precision,
                    "valid": list(_VALID_PRECISIONS)})
    cfg = _models_config()
    cfg["precision"] = p
    _kv_set(_MODELS_CONFIG_KEY, cfg)
    return ok({**cfg,
               "persisted": True,
               "effective": "下次模型加载时生效，不切换在途模型精度"})


def _export_model_tarball(model: dict, out_path: Path) -> dict:
    """同步执行模型导出（线程池调用）：tar.gz + manifest + SHA256。"""
    src = Path(model.get("file_path") or "")
    files: list[dict] = []
    if src.is_dir():
        for f in sorted(src.rglob("*")):
            if f.is_file():
                files.append({"rel": str(f.relative_to(src)).replace("\\", "/"),
                              "path": f, "size": f.stat().st_size})
    elif src.is_file():
        files.append({"rel": src.name, "path": src, "size": src.stat().st_size})

    manifest = {
        "format": "omnispace.model.export/v1",
        "model_id": model.get("id", ""),
        "name": model.get("name", ""),
        "category": model.get("category", ""),
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "file_count": len(files),
        "total_bytes": sum(f["size"] for f in files),
    }
    try:
        tf = tarfile.open(out_path, "w:gz", compresslevel=1)
    except TypeError:  # 旧版本无 compresslevel 形参
        tf = tarfile.open(out_path, "w:gz")
    with tf:
        info = tarfile.TarInfo("manifest.json")
        payload = json.dumps(manifest, ensure_ascii=False,
                             indent=2).encode("utf-8")
        info.size = len(payload)
        tf.addfile(info, __import__("io").BytesIO(payload))
        for f in files:
            tf.add(f["path"], arcname=f"{model.get('id', 'model')}/{f['rel']}")

    sha = hashlib.sha256()
    with open(out_path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            sha.update(chunk)
    digest = sha.hexdigest()
    sidecar = out_path.with_suffix(out_path.suffix + ".sha256")
    sidecar.write_text(f"{digest}  {out_path.name}\n", encoding="utf-8")
    return {"sha256": digest, "file_count": len(files),
            "size_mb": round(out_path.stat().st_size / (1024 ** 2), 2),
            "sidecar": str(sidecar)}


@router.post("/models/export")
async def models_export(req: ModelExportRequest) -> dict[str, Any]:
    """模型导出（MODEL-037）：模型目录 → .tar.gz + manifest + SHA256 侧车。

    产物落 data/generated/exports/models/；安全约束：仅允许导出
    项目根目录内的模型路径（拒绝任意磁盘路径）。
    """
    model = _find_model(req.model_id)
    if model is None:
        raise ApiError(30001, "模型文件未找到，请导入模型",
                       detail={"model_id": req.model_id})
    raw = model.get("file_path") or ""
    if not raw or not Path(raw).exists():
        raise ApiError(30001, "模型无本地文件，无法导出",
                       detail={"model_id": req.model_id})
    resolved = Path(raw).resolve()
    try:
        if ROOT_DIR.resolve() not in resolved.parents \
                and resolved != ROOT_DIR.resolve():
            raise ApiError(40004, "仅允许导出项目目录内的模型",
                           detail={"path": raw})
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ApiError(40004, f"路径校验失败: {exc}") from exc

    out_dir = DATA_DIR / "generated" / "exports" / "models"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (
        f"{req.model_id}_{time.strftime('%Y%m%d_%H%M%S')}.tar.gz")
    try:
        result = await run_blocking(
            _export_model_tarball, model, out_path)
    except Exception as exc:  # noqa: BLE001
        raise ApiError(20010, f"模型导出失败: {exc}") from exc
    return ok({"model_id": req.model_id,
               "export_path": str(out_path), **result},
              message="模型已导出（含 SHA256 校验文件）")


def _run_dialog_benchmark(req: ModelBenchmarkRequest) -> dict:
    """同步执行对话模型基准（经 run_blocking 卸载）：N 次真实推理采样。"""
    from ..services.inference.dialog_engine import get_dialog_engine

    engine = get_dialog_engine()
    if not engine.is_ready:
        raise ApiError(20012,
                       "对话模型未加载，请先 POST /models/load 再基准测试",
                       detail={"loaded_models": [
                           m["model_id"] for m in
                           get_model_manager().get_loaded_models()]})
    loaded_id = engine.model_name
    if req.model_id and req.model_id != loaded_id:
        raise ApiError(40004,
                       f"指定模型未加载（当前已加载: {loaded_id}）",
                       detail={"requested": req.model_id,
                               "loaded": loaded_id})

    runs = max(1, min(int(req.runs), 10))
    max_tokens = max(8, min(int(req.max_new_tokens), 256))
    torch = __import__("torch")
    try:
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats(0)
    except Exception:  # noqa: BLE001
        pass

    tot_tokens = 0
    tot_ms = 0.0
    first_ms = 0.0
    prompt = "用一句话介绍人工智能的发展历史。"
    for _ in range(runs):
        engine.chat([{"role": "user", "content": prompt}],
                    max_new_tokens=max_tokens)
        tot_tokens += int(engine.last_output_tokens)
        tot_ms += float(engine.last_total_ms)
        first_ms += float(engine.last_first_token_ms)

    vram_peak = 0.0
    try:
        if torch.cuda.is_available():
            vram_peak = round(
                torch.cuda.max_memory_allocated(0) / (1024 ** 3), 2)
    except Exception:  # noqa: BLE001
        pass
    tokens_per_s = round(tot_tokens / (tot_ms / 1000.0), 2) if tot_ms else 0.0
    return {
        "model_id": loaded_id, "engine": "dialog", "runs": runs,
        "tokens_per_s": tokens_per_s,
        "first_token_ms": round(first_ms / runs, 1),
        "total_ms": round(tot_ms, 1),
        "output_tokens": tot_tokens,
        "vram_peak_gb": vram_peak,
    }


@router.post("/models/benchmark")
async def models_benchmark(req: ModelBenchmarkRequest) -> dict[str, Any]:
    """模型性能基准（MODEL-038）：对已加载对话模型跑 N 次真实推理。

    测 Tokens/s、首 token 延迟、显存峰值；结果落 model_benchmarks
    表供历史对比（GET /models/benchmark/history）。未加载 → 20012。
    """
    result = await run_blocking(_run_dialog_benchmark, req)
    db = get_db_safe()
    persisted = False
    if db is not None:
        try:
            db.executescript(_BENCH_DDL)
            await run_blocking(lambda: db.insert("model_benchmarks", {
                "id": uuid.uuid4().hex, "model_id": result["model_id"],
                "engine": result["engine"], "runs": result["runs"],
                "tokens_per_s": result["tokens_per_s"],
                "first_token_ms": result["first_token_ms"],
                "total_ms": result["total_ms"],
                "output_tokens": result["output_tokens"],
                "vram_peak_gb": result["vram_peak_gb"],
                "created_at": time.time()}))
            persisted = True
        except Exception as exc:  # noqa: BLE001
            log.warning("基准结果落库失败: %s", exc)
    return ok({**result, "persisted": persisted},
              message="基准测试完成")


@router.post("/models/download")
def models_download(req: ModelDownloadRequest) -> dict[str, Any]:
    """模型在线下载（MODEL-019/021/022 诚实语义）。

    OmniSpace AI 为离线单机定位（127.0.0.1 绑定 + 本地推理），不提供
    在线模型下载/断点续传；模型获取走本地导入通道。
    """
    raise ApiError(
        "MODEL_DOWNLOAD_OFFLINE",
        "离线单机定位：不提供在线模型下载；请将模型目录或 GGUF 文件放入"
        " models/ 后经 /models/import 导入（自动发现亦可直接识别）",
        detail={"import_endpoint": f"{API_PREFIX}/models/import",
                "requested": req.model_id or req.url or ""},
        suggestion="模型放入 models/ 目录后重启或调用 /models 列表即可见")

# 本项目仅供学习使用，商业授权请+Q 3559331368
