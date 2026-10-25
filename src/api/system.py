"""系统 API 路由（规格 §4.7 系统 API）。

端点清单：
- GET  /system/settings          获取设置
- PUT  /system/settings          更新设置
- POST /system/backup            备份
- POST /system/diagnose          运行 27 项检测（真实探测，审计 BK-002）
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
import re
import secrets
import tarfile
import threading
import time
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

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
    """读 system_settings 表（JSON 值）；异常/缺失返回 default。

    B1 根修（2026-09-20）：实现收拢至 data/settings_kv.py 单源
    （原与 api/models.py 逐行克隆）。
    """
    from ..data.settings_kv import kv_get
    return kv_get(key, default)


def _kv_set(key: str, value: Any) -> None:
    """写 system_settings 表（JSON 值，UPSERT）——单源同上。"""
    from ..data.settings_kv import kv_set as _kv_set_impl
    _kv_set_impl(key, value)


def _kv_set_strict(key: str, value: Any) -> None:
    """严格写（N-2 家族补丁 2026-10-19）：落库失败上抛，供设置整包
    等高价值写路径——「内存生效、落库静默失败、重启回退」不可接受。"""
    from ..data.settings_kv import kv_set_strict as _impl
    _impl(key, value)


def _load_persisted_settings() -> dict:
    """DB 持久化设置叠加默认值（DB 不可用 → 内存副本）。"""
    persisted = _kv_get(_SETTINGS_KEY)
    if isinstance(persisted, dict):
        merged = dict(SystemSettings().model_dump())
        merged.update(persisted)
        return merged
    # N-2 家族可观测性（2026-10-19）：回退内存快照（boot 态）时必须
    # 留痕——INFO 级可见，排障时能看出「页面显示的是旧值」。
    log.warning("system settings 读取降级：返回内存快照（DB 不可用或读取失败）")
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
    return ok(_mask_remote_key(_load_persisted_settings()))


@router.get("/system/lan-token")
def system_lan_token(request: Request) -> dict[str, Any]:
    """LAN 访问令牌（批2-1 2026-09-18）：回环或持有效令牌可读。

    持令牌者读令牌无提权面；远程无令牌请求到不了这里（中间件先拦）。
    纯本地（回环绑定）模式下令牌无意义，返回 enabled=false + 空串。
    """
    from ..middleware.lan_auth import get_lan_token, lan_auth_enabled
    enabled = lan_auth_enabled()
    token = get_lan_token() if enabled else ""
    return ok({"enabled": enabled, "token": token})


@router.put("/system/settings")
def system_settings_update(req: SystemSettings) -> dict[str, Any]:
    """更新系统设置（规格 §4.7）：写库持久化 + 刷新内存副本。"""
    global _settings
    data = req.model_dump()
    # 值白名单校验（2026-10-19 BUG-02 修复）：单选键拒绝非法值直写，
    # 空串按缺省归一（兼容历史脏库，老值 '' 视为未设置）
    _ENUM_GUARDS: dict[str, tuple[str, set[str]]] = {
        "launch_mode": ("shell", {"shell", "browser"}),
        "ui_performance": ("full", {"full", "lite"}),
    }
    for _key, (_default, _allowed) in _ENUM_GUARDS.items():
        _val = data.get(_key)
        if _val == "" or _val is None:
            data[_key] = _default
        elif _val not in _allowed:
            raise ApiError("SYSTEM_PARAM_INVALID",
                           f"{_key} 非法值: {_val!r}",
                           suggestion=f"允许值：{sorted(_allowed)}")
    # 打码回写保护（2026-09-15 审计修复）：GET 出网是掩码，前端整包
    # 回存时若不拦会把真实 key 覆写成掩码串——掩码入参保留旧值
    if data.get("remote_dialog_api_key") == _REMOTE_KEY_MASK:
        data["remote_dialog_api_key"] = str(
            _load_persisted_settings().get("remote_dialog_api_key") or "")
    # N-2 家族补丁（2026-10-19）：先严格落库、成功才提交内存——
    # 此前内存先改、落库静默失败，重启静默回退且用户毫不知情。
    try:
        _kv_set_strict(_SETTINGS_KEY, data)
    except Exception as exc:  # noqa: BLE001
        raise ApiError("SYSTEM_DB_BUSY",
                       "设置保存失败（数据库忙），本次修改未生效，请稍后重试") from exc
    _settings = data
    # 远程对话配置热生效（批3 D3）：清 5s TTL 缓存，下一跳即用新值
    try:
        from ..services.inference.backends.remote_backend import invalidate_remote_config_cache
        invalidate_remote_config_cache()
    except Exception:  # noqa: BLE001 - 缓存清理失败下个 TTL 自愈
        log.debug("system_settings_update: 降级忽略", exc_info=True)
    try:  # 批5 P18 审计
        from ..services.audit_log import log_audit
        log_audit('system', 'settings_update', target=",".join(sorted(data.keys()))[:180])
    except Exception:  # noqa: BLE001
        pass
    return ok(_settings, message="设置已更新")


# ── 界面偏好镜像（2026-09-12：修复「选型重启回退」类 bug）──────────────
# 前端把对话模型选择/漫剧模型配置/提示词配置/布局偏好等 localStorage 键
# 镜像到此 KV（key='ui.prefs'，值为 {原localStorage键: JSON值}）；
# 启动渲染前回放进 localStorage——localStorage 因换端口顺延（5800→5801）、
# 桌面壳 WebView 配置、清缓存丢失时，选型不再打回默认。
_UI_PREFS_KEY = "ui.prefs"

# P-19 收口（2026-10-24）：宽松 KV 加形状门槛。不做业务白名单——
# 白名单须前后端两处同步维护，前端新增偏好后端漏更=设置静默丢失；
# 命名空间+大小+数量门槛达到同等防护且免维护。超限键「拒键不拒包」
# （部分成功）：前端是读合并写整包，整包 400 会把全部合法设置一并打回。
_UI_PREFS_KEY_RE = re.compile(r"^omnispace\.[\w.]{1,64}$")
_UI_PREFS_VALUE_MAX_BYTES = 8 * 1024
_UI_PREFS_MAX_KEYS = 64


@router.get("/system/ui_prefs")
def ui_prefs_get() -> dict[str, Any]:
    """读取界面偏好镜像（localStorage 键值对，跨端口/跨配置持久）。"""
    prefs = _kv_get(_UI_PREFS_KEY, {})
    return ok(prefs if isinstance(prefs, dict) else {})


@router.put("/system/ui_prefs")
def ui_prefs_update(req: dict = Body(...)) -> dict[str, Any]:
    """写入界面偏好镜像（前端读合并写整包提交；单用户桌面场景无并发竞争）。

    P-19（2026-10-24）：键须匹配 ``omnispace.<名>``（≤64 段内字符）、
    单键 JSON 值 ≤8KB、总键数 ≤64；超限键丢弃并如实返回 dropped，
    其余合法键照常保存（部分成功语义，防脏键连坐）。
    """
    if not isinstance(req, dict):
        return ok({}, message="空偏好已忽略")
    accepted: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in req.items():
        if len(accepted) >= _UI_PREFS_MAX_KEYS:
            dropped.append(str(key))
            continue
        if not isinstance(key, str) or not _UI_PREFS_KEY_RE.match(key):
            dropped.append(str(key))
            continue
        try:
            if len(json.dumps(value, ensure_ascii=False)) > _UI_PREFS_VALUE_MAX_BYTES:
                dropped.append(key)
                continue
        except (TypeError, ValueError):
            dropped.append(key)
            continue
        accepted[key] = value
    _kv_set(_UI_PREFS_KEY, accepted)
    if dropped:
        return ok({"saved": accepted, "dropped": dropped},
                  message=f"界面偏好已保存（{len(dropped)} 个键不合规被忽略）")
    return ok(accepted, message="界面偏好已保存")


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


# ── remote_dialog_api_key 秘密保护（2026-09-15 审计修复）──────────────
# 该字段是批3 旧配置（models.py SystemSettings），未纳入 B0 的
# cloud.providers 加密迁移——GET 出网明文 + 备份 JSON 明文落盘两条
# 泄密面（自动备份接活后后者变为每日一次）。修复口径：
#   GET 打码（掩码串）+ PUT 掩码保留旧值 + 备份 payload 排除
# （settings JSON 无恢复通道——/system/restore 只认 .db 副本——
# 排除零功能损失；DB 热备里的 cloud.providers 本就是 B0 密文）。
_REMOTE_KEY_MASK = "***"


def _mask_remote_key(s: dict) -> dict:
    """GET 出网打码：key 非空 → 掩码（空值保持空，前端表单语义不变）。"""
    out = dict(s)
    if str(out.get("remote_dialog_api_key") or "").strip():
        out["remote_dialog_api_key"] = _REMOTE_KEY_MASK
    return out


def _redact_remote_key(s: dict) -> dict:
    """备份 payload 排除秘密：置空落盘（恢复侧不存在，无需还原）。"""
    out = dict(s)
    if str(out.get("remote_dialog_api_key") or "").strip():
        out["remote_dialog_api_key"] = ""
    return out


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
        # M10 根修（2026-10-21 审计）：_settings 是内存态，重启后未被
        # KV 回填——改用 _load_persisted_settings()（与自动链一致）
        "settings": _redact_remote_key(_load_persisted_settings()),
    }

    def _write_settings_backup() -> str:
        # 批5 P16：备份文件加密（密钥不可用=明文回退原后缀，绝不假加密）
        from ..data.crypto import encrypt_bytes_gcm, is_encrypted_bytes
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        blob = encrypt_bytes_gcm(raw)
        suffix = ".json.enc" if is_encrypted_bytes(blob) else ".json"
        path = BACKUP_DIR / f"backup_{_now_ts()}{suffix}"
        path.write_bytes(blob)
        return str(path)

    def _backup_db() -> str:
        # 批5 P16：热备→整文件加密 .db.enc（密钥不可用明文回退 .db）
        import sqlite3

        from ..data.crypto import encrypt_bytes_gcm, is_encrypted_bytes
        plain = BACKUP_DIR / f"omnispace_{_now_ts()}.db"
        src_conn = sqlite3.connect(str(DB_PATH))
        try:
            dst_conn = sqlite3.connect(str(plain))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        blob = encrypt_bytes_gcm(plain.read_bytes())
        if is_encrypted_bytes(blob):
            dest = plain.with_suffix(plain.suffix + ".enc")
            dest.write_bytes(blob)
            plain.unlink(missing_ok=True)
            return str(dest)
        return str(plain)

    def _backup_aux_dbs() -> list[str]:
        """P1-10 批 1（2026-10-24）：辅助库热备（flow_trace+model_usage）。

        同主库 sqlite3 backup API + GCM 加密；库不存在静默跳过。
        """
        import sqlite3

        from ..data.crypto import encrypt_bytes_gcm, is_encrypted_bytes
        aux_paths = [
            (ROOT_DIR / "logs" / "flow_trace.db", "flow_trace"),
            (DATA_DIR / "model_usage.db", "model_usage"),
        ]
        results: list[str] = []
        for src_path, name in aux_paths:
            if not src_path.is_file():
                continue
            try:
                plain = BACKUP_DIR / f"{name}_{_now_ts()}.db"
                s = sqlite3.connect(str(src_path))
                try:
                    d = sqlite3.connect(str(plain))
                    try:
                        s.backup(d)
                    finally:
                        d.close()
                finally:
                    s.close()
                blob = encrypt_bytes_gcm(plain.read_bytes())
                if is_encrypted_bytes(blob):
                    dest = plain.with_suffix(plain.suffix + ".enc")
                    dest.write_bytes(blob)
                    plain.unlink(missing_ok=True)
                    results.append(str(dest))
                else:
                    results.append(str(plain))
            except Exception as exc:  # noqa: BLE001 - 单库失败不阻断其余
                log.warning("辅助库备份失败 %s: %s", name, exc)
        return results

    def _backup_vector_store() -> str:
        """P1-10 批 2A（2026-10-24）：Chroma 向量库整目录 tar.gz 备份。

        备份前 wal_checkpoint 保证一致性（Chroma 内部 SQLite WAL）；
        目录不存在返回空串。GCM 加密同主库。
        """
        import tarfile
        import tempfile

        from ..data.crypto import encrypt_bytes_gcm, is_encrypted_bytes
        vs_dir = DATA_DIR / "vector_store"
        if not vs_dir.is_dir() or not any(vs_dir.iterdir()):
            return ""
        try:
            # Chroma 内部 SQLite checkpoint（尽力而为）
            chroma_db = vs_dir / "chroma.sqlite3"
            if chroma_db.is_file():
                import sqlite3 as _sq
                try:
                    c = _sq.connect(str(chroma_db))
                    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    c.close()
                except Exception:
                    pass
            # tar.gz 打包
            tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
            tmp.close()
            with tarfile.open(tmp.name, "w:gz") as tar:
                tar.add(str(vs_dir), arcname="vector_store")
            # 加密
            raw = Path(tmp.name).read_bytes()
            blob = encrypt_bytes_gcm(raw)
            if is_encrypted_bytes(blob):
                dest = BACKUP_DIR / f"vector_store_{_now_ts()}.tar.gz.enc"
                dest.write_bytes(blob)
                Path(tmp.name).unlink(missing_ok=True)
                return str(dest)
            dest = BACKUP_DIR / f"vector_store_{_now_ts()}.tar.gz"
            dest.write_bytes(raw)
            Path(tmp.name).unlink(missing_ok=True)
            return str(dest)
        except Exception as exc:  # noqa: BLE001
            log.warning("向量库备份失败: %s", exc)
            return ""

    def _backup_media() -> str:
        """M11 修复（2026-10-24 用户批准）：媒体目录纳入备份。

        comic_assets/keyframes/uploads 三个目录 tar.gz 一包——此前
        备份只保数据库（恢复旧库后图片丢失但 DB 记录在=用户以为有
        备份实则媒体无护）。GCM 加密同主库。
        """
        import tarfile
        import tempfile

        from ..data.crypto import encrypt_bytes_gcm, is_encrypted_bytes
        media_dirs = [d for d in (
            DATA_DIR / "comic_assets",
            DATA_DIR / "keyframes",
            DATA_DIR / "uploads",
        ) if d.is_dir() and any(d.iterdir())]
        if not media_dirs:
            return ""
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
            tmp.close()
            with tarfile.open(tmp.name, "w:gz") as tar:
                for d in media_dirs:
                    tar.add(str(d), arcname=d.name)
            raw = Path(tmp.name).read_bytes()
            blob = encrypt_bytes_gcm(raw)
            if is_encrypted_bytes(blob):
                dest = BACKUP_DIR / f"media_{_now_ts()}.tar.gz.enc"
                dest.write_bytes(blob)
                Path(tmp.name).unlink(missing_ok=True)
                return str(dest)
            dest = BACKUP_DIR / f"media_{_now_ts()}.tar.gz"
            dest.write_bytes(raw)
            Path(tmp.name).unlink(missing_ok=True)
            return str(dest)
        except Exception as exc:  # noqa: BLE001
            log.warning("媒体目录备份失败: %s", exc)
            return ""

    # 写盘/热备属磁盘 IO，经 run_blocking 卸载（审计 09-10 P2-3）
    file_path = ""
    db_path = ""
    try:
        file_path = await run_blocking(_write_settings_backup)
    except Exception as exc:  # noqa: BLE001
        log.warning("备份写盘失败，仅返回内存副本：%s", exc, exc_info=True)

    # 数据库热备（sqlite3 backup API，WAL 下安全）
    db = get_db_safe()
    if db is not None and DB_PATH.is_file():
        try:
            db_path = await run_blocking(_backup_db)
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库热备失败：%s", exc, exc_info=True)

    # P1-10 批 1+2（2026-10-24）：辅助库+向量库纳入备份
    aux_paths: list[str] = []
    vector_path = ""
    try:
        aux_paths = await run_blocking(_backup_aux_dbs)
    except Exception as exc:  # noqa: BLE001
        log.warning("辅助库备份失败：%s", exc, exc_info=True)
    try:
        vector_path = await run_blocking(_backup_vector_store)
    except Exception as exc:  # noqa: BLE001
        log.warning("向量库备份失败：%s", exc, exc_info=True)
    # M11（2026-10-24 用户批准）：媒体目录纳入备份
    media_path = ""
    try:
        media_path = await run_blocking(_backup_media)
    except Exception as exc:  # noqa: BLE001
        log.warning("媒体目录备份失败：%s", exc, exc_info=True)

    return ok({"backup_id": backup_id, "path": file_path,
               "db_path": db_path,
               "aux_paths": aux_paths,
               "vector_store_path": vector_path,
               "media_path": media_path,
               "size_bytes": len(json.dumps(payload)),
               # BUG-07 修复（2026-10-19）：库副本真实体积（此前前端 toast
               # 只拿 settings 快照的 size_bytes，12MB 库显示成「0KB」）
               "db_size_bytes": (Path(db_path).stat().st_size
                                 if db_path and Path(db_path).is_file() else 0)})


# ── B10（2026-09-14）恢复端点：半套备份 → 全套闭环 ─────────────────
_RESTORE_PENDING_DB = DATA_DIR / "omnispace.restore-pending.db"
_RESTORE_PENDING_MARK = DATA_DIR / "omnispace.restore-pending.json"


@router.get("/system/restore/list")
def restore_list() -> dict[str, Any]:
    """列出可恢复的 DB 副本（文件名+体积+修改时间，供恢复 UI 下拉）。"""
    items = []
    if BACKUP_DIR.is_dir():
        from ..data.crypto import is_encrypted_bytes
        files = sorted([*BACKUP_DIR.glob("omnispace_*.db"),
                        *BACKUP_DIR.glob("omnispace_*.db.enc")],
                       key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files:
            try:
                encrypted = (f.suffix == ".enc"
                             and is_encrypted_bytes(f.read_bytes()[:64]))
            except OSError:
                encrypted = f.suffix == ".enc"
            items.append({"filename": f.name,
                          "size_bytes": f.stat().st_size,
                          "mtime": f.stat().st_mtime,
                          "encrypted": encrypted})
    return ok({"backups": items,
               "pending_restore": _RESTORE_PENDING_MARK.is_file()})


# ═══════════════════════════════════════════════════════════════════
#  批4 P11（2026-09-19）：存储清理中心——盘点只读 + 白名单清理
# ═══════════════════════════════════════════════════════════════════
# 硬边界：绝不触碰模型权重目录（models/）与用户作品正文/资产原图
# （comic_assets/keyframes 只盘点不清理——它们是作品数据，删除走各
# 模块的正规删除链）；清理动作全部白名单+目录 containment 闸。

_STORAGE_TARGETS: list[dict[str, Any]] = [
    {"key": "backups", "label": "系统备份", "dir": DATA_DIR / "backups",
     "cleanable": "backups_keep", "note": "数据库与设置快照（恢复用）"},
    {"key": "generated_images", "label": "生成图片",
     "dir": DATA_DIR / "generated" / "images",
     "cleanable": "", "note": "绘画/生成产物（删除请走各页历史记录）"},
    {"key": "thumbs", "label": "缩略图缓存",
     "dir": DATA_DIR / "generated" / "images" / ".thumbs",
     "cleanable": "thumbs", "note": "可安全重建的缓存"},
    {"key": "generated_videos", "label": "生成视频",
     "dir": DATA_DIR / "generated" / "videos",
     "cleanable": "", "note": "视频产物（删除请走生成记录）"},
    {"key": "exports", "label": "导出成品",
     "dir": DATA_DIR / "generated" / "exports",
     "cleanable": "exports", "note": "漫画成册/漫剧导出件（可重新导出）"},
    {"key": "temp", "label": "临时文件",
     "dir": DATA_DIR / "generated" / "temp",
     "cleanable": "temp", "note": "生成过程中间产物"},
    {"key": "comfy_h3", "label": "视频链中间帧",
     "dir": DATA_DIR / "comfyui" / "output" / "h3_chains",
     "cleanable": "comfy_h3", "note": "H3 视频链工作目录（可重建）"},
    {"key": "keyframes", "label": "分镜关键帧",
     "dir": DATA_DIR / "keyframes",
     "cleanable": "", "note": "作品数据（删除请走分镜版本管理）"},
    {"key": "comic_assets", "label": "资产图",
     "dir": DATA_DIR / "comic_assets",
     "cleanable": "", "note": "作品数据（删除请走资产库）"},
    {"key": "knowledge_images", "label": "图片知识原图",
     "dir": DATA_DIR / "knowledge_images",
     "cleanable": "", "note": "作品数据（删除请走知识库）"},
    {"key": "character_lora", "label": "人物LoRA产物",
     "dir": DATA_DIR / "character_lora",
     "cleanable": "", "note": "训练产物（删除请走训练中心）"},
    {"key": "logs", "label": "运行日志",
     "dir": LOGS_DIR, "cleanable": "old_logs",
     "note": "默认保留 30 天"},
]


def _dir_stats(p: Path) -> dict[str, Any]:
    """目录体积/条数（不存在=0；单文件异常不阻断盘点）。"""
    total = 0
    files = 0
    if p.is_dir():
        for f in p.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
                    files += 1
            except OSError:
                continue
    return {"bytes": total, "files": files}


def _safe_clear_dir(d: Path, older_than_days: float = 0.0,
                    skip_names: set[str] | None = None) -> dict[str, Any]:
    """白名单目录内清空/按龄删（containment：resolve 后必须仍在 d 内）。"""
    root = d.resolve()
    if not d.is_dir():
        return {"deleted": 0, "freed_bytes": 0, "samples": []}
    deleted = freed = 0
    samples: list[str] = []
    cutoff = time.time() - older_than_days * 86400
    for f in sorted(d.rglob("*")):
        try:
            if not f.is_file():
                continue
            if skip_names and f.name in skip_names:
                continue
            if older_than_days > 0 and f.stat().st_mtime > cutoff:
                continue
            fr = f.resolve()
            if root not in fr.parents:
                continue  # 穿越守卫（理论不可达，rglob 不会出根）
            size = f.stat().st_size
            f.unlink()
            deleted += 1
            freed += size
            if len(samples) < 20:
                samples.append(f.name)
        except OSError:
            continue
    # 全清模式：回收空子目录（保留根）
    if older_than_days <= 0:
        for sub in sorted(d.rglob("*"), reverse=True):
            try:
                if sub.is_dir() and sub.resolve() != root:
                    sub.rmdir()  # 只删空目录，非空自动 OSError 跳过
            except OSError:
                continue
    return {"deleted": deleted, "freed_bytes": freed, "samples": samples}


@router.get("/system/storage/inventory")
def storage_inventory() -> dict[str, Any]:
    """批4 P11：存储盘点（各产物目录只读扫描 + 磁盘余量）。"""
    targets = []
    for t in _STORAGE_TARGETS:
        st = _dir_stats(t["dir"])
        targets.append({**t, "dir": str(t["dir"]), **st})
    disks = []
    try:
        import psutil
        seen: set[str] = set()
        for dp in {str(ROOT_DIR)[:3], "C:\\", str(DATA_DIR)[:3]}:  # portable-ok（盘符枚举）
            drive = dp.rstrip("\\") + "\\"
            if drive.lower() in seen:
                continue
            seen.add(drive.lower())
            try:
                u = psutil.disk_usage(drive)
                disks.append({"drive": drive,
                              "total_gb": round(u.total / 1024**3, 1),
                              "free_gb": round(u.free / 1024**3, 1),
                              "used_percent": round(u.used / u.total * 100)})
            except OSError:
                continue
    except ImportError:
        pass
    total_bytes = sum(t["bytes"] for t in targets)
    return ok({"targets": targets, "disks": disks,
               "total_mb": round(total_bytes / 1024**2, 1)})


@router.post("/system/storage/cleanup")
def storage_cleanup(req: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """批4 P11：白名单清理动作（前端逐动作独立确认后调用）。

    动作（action）：
    - thumbs / temp / exports / comfy_h3：清空对应可重建目录
    - backups_keep：只保留最近 keep 份 DB 备份+设置快照（默认 3）
    - old_logs：删 logs/ 下超 days 天（默认 30）的文件；活跃
      backend.log 等跳过（活文件不可删）
    """
    action = str(req.get("action") or "")
    try:
        keep = max(1, min(30, int(req.get("keep") or 3)))
        days = max(1, min(3650, int(req.get("days") or 30)))
    except (TypeError, ValueError):
        keep, days = 3, 30

    result: dict[str, Any] | None = None
    label = ""
    if action in ("thumbs", "temp", "exports", "comfy_h3"):
        t = next(x for x in _STORAGE_TARGETS if x["cleanable"] == action)
        label = t["label"]
        result = _safe_clear_dir(t["dir"])
    elif action == "backups_keep":
        label = "旧备份"
        deleted = freed = 0
        samples: list[str] = []
        # R5-Z2 根修（2026-09-20）：.enc 加密备份与连字符命名的历史库
        # 原先不在任何清理 glob 内——手动备份每点一次净增 ~137MB 永久占用。
        for pattern in ("omnispace_*.db", "omnispace_*.db.enc",
                        "omnispace-*.db", "omnispace-*.db.enc",
                        "backup_*.json", "backup_*.json.enc"):
            files = sorted(BACKUP_DIR.glob(pattern),
                           key=lambda f: f.stat().st_mtime, reverse=True)
            for f in files[keep:]:
                try:
                    size = f.stat().st_size
                    f.unlink()
                    deleted += 1
                    freed += size
                    if len(samples) < 20:
                        samples.append(f.name)
                except OSError:
                    continue
        result = {"deleted": deleted, "freed_bytes": freed, "samples": samples}
    elif action == "old_logs":
        label = f"{days} 天前的旧日志"
        result = _safe_clear_dir(
            LOGS_DIR, older_than_days=days,
            skip_names={"backend.log", "backend_stderr.log", "boot.log",
                        "heartbeat_history.jsonl"})
    else:
        raise ApiError("SYSTEM_PARAM_INVALID",
                       "未知清理动作（白名单：thumbs/temp/exports/comfy_h3/"
                       "backups_keep/old_logs）")
    try:
        from ..services.event_log import log_event
        log_event("system", "storage_cleanup",
                  f"清理「{label}」：删 {result['deleted']} 项，释放 "
                  f"{result['freed_bytes'] / 1024 / 1024:.1f}MB",
                  level="success")
    except Exception:  # noqa: BLE001
        log.debug("storage_cleanup: 落档降级忽略", exc_info=True)
    return ok({"action": action, "label": label, **result,
               "freed_mb": round(result["freed_bytes"] / 1024**2, 1)})


@router.post("/system/backups/delete")
def backups_delete(req: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """批4 P11：删除单个备份文件（白名单文件名形态+目录 containment）。"""
    name = Path(str(req.get("filename") or "")).name
    # R5-Z2 根修（2026-09-20）：允许删除 .enc 加密备份与连字符历史库
    # （原先 UI 连手动删除 .enc 的入口都没有）。
    _ok_name = (
        (name.startswith("omnispace_") or name.startswith("omnispace-"))
        and name.endswith((".db", ".db.enc"))
    ) or (name.startswith("backup_") and name.endswith((".json", ".json.enc")))
    if not _ok_name:
        raise ApiError("SYSTEM_PARAM_INVALID", "备份文件名不合法")
    target = (BACKUP_DIR / name).resolve()
    if BACKUP_DIR.resolve() not in target.parents:
        raise ApiError("SYSTEM_PARAM_INVALID", "路径越界")
    if not target.is_file():
        raise ApiError("SYSTEM_BACKUP_NOT_FOUND", "备份文件不存在")
    size = target.stat().st_size
    target.unlink()
    return ok({"filename": name, "freed_mb": round(size / 1024**2, 1)})


# ═══════════════════════════════════════════════════════════════════
#  批4 P19（2026-09-19）：硬件健康中心——四项仪表聚合 + 阈值三色
# ═══════════════════════════════════════════════════════════════════

@router.get("/system/hardware/selfcheck")
def hardware_selfcheck() -> dict[str, Any]:
    """批5 P22（2026-09-19）：环境与硬件变更自检。

    ①GPU 驱动版本（pynvml；空=探测失败如实报）
    ②外部路径模型可达性（移动硬盘拔盘→登记仍在→如实标不可达）
    ③换硬件后的激活换绑指引（人话步骤）
    """
    driver = ""
    gpu_name = ""
    try:
        import pynvml  # type: ignore
        pynvml.nvmlInit()
        try:
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            gpu_name = pynvml.nvmlDeviceGetName(h)
            if isinstance(gpu_name, bytes):
                gpu_name = gpu_name.decode("utf-8", errors="replace")
            d = pynvml.nvmlSystemGetDriverVersion()
            driver = d.decode("utf-8", "replace") if isinstance(d, bytes) else str(d)
        finally:
            pynvml.nvmlShutdown()
    except Exception:  # noqa: BLE001
        log.debug("selfcheck: GPU 探测降级", exc_info=True)

    # 外部模型可达性（manifest 里 file_path 是绝对路径的=外挂盘登记）
    unreachable: list[dict[str, str]] = []
    try:
        db = get_db_safe()
        if db is not None:
            for r in db.query(
                    "SELECT id, name, file_path FROM models"
                    r" WHERE file_path LIKE '%:%\%' OR file_path LIKE '%:/%'"):
                fp = str(r.get("file_path") or "")
                if fp and not Path(fp).exists():
                    unreachable.append({"model_id": r["id"],
                                        "name": r.get("name", ""),
                                        "path": fp})
    except Exception:  # noqa: BLE001
        log.debug("selfcheck: 外部模型检查降级", exc_info=True)

    # 驱动过旧启发式（major < 500 = 2022 年前架构，CUDA 12 时代之前）
    driver_old = False
    if driver:
        try:
            driver_old = int(driver.split(".")[0]) < 500
        except (ValueError, IndexError):
            pass

    return ok({
        "gpu": {"name": gpu_name, "driver_version": driver,
                "driver_old": driver_old,
                "hint": ("驱动较旧（major<500），如遇 CUDA 报错建议先升"
                         "级显卡驱动" if driver_old else
                         ("驱动版本读取失败" if not driver else "正常"))},
        "external_models_unreachable": unreachable,
        "rebind_guide": [
            "换电脑/换硬盘/换显卡后激活失效属正常（指纹变了）",
            "旧机：启动页「产品激活」卡 → 点「解绑本机」→ 复制解绑码",
            "新机：把解绑码发给卖家，卖家在发码台验码后发新激活码",
            "输入新激活码即完成换绑（旧机授权即时作废）",
        ],
    })


@router.get("/system/hardware/health")
def hardware_health() -> dict[str, Any]:
    """批4 P19：显存/内存/温度/磁盘 四项聚合 + 阈值状态（三色）。

    阈值与 resource_guard/scheduler 同源（config.scheduler.thresholds）；
    温度传感器 Windows 常缺——如实标 unavailable，绝不编数。
    """
    from ..config import get_config
    th = (get_config().get("scheduler") or {}).get("thresholds") or {}
    def _pct(v: float) -> float:
        """批4 P19→大考 P-11 修复：config 阈值为比例（0~1），历史代码按
        百分数（0~100）语义用——16.3% > 0.80 恒 danger 误报实锤。
        归一：<1 视为比例 ×100。"""
        return v * 100.0 if 0 < v < 1 else v
    vram_warn = _pct(float(th.get("gpu_vram_warning", 80)))
    vram_crit = _pct(float(th.get("gpu_vram_critical", 90)))
    ram_crit = _pct(float(th.get("ram_critical_percent", 90)))

    def _level(pct: float, warn: float, crit: float) -> str:
        return "danger" if pct >= crit else ("warning" if pct >= warn else "ok")

    out: dict[str, Any] = {"vram": {"available": False},
                           "ram": {"available": False},
                           "temp": {"available": False},
                           "disks": [], "thresholds": {
                               "vram_warn": vram_warn, "vram_crit": vram_crit,
                               "ram_crit": ram_crit,
                               "disk_warn_gb": 32, "disk_crit_gb": 8}}
    # 显存（pynvml，缺席如实 unavailable）
    try:
        import pynvml  # type: ignore
        pynvml.nvmlInit()
        try:
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            m = pynvml.nvmlDeviceGetMemoryInfo(h)
            used_pct = (m.total - m.free) / m.total * 100
            out["vram"] = {
                "available": True,
                "total_mb": int(m.total // 1048576),
                "used_mb": int((m.total - m.free) // 1048576),
                "used_percent": round(used_pct, 1),
                "level": _level(used_pct, vram_warn, vram_crit)}
        finally:
            pynvml.nvmlShutdown()
    except Exception:  # noqa: BLE001 - pynvml 缺席走内存/磁盘仍可用
        log.debug("hardware_health: 显存探测降级", exc_info=True)
    # 内存/温度/磁盘
    try:
        import psutil
        vm = psutil.virtual_memory()
        out["ram"] = {"available": True,
                      "total_gb": round(vm.total / 1024**3, 1),
                      "used_gb": round((vm.total - vm.available) / 1024**3, 1),
                      "used_percent": vm.percent,
                      "level": _level(vm.percent, ram_crit - 5, ram_crit)}
        try:
            temps: dict = getattr(psutil, "sensors_temperatures",
                                   lambda: {})()
            if temps:
                entries = temps.get("coretemp") or temps.get(
                    "acpitz") or next(iter(temps.values()))
                t = max((e.current or 0) for e in entries) if entries else None
                if t:
                    out["temp"] = {"available": True, "celsius": round(t, 1),
                                   "level": ("danger" if t >= 90 else
                                             "warning" if t >= 80 else "ok")}
        except Exception:  # noqa: BLE001
            pass
        seen: set[str] = set()
        for dp in (str(DATA_DIR)[:3], "C:\\"):  # portable-ok（盘符枚举）
            drive = dp.rstrip("\\") + "\\"
            if drive.lower() in seen:
                continue
            seen.add(drive.lower())
            u = psutil.disk_usage(drive)
            free_gb = u.free / 1024**3
            out["disks"].append({
                "drive": drive, "free_gb": round(free_gb, 1),
                "total_gb": round(u.total / 1024**3, 1),
                "level": ("danger" if free_gb < 8 else
                          "warning" if free_gb < 32 else "ok")})
    except ImportError:
        log.debug("hardware_health: psutil 缺席")
    # 保护动作清单（现有散件能力如实展示）
    out["protections"] = [
        {"key": "vram_critical", "label": "显存≥90% 自动卸载非活跃模型",
         "enabled": True},
        {"key": "ram_gate", "label": "内存危急线拒新任务+腾内存",
         "enabled": True},
        {"key": "thermal", "label": "GPU≥90°C 暂停接单", "enabled": True},
        {"key": "degrade", "label": "GPU 高压自动降参保质量", "enabled": True},
        {"key": "watchdog", "label": "后端异常退出自动补位（≤5次）",
         "enabled": True},
    ]
    return ok(out)


@router.post("/system/restore")
async def system_restore(req: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """安排从备份恢复（**下次重启时生效**，当前会话不受影响）。

    请求: {"filename": "omnispace_<ts>.db"}——文件必须位于 data/backups/
    （白名单目录，防路径穿越）。安排后写入 restore-pending 标记；启动
    链在数据库初始化**之前**检测到标记：先把当前主库备份一份（防呆），
    再用副本替换主库并清除标记。全程可回退（替换前的那份仍在 backups/）。
    """
    filename = str(req.get("filename") or "")
    safe = Path(filename).name
    if safe != filename or not safe.startswith("omnispace_") \
            or not (safe.endswith(".db") or safe.endswith(".db.enc")):
        raise ApiError("SYSTEM_PARAM_INVALID", "非法备份文件名",
                       suggestion="请从 GET /system/restore/list 返回的清单中选择")
    src = BACKUP_DIR / safe
    if not src.is_file():
        raise ApiError("SYSTEM_BACKUP_NOT_FOUND", "备份文件不存在",
                       detail={"filename": safe})
    size = src.stat().st_size
    if size < 4096:
        raise ApiError("SYSTEM_PARAM_INVALID",
                       "备份文件过小（疑似损坏），已拒绝恢复")

    def _stage() -> None:
        # 批5 P16：加密备份先解密再暂存（明文备份直拷，头嗅探分流）
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        import shutil
        if src.suffix == ".enc":
            from ..data.crypto import decrypt_bytes_gcm
            _RESTORE_PENDING_DB.write_bytes(
                decrypt_bytes_gcm(src.read_bytes()))
        else:
            shutil.copy2(src, _RESTORE_PENDING_DB)
        # P1-10 批 3（2026-10-24）：同时间戳辅助库+向量库一并暂存
        # （best-effort：缺失/失败不阻断主库恢复）
        import re as _re
        _ts_match = _re.search(r"(\d{8}_\d{6})", safe)
        _ts = _ts_match.group(1) if _ts_match else ""
        _aux_staged: list[str] = []
        if _ts:
            for _prefix, _target in (
                    ("flow_trace", "flow_trace.restore-pending.db"),
                    ("model_usage", "model_usage.restore-pending.db")):
                for _ext in (".db.enc", ".db"):
                    _aux_src = BACKUP_DIR / f"{_prefix}_{_ts}{_ext}"
                    if _aux_src.is_file():
                        try:
                            _aux_dst = DATA_DIR / _target
                            if _ext == ".enc":
                                from ..data.crypto import decrypt_bytes_gcm
                                _aux_dst.write_bytes(
                                    decrypt_bytes_gcm(_aux_src.read_bytes()))
                            else:
                                shutil.copy2(_aux_src, _aux_dst)
                            _aux_staged.append(_prefix)
                        except Exception:
                            pass
                        break
            # 向量库 tar.gz
            for _ext in (".tar.gz.enc", ".tar.gz"):
                _vs_src = BACKUP_DIR / f"vector_store_{_ts}{_ext}"
                if _vs_src.is_file():
                    try:
                        _vs_dst = DATA_DIR / "vector_store.restore-pending.tar.gz"
                        if _ext == ".tar.gz.enc":
                            from ..data.crypto import decrypt_bytes_gcm
                            _vs_dst.write_bytes(
                                decrypt_bytes_gcm(_vs_src.read_bytes()))
                        else:
                            shutil.copy2(_vs_src, _vs_dst)
                        _aux_staged.append("vector_store")
                    except Exception:
                        pass
                    break
        _RESTORE_PENDING_MARK.write_text(
            json.dumps({"source": safe, "staged_at": time.time(),
                        "size": size, "aux_staged": _aux_staged},
                       ensure_ascii=False),
            encoding="utf-8")

    await run_blocking(_stage)
    from ..services.event_log import log_event
    log_event("system", "restore_staged",
              "已安排从备份恢复（下次重启时生效）",
              level="warning",
              detail=json.dumps({"source": safe}, ensure_ascii=False))
    return ok({"staged": True, "source": safe,
               "hint": "重启应用后恢复生效；替换前的当前库会先备份到 backups/"})


@router.post("/system/restore/cancel")
def restore_cancel() -> dict[str, Any]:
    """撤销已安排的恢复（2026-10-19 BUG-05 修复）。

    此前安排后无任何撤销通道，误触即成定时炸弹（下次重启整库回滚）。
    删除暂存副本与标记文件即可解除，不触碰当前主库。
    """
    if not _RESTORE_PENDING_MARK.is_file() and not _RESTORE_PENDING_DB.is_file():
        raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", "当前没有待恢复的安排")
    _RESTORE_PENDING_MARK.unlink(missing_ok=True)
    _RESTORE_PENDING_DB.unlink(missing_ok=True)
    from ..services.event_log import log_event
    log_event("system", "restore_cancel",
              "已撤销恢复安排（标记与暂存已清除）", level="info")
    return ok({"cancelled": True})


# ═══════════════════════════════════════════════════════════════════
#  系统诊断（审计 BK-002：27 项全部真实探测，不再硬编码）
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
        from ..config import get_config
        _comfy_mode = str((get_config().get("paint") or {}).get(
            "gen_engine", "legacy")).strip().lower() == "comfy"
    except Exception:  # noqa: BLE001
        _comfy_mode = False
    if _comfy_mode:
        # W3-C：comfy 档探测便携版+权重齐备性（ComfyUI 常驻语义）
        try:
            from ..services.inference.comfy_paint_engine import (
                comfy_paint_available,
            )
            if comfy_paint_available():
                return "pass", "绘画出图就绪（ComfyUI klein-9b-fp8 栈）"
            return "warn", ("ComfyUI klein 出图栈不可用（便携版或权重缺失），"
                            "绘画功能不可用")
        except Exception as exc:  # noqa: BLE001
            return "warn", f"绘画引擎探测失败: {exc}"
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
        log.debug("_find_model_dirs: 降级忽略", exc_info=True)
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


# 27 项诊断注册表（名称 + 真实探测函数）
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


def _probe_activation_state() -> tuple[str, str]:
    """激活子系统真实状态（BUG-11 修复：陈旧探针曾谎报「未实现」）。"""
    try:
        from .. import license_gate as _gate
        st = _gate.status()
        if not st.get("gate_enabled"):
            return ("warn", "激活体系已实现未启用（开发版未注入公钥，门禁"
                            "关闭；发码台/激活链随发行包接入）")
        if st.get("activated"):
            return ("pass", "激活体系已启用且授权有效")
        return ("warn", f"激活体系已启用但授权异常：{st.get('reason') or '未激活'}")
    except Exception as exc:  # noqa: BLE001 - 探测异常如实上报
        return ("warn", f"激活状态探测失败: {exc}")


def _probe_fingerprint_state() -> tuple[str, str]:
    """机器指纹子系统真实状态（BUG-11 修复同批）。"""
    try:
        from .. import license_gate as _gate
        fps = _gate.collect_fingerprints()
        n = len(fps)
        if n >= 2:
            return ("pass", f"机器指纹采集正常（{n} 路指纹参与交叉校验）")
        if n == 1:
            return ("warn", "仅 1 路指纹可采集（发行机要求多路交叉锚，"
                            "单路指纹强度不足）")
        return ("warn", "机器指纹采集为空（当前环境探测受限）")
    except Exception as exc:  # noqa: BLE001 - 探测异常如实上报
        return ("warn", f"机器指纹探测失败: {exc}")


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
    # BUG-11 修复（2026-10-21）：激活/机器指纹子系统早已实现（license_gate.py
    # 门面 + license_gate_impl.py 锁芯 + main.py 启动预热），陈旧探针却硬编码
    # 「未实现」——把「未启用」谎报成「未实现」。改为读取真实门禁状态。
    ("激活状态", _probe_activation_state),
    ("机器指纹一致性", _probe_fingerprint_state),
    ("任务队列运行", _probe_task_queue),
    ("WS Hub 运行", _probe_ws_hub),
    ("调度引擎运行", _probe_scheduler),
    ("功能互斥锁", _probe_feature_lock),
    ("断点续传扫描", _probe_resume_scan),
    ("云端 API 配置", _probe_cloud_api),
]


@router.get("/system/modules/health")
def system_modules_health() -> dict[str, Any]:
    """批2 P31（2026-09-19）：功能舱壁健康快照（对话/绘画/视频/训练）。

    状态栏初拉 + 恢复指南用；变化态经 WS module_health 实时推送。
    附带自愈运行态（单飞/冷却/尝试上限）。
    """
    from ..services import module_health, self_heal
    return ok({**module_health.snapshot(), "self_heal": self_heal.state_snapshot()})


@router.post("/system/modules/health/recover")
async def system_modules_recover(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """批2 P32/P33：手动触发某舱自动恢复（恢复指南卡「再试一次」）。

    人工点击=新一轮授权：清空该舱自动尝试计数后走统一自愈链
    （单飞+冷却闸仍在，防连点风暴）。
    """
    module = str((body or {}).get("module") or "").strip()
    from ..services import module_health, self_heal
    if module not in module_health.MODULES:
        raise ApiError("SYSTEM_PARAM_INVALID", "module 须为 dialog/paint/video/training")
    self_heal.reset_module_attempts(module)
    started = await run_in_threadpool(
        lambda: self_heal.try_recover(module, trigger="manual"))
    return ok({"module": module, "started": bool(started)})


@router.get("/system/health-check")
def system_health_check() -> dict[str, Any]:
    """一键体检（自愈批4，docs/自愈与横切内建方案-2026-09-10.md §批4）。

    运行时健康只读体检（显存余量/内存/磁盘/孤儿推理进程/模型对账/
    生成队列/日志保留/激活状态），与 POST /system/diagnose（环境级 27 项）
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
        log.debug("system_health_check: 降级忽略", exc_info=True)
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
        log.debug("system_health_repair: 降级忽略", exc_info=True)
    return ok(result)


