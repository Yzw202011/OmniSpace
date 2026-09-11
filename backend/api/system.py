"""系统 API 路由（规格 §4.7 系统 API）。

端点清单：
- GET  /system/settings          获取设置
- PUT  /system/settings          更新设置
- POST /system/backup            备份
- POST /system/diagnose          运行 26 项检测（真实探测，审计 BK-002）
- POST /system/project/export    打包 .omnispace（真实归档，审计 BK-041）
- POST /system/project/import    导入项目（真实恢复，审计 BK-042）
- GET  /system/version           版本信息
- GET  /system/update            软件更新（RC 免安装形态有意省略在线更新，见 F-09）

约定：router 不带 prefix；成功 ok(data)；错误抛 ApiError。
安全：所有接受路径入参的端点必须过 _resolve_safe_path 白名单校验
（审计 BK-028，文档C 安全铁律：输入校验/路径校验）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import tarfile
import threading
import time
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from fastapi import APIRouter, Body, Query
from fastapi.responses import FileResponse

from .. import startup_check
from ..config import APP_VERSION, DATA_DIR, DB_PATH, HOST, LOGS_DIR, MODELS_DIR, PORT, ROOT_DIR
from ..data.database import Database, get_db_safe
from ..data.models import ProjectExport, ProjectImport, SystemSettings
from ..middleware.error_handler import ApiError, ok
from ..services.offload import run_blocking

router = APIRouter()
log = logging.getLogger("omnispace.api.system")

# ── 内存态设置存储（服务层不可用时的兜底数据源）──────────────────────
_settings: dict = SystemSettings().model_dump()

# 项目导出目录（任务约定：写入 generated/exports/）
EXPORT_DIR = DATA_DIR / "generated" / "exports"
BACKUP_DIR = DATA_DIR / "backups"

# ── system_settings KV 持久化（SET 批：设置重启保持）─────────────────
_SETTINGS_KEY = "system.settings"


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


def _load_persisted_settings() -> dict:
    """DB 持久化设置叠加默认值（DB 不可用 → 内存副本）。"""
    persisted = _kv_get(_SETTINGS_KEY)
    if isinstance(persisted, dict):
        merged = dict(SystemSettings().model_dump())
        merged.update(persisted)
        return merged
    return dict(_settings)

# ── 路径白名单（审计 BK-028）────────────────────────────────────────
# 接受路径入参的端点仅允许访问白名单根目录内的文件：
# 项目根（含 data/、models/、generated/ 等）。resolve() 归一化 .. 与
# 符号链接后做前缀校验，穿越到白名单外的路径一律拒绝。
_PATH_WHITELIST_ROOTS = (ROOT_DIR,)


def _resolve_safe_path(raw_path: str) -> Path:
    """解析用户输入路径并校验在白名单根目录内（审计 BK-028）。

    Raises:
        ApiError SYSTEM_PARAM_INVALID: 路径为空或无法解析
        ApiError SYSTEM_UNAUTHORIZED:  路径不在白名单根目录内（含 .. 穿越）
    """
    if not raw_path or not raw_path.strip():
        raise ApiError("SYSTEM_PARAM_INVALID", "file_path 不能为空")
    try:
        resolved = Path(raw_path.strip()).expanduser().resolve()
    except Exception as exc:  # noqa: BLE001
        raise ApiError("SYSTEM_PARAM_INVALID", "路径无法解析",
                       detail={"file_path": raw_path, "error": str(exc)}) from exc
    for root in _PATH_WHITELIST_ROOTS:
        try:
            root_resolved = Path(root).resolve()
        except Exception:  # noqa: BLE001
            continue
        if resolved == root_resolved or root_resolved in resolved.parents:
            return resolved
    raise ApiError(
        "SYSTEM_UNAUTHORIZED", "路径不在允许的目录范围内",
        detail={"file_path": raw_path,
                "allowed_roots": [str(r) for r in _PATH_WHITELIST_ROOTS]},
        suggestion="仅允许导入/访问项目目录（含 data/、models/、generated/）内的文件")


def _now_ts() -> str:
    """时间戳字符串，用于生成文件名。"""
    return time.strftime("%Y%m%d_%H%M%S", time.localtime())


@router.get("/system/settings")
def system_settings_get() -> dict[str, Any]:
    """获取系统设置（规格 §4.7，持久化 system_settings 表）。"""
    return ok(_load_persisted_settings())


@router.put("/system/settings")
def system_settings_update(req: SystemSettings) -> dict[str, Any]:
    """更新系统设置（规格 §4.7）：写库持久化 + 刷新内存副本。"""
    global _settings
    _settings = req.model_dump()
    _kv_set(_SETTINGS_KEY, _settings)
    # 远程对话配置热生效（批3 D3）：清 5s TTL 缓存，下一跳即用新值
    try:
        from ..services.inference.backends.remote_backend import invalidate_remote_config_cache
        invalidate_remote_config_cache()
    except Exception:  # noqa: BLE001 - 缓存清理失败下个 TTL 自愈
        pass
    return ok(_settings, message="设置已更新")


@router.get("/system/web_search")
def web_search_settings_get() -> dict[str, Any]:
    """联网搜索 v1 配置读取（架构升级计划 B-阶段一；默认关）。"""
    from ..services.web_search import get_web_search_settings
    return ok(get_web_search_settings())


@router.put("/system/web_search")
def web_search_settings_update(req: dict = Body(...)) -> dict[str, Any]:
    """联网搜索 v1 配置更新（enabled/provider/trigger 等服务端校验）。"""
    from ..services.web_search import update_web_search_settings
    return ok(update_web_search_settings(req), message="联网搜索配置已更新")


@router.post("/system/dialog-remote/test")
def system_dialog_remote_test(req: dict = Body(...)) -> dict[str, Any]:
    """测试远程推理服务器连通性（批3 D3 设置页「测试连接」按钮）。

    探测 {base_url}/health（404 回退 /v1/models），任一 2xx 即可达。
    """
    from ..services.inference.backends.remote_backend import probe_remote_health
    base_url = str(req.get("base_url") or "").strip()
    api_key = str(req.get("api_key") or "").strip()
    reachable, detail = probe_remote_health(base_url, api_key, timeout_s=5.0)
    return ok({"reachable": reachable, "detail": detail,
               "base_url": base_url.rstrip("/")})


@router.post("/system/backup")
async def system_backup() -> dict[str, Any]:
    """备份（规格 §4.7）：设置 JSON + SQLite 数据库真实副本（审计 BK-019）。

    数据库副本经 sqlite3 backup API 在线热备，写到 data/backups/。
    """
    backup_id = uuid.uuid4().hex
    payload = {
        "app_version": APP_VERSION,
        "backup_id": backup_id,
        "created_at": time.time(),
        "settings": _settings,
    }

    def _write_settings_backup() -> str:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        path = BACKUP_DIR / f"backup_{_now_ts()}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return str(path)

    def _backup_db() -> str:
        import sqlite3
        dest = BACKUP_DIR / f"omnispace_{_now_ts()}.db"
        src_conn = sqlite3.connect(str(DB_PATH))
        try:
            dst_conn = sqlite3.connect(str(dest))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        return str(dest)

    # 写盘/热备属磁盘 IO，经 run_blocking 卸载（审计 09-10 P2-3）
    file_path = ""
    db_path = ""
    try:
        file_path = await run_blocking(_write_settings_backup)
    except Exception as exc:  # noqa: BLE001
        log.warning("备份写盘失败，仅返回内存副本：%s", exc)

    # 数据库热备（sqlite3 backup API，WAL 下安全）
    db = get_db_safe()
    if db is not None and DB_PATH.is_file():
        try:
            db_path = await run_blocking(_backup_db)
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库热备失败：%s", exc)

    return ok({"backup_id": backup_id, "path": file_path,
               "db_path": db_path,
               "size_bytes": len(json.dumps(payload))})


# ═══════════════════════════════════════════════════════════════════
#  系统诊断（审计 BK-002：26 项全部真实探测，不再硬编码）
# ═══════════════════════════════════════════════════════════════════
#
# 状态语义：pass=正常；warn=可用但有缺失/降级/未实现；fail=阻断性故障。
# 不存在的子系统（数据库加密/激活/机器指纹）如实返回 warn + "未实现"，
# 禁止谎报 pass（安全状态误导）。

def _from_startup(check_fn: Callable[[], startup_check.CheckResult]) -> tuple[str, str]:
    """复用 startup_check 真实检查项：passed+level → pass/warn/fail。"""
    try:
        r = check_fn()
    except Exception as exc:  # noqa: BLE001
        return "fail", f"检查执行异常: {exc}"
    if r.passed:
        return "pass", r.detail
    return ("fail" if r.level == "error" else "warn"), r.detail


def _probe_db_connection() -> tuple[str, str]:
    db = get_db_safe()
    if db is None:
        return "fail", "数据库连接失败（已降级内存模式）"
    try:
        row = db.query_one("SELECT 1 AS ok")
        if row and row.get("ok") == 1:
            return "pass", f"SQLite 连接正常: {DB_PATH}"
        return "fail", "数据库探测查询返回异常"
    except Exception as exc:  # noqa: BLE001
        return "fail", f"数据库查询失败: {exc}"


def _probe_db_wal() -> tuple[str, str]:
    db = get_db_safe()
    if db is None:
        return "fail", "数据库不可用，无法检测 WAL 模式"
    try:
        row = db.query_one("PRAGMA journal_mode")
        mode = str(list(row.values())[0]).lower() if row else "unknown"
        if mode == "wal":
            return "pass", "WAL 模式已启用"
        return "warn", f"当前 journal_mode={mode}（未启用 WAL）"
    except Exception as exc:  # noqa: BLE001
        return "warn", f"WAL 检测失败: {exc}"


def _probe_db_encryption() -> tuple[str, str]:
    # 诚实标注：SQLCipher 未启用，明文 SQLite（config.yaml 自述，
    # 仅本地 127.0.0.1 运行，规格 §14 约束1/2）。严禁谎报 pass。
    return ("warn", "未启用 SQLCipher 加密：明文 SQLite，仅绑定 127.0.0.1 "
            "本机运行（规格 §14），数据不出本机")


def _probe_dialog_model() -> tuple[str, str]:
    try:
        from ..services.inference.dialog_engine import get_dialog_engine
        engine = get_dialog_engine()
        models = engine.available_models()
        if models:
            return "pass", f"对话模型就绪: {', '.join(models)}"
        return "warn", "对话模型未找到（qwen3-vl-4b/qwen2-vl-2b），对话功能不可用"
    except Exception as exc:  # noqa: BLE001
        return "warn", f"对话引擎探测失败: {exc}"


def _probe_paint_model() -> tuple[str, str]:
    try:
        from ..services.inference.paint_engine import get_paint_engine
        engine = get_paint_engine()
        models = engine.available_models()
        if models:
            return "pass", f"绘画模型就绪: {', '.join(models)}"
        return "warn", "绘画模型未找到（sdxl-base-1.0），绘画功能不可用"
    except Exception as exc:  # noqa: BLE001
        return "warn", f"绘画引擎探测失败: {exc}"


def _find_model_dirs(*keywords: str) -> list[str]:
    """在 models/ 下按关键字（小写子串）探测疑似模型目录（两层）。"""
    hits: list[str] = []
    try:
        for child in sorted(MODELS_DIR.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            name = child.name.lower()
            if any(k in name for k in keywords):
                hits.append(child.name)
                continue
            try:
                for grand in sorted(child.iterdir()):
                    if grand.is_dir() and any(
                            k in grand.name.lower() for k in keywords):
                        hits.append(f"{child.name}/{grand.name}")
            except OSError:
                continue
    except OSError:
        pass
    return hits


def _probe_video_model() -> tuple[str, str]:
    hits = _find_model_dirs("ltx", "wan2", "cogvideo")
    if hits:
        return "pass", f"AI 视频模型就绪: {', '.join(hits)}"
    # 诚实降级标注（审计 BK-014）：视频模型未随包，走 Ken Burns 降级管线
    return ("warn", "LTX-2/Wan2.1/CogVideoX 未随包：视频生成走 Ken Burns "
            "图片推拉降级管线（响应携带 degraded 标记）")


def _probe_voice_model() -> tuple[str, str]:
    hits = _find_model_dirs("cosyvoice", "chattts")
    if hits:
        return "pass", f"音色模型就绪: {', '.join(hits)}"
    # 诚实降级标注（审计 BK-013）：TTS 模型未随包，合成为静音占位
    return ("warn", "CosyVoice/ChatTTS 未随包：语音合成输出静音占位 WAV "
            "（响应携带 degraded 标记）")


def _probe_dir_writable(path: Path, label: str) -> tuple[str, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return "pass", f"{label}可写: {path}"
    except Exception as exc:  # noqa: BLE001
        return "fail", f"{label}不可写: {exc}"


def _probe_task_queue() -> tuple[str, str]:
    try:
        from ..services.lora_training_service import LoRATrainingService
        svc = LoRATrainingService.instance()
        qsize = svc._queue.qsize()
        worker = getattr(svc, "_worker", None)
        if worker is not None and worker.is_alive():
            return "pass", f"训练任务队列运行中（排队 {qsize} 项，工作线程存活）"
        if qsize == 0:
            return "pass", "训练任务队列空闲（工作线程按需启动）"
        return "warn", f"队列积压 {qsize} 项但工作线程未运行"
    except Exception as exc:  # noqa: BLE001
        return "warn", f"任务队列探测失败: {exc}"


def _probe_ws_hub() -> tuple[str, str]:
    try:
        from ..services.ws_hub import get_ws_hub
        hub = get_ws_hub()
        if hub._loop is None:
            return "warn", "WS Hub 未绑定事件循环（应用未完成启动）"
        telemetry = hub._telemetry_task is not None and not hub._telemetry_task.done()
        conns = len(hub._conns)
        if telemetry:
            return "pass", f"WS Hub 运行中（连接 {conns} 条，遥测推送正常）"
        return "warn", f"WS Hub 已绑定但遥测任务未运行（连接 {conns} 条）"
    except Exception as exc:  # noqa: BLE001
        return "warn", f"WS Hub 探测失败: {exc}"


def _probe_scheduler() -> tuple[str, str]:
    try:
        from ..services.scheduler import get_scheduler
        sched = get_scheduler()
        state = sched.get_state()
        if sched._running:
            return "pass", f"调度引擎运行中（协同模式: {state.get('mode')}）"
        return "warn", "调度引擎后台循环未启动"
    except Exception as exc:  # noqa: BLE001
        return "warn", f"调度引擎探测失败: {exc}"


def _probe_feature_lock() -> tuple[str, str]:
    try:
        from ..middleware.feature_lock import get_feature_lock
        status = get_feature_lock().status()
        active = status.get("active_feature")
        if active:
            return "pass", f"功能互斥锁正常（当前持有: {active}）"
        return "pass", "功能互斥锁正常（当前空闲）"
    except Exception as exc:  # noqa: BLE001
        return "fail", f"功能互斥锁探测失败: {exc}"


def _probe_resume_scan() -> tuple[str, str]:
    """断点续传扫描：真实扫描 models/ 下的 .incomplete 下载残留。"""
    try:
        leftovers = [str(p.relative_to(MODELS_DIR))
                     for p in MODELS_DIR.rglob("*.incomplete")]
    except Exception as exc:  # noqa: BLE001
        return "warn", f"断点续传扫描失败: {exc}"
    if leftovers:
        return ("warn", f"发现 {len(leftovers)} 个未完成下载残留，"
                f"可在模型页续传: {', '.join(leftovers[:3])}"
                + (" 等" if len(leftovers) > 3 else ""))
    return "pass", "无未完成下载残留"


# 26 项诊断注册表（名称 + 真实探测函数）
def _probe_cloud_api() -> tuple[str, str]:
    """云端 API 配置诊断（2026-09-06 系统日志升级）：连接/绑定概览。

    未配置=pass（云端是可选增量，未配置并非故障）；配置了则汇报
    连接数与生效绑定（Key 一律打码）。
    """
    try:
        from ..services.cloud_provider_service import (
            ALL_SLOTS,
            ensure_legacy_remote_migrated,
            get_bindings,
            list_providers,
        )
        ensure_legacy_remote_migrated()
        provs = list_providers(mask=True)
        bindings = get_bindings(mask=True)
        if not provs:
            return ("pass", "未配置云端连接（可选功能；添加后可把各工位"
                            "切到云端模型，本地行为不变）")
        enabled = sum(1 for p in provs if p.get("enabled"))
        bound = [f"{ALL_SLOTS.get(slot, slot)}→{b.get('provider_name', '')}"
                 for slot, b in bindings.items()]
        detail = (f"连接 {len(provs)} 条（启用 {enabled}），"
                  f"绑定 {len(bindings)} 项：{'；'.join(bound) or '无'}")
        return ("pass", detail)
    except Exception as exc:  # noqa: BLE001 - 探测异常如实上报
        return ("warn", f"云端配置读取异常: {exc}")


_DIAG_PROBES = [
    ("GPU 可用性", lambda: _from_startup(startup_check._check_gpu_detection)),
    ("GPU 驱动版本", lambda: _from_startup(startup_check._check_gpu_driver)),
    ("显存容量", lambda: _from_startup(startup_check._check_gpu_vram)),
    ("CUDA 可用性", lambda: _from_startup(startup_check._check_cuda_available)),
    ("CPU 核心数", lambda: _from_startup(startup_check._check_cpu_cores)),
    ("内存容量", lambda: _from_startup(startup_check._check_ram_total)),
    ("磁盘剩余空间", lambda: _from_startup(startup_check._check_disk_free)),
    ("数据库连接", _probe_db_connection),
    ("数据库 WAL 模式", _probe_db_wal),
    ("数据库加密", _probe_db_encryption),
    ("对话模型就绪", _probe_dialog_model),
    ("绘画模型就绪", _probe_paint_model),
    ("视频模型就绪", _probe_video_model),
    ("音色模型就绪", _probe_voice_model),
    ("FFmpeg 可用性", lambda: _from_startup(startup_check._check_ffmpeg)),
    ("模型缓存目录", lambda: _probe_dir_writable(MODELS_DIR, "模型缓存目录")),
    ("训练数据目录", lambda: _probe_dir_writable(DATA_DIR / "training", "训练数据目录")),
    ("导出目录可写", lambda: _probe_dir_writable(EXPORT_DIR, "导出目录")),
    ("日志目录可写", lambda: _probe_dir_writable(LOGS_DIR, "日志目录")),
    # 诚实标注（审计 BK-018）：激活/机器指纹子系统 v2.3.1 未实现，如实告知
    ("激活状态", lambda: ("warn", "v2.3.1 未实现激活体系（无 license 子系统），本项如实标记")),
    ("机器指纹一致性", lambda: ("warn", "v2.3.1 未实现机器指纹体系，本项如实标记")),
    ("任务队列运行", _probe_task_queue),
    ("WS Hub 运行", _probe_ws_hub),
    ("调度引擎运行", _probe_scheduler),
    ("功能互斥锁", _probe_feature_lock),
    ("断点续传扫描", _probe_resume_scan),
    ("云端 API 配置", _probe_cloud_api),
]


@router.get("/system/health-check")
def system_health_check() -> dict[str, Any]:
    """一键体检（自愈批4，docs/自愈与横切内建方案-2026-09-10.md §批4）。

    运行时健康只读体检（显存余量/内存/磁盘/孤儿推理进程/模型对账/
    生成队列/日志保留/激活状态），与 POST /system/diagnose（环境级 26 项）
    互补。同步 def 走线程池执行，psutil/torch 扫描不堵事件循环。
    """
    from ..services import health_check
    report = health_check.run_health_check()
    try:
        from ..services.event_log import log_event
        warn = report["counts"]["warn"]
        log_event("system", "health_check",
                  f"一键体检完成：{report['summary']}",
                  level="info" if not warn else "warning",
                  detail=json.dumps(report["counts"], ensure_ascii=False))
    except Exception:  # noqa: BLE001 - 落档失败不影响体检
        pass
    return ok(report)


@router.post("/system/health-repair")
async def system_health_repair(
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    """一键修复（自愈批4）：白名单制，只做零数据风险动作。

    支持 action：clean_logs（清过期日志）/ rebuild_dirs（重建缺失系统
    目录）/ kill_orphans（清确认孤儿推理进程，守卫=绝不碰后端本体）。
    """
    from ..services import health_check
    action = str(body.get("action") or "").strip()
    result = health_check.repair(action)
    try:
        from ..services.event_log import log_event
        log_event("system", "health_repair",
                  f"一键修复（{action}）：{result.get('friendly', '')}",
                  level="success")
    except Exception:  # noqa: BLE001
        pass
    return ok(result)


@router.post("/system/diagnose")
async def system_diagnose() -> dict[str, Any]:
    """运行 26 项检测（规格 §4.7，审计 BK-002 真实探测版）。

    每项实时探测硬件/数据库/引擎/目录真实状态；不存在的子系统
    如实返回 warn + "未实现"，绝不硬编码假 pass。
    """
    results = []
    for i, (name, probe) in enumerate(_DIAG_PROBES):
        try:
            status, detail = probe()
        except Exception as exc:  # noqa: BLE001 - 单项异常不阻断整体诊断
            log.warning("诊断项 %s 探测异常: %s", name, exc)
            status, detail = "warn", f"探测异常: {exc}"
        results.append({"index": i + 1, "name": name,
                        "status": status, "detail": detail})
    summary = {
        "total": len(results),
        "pass": sum(1 for r in results if r["status"] == "pass"),
        "warn": sum(1 for r in results if r["status"] == "warn"),
        "fail": sum(1 for r in results if r["status"] == "fail"),
    }
    return ok({"items": results, "summary": summary,
               "checked_at": time.time()})


# ═══════════════════════════════════════════════════════════════════
#  项目导出 / 导入（审计 BK-041/BK-042：真实归档与恢复）
# ═══════════════════════════════════════════════════════════════════
#
# .omnispace 归档 = ZIP 容器：
#   manifest.json    格式标识/版本/导出时间
#   project.json     projects 行
#   storyboard.json  storyboards 行 + storyboard_rows 全量
#   director.json    director_stages / cameras / characters（分镜关联）
#   assets.json      资产清单（视频任务/音色 + 引用文件存在性快照）

_ARCHIVE_FORMAT = "omnispace-project"
_ARCHIVE_FORMAT_VERSION = 1


def _collect_project_bundle(db: Database, project_id: str) -> dict:
    """从数据库采集项目完整数据包。"""
    project = db.query_one(
        "SELECT id, name, path, created_at, updated_at"
        " FROM projects WHERE id=?", (project_id,))
    if project is None:
        raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", "项目不存在",
                       detail={"project_id": project_id})

    storyboard = db.query_one(
        "SELECT id, project_id, name, created_at, updated_at"
        " FROM storyboards WHERE project_id=?", (project_id,))
    rows: list[dict] = []
    stages: list[dict] = []
    cameras: list[dict] = []
    characters: list[dict] = []
    video_tasks: list[dict] = []
    if storyboard is not None:
        rows = db.query(
            "SELECT id, storyboard_id, shot_number, original_dialogue,"
            " description, characters, scene, props, voice_id,"
            " voice_emotion, director_stage_done, generation_status,"
            " is_ai_generated, sort_index"
            " FROM storyboard_rows WHERE storyboard_id=?"
            " ORDER BY sort_index ASC, shot_number ASC",
            (storyboard["id"],))
        stages = db.query(
            "SELECT id, storyboard_id, scene_id, name, panorama_path,"
            " created_at FROM director_stages WHERE storyboard_id=?",
            (storyboard["id"],))
        for st in stages:
            cameras += db.query(
                "SELECT id, stage_id, name, position, rotation, fov"
                " FROM director_cameras WHERE stage_id=?", (st["id"],))
            characters += db.query(
                "SELECT id, stage_id, character_id, name, position,"
                " rotation, scale, locked"
                " FROM director_characters WHERE stage_id=?", (st["id"],))
        row_ids = [r["id"] for r in rows]
        for rid in row_ids:
            video_tasks += db.query(
                "SELECT id, storyboard_row_id, description, audio_path,"
                " resolution, fps, duration_seconds, codec, model_used,"
                " status, progress, file_path, generation_time_ms,"
                " has_audio_sync, created_at, updated_at"
                " FROM video_tasks WHERE storyboard_row_id=?", (rid,))

    voices = db.query(
        "SELECT id, name, character_id, is_preset, file_path, emotion,"
        " created_at FROM voice_profiles")

    # 资产清单：引用文件 + 存在性快照（不打包二进制，仅登记清单）
    referenced = []
    for vt in video_tasks:
        for key in ("file_path", "audio_path"):
            fp = vt.get(key) or ""
            if fp:
                referenced.append(fp)
    for st in stages:
        if st.get("panorama_path"):
            referenced.append(st["panorama_path"])
    files_manifest = []
    for fp in sorted(set(referenced)):
        p = Path(fp)
        files_manifest.append({
            "path": fp,
            "exists": p.is_file(),
            "size_bytes": p.stat().st_size if p.is_file() else 0,
        })

    return {
        "project": project,
        "storyboard": storyboard,
        "rows": rows,
        "stages": stages,
        "cameras": cameras,
        "characters": characters,
        "video_tasks": video_tasks,
        "voice_profiles": voices,
        "files_manifest": files_manifest,
    }


def _write_project_archive(path: Path, manifest: dict, bundle: dict) -> None:
    """同步写入 .omnispace ZIP 归档（经 run_blocking 卸载的同步核心）。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("project.json",
                    json.dumps(bundle["project"], ensure_ascii=False, indent=2))
        zf.writestr("storyboard.json",
                    json.dumps({"storyboard": bundle["storyboard"],
                                "rows": bundle["rows"]},
                               ensure_ascii=False, indent=2))
        zf.writestr("director.json",
                    json.dumps({"stages": bundle["stages"],
                                "cameras": bundle["cameras"],
                                "characters": bundle["characters"]},
                               ensure_ascii=False, indent=2))
        zf.writestr("assets.json",
                    json.dumps({"video_tasks": bundle["video_tasks"],
                                "voice_profiles": bundle["voice_profiles"],
                                "files": bundle["files_manifest"]},
                               ensure_ascii=False, indent=2))


