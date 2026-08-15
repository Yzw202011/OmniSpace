"""模型管理 API 路由（规格 §4.5 模型管理 API + TASK-011 运行时接线）。

端点清单（/v1 前缀由 main.py 挂载）：
- GET    /models                模型列表（路由表 ∪ 磁盘扫描，downloaded 标记）
- GET    /models/{model_id}     模型详情
- POST   /models/import         导入模型（ModelImportRequest）
- POST   /models/{model_id}/verify  SHA256 校验
- DELETE /models/{model_id}     从注册表移除模型
- PUT    /models/select         手动选择模型（ModelSelectRequest）
- POST   /models/load           加载模型到 GPU（ensure_loaded 接线）
- POST   /models/unload         从 GPU 卸载模型
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

import asyncio
import hashlib
import json
import logging
import os
import tarfile
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..config import API_PREFIX, DATA_DIR, MODELS_DIR, ROOT_DIR
from ..data.database import get_db_safe, parse_json
from ..data.models import (DIALOG_ROUTING_TABLE, ModelCategory,
                           ModelImportRequest, ModelSelectRequest,
                           ModelStatus, PAINT_ROUTING_TABLE, VIDEO_ROUTING_TABLE)
from ..middleware.error_handler import ApiError, ok
from ..middleware.feature_lock import get_feature_lock
from ..services.model_manager import get_model_manager

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


def _seed_db(db) -> None:
    """表为空时从路由表播种初始模型到数据库。"""
    if db.count("models") > 0:
        return
    for m in DIALOG_ROUTING_TABLE:
        db.insert("models", {
            "id": m["model"], "name": m["model"],
            "category": ModelCategory.DIALOG.value, "purpose": "对话",
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
        _register(m["model"], m["model"], ModelCategory.DIALOG, "对话",
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


def _kv_get(key: str, default=None):
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


def _kv_set(key: str, value) -> None:
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
    """读 models_manifest.json 中指定模型的条目（无则空 dict）。"""
    try:
        data = json.loads(
            (MODELS_DIR / "models_manifest.json").read_text("utf-8"))
        entry = (data.get("models") or {}).get(model_id)
        return entry if isinstance(entry, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


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
def models_list():
    """模型列表（规格 §4.5）。按类别分组，合并磁盘扫描 downloaded 标记。"""
    items = _merged_models()
    grouped: dict[str, list[dict]] = {}
    for m in items:
        grouped.setdefault(m["category"], []).append(m)
    return ok({"groups": grouped, "models": items, "total": len(items),
               "downloaded": sum(1 for m in items if m.get("downloaded"))})


@router.get("/models/status")
def models_status():
    """模型管理全景状态（TASK-011）：GPU + 已加载 + 互斥 + 预测器。"""
    mgr = get_model_manager()
    return ok(mgr.get_status())


@router.get("/models/predict")
def models_predict(current_feature: str = Query("", description="当前功能名")):
    """ML 预测下一功能（TASK-011）：概率 > 0.7 时返回预加载建议。"""
    mgr = get_model_manager()
    result = mgr.predict_next_feature(current_feature or None)
    return ok(result)


# ── 规格 §7.1 契约别名（审计 P2-4/P2-6）─────────────────────────────
# 规格 §7.1.4 模型管理核心端点：/list, /load, /unload, /health。
# 注意（审计 P2-4）：以下命名路由必须声明在 /models/{model_id} 之前，
# 否则 "list"/"health"/"vram" 会被参数化路由吞掉当作 model_id。

@router.get("/models/list")
def models_list_alias():
    """规格 §7.1 契约别名：/models/list → 模型列表（同 GET /models）。"""
    return models_list()


@router.get("/models/vram")
def models_vram():
    """规格 §7.1 契约端点：显存全景（GPU 状态 + 逻辑预留 + 已加载占用）。"""
    mgr = get_model_manager()
    gpu = mgr.get_gpu_status()
    loaded = mgr.get_loaded_models()
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
    })


@router.get("/models/health")
def models_health():
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
def models_usage(req: ModelUsageEvent):
    """记录一次功能切换事件（TASK-011 ML 预测学习数据源）。"""
    mgr = get_model_manager()
    mgr.record_feature_switch(req.from_feature, req.to_feature)
    return ok({"recorded": True, "events": mgr.predictor.event_count})


@router.get("/models/config")
def models_config_get():
    """模型加载配置读取（MODEL-034）：量化精度偏好。"""
    cfg = _models_config()
    return ok({**cfg,
               "valid_precisions": list(_VALID_PRECISIONS),
               "effective": "下次模型加载时生效，不切换在途模型精度",
               "quantization_note": "int8/int4 需 bitsandbytes，未安装时"
                                    "回退 bf16 并记日志（绝不伪造量化）"})


@router.get("/models/update")
def models_update_check():
    """模型版本更新检查（MODEL-023）。

    离线单机定位：默认无远程版本源，如实返回 offline；若在
    system_settings 配置 models.update_url 则短超时探测远程
    manifest 并对比 version 字段。
    """
    local_ver = ""
    try:
        data = json.loads(
            (MODELS_DIR / "models_manifest.json").read_text("utf-8"))
        local_ver = str(data.get("version") or "")
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
                             limit: int = Query(default=20, ge=1, le=100)):
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
def models_detail(model_id: str):
    """模型详情（规格 §4.5 + MODEL-036 依赖关系字段）。"""
    model = _find_model(model_id)
    if model is None:
        raise ApiError(30001, "模型文件未找到，请导入模型",
                       detail={"model_id": model_id})
    model = dict(model)
    model["dependencies"] = _model_dependencies(model)
    return ok(model)


@router.post("/models/import")
async def models_import(req: ModelImportRequest):
    """导入模型（规格 §4.5）。校验路径后登记到注册表。"""
    path = Path(req.path)
    if not path.is_absolute():
        path = (ROOT_DIR / path).resolve()
    if not path.exists():
        raise ApiError(30001, "模型文件未找到，请导入模型",
                       detail={"path": req.path})

    model_id = uuid.uuid4().hex
    size_gb = round(path.stat().st_size / (1024 ** 3), 2) if path.is_file() else 0.0
    info = {
        "id": model_id,
        "name": path.stem,
        "category": ModelCategory.AUXILIARY.value,
        "purpose": "用户导入",
        "size_gb": size_gb,
        "params": "",
        "min_vram_gb": 0.0,
        "associated_features": [],
        "status": ModelStatus.READY.value,
        "file_path": str(path),
        "sha256": "",
    }
    db = get_db_safe()
    if db is not None:
        try:
            db.insert("models", info)
            return ok(info, message="模型已导入")
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库写入失败，降级内存存储: %s", exc)

    _models[model_id] = info
    return ok(info, message="模型已导入")


@router.post("/models/load")
async def models_load(req: ModelLoadRequest):
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
    try:
        # ensure_loaded 是数秒级阻塞调用，放到线程池避免卡住事件循环
        loaded = await asyncio.to_thread(
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
    return ok({"model_id": req.model_id, "category": category, "loaded": True,
               "loaded_models": mgr.get_loaded_models()},
              message="模型已加载")


@router.post("/models/unload")
def models_unload(req: ModelUnloadRequest):
    """从 GPU 卸载模型（TASK-011 接线 ModelManager.unload_model）。"""
    mgr = get_model_manager()
    if not mgr.unload_model(req.model_id):
        raise ApiError(20012, f"模型未处于已加载状态: {req.model_id}",
                       detail={"model_id": req.model_id})
    return ok({"model_id": req.model_id, "loaded": False,
               "loaded_models": mgr.get_loaded_models()},
              message="模型已卸载")


def _category_to_feature(category: str) -> str:
    """模型类别 -> 功能锁功能名（用于互斥检查）。"""
    return {
        "dialog": "dialog", "language": "dialog",
        "vision": "paint", "video": "video_gen",
    }.get((category or "").strip().lower(), "")


def _compute_model_fingerprint(file_path: str) -> str | None:
    """同步计算模型文件 SHA256 指纹（供 asyncio.to_thread 调用）。

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
async def models_verify(model_id: str):
    """SHA256 校验（规格 §4.5）。计算模型文件指纹。

    审计 BK-044 诚实行为：模型无本地文件时不再伪造模拟指纹，
    返回 verified:false + skipped:true + reason 中文字段
    （与 draw/manga degrade_reason 诚实降级风格一致），保持 API 可用。
    """
    model = _find_model(model_id)
    if model is None:
        raise ApiError(30001, "模型文件未找到，请导入模型",
                       detail={"model_id": model_id})

    # 审计 R1-05：大文件哈希为秒级阻塞计算，放线程池避免卡住事件循环
    digest = await asyncio.to_thread(
        _compute_model_fingerprint, model.get("file_path") or "")
    if digest is None:
        return ok({"model_id": model_id, "verified": False,
                   "skipped": True,
                   "reason": "本地模型文件不存在，已跳过校验"})

    # 持久化指纹
    db = get_db_safe()
    if db is not None:
        try:
            db.update("models", {"sha256": digest}, "id=?", (model_id,))
        except Exception as exc:  # noqa: BLE001
            log.warning("指纹持久化失败: %s", exc)
    if model_id in _models:
        _models[model_id]["sha256"] = digest
    return ok({"model_id": model_id, "sha256": digest, "verified": True})