@router.post("/system/diagnose")
async def system_diagnose() -> dict[str, Any]:
    """运行 27 项检测（规格 §4.7，审计 BK-002 真实探测版）。

    每项实时探测硬件/数据库/引擎/目录真实状态；不存在的子系统
    如实返回 warn + "未实现"，绝不硬编码假 pass。
    """
    results = []
    for i, (name, probe) in enumerate(_DIAG_PROBES):
        try:
            status, detail = probe()
        except Exception as exc:  # noqa: BLE001 - 单项异常不阻断整体诊断
            log.warning("诊断项 %s 探测异常: %s", name, exc, exc_info=True)
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
#   director.json    恒空数组（3D 导演台 2026-08-29 剔除；保留成员仅为归档格式兼容）
#   assets.json      资产清单（视频任务/音色 + 引用文件存在性快照）

_ARCHIVE_FORMAT = "omnispace-project"
_ARCHIVE_FORMAT_VERSION = 1


def _collect_project_bundle(db: Database, project_id: str) -> dict:
    """从数据库采集项目完整数据包。"""
    project = db.query_one(
        "SELECT id, name, path, created_at, updated_at"
        " FROM projects WHERE id=?", (project_id,))
    if project is None:
        raise ApiError("STORYBOARD_PROJECT_NOT_FOUND", "项目不存在",
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
            " voice_emotion, generation_status,"
            " is_ai_generated, sort_index"
            " FROM storyboard_rows WHERE storyboard_id=?"
            " ORDER BY sort_index ASC, shot_number ASC",
            (storyboard["id"],))
        # 3D 导演台残留清除（2026-10-23）：director_* 三表已 DROP，
        # 导出链不再读它们——stages/cameras/characters 恒空数组
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
    files_manifest = []
    for fp in sorted(set(referenced)):
        p = Path(fp)
        files_manifest.append({
            "path": fp,
            "exists": p.is_file(),
            "size_bytes": p.stat().st_size if p.is_file() else 0,
        })

    # 注意：内部函数返回裸字典（2026-10-19 BUG-06 修复）——此前误用 ok()
    # 包成 HTTP 信封，下游 _write_project_archive 取 bundle["project"] 必炸，
    # 项目导出全类型瘫痪。
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
        log.error("项目导出写盘失败：%s", exc, exc_info=True)
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
    projects/storyboards/storyboard_rows 数据库记录。
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
    project, sb_pack = await run_blocking(
        _parse_project_archive, path)
    project_id, restored = await run_blocking(
        _restore_project_records, db, project, sb_pack)

    return ok({"project_id": project_id, "imported": True,
               "source": str(path), "restored": restored},
              message="项目导入完成")