@router.post("/system/project/export")
async def system_project_export(req: ProjectExport) -> dict[str, Any]:
    """打包 .omnispace（规格 §4.7，审计 BK-041 真实归档版）。

    导出项目元数据 + 分镜表 + 导演台数据 + 资产清单为 ZIP 归档，
    写入 data/generated/exports/，返回真实 file_path 与 file_exists。
    """
    if not req.project_id:
        raise ApiError("SYSTEM_PARAM_INVALID", "project_id 不能为空")
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_UNAVAILABLE", "数据库不可用，无法导出项目")

    # 审计 R1-10：多轮 SQLite 查询与 ZIP 压缩均为阻塞 IO，
    # 经 run_blocking 卸载避免卡住事件循环
    bundle = await run_blocking(
        _collect_project_bundle, db, req.project_id)
    archive_name = f"{req.project_id}_{_now_ts()}.omnispace"
    try:
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = EXPORT_DIR / archive_name
        manifest = {
            "format": _ARCHIVE_FORMAT,
            "format_version": _ARCHIVE_FORMAT_VERSION,
            "app_version": APP_VERSION,
            "project_id": req.project_id,
            "exported_at": time.time(),
        }
        await run_blocking(_write_project_archive, path, manifest, bundle)
        file_path = str(path)
        size_bytes = path.stat().st_size
        file_exists = True
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("项目导出写盘失败：%s", exc)
        raise ApiError("SYSTEM_BACKUP_FAILED", "项目导出失败，请检查磁盘空间",
                       detail={"project_id": req.project_id, "error": str(exc)}) from exc

    return ok({"project_id": req.project_id, "archive": archive_name,
               "path": file_path, "file_path": file_path,
               "file_exists": file_exists, "size_bytes": size_bytes,
               "storyboard_rows": len(bundle["rows"]),
               "video_tasks": len(bundle["video_tasks"])})


