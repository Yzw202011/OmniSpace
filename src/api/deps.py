"""模型自动加载依赖工厂（自愈批3，2026-09-11 自愈与横切内建方案 §批3）。

产品铁律 ux-model-load-auto 的架构化：「模型未加载让用户再点一次」从
制度上不可能——端点声明一行依赖即自带全链：

    engine=Depends(require_engine(get_xxx_engine, label="绘画模型"))
      = is_ready 检查（就绪零开销直通）
        → WS task_progress 进度广播（复用 ws_hub 现有通道，前端任务卡可见）
        → run_blocking(engine.ensure_loaded, hint)（数秒级阻塞绝不进事件循环，
          models.py:1169 同款纪律）
        → 成功/失败自动 log_event（批2 时间线自动可见）
        → 失败抛 MODEL_LOAD_FAILED 语义码（批1 默认出路自动随行）

⚠️ 实施守卫（2026-09-11 试点取证，方案书 §批3 已同步写回）：
  本依赖在 handler 之前执行。凡装载必须发生在「翻译/腾挪」之后的端点
  **禁止**改用本依赖，须保持链内既有 ensure_loaded 顺序：
    - 绘画生成链 draw.py:621（翻译兜底先腾挪显存→装载→shrink_working_set）
    - 漫剧关键帧链 keyframe.py:1270（ComfyUI 互斥→klein 链逐个试→降级翻译）
    - 漫剧资产/故事线描述词 manga/common.py:1401（翻译先于 ensure_loaded 铁律）
  历史事故：翻译进场驱逐绘画模型（2026-08-31）。
  适用对象 = 引擎身份静态、无翻译前驱的简单端点（首个用户：B阶段二新端点）。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..services.offload import run_blocking

log = logging.getLogger(__name__)

#: WS 进度广播的标签前缀（前端任务卡 task_id = engine-{module}-{kind}）
_BROADCAST_KIND = "engine"


def _broadcast_progress(module: str, label: str, *,
                        percent: int, status: str, note: str = "") -> None:
    """经 ws_hub 现有通道广播装载进度（任何失败静默——观测不阻断业务）。"""
    try:
        from ..services.ws_hub import get_ws_hub
        get_ws_hub().broadcast({
            "type": "task_progress",
            "data": {
                "task_id": f"{_BROADCAST_KIND}-{module}",
                "module": module,
                "kind": _BROADCAST_KIND,
                "id": module,
                "current": 0, "total": 1,
                "percent": max(0, min(100, int(percent))),
                "label": note or label, "status": status, "error": "",
            },
        })
    except Exception:  # noqa: BLE001 - 进度可见性失败不影响装载
        log.debug("_broadcast_progress: 降级忽略", exc_info=True)


def _log_load_event(module: str, label: str, *,
                    ok: bool, duration_ms: float, err: str) -> None:
    """装载结果自动进用户时间线（log_event 永不抛错，双保险再吞一次）。"""
    try:
        from ..services.event_log import log_event
        if ok:
            log_event(module, "engine_autoload",
                      f"{label}加载完成（用时 {duration_ms / 1000:.1f} 秒）",
                      level="success", duration_ms=duration_ms)
        else:
            log_event(module, "engine_autoload",
                      f"{label}自动加载失败：{err}",
                      level="error", detail=err[:300], duration_ms=duration_ms)
    except Exception:  # noqa: BLE001
        log.debug("_log_load_event: 降级忽略", exc_info=True)


def _last_error(engine: Any) -> str:
    """引擎最后错误的人话提取（各引擎 get_status 口径不一，逐层兜底）。"""
    try:
        status = engine.get_status()
        return str(status.get("last_error") or "")
    except Exception:  # noqa: BLE001
        return ""


def require_engine(
    engine_getter: Callable[[], Any],
    *,
    hint: str | None = None,
    label: str = "模型",
    module: str = "models",
) -> Callable[[], Awaitable[Any]]:
    """FastAPI 依赖工厂：端点一行声明即自带「未加载→自动加载→续跑」。

    Args:
        engine_getter: 引擎获取函数（模块级 getter，如 get_paint_engine）。
        hint: 透传 ensure_loaded 的模型提示（默认 None=引擎自选）。
        label: 人话名（进度卡/时间线用，如「绘画模型」）。
        module: 时间线模块标签（对齐 LogsPage MODULE_NAMES）。
    """
    async def _dep() -> Any:
        engine = engine_getter()
        if getattr(engine, "is_ready", False):
            return engine

        _broadcast_progress(module, label, percent=1,
                            status="running", note=f"{label}加载中，完成后自动继续")
        start = time.perf_counter()
        loaded = False
        err = ""
        try:
            loaded = bool(await run_blocking(engine.ensure_loaded, hint))
            if not loaded:
                err = _last_error(engine) or "引擎返回加载失败"
        except Exception as exc:  # noqa: BLE001 - 装载异常统一转语义码
            loaded = False
            err = str(exc) or type(exc).__name__
        duration_ms = (time.perf_counter() - start) * 1000

        if not loaded:
            _broadcast_progress(module, label, percent=0, status="error",
                                note=f"{label}加载失败：{err}")
            _log_load_event(module, label, ok=False,
                            duration_ms=duration_ms, err=err)
            from ..middleware.error_handler import ApiError
            raise ApiError("MODEL_LOAD_FAILED",
                           f"{label}自动加载失败：{err}",
                           detail={"engine": module, "hint": hint or ""})
        _broadcast_progress(module, label, percent=100, status="success")
        _log_load_event(module, label, ok=True, duration_ms=duration_ms, err="")
        return engine

    return _dep