@router.delete("/models/{model_id}")
def models_delete(model_id: str):
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


@router.put("/models/select")
def models_select(req: ModelSelectRequest):
    """手动选择模型（规格 §4.5）。feature -> model_id 绑定。"""
    valid_features = ("dialog", "paint", "video", "voice")
    if req.feature not in valid_features:
        raise ApiError(40004, "feature 必须是 dialog/paint/video/voice",
                       detail={"feature": req.feature})
    if _find_model(req.model_id) is None:
        raise ApiError(30001, "模型文件未找到，请导入模型",
                       detail={"model_id": req.model_id})
    _selections[req.feature] = req.model_id
    return ok({"feature": req.feature, "model_id": req.model_id,
               "selections": dict(_selections)})


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
def models_config_put(req: ModelConfigRequest):
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
async def models_export(req: ModelExportRequest):
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
        result = await asyncio.to_thread(
            _export_model_tarball, model, out_path)
    except Exception as exc:  # noqa: BLE001
        raise ApiError(20010, f"模型导出失败: {exc}") from exc
    return ok({"model_id": req.model_id,
               "export_path": str(out_path), **result},
              message="模型已导出（含 SHA256 校验文件）")


def _run_dialog_benchmark(req: ModelBenchmarkRequest) -> dict:
    """同步执行对话模型基准（线程池调用）：N 次真实推理采样。"""
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
async def models_benchmark(req: ModelBenchmarkRequest):
    """模型性能基准（MODEL-038）：对已加载对话模型跑 N 次真实推理。

    测 Tokens/s、首 token 延迟、显存峰值；结果落 model_benchmarks
    表供历史对比（GET /models/benchmark/history）。未加载 → 20012。
    """
    result = await asyncio.to_thread(_run_dialog_benchmark, req)
    db = get_db_safe()
    persisted = False
    if db is not None:
        try:
            db.executescript(_BENCH_DDL)
            db.insert("model_benchmarks", {
                "id": uuid.uuid4().hex, "model_id": result["model_id"],
                "engine": result["engine"], "runs": result["runs"],
                "tokens_per_s": result["tokens_per_s"],
                "first_token_ms": result["first_token_ms"],
                "total_ms": result["total_ms"],
                "output_tokens": result["output_tokens"],
                "vram_peak_gb": result["vram_peak_gb"],
                "created_at": time.time()})
            persisted = True
        except Exception as exc:  # noqa: BLE001
            log.warning("基准结果落库失败: %s", exc)
    return ok({**result, "persisted": persisted},
              message="基准测试完成")


@router.post("/models/download")
def models_download(req: ModelDownloadRequest):
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