def _read_archive_json(zf: zipfile.ZipFile, name: str, required: bool = True) -> Any:
    """读取归档内 JSON 成员；缺失/损坏按 PROJECT_FILE_CORRUPTED 处理。"""
    try:
        raw = zf.read(name)
    except KeyError:
        if required:
            raise ApiError("PROJECT_FILE_CORRUPTED",
                           f"归档缺少必需成员 {name}") from None
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ApiError("PROJECT_FILE_CORRUPTED",
                       f"归档成员 {name} 解析失败: {exc}") from exc


def _fresh_id(db: Database, table: str, old_id: str) -> str:
    """id 冲突时生成新 id，否则沿用原 id。"""
    if old_id and not db.query_one(f"SELECT id FROM {table} WHERE id=?",
                                   (old_id,)):
        return old_id
    return uuid.uuid4().hex


@router.post("/system/project/import")
async def system_project_import(req: ProjectImport) -> dict[str, Any]:
    """导入项目（规格 §4.7，审计 BK-042 真实恢复版）。

    路径经白名单校验（BK-028）→ 解析 .omnispace 归档 → 恢复
    projects/storyboards/storyboard_rows/director_* 数据库记录。
    任何一步失败均返回错误码，绝不谎报 imported:true。
    """
    path = _resolve_safe_path(req.file_path)  # BK-028 白名单 + 穿越校验
    if not path.is_file():
        raise ApiError("FILE_NOT_FOUND", "项目归档文件不存在",
                       detail={"file_path": str(path)})

    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_UNAVAILABLE", "数据库不可用，无法导入项目")

    # 审计 R1-10：归档解析与多轮 SQLite 恢复均为阻塞 IO，
    # 经 run_blocking 卸载避免卡住事件循环
    project, sb_pack, director = await run_blocking(
        _parse_project_archive, path)
    project_id, restored = await run_blocking(
        _restore_project_records, db, project, sb_pack, director)

    return ok({"project_id": project_id, "imported": True,
               "source": str(path), "restored": restored},
              message="项目导入完成")