def _parse_project_archive(path: Path) -> tuple[dict, dict]:
    """同步解析 .omnispace 归档（经 run_blocking 卸载的同步核心）。

    返回 (project, storyboard_pack)；容器/成员损坏时
    抛 ApiError PROJECT_FILE_CORRUPTED。旧归档的 director.json
    （3D 导演台残留，2026-10-23 剔除）如存在则忽略。
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
        # director.json（3D 导演台残留）不再读取——旧归档如有该成员直接忽略

    if not isinstance(project, dict) or not project.get("id"):
        raise ApiError("PROJECT_FILE_CORRUPTED", "project.json 缺少项目记录")
    return project, sb_pack


def _restore_project_records(db: Database, project: dict,
                             sb_pack: dict) -> tuple[str, dict]:
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
                    "generation_status": r.get("generation_status", "pending"),
                    "is_ai_generated": int(bool(r.get("is_ai_generated", 0))),
                    "sort_index": int(r.get("sort_index", 0)),
                })
                restored["storyboard_rows"] += 1
            # 3D 导演台残留清除（2026-10-23）：director_* 三表已 DROP，
            # 旧归档中的 director 数据不再恢复（stages/cameras/characters 恒 0）
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        # 恢复中途失败：级联删除已建项目，避免半截数据
        log.error("项目导入恢复失败：%s", exc, exc_info=True)
        try:
            db.delete("projects", "id=?", (project_id,))
        except Exception:  # noqa: BLE001
            log.debug("_restore_project_records: 降级忽略", exc_info=True)
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


@router.post("/system/shutdown")
async def system_shutdown() -> dict[str, Any]:
    """触发后端优雅关停（P1-12 批 2，用户批准 2026-10-24）。

    供 boot.py 看门狗从硬杀改优雅优先：SIGTERM → uvicorn 优雅退出
    → lifespan 清理（引擎卸载/队列排空/ComfyUI 清理/浏览器池关停）。
    仅本机回环可调（非回环由 TrustedHost 中间件拦）。
    """
    import os
    import signal
    import threading

    def _send_sigterm() -> None:
        time.sleep(0.5)  # 先返回响应再杀
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_send_sigterm, daemon=True).start()
    return ok({"shutting_down": True},
              message="优雅关停已触发（lifespan 清理后退出）")


@router.get("/system/update")
def system_update() -> dict[str, Any]:
    """软件更新状态（升级机制批2 语义激活：原 supported:false 占位退役）。

    联动 upgrade_service 摘要：本构建带应用内升级机制（updates/ 目录 +
    设置页「软件升级」），在线检查更新仍不做（D3=纯离线，另立项）。
    """
    try:
        from ..services.upgrade_service import current_info, scan_updates
        compatible = [p for p in scan_updates() if p.get("compatible")]
        return ok({
            "supported": True,
            "channel": "offline-package",
            "current": current_info(),
            "compatible_updates": len(compatible),
            "latest": (compatible[0].get("manifest") if compatible else None),
        })
    except Exception:  # noqa: BLE001 - 升级层故障按降级语义返回
        return ok({
            "supported": True,
            "channel": "offline-package",
            "current": {"version": APP_VERSION},
            "compatible_updates": 0,
            "latest": None,
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
        log.warning("系统信息硬件摘要采集失败（降级）: %s", exc, exc_info=True)
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
            log.warning("torch.set_num_threads 失败: %s", exc, exc_info=True)

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
# B10（2026-09-14）：enabled 默认 True（数据保全 P0 收口——旧默认关
# 导致「库损坏→静默空库」时无近期快照可救）；DB 副本滚动保留 3 份。
_BACKUP_CFG_KEY = "system.backup_config"
_BACKUP_CFG_DEFAULT = {"enabled": True, "interval_hours": 24}
_BACKUP_KEEP_DB = 3
_BACKUP_KEEP_JSON = 3
_LAST_BACKUP_KEY = "system.last_auto_backup"
_backup_thread_started = False


def _backup_config() -> dict:
    cfg = _kv_get(_BACKUP_CFG_KEY)
    if not isinstance(cfg, dict):
        return dict(_BACKUP_CFG_DEFAULT)
    return {**_BACKUP_CFG_DEFAULT, **cfg}


def _perform_backup() -> dict:
    """执行一次备份（设置 JSON + SQLite 热备），供手动/自动共用。

    BUG-10 修复（2026-10-20 拍板 A）：本函数此前纯明文落盘——调度器
    （每日自动备份）与端点版（POST /system/backup，GCM 加密）双实现
    口径分裂，自动备份一直在裸奔整库。现对齐端点同款加密；密钥不可用
    回退明文（可用性优先，绝不假加密），与 recovery.py 自动备份同语义。
    """
    backup_id = uuid.uuid4().hex
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"app_version": APP_VERSION, "backup_id": backup_id,
               "created_at": time.time(),
               "settings": _redact_remote_key(_load_persisted_settings())}
    path = BACKUP_DIR / f"backup_{_now_ts()}.json"
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        from ..data.crypto import encrypt_bytes_gcm, is_encrypted_bytes
        blob = encrypt_bytes_gcm(raw)
        if is_encrypted_bytes(blob):
            path = path.with_suffix(path.suffix + ".enc")
            path.write_bytes(blob)
        else:
            path.write_bytes(raw)
    except Exception:  # noqa: BLE001 - 加密失败保留明文（可用性优先）
        log.warning("_perform_backup 设置快照加密失败，保留明文", exc_info=True)
        path.write_bytes(raw)
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
        try:
            from ..data.crypto import encrypt_bytes_gcm, is_encrypted_bytes
            blob = encrypt_bytes_gcm(dest.read_bytes())
            if is_encrypted_bytes(blob):
                enc = dest.with_suffix(dest.suffix + ".enc")
                enc.write_bytes(blob)
                dest.unlink(missing_ok=True)
                dest = enc
        except Exception:  # noqa: BLE001 - 加密失败保留明文（可用性优先）
            log.warning("_perform_backup 库副本加密失败，保留明文", exc_info=True)
        db_path = str(dest)
    # P1-10 批 1+2（2026-10-24）：辅助库+向量库纳入自动备份
    aux_paths: list[str] = []
    for src_path, name in (
            (ROOT_DIR / "logs" / "flow_trace.db", "flow_trace"),
            (DATA_DIR / "model_usage.db", "model_usage")):
        if not src_path.is_file():
            continue
        try:
            import sqlite3 as _sq
            aux = BACKUP_DIR / f"{name}_{_now_ts()}.db"
            s = _sq.connect(str(src_path))
            try:
                d = _sq.connect(str(aux))
                try:
                    s.backup(d)
                finally:
                    d.close()
            finally:
                s.close()
            try:
                from ..data.crypto import encrypt_bytes_gcm, is_encrypted_bytes
                blob = encrypt_bytes_gcm(aux.read_bytes())
                if is_encrypted_bytes(blob):
                    enc = aux.with_suffix(aux.suffix + ".enc")
                    enc.write_bytes(blob)
                    aux.unlink(missing_ok=True)
                    aux = enc
            except Exception:
                pass
            aux_paths.append(str(aux))
        except Exception as exc:  # noqa: BLE001
            log.warning("自动备份辅助库失败 %s: %s", name, exc)
    _prune_old_backups()
    return {"backup_id": backup_id, "path": str(path), "db_path": db_path,
            "aux_paths": aux_paths}


def _prune_old_backups() -> None:
    """滚动清理：DB 副本与设置 JSON 各保留最近 N 份（B10 数据保全）。"""
    try:
        for pattern, keep in (("omnispace_*.db", _BACKUP_KEEP_DB),
                              ("omnispace_*.db.enc", _BACKUP_KEEP_DB),
                              ("omnispace-*.db", _BACKUP_KEEP_DB),
                              ("omnispace-*.db.enc", _BACKUP_KEEP_DB),
                              ("backup_*.json", _BACKUP_KEEP_JSON),
                              ("backup_*.json.enc", _BACKUP_KEEP_JSON)):
            files = sorted(BACKUP_DIR.glob(pattern), key=lambda f: f.stat().st_mtime)
            for old in files[:-keep] if len(files) > keep else []:
                old.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("备份滚动清理失败（不影响备份本身）: %s", exc, exc_info=True)


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
            log.warning("自动备份执行失败: %s", exc, exc_info=True)
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
                log.debug("_write_full_export: 降级忽略", exc_info=True)
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
        log.error("全量导出失败: %s", exc, exc_info=True)
        raise ApiError("SYSTEM_BACKUP_FAILED", "全量导出失败，请检查磁盘空间",
                       detail={"error": str(exc)}) from exc
    return ok({"path": str(dest), "size_bytes": dest.stat().st_size,
               "sha256": sha, "format": "tar.gz",
               "includes": ["omnispace.db(热备)", "settings_snapshot.json",
                            "generated/"]})


# ── SET-023 API Key 管理（bcrypt 哈希 + 脱敏显示）────────────────────
_API_KEYS_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        module TEXT NOT NULL,
        action TEXT NOT NULL,
        target TEXT DEFAULT '',
        result TEXT DEFAULT 'ok',
        detail TEXT DEFAULT ''
);
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


def _ensure_audit_table() -> None:
    try:
        db = get_db_safe()
        if db is not None:
            db.sql("CREATE TABLE IF NOT EXISTS audit_log ("
                   "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                   "ts TEXT NOT NULL, module TEXT NOT NULL,"
                   "action TEXT NOT NULL, target TEXT DEFAULT '',"
                   "result TEXT DEFAULT 'ok', detail TEXT DEFAULT '')")
    except Exception:  # noqa: BLE001 - 建表失败由查询侧兜底
        log.debug("audit table ensure 降级", exc_info=True)


@router.get("/system/files/gallery")
def files_gallery(module: str = "all",
                  limit: int = 48,
                  offset: int = 0) -> dict[str, Any]:
    """批6 P34（2026-09-19）：生成物文件库——四产物目录聚合分页。

    只读（删除走各模块正规链）；缩略图经 /manga/media 白名单回读。
    """
    roots: list[tuple[str, str, Path]] = []
    gen = DATA_DIR / "generated"
    if module in ("all", "images"):
        roots.append(("images", "生成图片", gen / "images"))
    if module in ("all", "keyframes"):
        roots.append(("keyframes", "分镜关键帧", DATA_DIR / "keyframes"))
    if module in ("all", "videos"):
        roots.append(("videos", "生成视频", gen / "videos"))
    if module in ("all", "exports"):
        roots.append(("exports", "导出成品", gen / "exports"))
    items: list[dict[str, Any]] = []
    for key, label, root in roots:
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            try:
                if not f.is_file() or f.name.startswith("."):
                    continue
                st = f.stat()
                rel = str(f.relative_to(DATA_DIR)).replace("\\", "/")
                ext = f.suffix.lower()
                items.append({
                    "module": key, "module_label": label,
                    "name": f.name, "path": rel,
                    "url": f"/api/v1/manga/media/{rel}",
                    "is_video": ext in (".mp4", ".webm", ".gif"),
                    "size_bytes": st.st_size, "mtime": st.st_mtime,
                })
            except OSError:
                continue
    items.sort(key=lambda x: x["mtime"], reverse=True)
    page = items[offset:offset + limit]
    return ok({"items": page, "total": len(items),
               "offset": offset, "limit": limit})


@router.get("/system/tasks/center")
def system_tasks_center() -> dict[str, Any]:
    """批6 P24（2026-09-19）：全局任务中心——四队列只读聚合。

    每路 fail-soft：单源故障如实标注 errors[]，不伪装全空。
    """
    from ..middleware.error_handler import ok as _ok  # noqa: F401
    cards: list[dict[str, Any]] = []
    errors: list[str] = []
    db = get_db_safe()

    def _card(module: str, name: str, status: str, progress: float,
              detail: str = "", task_id: str = "", queue_pos: int = 0) -> None:
        cards.append({"module": module, "name": name, "status": status,
                      "progress": round(progress, 3), "detail": detail,
                      "task_id": task_id, "queue_position": queue_pos})

    # ① 视频任务（近 20 条非终态优先）
    try:
        if db is not None:
            for r in db.query(
                    "SELECT id, storyboard_row_id, status, progress,"
                    " model_used, created_at FROM video_tasks"
                    " ORDER BY (status IN ('pending','generating')) DESC,"
                    " created_at DESC LIMIT 20"):
                _card("video", f"镜 {r.get('storyboard_row_id', '')[:8]}",
                      str(r.get("status") or "pending"),
                      float(r.get("progress") or 0),
                      detail=str(r.get("model_used") or ""),
                      task_id=str(r["id"]))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"video: {str(exc)[:80]}")

    # ② 训练任务（learn+style 两源）
    try:
        if db is not None:
            # F-1（2026-10-19）：error 列历史 0/22 填充（写路径=训练中心
            # G-01 缺口，归训练线）；此处先接通读侧，写侧落地后卡片即显因。
            for r in db.query(
                    "SELECT id, dataset_path, status, progress, error"
                    " FROM train_tasks ORDER BY created_at DESC LIMIT 10"):
                _detail = str(r.get("error") or "").strip()
                _card("training", str(r.get("dataset_path") or "知识训练")[:30],
                      str(r.get("status") or ""), float(r.get("progress") or 0),
                      detail=_detail[:60], task_id=str(r["id"]))
            # style_tasks 为训练服务懒建散表（迁移体系外）——新库未触
            # 风格训练时表不存在，按空列表处理而非 fail-soft 错误
            #（round-2026-09-20：批6 恒真断言掏空掩盖的「新库任务中心
            # 必带 training: no such table」真问题，F11 连带根修）。
            _style_tbl = db.query_one(
                "SELECT name FROM sqlite_master"
                " WHERE type='table' AND name='style_tasks'")
            if _style_tbl is not None:
                for r in db.query(
                        "SELECT id, status, progress FROM style_tasks"
                        " ORDER BY created_at DESC LIMIT 10"):
                    _card("training", str(r.get("name") or "风格训练"),
                          str(r.get("status") or ""),
                          float(r.get("progress") or 0),
                          task_id=str(r["id"]))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"training: {str(exc)[:80]}")

    # ③ 对话/绘画引擎态（预热中=进行中卡片）
    try:
        from ..services.inference.dialog_engine import get_dialog_engine
        st = get_dialog_engine().get_status()
        if st.get("state") == "loading":
            _card("dialog", "对话模型加载中", "loading", 0.5)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"dialog: {str(exc)[:80]}")
    try:
        from ..config import get_config
        if str((get_config().get("paint") or {}).get(
                "gen_engine", "legacy")).strip().lower() == "comfy":
            from ..services.inference.comfy_paint_engine import get_comfy_paint_engine
            eng = get_comfy_paint_engine()
            if getattr(eng, "is_running", lambda: False)():
                _card("paint", "ComfyUI 出图引擎", "running", 1.0)
        else:
            from ..services.inference.paint_engine import get_paint_engine
            st = get_paint_engine().get_status()
            if st.get("loaded"):
                _card("paint", f"绘画就绪 {str(st.get('model', ''))[:20]}",
                      "ready", 1.0)
            elif st.get("loading"):
                _card("paint", "绘画模型加载中", "loading", 0.5)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"paint: {str(exc)[:80]}")

    active = sum(1 for c in cards
                 if c["status"] in ("pending", "generating", "running",
                                    "loading", "training"))
    return ok({"cards": cards, "active": active, "errors": errors})


@router.get("/system/audit/list")
def audit_list(limit: int = Query(200, ge=1, le=1000),
               module: str = Query("", description="按模块过滤（空=全部）")) -> dict[str, Any]:
    """批5 P18：敏感操作审计（近 N 条；删除/导出/设置/云端/激活留痕）。"""
    from ..services import audit_log as _audit
    _ensure_audit_table()
    return ok({"items": _audit.list_audit(limit=limit, module=module.strip())})


@router.post("/system/audit/clear")
def audit_clear() -> dict[str, Any]:
    """批5 P18：清空审计（确认动作在前端）。"""
    from ..services import audit_log as _audit
    n = _audit.clear_audit()
    _audit.log_audit("system", "audit_clear", target="audit_log",
                     detail=f"cleared {n}")
    return ok({"cleared": n})


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
        raise ApiError("SYSTEM_APIKEY_NOT_FOUND", "API Key 不存在",
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
        raise ApiError("FILE_NOT_FOUND", "日志文件不存在")
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
        log.error("自重启失败: %s", exc, exc_info=True)


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
        log.debug("_dir_size: 降级忽略", exc_info=True)
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
        log.debug("system_disk: 降级忽略", exc_info=True)
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
        raise ApiError("SYSTEM_DB_UNAVAILABLE", "数据库不可用")

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
