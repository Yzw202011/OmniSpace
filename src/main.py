"""OmniSpace AI v2.5.0 后端主入口（文档 §7.1 后端架构 / §6.4 启动时序）。

启动时序：端口绑定 → 数据库初始化 → 文件存储/缓存 → 调度引擎 → WebSocket → 就绪。
文档引用：§7.1 路由层 BASE_URL=/api/v1（ADR-03 由 /v1 迁移）, §8.4 模块间协调, §14 约束2: 绑定127.0.0.1:5800
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import time

# V8 回归修复（2026-09-09 14:30）：tqdm 进度条写 stderr 时，后端以
# Hidden Window + 重定向 stderr 启动长时间后句柄会失效（OSError
# [Errno 22] Invalid argument，实锤栈 tqdm/std.py:446 flush）。
# 禁用 tqdm/hf 进度条 = 服务端进程本就不需要可视化进度。
os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match, Mount
from starlette.types import Scope

from . import config
from .data.database import get_db
from .middleware.cors import setup_cors
from .middleware.error_handler import ApiError, error, ok
from .middleware.logger import setup_logging
from .middleware.rate_limit import setup_rate_limit

# ── 日志初始化 ──────────────────────────────────────────────────
log = setup_logging()
_boot_ts = time.time()

# ── v2.1 调度引擎可用性探测 ──────────────────────────────────────
_SCHEDULER_AVAILABLE = False
try:
    from .services.scheduler import get_scheduler as _get_scheduler
    _SCHEDULER_AVAILABLE = True
except Exception:
    _get_scheduler = None  # type: ignore


# ═══════════════════════════════════════════════════════════════════
#  Lifespan
# ═══════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("=" * 60)
    log.info("OmniSpace AI v2.5.0 后端启动 (%s:%s)", config.HOST, config.PORT)
    log.info("版本: %s", config.APP_VERSION)
    log.info("=" * 60)
    # 全机单实例限制（2026-08-31）：双栈叠载会塞爆显存（当日实测关键帧采样
    # 20+ 分钟直至超时；08-29 蓝屏诱因同源），置于一切重资源初始化之前秒失败
    from .single_instance import acquire as _acquire_single_instance
    if not _acquire_single_instance():
        log.error("!" * 60)
        log.error("本机已有 OmniSpace 后端实例在运行，按「一台电脑只允许一个实例」拒绝启动。")
        log.error("访问现有实例请用浏览器/启动页地址；重启请先运行根目录 停止OmniSpace.bat。")
        log.error("调试确需双开：设置环境变量 OMNISPACE_ALLOW_MULTI=1（显存挤爆自担）。")
        log.error("!" * 60)
        raise RuntimeError("OmniSpace 单实例限制：本机已有后端实例在运行")
    # 激活门禁（P5）：启动即预热机器指纹——避免首个业务请求在中间件里
    # 同步跑 PowerShell 采集（阻塞事件循环）；门禁未启用时零开销
    from . import license_gate as _license_gate
    if _license_gate.gate_enabled():
        try:
            _license_gate.collect_fingerprints(force=True)
            _license_gate.is_activated()
            log.info("激活门禁已启用：%s", _license_gate.status().get("activated")
                     and "已激活" or "未激活")
        except Exception as _exc:  # noqa: BLE001 - 门禁初始化失败不阻断启动
            log.warning("激活门禁初始化异常（不阻断启动）：%s", _exc)
    # 风格库加密种子（P6 锁4）：首启动空表自动导入（金库未启用则跳过）
    try:
        from .services.style_seed import ensure_seed as _ensure_seed
        log.info("风格库种子：%s", _ensure_seed())
    except Exception as _exc:  # noqa: BLE001 - 种子导入失败不阻断启动
        log.warning("风格库种子导入异常（不阻断启动）：%s", _exc)
    # 外部模型包登记（体验流 2b 跨盘降级）：boot 拖入识别写的标记 →
    # 按外部 manifest 幂等回填 file_path（激活门禁拦着 API，只能启动期做）
    try:
        from .services.model_manager.external_bootstrap import (
            bootstrap_external_models as _bootstrap_external,
        )
        _r = _bootstrap_external()
        if _r:
            log.info("外部模型包登记：%s", _r)
    except Exception as _exc:  # noqa: BLE001 - 登记失败不阻断启动
        log.warning("外部模型包登记异常（不阻断启动）：%s", _exc)

    # B6 步3（2026-09-14）：模型账实对账闸上线——validate_against_disk
    # 此前只在无人跑的 startup_check 里（线上 0 次执行，R7 审计），现挂
    # lifespan：ghost（清单有盘无）/ orphan（盘有清单无）/ required_missing
    # 三计数写大白话事件日志。只读不阻断；同时是一键体检的数据源。
    try:
        from .data.model_registry import validate_against_disk
        from .services.event_log import log_event as _le
        _rep = validate_against_disk()
        _ghost = len(_rep["ghost_entries"])
        _orphan = len(_rep["orphan_dirs"])
        _missing = len(_rep["required_missing"])
        if _ghost or _orphan or _missing:
            _le("models", "registry_drift",
                f"模型账本对不上：清单里 { _ghost } 个模型盘上找不到，"
                f"盘上 { _orphan } 个目录没登记，{ _missing } 个必备模型缺失。"
                "其余功能不受影响，可在模型管理页核对。",
                level="warning",
                detail=json.dumps({"ghost": _rep["ghost_entries"],
                                   "orphan": _rep["orphan_dirs"],
                                   "required_missing":
                                       _rep["required_missing"]},
                                  ensure_ascii=False)[:500])
        else:
            _le("models", "registry_check",
                "模型账本与磁盘对账一致", level="info")
        log.info("模型账实对账：ghost=%d orphan=%d required_missing=%d",
                 _ghost, _orphan, _missing)
    except Exception as _exc:  # noqa: BLE001 - 对账失败不阻断启动
        log.warning("模型账实对账异常（不阻断启动）：%s", _exc)
    # 审计 R3-BE3：非回环绑定醒目告警（API 无认证体系，规格 §14 约束2 要求 127.0.0.1）
    if config.HOST not in ("127.0.0.1", "localhost"):
        log.warning("!" * 60)
        log.warning("安全告警：当前绑定非回环地址 %s，API 无认证体系，"
                    "存在局域网暴露风险（规格 §14 约束2 要求 127.0.0.1）",
                    config.HOST)
        log.warning("!" * 60)
        # B8（2026-09-14）：LAN 豁免升格为用户时间线大白话告警
        try:
            from .services.event_log import log_event as _le_lan
            _le_lan("system", "lan_exposure",
                    "局域网模式已开启：本机所有数据（对话/项目/文件）对同一"
                    "WiFi 下设备可见，且接口无密码。仅在你完全信任当前网络时"
                    "继续使用；关闭方法：不设 OMNISPACE_ALLOW_LAN 环境变量重启。",
                    level="warning")
        except Exception:  # noqa: BLE001
            pass

    # T+0s: 数据库
    try:
        db = get_db()
        db.query_one("SELECT 1 AS one")
        log.info("T+0s 数据库初始化完成: %s", config.DB_PATH)
    except Exception as exc:
        log.error("数据库初始化失败: %s", exc)
        raise

    # T+0.5（B10 数据保全 2026-09-14）：恢复待办处理——/system/restore
    # 安排的 pending 副本经 **sqlite backup API 在线恢复**到主库。
    #
    # 实弹演练两轮修正：文件层覆盖（copy2/原子 replace）在 Windows 上
    # 会被 boot/杀软等共享读锁卡死（WinError 32 ×2 实弹复现）——改为
    # sqlite backup API（锁由 sqlite 管理，对已初始化主库在线恢复）。
    # 防呆：恢复前当前库先 copy2 一份到 backups/（读锁无碍 copy）。
    try:
        _restore_pending = config.DATA_DIR / "omnispace.restore-pending.db"
        _restore_mark = config.DATA_DIR / "omnispace.restore-pending.json"
        if _restore_pending.is_file() and _restore_mark.is_file():
            import shutil as _shutil
            import sqlite3 as _sq
            _info = json.loads(_restore_mark.read_text(encoding="utf-8"))
            if config.DB_PATH.is_file():
                _safe_cur = (config.DATA_DIR / "backups" /
                             f"omnispace_pre_restore_{int(time.time())}.db")
                config.DATA_DIR.joinpath("backups").mkdir(parents=True,
                                                          exist_ok=True)
                _shutil.copy2(config.DB_PATH, _safe_cur)
            _src = _sq.connect(str(_restore_pending))
            try:
                _dst = _sq.connect(str(config.DB_PATH))
                try:
                    _src.backup(_dst)  # pending → 主库（在线恢复）
                finally:
                    _dst.close()
            finally:
                _src.close()
            _restore_pending.unlink(missing_ok=True)
            _restore_mark.unlink(missing_ok=True)
            log.warning("备份恢复已生效（来源 %s）；替换前的当前库已备份",
                        _info.get("source"))
            from .services.event_log import log_event as _le_restore
            _le_restore("system", "restore_applied",
                        "备份恢复已完成（本次启动时已替换主库），"
                        "替换前的旧库已备份", level="warning")
    except Exception as _exc:  # noqa: BLE001 - 恢复失败保留标记下轮重试
        log.error("备份恢复执行失败（标记保留）：%s", _exc)

    # T+3s: 文件存储与缓存
    try:
        from .data.cache import get_cache
        from .data.file_store import get_file_store
        get_file_store()
        get_cache()
        log.info("T+3s 文件存储与缓存初始化完成")
    except Exception as exc:
        log.warning("文件存储/缓存初始化异常: %s", exc)

    # T+4s: 预加载嵌入模型（显存布局关键优化，勿后移）
    # bge-large-zh 是长驻 CUDA 的小模型（~1.3GB）。若等到首次 RAG 才加载，
    # 其权重会落在对话/绘画大模型之后分配的高位显存段；大模型卸载时这些
    # 被小权重"钉住"的段无法归还驱动（PyTorch 分配器 inactive_split），
    # 实测 16GB 卡上卸载对话模型后空闲仅 5.1GB（应为 13.4GB），导致后续
    # 所有大模型加载全部报"显存不足"。启动时最先加载嵌入模型，让其权重
    # 独占低位段，大模型段即可整体释放（实测卸载后空闲回到 13.4GB）。
    # 附带收益：首次 RAG 检索无模型加载冷延迟（曾测得 2825ms 超标）。
    try:
        from .data.vector_db import get_vector_db
        get_vector_db().warmup()  # 触发嵌入模型加载+前向推理（CUDA 长驻低位显存段）
        log.info("T+4s 嵌入模型预加载完成（CUDA 长驻）")
    except Exception as exc:
        log.warning("嵌入模型预加载失败（降级运行）: %s", exc)

    # T+5s: 调度引擎（硬件监控 / 模型调度，可选）
    if _SCHEDULER_AVAILABLE:
        try:
            scheduler = _get_scheduler()
            await scheduler.start()
            log.info("T+5s 调度引擎已启动")
        except Exception as exc:
            log.warning("调度引擎启动失败（降级运行）: %s", exc)
    else:
        log.warning("调度引擎模块不可用，跳过启动（降级运行）")

    # T+5.5s: 浏览器进程池预热（TASK-054：学习会话 acquire <1s）
    # 后台守护线程执行 Chromium 启动（~2-15s），不阻塞就绪时序；
    # playwright 不可用时自动跳过（懒初始化降级，行为与 v2.3 一致）。
    try:
        from .services.browser_pool import get_browser_pool
        get_browser_pool().warmup(headless=True, background=True)
        log.info("T+5.5s 浏览器进程池预热已调度（后台）")
    except Exception as exc:
        log.warning("浏览器进程池预热调度失败（降级懒初始化）: %s", exc)

    # T+5.8s: 资源占用采样与巡检仪表（P3-⑤）：后台守护线程 30s 采集
    # RAM/显存/磁盘 快照（近 2 小时趋势 + 每日 JSONL），巡检越界告警。
    # 采样器幂等自愈：即使此处失败，访问 /hardware/resource-samples 时
    # 也会经单例自启，不阻塞就绪时序。
    try:
        from .services.resource_sampler import get_resource_sampler
        get_resource_sampler().start()
        log.info("T+5.8s 资源占用采样器已启动（巡检仪表化）")
    except Exception as exc:
        log.warning("资源占用采样器启动失败（降级：访问时自启）: %s", exc)

    # T+6s: WebSocket 消息中枢（规格 §2.2 /ws 协议）
    # 绑定事件循环、启动遥测推送，并向绘画/LoRA训练/浏览器Agent 注入广播器
    try:
        from .api import draw as _draw_api
        from .services import browser_agent_service as _agent_svc
        from .services import lora_training_service as _lora_svc
        from .services.ws_hub import get_ws_hub
        hub = get_ws_hub()
        hub.bind_loop(asyncio.get_running_loop())
        hub.start_telemetry()
        # 页面守卫（2026-09-03 方案A）：全部页面关闭且无任务 → 正规链退出
        from .services.page_guard import get_page_guard
        await get_page_guard().start()
        _draw_api.set_ws_broadcaster(hub.broadcast)
        _lora_svc.set_ws_broadcaster(hub.broadcast)
        _agent_svc.set_ws_broadcaster(hub.broadcast)
        # 漫剧生图进度广播（2026-08-27 按钮实时进度条）：关键帧/资产
        # 生成循环与采样步级回调 → task_progress 推前端按钮进度条
        from .api.manga import common as _manga_common
        _manga_common.set_ws_broadcaster(hub.broadcast)
        # 统一模型切换引擎（P0 2026-08-25）：广播器 + 事件循环
        # （功能锁跨线程释放经 run_coroutine_threadsafe 需要 loop）
        from .services.switch_engine import get_switch_engine
        get_switch_engine().bind(hub.broadcast, asyncio.get_running_loop())
        log.info("T+6s WebSocket 消息中枢已启动（/ws），广播器已注入 paint/learn/agent/switch")
    except Exception as exc:
        log.warning("WebSocket 消息中枢启动失败（降级运行）: %s", exc)

    # B1（2026-09-13）：就绪日志报实测耗时——旧固定文案「T+10s」与真实
    # 值不符（boot.log 实测 T+16s、冷机口径 25s~2min），固定文案误导排障
    log.info("后端就绪 T+%.0fs，等待请求", time.time() - _boot_ts)
    log.info("=" * 60)
    # ── 统一事件日志（2026-08-21 日志可视化）─────────────────
    try:
        from .services.event_log import log_event, start_cleanup_task
        start_cleanup_task()  # 30 天自动清除（启动即清一次 + 每日巡检）
        log_event(
            "system", "backend_started",
            f"OmniSpace 后端已启动就绪（版本 {config.APP_VERSION}），"
            f"监听地址 {config.HOST}:{config.PORT}",
            level="success",
            detail=f"db={config.DB_PATH}, api_prefix={config.API_PREFIX}")
    except Exception:  # noqa: BLE001 - 事件系统故障不阻断启动
        log.exception("事件日志初始化失败")
    # ── 升级包启动扫描（升级机制批2）：有可升级包则 WS 广播 ──
    try:
        from .services.upgrade_service import scan_updates
        compatible = [p for p in scan_updates() if p.get("compatible")]
        if compatible:
            from .services.ws_hub import get_ws_hub
            get_ws_hub().broadcast({
                "type": "update_available",
                "data": {"count": len(compatible),
                         "to_version": compatible[0].get("manifest", {})
                         .get("to_version", "")},
            })
    except Exception:  # noqa: BLE001 - 升级扫描失败不阻断启动
        log.warning("升级包启动扫描失败（忽略）")
    # ── 执行流程追踪（2026-08-23 流程记录机制优化）─────────
    try:
        from .services import flow_trace
        flow_trace.recover_orphans()   # 上一进程遗留 running → orphan
        flow_trace.start_cleanup_task()
    except Exception:  # noqa: BLE001 - 追踪失败不阻断启动
        log.warning("流程追踪初始化异常（降级运行）")
    # ── 自动备份调度（SET-019 补线，2026-09-15 审计修复）─────
    # 病灶：_ensure_backup_scheduler 此前只在 GET/PUT /system/backup/config
    # 端点内被调用，主启动链未注册——后端重启守护线程即丢，无人打开
    # 设置页则调度永不启动（审计实锤：system.last_auto_backup 键从未
    # 存在=一次都没跑过）。现接进启动链，与事件清理/流程追踪同模式。
    try:
        from .api.system import _ensure_backup_scheduler
        _ensure_backup_scheduler()
    except Exception:  # noqa: BLE001 - 备份调度失败不阻断启动
        log.warning("自动备份调度启动失败（忽略）")
    # ── 知识库体检周报（知识学习升级批4）：启动即查 + 每 7 天巡检 ──
    try:
        from .services.knowledge_checkup import start_checkup_task
        start_checkup_task()
    except Exception:  # noqa: BLE001 - 体检失败不阻断启动
        log.warning("知识库体检任务启动异常（降级运行）")
    # ── 崩溃取证心跳（2026-09-01 日志机制方案 C）：上次异常退出检测 ──
    try:
        from .services import heartbeat
        prev = heartbeat.check_previous_crash()
        if prev:
            from .services.event_log import log_event
            log_event(
                "system", "system_crash_detected",
                f"检测到上次后端为异常退出（进程 {prev['pid']}，最后心跳 "
                f"{prev['stale_seconds']:.0f} 秒前）——崩溃/被强杀/断电均"
                "属此类；排障请优先查看该时段日志（导出诊断包会标记）",
                level="error",
                detail=f"last_seen_epoch={prev['last_seen']}")
        heartbeat.start()
    except Exception:  # noqa: BLE001 - 取证失败不阻断启动
        log.warning("崩溃取证心跳初始化异常（忽略）")
    # ── 视频任务遗留恢复（审计 P1 修复，2026-08-29）：后台 worker
    # 随进程消失，遗留 generating 行永远无人收尾，/status 会无限
    # 回传旧进度——启动即改写为 error（与 flow_trace 孤儿恢复同时机）
    try:
        n = get_db().update("video_tasks", {"status": "error"},
                            "status=?", ("generating",))
        if n:
            log.warning("视频任务遗留恢复: %d 条 generating → error", n)
    except Exception:  # noqa: BLE001 - 恢复失败不阻断启动
        log.warning("视频任务遗留恢复失败（忽略）")
    # 小说章节遗留恢复（批2 MVP 2026-09-05）：生成 worker 随进程消失，
    # 遗留 generating 章节无人收尾 → 启动即改写 error（用户可重新生成）
    try:
        n = get_db().update(
            "novel_chapters",
            {"status": "error", "progress": 0.0,
             "error": "后端重启中断，请重新生成"},
            "status=?", ("generating",))
        if n:
            log.warning("小说章节遗留恢复: %d 条 generating → error", n)
    except Exception:  # noqa: BLE001 - 恢复失败不阻断启动
        log.warning("小说章节遗留恢复失败（忽略）")
    yield

    # 关闭
    log.info("OmniSpace AI 后端关闭中...")
    try:
        from .services.event_log import log_event
        log_event("system", "backend_stopped", "OmniSpace 后端已正常关闭",
                  level="info")
    except Exception:  # noqa: BLE001
        pass
    # 正常退出才摘心跳（残留 = 下次启动判定为异常退出）
    try:
        from .services import heartbeat
        heartbeat.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        from .services.browser_pool import get_browser_pool
        get_browser_pool().shutdown()
    except Exception:
        pass
    try:
        from .services.ws_hub import get_ws_hub
        await get_ws_hub().stop_telemetry()
    except Exception:
        pass
    try:
        from .services.page_guard import get_page_guard
        await get_page_guard().stop()
    except Exception:
        pass
    if _SCHEDULER_AVAILABLE:
        try:
            scheduler = _get_scheduler()
            if hasattr(scheduler, "stop"):
                await scheduler.stop()
        except Exception:
            pass
    log.info("后端已停止")


# ═══════════════════════════════════════════════════════════════════
#  应用工厂
# ═══════════════════════════════════════════════════════════════════

# v2.1 API 路由模块（规格 §4 API接口完整定义）
_API_MODULES = [
    "dialog",    # §4.2 对话API
    "draw",      # §4.3 绘画API
    "manga",     # §4.4 漫剧API
    "learn",     # 知识学习API（训练任务/数据集）
    "learning",  # v2.3 学习API（主题/会话/调度配额）
    "knowledge", # v2.3 知识库+行为学习API
    "browser",   # v2.3 浏览器Agent API
    "models",    # §4.5 模型管理API
    "style",     # 文档 §7.1.4 视频风格API（/style/train, /preview, /versions）
    "vision_tools",  # 文档E 附录B /art：TripoSR/SAM/MiDaS/YOLOv8 随包模型接线（F-01~F-04）
    "voice",     # 语音API（/voice/transcribe Whisper ASR + /voice/synthesize TTS 自动装载链）
    "hardware",  # §4.6 硬件API
    "system",    # §4.7 系统API
    "logs",      # 系统日志API（2026-08-21 日志可视化：事件查询/统计/清理）
    "license",   # 激活门禁API（P5：状态展示/激活提交，未激活态白名单）
    "novel",     # 小说模块API（批2 MVP 2026-09-05：项目/大纲/章节/角色/伏笔/导出）
    "cloud",     # 云端API服务商管理（批1 2026-09-06：Provider/绑定/测试，用户自带Key）
    "upgrade",   # 应用内升级API（升级机制批2 2026-09-11：包导入/扫描/开始升级）
    "plugins",   # 插件系统API（OSP v1 P1 2026-09-16：清单/加载/卸载/invoke）
]


def _register_routers(app: FastAPI) -> None:
    """注册 v2.1 API 路由模块。"""
    registered = 0
    for name in _API_MODULES:
        try:
            mod = importlib.import_module(f".api.{name}", package=__package__)
            if hasattr(mod, "router"):
                app.include_router(mod.router, prefix=config.API_PREFIX)
                registered += 1
                log.info("已注册 v2.1 路由: api.%s", name)
        except ImportError as exc:
            log.warning("导入 api.%s 失败: %s", name, exc)
        except Exception as exc:
            log.warning("注册 api.%s 失败: %s", name, exc)
    log.info("路由注册完成: %d 个模块", registered)


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用实例。"""
    app = FastAPI(
        title="OmniSpace AI",
        version=config.APP_VERSION,
        description="OmniSpace AI v2.5.0 — 全模态创作工作站",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # 中间件
    setup_cors(app)        # §6.1 L4: 仅本地
    setup_rate_limit(app)  # config.yaml system.rate_limit（当前 300 req/min）

    # 激活门禁（P5）：发行包注入公钥后启用——未激活时业务 API 全 403，
    # 白名单=健康检查/激活接口/前端静态页。开发构建（无公钥）完全旁路。
    from . import license_gate

    @app.middleware("http")
    async def license_gate_middleware(request: Request,
                                      call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if license_gate.gate_enabled():
            path = request.url.path
            allowed = (path in ("/health", "/")
                       or path.startswith("/api/v1/license")
                       or not path.startswith("/api/"))
            if not allowed:
                activated, reason = license_gate.is_activated()
                if not activated:
                    return error(
                        "LICENSE_REQUIRED", "产品尚未激活",
                        reason or "尚未激活",
                        suggestion="请把收到的激活码粘贴到激活窗口完成激活")
        return await call_next(request)

    # 请求上下文（文档D meta.request_id/duration_ms 支撑）
    # 审计 R3-BE6：最后注册 = 最外层用户中间件（Starlette insert(0) 语义），
    # 使限流/CORS 拒绝的响应也带 X-Request-ID
    from .middleware.request_context import RequestContextMiddleware
    app.add_middleware(RequestContextMiddleware)
    # 自愈批2（2026-09-11）：API 事件日志自动兜底——变更类请求/失败信封/慢
    # 请求自动进用户时间线（docs/自愈与横切内建方案-2026-09-10.md §批2）。
    # 注册在 RequestContext 之后 = 其外层，响应流完成时读到最终信封再落档。
    from .middleware.event_log_auto import EventLogAutoMiddleware
    app.add_middleware(EventLogAutoMiddleware)
    # 审计 R3-SEC：Host 头校验（防 DNS 重绑定攻击）。最后注册 = 最外层
    # 用户中间件，非法 Host 在路由/其他中间件之前即被 400 拦截。
    # 已核实 Starlette 1.3.1 实现：仅取 Host 头 hostname 部分比对
    # （split(":")[0]），任意端口均放行，不影响 127.0.0.1:5800 正常访问。
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]"],
    )

    # 异常处理（§6.1 / §8）
    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
        log.warning("ApiError: code=%s msg=%s", exc.code, exc.message)
        return error(exc.code, exc.message, exc.detail, suggestion=exc.suggestion)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [{"loc": [str(p) for p in e.get("loc", [])], "msg": e.get("msg", "")}
                  for e in exc.errors()[:5]]
        detail_str = "; ".join(f"{'.'.join(f['loc'])} {f['msg']}" for f in fields)
        log.warning("参数校验失败: %s", detail_str)
        return error("SYSTEM_PARAM_INVALID", "参数校验失败：" + detail_str,
                     {"fields": fields})

    # 审计 P1-1：StarletteHTTPException 统一包装（404 路由不存在 / 405 方法
    # 不允许等框架级异常），避免泄漏 FastAPI 默认 {"detail": "Not Found"}
    # 结构——前端只需面对统一 {success,data,error,meta} 信封契约（审计 R1-07）。
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            log.info("路由不存在: %s", getattr(_, "url", "").path if _ else "")
            return error("SYSTEM_RESOURCE_NOT_FOUND", "资源不存在",
                         {"path": str(_.url.path) if _ else "",
                          "http_status": 404})
        if exc.status_code == 405:
            return error("SYSTEM_PARAM_INVALID", "请求方法不允许",
                         {"path": str(_.url.path) if _ else "",
                          "method": getattr(_, "method", ""),
                          "http_status": 405})
        log.warning("HTTP 异常: status=%s detail=%s",
                    exc.status_code, exc.detail)
        return error("SYSTEM_INTERNAL_ERROR",
                     f"HTTP 错误 {exc.status_code}: {exc.detail}",
                     {"http_status": exc.status_code})

    @app.exception_handler(Exception)
    async def unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        log.exception("未捕获异常: %s", exc)
        try:  # 大白话事件：系统异常（用户可见）
            from .services.event_log import log_event
            log_event(
                "system", "unexpected_error",
                f"系统遇到了一个意外错误（{type(exc).__name__}），"
                "相关功能可能暂时不可用，其他功能不受影响",
                level="error", detail=str(exc)[:300])
        except Exception:  # noqa: BLE001
            pass
        return error("SYSTEM_INTERNAL_ERROR", "内部错误，请查看服务端日志",
                     {"type": type(exc).__name__})

    # 路由注册
    _register_routers(app)

    # 健康检查
    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, Any]:
        db_status = "ok"
        try:
            get_db().query_one("SELECT 1 AS one")
        except Exception:
            db_status = "error"
        return ok({"status": "healthy", "version": config.APP_VERSION,
                    "build": config.BUILD_ID,
                    "uptime_s": round(time.time() - _boot_ts, 1), "db": db_status})

    # WebSocket: 对话流式（§4.2 ws://127.0.0.1:5800/api/v1/dialog/stream/{session_id}）
    # DEPRECATED（F-011）：新前端改用 POST /api/v1/chat/stream（SSE）；
    # 本 WS 端点仅为兼容现有前端 ws.ts 保留。
    # 真实引擎推理实现在 api.dialog.handle_dialog_stream（协议与前端 ws.ts 契约对齐）
    @app.websocket(f"{config.API_PREFIX}/dialog/stream/{{session_id}}")
    async def dialog_stream(websocket: WebSocket, session_id: str) -> None:
        from .middleware.cors import ws_origin_guard
        if not await ws_origin_guard(websocket):  # 审计 R3-SEC1：防 CSWSH
            return
        from .api.dialog import handle_dialog_stream
        await handle_dialog_stream(websocket, session_id)

    # WebSocket: 硬件实时监控（§4.6 ws://127.0.0.1:5800/api/v1/hardware/realtime）
    # 注意：hardware.py 中已通过 router 注册了 /hardware/realtime WebSocket 端点，
    # 经 prefix=/api/v1 挂载后即可访问，此处无需重复定义。

    # WebSocket: 通用消息中枢（§2.2 ws://127.0.0.1:5800/ws）
    # 前端 useWebSocket 默认连接此端点；支持 ping/pong 与 subscribe_system。
    @app.websocket("/ws")
    async def ws_hub_endpoint(websocket: WebSocket) -> None:
        from .middleware.cors import ws_origin_guard
        if not await ws_origin_guard(websocket):  # 审计 R3-SEC1：防 CSWSH
            return
        from .services.ws_hub import get_ws_hub
        await get_ws_hub().handle_connection(websocket)

    # 前端静态资源（§14约束2：离线本地前端）
    class NoCacheStaticFiles(StaticFiles):
        async def get_response(self, path: str, scope: Scope) -> Response:
            resp = await super().get_response(path, scope)
            resp.headers["Cache-Control"] = "no-cache"
            return resp

    # 优先使用构建后的 dist 目录
    dist_dir = config.FRONTEND_DIR / "dist"
    static_dir = dist_dir if dist_dir.exists() else config.FRONTEND_DIR
    # favicon 必须在根路径挂载之前注册，否则被 Mount("/") 吞掉
    # 注意：必须用无 body 的 Response——JSONResponse(content=None) 会渲染
    # 4 字节 b"null"，而 starlette 对 204 不下发 content-length，uvicorn
    # 对 204 期望 0 字节 body，收到即抛 "Response content longer than
    # Content-Length"（2026-08-22 修复，存量日志 288 次该错误均源于此）
    from fastapi import Response as _FResponse

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return _FResponse(status_code=204)
    if static_dir.exists():
        # Vite 构建产物引用 /assets/...（base=/），挂载在根路径使其可直接访问；
        # html=True 使 GET / 返回 index.html（前端使用 Hash 路由，无需 SPA fallback）。
        # API（/api/v1）、/health、WebSocket 路由均先于本挂载注册，优先匹配不受影响。
        # 注意：Mount("/") 会全量匹配——用 _ApiAwareMount 排除 API 命名空间，
        # 使 GET 打到 POST 端点能回退到路由部分匹配返回 405（而非静态 404），
        # 未知 /api/v1 路径返回 JSON 404（而非 HTML）。
        class _ApiAwareMount(Mount):
            _API_PREFIXES = ("/api/v1", "/health", "/ws", "/favicon.ico")

            def matches(self, scope: Scope) -> tuple[Match, dict]:  # type: ignore[override]
                path = scope.get("path", "")
                for p in self._API_PREFIXES:
                    if path == p or path.startswith(p + "/"):
                        return Match.NONE, {}
                return super().matches(scope)

        static_app = NoCacheStaticFiles(directory=str(static_dir), html=True)
        app.router.routes.append(
            _ApiAwareMount("/", app=static_app, name="static"))

    return app


# ── 应用实例（uvicorn 直接引用）──────────────────────────────────
app = create_app()


def main() -> None:
    """启动 uvicorn 服务器。"""
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
# 本项目仅供学习使用，商业授权请+Q 3559331368