def _parse_project_archive(path: Path) -> tuple[dict, dict, dict]:
    """同步解析 .omnispace 归档（经 run_blocking 卸载的同步核心）。

    返回 (project, storyboard_pack, director_pack)；容器/成员损坏时
    抛 ApiError PROJECT_FILE_CORRUPTED。
    """
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ApiError("PROJECT_FILE_CORRUPTED",
                       "不是有效的 .omnispace 归档（ZIP 容器损坏）") from None
    with zf:
        manifest = _read_archive_json(zf, "manifest.json")
        if not isinstance(manifest, dict) \
                or manifest.get("format") != _ARCHIVE_FORMAT:
            raise ApiError("PROJECT_FILE_CORRUPTED",
                           "归档格式标识无效（非 OmniSpace 项目归档）")
        project = _read_archive_json(zf, "project.json")
        sb_pack = _read_archive_json(zf, "storyboard.json") or {}
        director = _read_archive_json(zf, "director.json", required=False) or {}

    if not isinstance(project, dict) or not project.get("id"):
        raise ApiError("PROJECT_FILE_CORRUPTED", "project.json 缺少项目记录")
    return project, sb_pack, director


def _restore_project_records(db: Database, project: dict, sb_pack: dict,
                             director: dict) -> tuple[str, dict]:
    """同步恢复项目数据库记录（经 run_blocking 卸载的同步核心）。

    id 冲突时重映射；任何失败级联删除已建项目（回滚）并抛
    PROJECT_FILE_CORRUPTED，绝不谎报 imported:true。
    """
    now = time.time()
    project_id = _fresh_id(db, "projects", str(project["id"]))
    restored = {"storyboard_rows": 0, "stages": 0, "cameras": 0,
                "characters": 0}
    try:
        db.insert("projects", {
            "id": project_id,
            "name": project.get("name") or "导入项目",
            "path": project.get("path", ""),
            "created_at": now, "updated_at": now,
        })

        storyboard = sb_pack.get("storyboard")
        rows = sb_pack.get("rows") or []
        if isinstance(storyboard, dict) and storyboard.get("id"):
            sb_id = _fresh_id(db, "storyboards", str(storyboard["id"]))
            db.insert("storyboards", {
                "id": sb_id, "project_id": project_id,
                "name": storyboard.get("name", ""),
                "created_at": now, "updated_at": now,
            })
            for r in rows:
                if not isinstance(r, dict):
                    continue
                row_id = _fresh_id(db, "storyboard_rows", str(r.get("id", "")))
                db.insert("storyboard_rows", {
                    "id": row_id,
                    "storyboard_id": sb_id,
                    "shot_number": int(r.get("shot_number", 0)),
                    "original_dialogue": r.get("original_dialogue", ""),
                    "description": r.get("description", ""),
                    "characters": r.get("characters", []),
                    "scene": r.get("scene", ""),
                    "props": r.get("props", []),
                    "voice_id": r.get("voice_id", ""),
                    "voice_emotion": r.get("voice_emotion", "默认"),
                    "director_stage_done": int(bool(r.get("director_stage_done", 0))),
                    "generation_status": r.get("generation_status", "pending"),
                    "is_ai_generated": int(bool(r.get("is_ai_generated", 0))),
                    "sort_index": int(r.get("sort_index", 0)),
                })
                restored["storyboard_rows"] += 1

            stage_id_map: dict[str, str] = {}
            for st in director.get("stages") or []:
                if not isinstance(st, dict):
                    continue
                new_sid = _fresh_id(db, "director_stages", str(st.get("id", "")))
                stage_id_map[str(st.get("id", ""))] = new_sid
                db.insert("director_stages", {
                    "id": new_sid, "storyboard_id": sb_id,
                    "scene_id": st.get("scene_id", ""),
                    "name": st.get("name", ""),
                    "panorama_path": st.get("panorama_path", ""),
                    "created_at": now,
                })
                restored["stages"] += 1
            for cam in director.get("cameras") or []:
                if not isinstance(cam, dict):
                    continue
                target_stage = stage_id_map.get(str(cam.get("stage_id", "")))
                if not target_stage:
                    continue
                db.insert("director_cameras", {
                    "id": _fresh_id(db, "director_cameras", str(cam.get("id", ""))),
                    "stage_id": target_stage,
                    "name": cam.get("name", ""),
                    "position": cam.get("position", {}),
                    "rotation": cam.get("rotation", {}),
                    "fov": int(cam.get("fov", 60)),
                })
                restored["cameras"] += 1
            for ch in director.get("characters") or []:
                if not isinstance(ch, dict):
                    continue
                target_stage = stage_id_map.get(str(ch.get("stage_id", "")))
                if not target_stage:
                    continue
                db.insert("director_characters", {
                    "id": _fresh_id(db, "director_characters", str(ch.get("id", ""))),
                    "stage_id": target_stage,
                    "character_id": ch.get("character_id", ""),
                    "name": ch.get("name", ""),
                    "position": ch.get("position", {}),
                    "rotation": ch.get("rotation", {}),
                    "scale": float(ch.get("scale", 1.0)),
                    "locked": int(bool(ch.get("locked", 0))),
                })
                restored["characters"] += 1
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        # 恢复中途失败：级联删除已建项目，避免半截数据
        log.error("项目导入恢复失败：%s", exc)
        try:
            db.delete("projects", "id=?", (project_id,))
        except Exception:  # noqa: BLE001
            pass
        raise ApiError("PROJECT_FILE_CORRUPTED",
                       "项目归档恢复失败，已回滚",
                       detail={"error": str(exc)}) from exc
    return project_id, restored


@router.get("/system/version")
def system_version() -> dict[str, Any]:
    """版本信息（规格 §4.7）。"""
    return ok({
        "version": APP_VERSION,
        "host": HOST,
        "port": PORT,
        "build": f"omnispace-{APP_VERSION}",
        "timestamp": time.time(),
    })


@router.get("/system/update")
def system_update() -> dict[str, Any]:
    """软件更新（文档B §7.1.4，审计 R2-B09 / F-09 契约对齐）。

    RC 为免安装整体替换形态，软件更新经"整体替换目录"完成，
    有意不提供在线更新端点。契约上保留端点并如实返回
    supported:false（比 404 语义更明确），前端据此隐藏更新入口。
    """
    return ok({
        "supported": False,
        "reason": "免安装 RC 形态，软件更新通过整体替换目录完成，"
                  "不提供在线更新端点",
        "channel": "manual-replace",
    })


@router.get("/system/info")
def system_info() -> dict[str, Any]:
    """系统信息聚合（文档B §7.1.4 /system/info，审计 R2-B09）。

    合并版本信息与硬件摘要（GPU 名/显存/tier/CPU/内存），
    供前端"关于/系统"页一次取齐；硬件采集失败时降级为版本信息。
    """
    data: dict = {
        "version": APP_VERSION,
        "host": HOST,
        "port": PORT,
        "build": f"omnispace-{APP_VERSION}",
        "timestamp": time.time(),
    }
    try:
        from ..data.models import detect_hardware_tier
        from .hardware import _build_hardware_profile
        profile = _build_hardware_profile()
        gpu = profile.get("gpu", {})
        data["hardware"] = {
            "gpu_name": gpu.get("name", ""),
            "vram_total_mb": gpu.get("vram_total_mb", 0),
            "tier": detect_hardware_tier(
                str(gpu.get("name", "")), int(gpu.get("vram_total_mb", 0))),
            "cpu_name": profile.get("cpu", {}).get("name", ""),
            "ram_total_gb": profile.get("ram", {}).get("total_gb", 0.0),
        }
    except Exception as exc:  # noqa: BLE001 - 硬件摘要失败不阻塞版本信息
        log.warning("系统信息硬件摘要采集失败（降级）: %s", exc)
    return ok(data)


# ═══════════════════════════════════════════════════════════════════
#  SET 批：推理/网络/训练默认值/自动备份/全量导出/API Key/日志/重启/磁盘
# ═══════════════════════════════════════════════════════════════════

# ── SET-010 推理参数配置 ─────────────────────────────────────────
_INFER_CFG_KEY = "system.inference_config"
_INFER_CFG_DEFAULT = {"num_threads": 0,  # 0 = 不限制（torch 默认物理核）
                      "clear_cache_on_unload": True}


def _inference_config() -> dict:
    cfg = _kv_get(_INFER_CFG_KEY)
    if not isinstance(cfg, dict):
        return dict(_INFER_CFG_DEFAULT)
    return {**_INFER_CFG_DEFAULT, **cfg}


@router.get("/system/inference/config")
def inference_config_get() -> dict[str, Any]:
    """推理参数配置（SET-010）：线程数 / 卸载清缓存策略。"""
    cfg = _inference_config()
    cfg["cpu_cores"] = os.cpu_count() or 1
    return ok(cfg)


@router.put("/system/inference/config")
def inference_config_put(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """更新推理参数配置（SET-010）。

    - num_threads: 1~物理核数（0=不限制），torch.set_num_threads
      即时生效于 CPU 算子；已加载 CUDA 模型不受影响（诚实标注）。
    - clear_cache_on_unload: 模型卸载时是否 torch.cuda.empty_cache()
      （持久化，卸载路径读取生效）。
    """
    cores = os.cpu_count() or 1
    try:
        num_threads = int(body.get("num_threads",
                                   _inference_config()["num_threads"]))
    except (TypeError, ValueError):
        raise ApiError("SYSTEM_PARAM_INVALID", "num_threads 必须是整数") from None
    if not 0 <= num_threads <= cores:
        raise ApiError("SYSTEM_PARAM_INVALID",
                       f"num_threads 取值范围 0~{cores}")
    clear_cache = bool(body.get(
        "clear_cache_on_unload",
        _inference_config()["clear_cache_on_unload"]))

    applied_now = False
    if num_threads > 0:
        try:
            import torch
            torch.set_num_threads(num_threads)
            applied_now = True
        except Exception as exc:  # noqa: BLE001
            log.warning("torch.set_num_threads 失败: %s", exc)

    cfg = {"num_threads": num_threads,
           "clear_cache_on_unload": clear_cache}
    _kv_set(_INFER_CFG_KEY, cfg)
    return ok({**cfg, "cpu_cores": cores, "applied_now": applied_now,
               "note": "num_threads 即时作用于 CPU 算子；"
                       "对已加载 CUDA 模型的 GPU 推理无影响"},
              message="推理配置已更新")


# ── SET-012/013/014 网络配置（代理/镜像/带宽）─────────────────────
_NETWORK_CFG_KEY = "system.network_config"
_NETWORK_CFG_DEFAULT = {"proxy_url": "", "hf_mirror": "",
                        "bandwidth_mbps": 0}  # 0 = 不限速


def _network_config() -> dict:
    cfg = _kv_get(_NETWORK_CFG_KEY)
    if not isinstance(cfg, dict):
        return dict(_NETWORK_CFG_DEFAULT)
    return {**_NETWORK_CFG_DEFAULT, **cfg}


def _apply_network_env(cfg: dict) -> list[str]:
    """把代理/镜像写入进程环境变量（对后续创建的联网连接生效）。"""
    applied: list[str] = []
    proxy = str(cfg.get("proxy_url") or "").strip()
    for var in ("HTTP_PROXY", "HTTPS_PROXY"):
        if proxy:
            os.environ[var] = proxy
        else:
            os.environ.pop(var, None)
    applied.append("proxy_env")
    mirror = str(cfg.get("hf_mirror") or "").strip()
    if mirror:
        os.environ["HF_ENDPOINT"] = mirror
    else:
        os.environ.pop("HF_ENDPOINT", None)
    applied.append("hf_endpoint_env")
    return applied


@router.get("/system/network/config")
def network_config_get() -> dict[str, Any]:
    """网络配置（SET-012/013/014）：代理 / HF 镜像源 / 带宽上限。"""
    return ok(_network_config())


@router.put("/system/network/config")
def network_config_put(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """更新网络配置（SET-012/013/014）。

    离线定位如实说明：本系统仅学习模块（网页抓取）与模型下载联网；
    代理/镜像写入进程环境变量即刻对后续连接生效，带宽上限持久化
    供学习抓取调度读取，均不影响本地推理。
    """
    proxy = str(body.get("proxy_url",
                         _network_config()["proxy_url"]) or "").strip()
    if proxy and not proxy.startswith(("http://", "https://", "socks5://")):
        raise ApiError("SYSTEM_PARAM_INVALID",
                       "proxy_url 仅支持 http/https/socks5 前缀")
    mirror = str(body.get("hf_mirror",
                          _network_config()["hf_mirror"]) or "").strip()
    if mirror and not mirror.startswith(("http://", "https://")):
        raise ApiError("SYSTEM_PARAM_INVALID",
                       "hf_mirror 必须是 http/https URL")
    try:
        bandwidth = float(body.get("bandwidth_mbps",
                                   _network_config()["bandwidth_mbps"]))
    except (TypeError, ValueError):
        raise ApiError("SYSTEM_PARAM_INVALID", "bandwidth_mbps 必须是数字") from None
    bandwidth = max(0.0, min(bandwidth, 10000.0))

    cfg = {"proxy_url": proxy, "hf_mirror": mirror,
           "bandwidth_mbps": bandwidth}
    _kv_set(_NETWORK_CFG_KEY, cfg)
    applied = _apply_network_env(cfg)
    return ok({**cfg, "applied": applied,
               "scope": "仅学习模块联网抓取与模型下载生效；本地推理不受影响"},
              message="网络配置已更新")


# ── SET-015 训练默认参数 ─────────────────────────────────────────

@router.get("/learn/train/defaults")
def train_defaults_get() -> dict[str, Any]:
    """训练默认参数（SET-015）：内置默认 + 用户覆盖合并结果。"""
    from ..services.lora_training_service import get_train_defaults
    return ok(get_train_defaults())


@router.put("/learn/train/defaults")
def train_defaults_put(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """更新训练默认参数（SET-015）。

    白名单字段: lora_rank/lora_alpha/lora_dropout/learning_rate/epochs/
    batch_size/gradient_accumulation_steps/max_seq_length。
    新建训练任务（手动/自动触发）按 内置默认 < 用户默认 < 显式参数
    顺序合并；非法类型/范围由服务层 clamp 兜底。
    """
    from ..services.lora_training_service import set_train_defaults
    if not isinstance(body, dict) or not body:
        raise ApiError("SYSTEM_PARAM_INVALID", "至少提供一个待更新字段")
    effective = set_train_defaults(body)
    return ok(effective, message="训练默认参数已更新")


# ── SET-019 自动备份（开关 + 间隔，后台定时线程）───────────────────
_BACKUP_CFG_KEY = "system.backup_config"
_BACKUP_CFG_DEFAULT = {"enabled": False, "interval_hours": 24}
_LAST_BACKUP_KEY = "system.last_auto_backup"
_backup_thread_started = False


def _backup_config() -> dict:
    cfg = _kv_get(_BACKUP_CFG_KEY)
    if not isinstance(cfg, dict):
        return dict(_BACKUP_CFG_DEFAULT)
    return {**_BACKUP_CFG_DEFAULT, **cfg}


def _perform_backup() -> dict:
    """执行一次备份（设置 JSON + SQLite 热备），供手动/自动共用。"""
    backup_id = uuid.uuid4().hex
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"app_version": APP_VERSION, "backup_id": backup_id,
               "created_at": time.time(),
               "settings": _load_persisted_settings()}
    path = BACKUP_DIR / f"backup_{_now_ts()}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    db_path = ""
    if DB_PATH.is_file():
        import sqlite3
        dest = BACKUP_DIR / f"omnispace_{_now_ts()}.db"
        src = sqlite3.connect(str(DB_PATH))
        try:
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        db_path = str(dest)
    return {"backup_id": backup_id, "path": str(path), "db_path": db_path}


def _backup_scheduler_loop() -> None:
    """自动备份后台线程：每分钟检查一次到期（SET-019）。"""
    while True:
        try:
            cfg = _backup_config()
            if cfg.get("enabled"):
                last = float(_kv_get(_LAST_BACKUP_KEY, 0) or 0)
                interval = float(cfg.get("interval_hours", 24)) * 3600
                if time.time() - last >= max(3600.0, interval):
                    _perform_backup()
                    _kv_set(_LAST_BACKUP_KEY, time.time())
                    log.info("自动备份完成（间隔 %.1fh）",
                             float(cfg.get("interval_hours", 24)))
        except Exception as exc:  # noqa: BLE001
            log.warning("自动备份执行失败: %s", exc)
        time.sleep(60)


def _ensure_backup_scheduler() -> None:
    global _backup_thread_started
    if not _backup_thread_started:
        _backup_thread_started = True
        threading.Thread(target=_backup_scheduler_loop, daemon=True).start()


@router.get("/system/backup/config")
def backup_config_get() -> dict[str, Any]:
    """自动备份配置（SET-019）：{enabled, interval_hours, last_backup_at}。"""
    _ensure_backup_scheduler()
    cfg = _backup_config()
    cfg["last_backup_at"] = float(_kv_get(_LAST_BACKUP_KEY, 0) or 0)
    return ok(cfg)


@router.put("/system/backup/config")
def backup_config_put(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """更新自动备份配置（SET-019）：开关 + 间隔（小时，下限 1h）。"""
    _ensure_backup_scheduler()
    cur = _backup_config()
    enabled = bool(body.get("enabled", cur["enabled"]))
    try:
        interval = float(body.get("interval_hours", cur["interval_hours"]))
    except (TypeError, ValueError):
        raise ApiError("SYSTEM_PARAM_INVALID", "interval_hours 必须是数字") from None
    if not 1 <= interval <= 24 * 30:
        raise ApiError("SYSTEM_PARAM_INVALID",
                       "interval_hours 取值范围 1~720")
    cfg = {"enabled": enabled, "interval_hours": interval}
    _kv_set(_BACKUP_CFG_KEY, cfg)
    return ok(cfg, message="自动备份配置已更新")


# ── SET-021 全量数据导出（tar.gz + SHA256）────────────────────────

class _HashWriter:
    """透传写并累计 SHA256 的文件包装器。"""

    def __init__(self, fp: BinaryIO) -> None:
        self._fp = fp
        self._hash = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self._hash.update(data)
        return self._fp.write(data)

    def flush(self) -> None:
        self._fp.flush()

    def tell(self) -> int:  # tarfile 需要
        return self._fp.tell()

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


def _write_full_export(dest: Path) -> str:
    """同步打包全量数据到 tar.gz 并返回 SHA256（经 run_blocking 卸载的同步核心）。

    内容：omnispace.db（sqlite3 热备副本，WAL 安全）+ settings JSON +
    generated/ 生成物。排除 backups/（备份副本）与 exports/（导出产物，
    避免递归打包自身）。
    """
    import sqlite3
    import tempfile

    hw = _HashWriter(dest.open("wb"))
    with tarfile.open(fileobj=hw, mode="w:gz",
                      format=tarfile.PAX_FORMAT) as tf:
        # 数据库热备副本
        if DB_PATH.is_file():
            tmp = Path(tempfile.mkdtemp(prefix="os_export_")) / "omnispace.db"
            src = sqlite3.connect(str(DB_PATH))
            try:
                dst = sqlite3.connect(str(tmp))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            tf.add(tmp, arcname="data/omnispace.db")
            try:
                tmp.unlink()
                tmp.parent.rmdir()
            except OSError:
                pass
        # 设置快照
        snapshot = json.dumps(_load_persisted_settings(),
                              ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo("data/settings_snapshot.json")
        info.size = len(snapshot)
        info.mtime = time.time()
        tf.addfile(info, fileobj=__import__("io").BytesIO(snapshot))
        # 生成物（图片/视频/漫剧资产等）
        gen_dir = DATA_DIR / "generated"
        if gen_dir.is_dir():
            for f in sorted(gen_dir.rglob("*")):
                if not f.is_file():
                    continue
                rel = f.relative_to(DATA_DIR)
                if rel.parts[:1] in (("exports",), ("backups",)) \
                        or "exports" in rel.parts or "backups" in rel.parts:
                    continue
                tf.add(f, arcname=f"data/{rel.as_posix()}")
    return hw.hexdigest()


@router.post("/system/export")
async def system_full_export() -> dict[str, Any]:
    """全量数据导出（SET-021）：tar.gz + SHA256，写 exports/ 目录。

    含数据库热备副本 + 设置快照 + generated/ 生成物；排除 backups/ 与
    exports/（避免递归打包）。返回路径 / 大小 / 校验和。
    """
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    dest = EXPORT_DIR / f"omnispace_full_{_now_ts()}.tar.gz"
    try:
        sha = await run_blocking(_write_full_export, dest)
    except Exception as exc:  # noqa: BLE001
        log.error("全量导出失败: %s", exc)
        raise ApiError("SYSTEM_BACKUP_FAILED", "全量导出失败，请检查磁盘空间",
                       detail={"error": str(exc)}) from exc
    return ok({"path": str(dest), "size_bytes": dest.stat().st_size,
               "sha256": sha, "format": "tar.gz",
               "includes": ["omnispace.db(热备)", "settings_snapshot.json",
                            "generated/"]})


# ── SET-023 API Key 管理（bcrypt 哈希 + 脱敏显示）────────────────────
_API_KEYS_DDL = """
CREATE TABLE IF NOT EXISTS api_keys (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL DEFAULT '',
    prefix       TEXT NOT NULL DEFAULT '',
    key_hash     TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL DEFAULT 0,
    last_used_at REAL NOT NULL DEFAULT 0
);
"""


def _ensure_api_keys_table() -> None:
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_UNAVAILABLE", "数据库不可用")
    db.executescript(_API_KEYS_DDL)


@router.get("/system/apikeys")
def api_keys_list() -> dict[str, Any]:
    """API Key 列表（SET-023）：仅返回脱敏前缀，绝不返回完整 Key。"""
    _ensure_api_keys_table()
    db = get_db_safe()
    rows = db.query(
        "SELECT id, name, prefix, created_at, last_used_at"
        " FROM api_keys ORDER BY created_at DESC")
    return ok({"items": [{**r, "masked": f"{r['prefix']}…***"}
                         for r in rows], "total": len(rows)})


@router.post("/system/apikeys")
def api_keys_create(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """创建 API Key（SET-023）：完整 Key 仅此一次返回，库存 bcrypt 哈希。"""
    import bcrypt

    _ensure_api_keys_table()
    name = str(body.get("name") or "").strip()[:64]
    if not name:
        raise ApiError("SYSTEM_PARAM_INVALID", "缺少必填参数: name")
    key = "osk-" + secrets.token_urlsafe(24)
    key_hash = bcrypt.hashpw(key.encode("utf-8"),
                             bcrypt.gensalt()).decode("ascii")
    kid = uuid.uuid4().hex
    db = get_db_safe()
    db.insert("api_keys", {
        "id": kid, "name": name, "prefix": key[:10],
        "key_hash": key_hash, "created_at": time.time(),
        "last_used_at": 0,
    })
    return ok({"id": kid, "name": name, "key": key,
               "prefix": key[:10],
               "note": "完整 Key 仅此一次返回，请妥善保存；"
                       "库内仅存储 bcrypt 哈希"},
              message="API Key 已创建")


@router.delete("/system/apikeys/{key_id}")
def api_keys_delete(key_id: str) -> dict[str, Any]:
    """删除 API Key（SET-023）。"""
    _ensure_api_keys_table()
    db = get_db_safe()
    row = db.query_one("SELECT id FROM api_keys WHERE id=?", (key_id,))
    if row is None:
        raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", "API Key 不存在",
                       detail={"id": key_id})
    db.delete("api_keys", "id=?", (key_id,))
    return ok({"id": key_id, "deleted": True})


# ── SET-025/026 日志级别 / 查询 / 导出 / 清理 ───────────────────────
_LOG_LEVEL_KEY = "system.log_level"
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_LOG_LINE_RE = __import__("re").compile(r"\[(DEBUG|INFO|WARNING|ERROR)\]")


def _apply_log_level(level: str) -> None:
    """运行时生效：omnispace 命名 logger + root 同步调整。"""
    lvl = getattr(logging, level, logging.INFO)
    logging.getLogger("omnispace").setLevel(lvl)
    logging.getLogger().setLevel(lvl)


@router.put("/system/logs/level")
def logs_level_put(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """运行时设置日志级别（SET-025）：即时生效 + 持久化。"""
    level = str(body.get("level", "") or "").strip().upper()
    if level not in _LOG_LEVELS:
        raise ApiError("SYSTEM_PARAM_INVALID",
                       f"level 仅支持 {list(_LOG_LEVELS)}")
    _apply_log_level(level)
    _kv_set(_LOG_LEVEL_KEY, level)
    return ok({"level": level}, message=f"日志级别已切换为 {level}")


def _log_files() -> list[Path]:
    """日志文件（按 mtime 新→旧，当前 backend.log 优先）。"""
    try:
        files = [p for p in LOGS_DIR.glob("*.log*") if p.is_file()]
    except OSError:
        return []
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


@router.get("/system/logs")
def system_logs(page: int = Query(1, ge=1),
                page_size: int = Query(100, ge=1, le=500),
                level: str = Query("")) -> dict[str, Any]:
    """日志分页查询（SET-026）：最新在前，可按级别过滤。"""
    lvl = level.strip().upper()
    if lvl and lvl not in _LOG_LEVELS:
        raise ApiError("SYSTEM_PARAM_INVALID",
                       f"level 仅支持 {list(_LOG_LEVELS)}")
    lines: list[str] = []
    for f in _log_files():
        try:
            lines.extend(f.read_text(encoding="utf-8",
                                     errors="replace").splitlines())
        except OSError:
            continue
    lines.reverse()  # 最新在前
    if lvl:
        lines = [ln for ln in lines
                 if f"[{lvl}]" in ln or not _LOG_LINE_RE.search(ln)]
    total = len(lines)
    start = (page - 1) * page_size
    return ok({"items": lines[start:start + page_size], "total": total,
               "page": page, "page_size": page_size})


@router.get("/system/logs/export")
def system_logs_export() -> FileResponse:
    """日志导出（SET-026）：当前 backend.log 以 .txt 下载。"""
    current = LOGS_DIR / "backend.log"
    if not current.is_file():
        raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", "日志文件不存在")
    return FileResponse(str(current), media_type="text/plain",
                        filename=f"omnispace_logs_{_now_ts()}.txt")


@router.post("/system/logs/cleanup")
def system_logs_cleanup(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """日志清理（SET-026）：删除 mtime 早于 keep_days 的日志文件
    （当前活跃 backend.log 永不删除）。"""
    try:
        keep_days = int(body.get("keep_days", 7))
    except (TypeError, ValueError):
        raise ApiError("SYSTEM_PARAM_INVALID", "keep_days 必须是整数") from None
    keep_days = max(1, min(keep_days, 365))
    cutoff = time.time() - keep_days * 86400
    current = (LOGS_DIR / "backend.log").resolve()
    removed: list[str] = []
    freed = 0
    for f in _log_files():
        try:
            if f.resolve() == current:
                continue
            st = f.stat()
            if st.st_mtime < cutoff:
                f.unlink()
                removed.append(f.name)
                freed += st.st_size
        except OSError:
            continue
    return ok({"removed": removed, "removed_count": len(removed),
               "freed_bytes": freed, "keep_days": keep_days})


# ── SET-029 系统重启（确认 token + 延迟自重启线程）────────────────────
_restart_pending = False


def _do_restart() -> None:
    """延迟后原地重启进程（os.execv 替换映像，保持端口/参数）。"""
    import sys
    time.sleep(1.5)
    log.warning("系统重启中（os.execv 原地替换进程）…")
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as exc:  # noqa: BLE001
        log.error("自重启失败: %s", exc)


@router.post("/system/restart")
def system_restart(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """重启后端（SET-029）：{"confirm": "RESTART"}。

    浏览器形态如实说明：无桌面壳守护进程，采用 os.execv 原地替换
    进程映像实现自重启；重启窗口（约 5~15s）内 API 不可用，前端应
    轮询 /health 等待恢复。
    """
    global _restart_pending
    if str(body.get("confirm", "")) != "RESTART":
        raise ApiError("SYSTEM_PARAM_INVALID",
                       '缺少确认 token：请提交 {"confirm": "RESTART"}')
    if _restart_pending:
        return ok({"restarting": True}, message="重启已在进行中")
    _restart_pending = True
    threading.Thread(target=_do_restart, daemon=True).start()
    return ok({"restarting": True, "delay_seconds": 1.5,
               "recovery": "重启窗口内 API 短暂不可用，请轮询 /health"},
              message="系统将在 1.5 秒后重启")


# ── SET-030 磁盘概览 ─────────────────────────────────────────────

def _dir_size(path: Path) -> int:
    total = 0
    try:
        for f in path.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


@router.get("/system/disk")
def system_disk(top: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    """磁盘概览（SET-030）：各卷用量 + 数据目录占用 + top 大文件。"""
    import psutil

    volumes = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        volumes.append({"mount": part.mountpoint,
                        "fstype": part.fstype,
                        "total_gb": round(usage.total / (1024 ** 3), 1),
                        "used_gb": round(usage.used / (1024 ** 3), 1),
                        "free_gb": round(usage.free / (1024 ** 3), 1),
                        "percent": usage.percent})

    data_dirs = {}
    for name in ("generated", "backups", "training", "learn"):
        d = DATA_DIR / name
        if d.is_dir():
            data_dirs[name] = round(_dir_size(d) / (1024 ** 2), 1)
    data_dirs["omnispace.db"] = round(
        DB_PATH.stat().st_size / (1024 ** 2), 1) if DB_PATH.is_file() else 0

    top_files = []
    try:
        for f in DATA_DIR.rglob("*"):
            try:
                if f.is_file():
                    top_files.append((f.stat().st_size, f))
            except OSError:
                continue
    except OSError:
        pass
    top_files.sort(key=lambda x: x[0], reverse=True)
    top_list = [{"path": str(f.relative_to(DATA_DIR)),
                 "size_mb": round(s / (1024 ** 2), 1)}
                for s, f in top_files[:top]]

    return ok({"volumes": volumes,
               "data_dir": str(DATA_DIR),
               "data_usage_mb": data_dirs,
               "top_files": top_list})


# ═══════════════════════════════════════════════════════════════════
# 本地算力 · 省钱账本（2026-09-08 用户拍板：保守口径，只算 AI 输出侧）
# ═══════════════════════════════════════════════════════════════════

# 字数→tokens 折算系数（Qwen 系中英混合文本经验值；UI 明标「估算」）
_CHARS_TO_TOKENS = 0.75
# 云端参考价（国内主流牌价档，集中一处便于拍板调整；UI 明标「参考价」）
_SAVINGS_PRICES = {
    "text_cny_per_mtok": 8.0,   # 文本输出（对齐 DeepSeek-V3 输出档）
    "image_cny_each": 0.2,      # 文生图 1024 档（万相 t2i 牌价区间）
    "video_cny_each": 1.5,      # 5s 图生视频（H3 同类云端档）
}


@router.get("/system/local-savings")
def get_local_savings() -> dict[str, Any]:
    """本地 GPU 产出统计与云端等价省钱估算（只读聚合，无迁移无缓存）。

    口径（保守，宁少报不多报；前端卡片须明标「估算 / 参考价」）：
    - 文本 tokens：dialog_messages 本地助手输出（content+reasoning）字数
      ×系数；输入侧不计；云端回复（model_used 以 cloud:: 开头）排除——
      绑云端的部分是真实开销，不算省钱
    - 生图张数：keyframes+comic_assets+paint_history 有产物路径的行；
      comic_assets 的 meta 标 engine=cloud 扣除。已知边界：keyframes
      云端分支无引擎标记暂计入本地（少数场景）；paint_history 云端
      分支不落表=天然只含本地
    - 视频条数：video_tasks 有产物且 model_used 非 cloud: 前缀
    - 写作台/漫剧文字链路：无「谁生成的」归因字段，v1 不计（防虚报）
    """
    db = get_db_safe()
    if db is None:
        raise ApiError(50001, "数据库不可用")

    def _count(sql: str) -> int:
        try:
            r = db.query_one(sql)
        except Exception:  # noqa: BLE001 - 懒建表（如 paint_history）在
            # 全新装机上要到该功能首次使用才创建：没建过=没用过，计 0
            return 0
        return int(list(r.values())[0]) if r else 0

    msg = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(content)"
        " + COALESCE(LENGTH(reasoning), 0)), 0) AS chars"
        " FROM dialog_messages WHERE role='assistant'"
        " AND (model_used IS NULL OR model_used = ''"
        " OR model_used NOT LIKE 'cloud::%')") or {"n": 0, "chars": 0}
    msg_n = int(msg["n"] or 0)
    msg_chars = int(msg["chars"] or 0)
    tokens = int(msg_chars * _CHARS_TO_TOKENS)

    kf = _count("SELECT COUNT(*) AS n FROM keyframes"
                " WHERE COALESCE(file_path, '') != ''")
    ca = _count("SELECT COUNT(*) AS n FROM comic_assets"
                " WHERE COALESCE(file_path, '') != ''"
                " AND COALESCE(meta, '') NOT LIKE '%cloud%'")
    ph = _count("SELECT COUNT(*) AS n FROM paint_history"
                " WHERE COALESCE(file_path, '') != ''")
    images = kf + ca + ph
    videos = _count(
        "SELECT COUNT(*) AS n FROM video_tasks"
        " WHERE COALESCE(file_path, '') != ''"
        " AND (model_used IS NULL OR model_used = ''"
        " OR model_used NOT LIKE 'cloud:%')")

    money = (tokens / 1_000_000.0 * _SAVINGS_PRICES["text_cny_per_mtok"]
             + images * _SAVINGS_PRICES["image_cny_each"]
             + videos * _SAVINGS_PRICES["video_cny_each"])

    return ok({
        "text": {"messages": msg_n, "chars": msg_chars,
                 "tokens_est": tokens},
        "images": {"count": images,
                   "keyframes": kf, "comic_assets": ca, "paint": ph},
        "videos": {"count": videos},
        "money": {"cny_est": round(money, 2), "prices": _SAVINGS_PRICES},
        "scope_note": "保守口径：仅统计 AI 输出侧（输入/文档未计），"
                      "写作台与漫剧文字链路未纳入；金额按云端参考价估算",
    })
