"""统一自愈链（批2 P32，2026-09-19）：模块 degraded → 恢复阶梯，全程透明。

大白话：一个舱连续失败 3 次判 degraded 后，这里自动上阵救火——
第1步探测引擎、第2步重拉引擎（复用预热原语，不另造轮子）、第3步验证，
成功即广播恢复；救不回来就广播失败（kind=failed）→ 前端弹「恢复指南」
卡（P33），绝不静默放弃。

防自愈风暴三闸：同模块单飞（同时只有一个恢复流）/ 冷却 90s /
自动尝试上限 2 次（超限直接进指南，不再空转）。

能力边界（诚实）：paint/dialog 有引擎级重拉原语；video 的 H3 与
training 无进程级重启入口——这两舱自愈=快速探测后如实转人工指南。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

log = logging.getLogger("omnispace.self_heal")

COOLDOWN_S = 90.0
MAX_AUTO_ATTEMPTS = 2
VERIFY_TIMEOUT_S = 150.0
VERIFY_POLL_S = 3.0

_lock = threading.Lock()
_running: set[str] = set()
_last_attempt: dict[str, float] = {}
_attempts: dict[str, int] = {}


def _emit(module: str, kind: str, step: str, message: str) -> None:
    """自愈事件广播（前端：progress→toast 留痕 / failed→恢复指南卡）。"""
    try:
        from .ws_hub import WsHub
        WsHub.instance().broadcast({
            "type": "self_heal",
            "data": {"module": module, "kind": kind, "step": step,
                     "message": message, "ts": time.time()},
        })
    except Exception:  # noqa: BLE001 - 广播失败不阻断恢复
        log.debug("self_heal 广播失败", exc_info=True)


def _comfy_mode() -> bool:
    try:
        from ..config import get_config
        return str((get_config().get("paint") or {}).get(
            "gen_engine", "legacy")).strip().lower() == "comfy"
    except Exception:  # noqa: BLE001
        return False


def _recover_paint(step_log: list[str]) -> bool:
    """绘画舱恢复：Comfy 模式=ensure_running；legacy=ensure_loaded。"""
    if _comfy_mode():
        from .inference.comfy_paint_engine import comfy_paint_available, get_comfy_paint_engine
        if not comfy_paint_available():
            step_log.append("ComfyUI 出图栈不可用（便携版或权重缺失）")
            return False
        step_log.append("正在重新拉起 ComfyUI 出图进程…")
        get_comfy_paint_engine().ensure_running()
    else:
        from .inference.paint_engine import get_paint_engine
        step_log.append("正在重新装载本地绘画管线…")
        get_paint_engine().ensure_loaded(None)
    # 轮询验证（引擎装载异步摊开；超时=失败转指南）
    deadline = time.time() + VERIFY_TIMEOUT_S
    while time.time() < deadline:
        time.sleep(VERIFY_POLL_S)
        if _comfy_mode():
            status: dict[str, Any] = {}
        else:
            from .inference.paint_engine import get_paint_engine
            status = get_paint_engine().get_status()
        # Comfy 模式以「不抛错返回」为准（ensure_running 同步拉起成功）；
        # legacy 以 loaded 状态为准
        if (not _comfy_mode()) and status.get("loaded"):
            return True
        if _comfy_mode():
            return True
    return False


def _recover_dialog(step_log: list[str]) -> bool:
    """对话舱恢复：ensure_loaded 内部处理 vLLM 拉起/热切换（含云端短路）。"""
    try:
        from .inference.backends.remote_backend import is_remote_dialog_enabled
        if is_remote_dialog_enabled():
            step_log.append("对话已由云端承载，本地无需恢复")
            return True
    except Exception:  # noqa: BLE001 - 探测失败按本地处理
        pass
    from .inference.dialog_engine import get_dialog_engine
    step_log.append("正在重新装载对话模型（vLLM 冷启动约 2-3 分钟）…")
    get_dialog_engine().ensure_loaded(None)
    deadline = time.time() + VERIFY_TIMEOUT_S
    while time.time() < deadline:
        time.sleep(VERIFY_POLL_S)
        if get_dialog_engine().get_status().get("state") == "ready":
            return True
    return False


def _run_recovery(module: str, trigger: str) -> None:
    """恢复线程主体：阶梯执行 + 全程广播 + 成败落账。"""
    from . import module_health
    steps: list[str] = []
    try:
        if module == "paint":
            ok_ = _recover_paint(steps)
        elif module == "dialog":
            ok_ = _recover_dialog(steps)
        else:
            # video（H3 按需拉起）/training（无引擎进程）：如实转人工
            _emit(module, "progress", "probe",
                  f"{module_health.MODULE_LABELS.get(module)}模块无进程级"
                  "引擎可自动重拉，转入恢复指南")
            ok_ = False
            steps.append("该模块无自动重拉入口")
        if ok_:
            module_health.record_success(module)
            _attempts_reset(module)
            _emit(module, "success", "verify",
                  f"{module_health.MODULE_LABELS.get(module)}已自动恢复"
                  "（引擎重拉并验证通过）")
            log.info("自愈成功: module=%s trigger=%s", module, trigger)
        else:
            _emit(module, "failed", "giveup",
                  f"自动恢复未成功（{steps[-1] if steps else '验证超时'}），"
                  "请按恢复指南人工处理")
            log.warning("自愈失败: module=%s trigger=%s steps=%s",
                        module, trigger, steps)
    except Exception as exc:  # noqa: BLE001 - 恢复流自身异常=失败转指南
        _emit(module, "failed", "error",
              f"自动恢复异常：{str(exc)[:200]}，请按恢复指南人工处理")
        log.warning("自愈异常: module=%s: %s", module, exc, exc_info=True)
    finally:
        with _lock:
            _running.discard(module)
            _last_attempt[module] = time.time()


def _attempts_reset(module: str) -> None:
    with _lock:
        _attempts[module] = 0


def try_recover(module: str, trigger: str = "") -> bool:
    """发起自动恢复（单飞+冷却+上限三闸）；True=已起恢复流。

    供 module_health degrade 触发与前端恢复指南「再试一次」共用；
    超出自动上限时仍放行（人工点击=新一轮授权，计数清零重计）。
    """
    from . import module_health
    if module not in module_health.MODULES:
        return False
    now = time.time()
    with _lock:
        if module in _running:
            return False
        if now - _last_attempt.get(module, 0.0) < COOLDOWN_S:
            return False
        _attempts[module] = _attempts.get(module, 0) + 1
        _running.add(module)
    _emit(module, "progress", "start",
          f"检测到{module_health.MODULE_LABELS.get(module)}模块连续异常"
          f"（第 {_attempts.get(module, 1)} 次自动尝试），正在自动修复…")
    threading.Thread(target=_run_recovery, args=(module, trigger),
                     daemon=True, name=f"self-heal-{module}").start()
    return True


def reset_module_attempts(module: str) -> None:
    """人工触发=新一轮授权：清该舱自动尝试计数（恢复指南「再试一次」）。"""
    with _lock:
        _attempts[module] = 0


def auto_attempts_left(module: str) -> int:
    with _lock:
        return max(0, MAX_AUTO_ATTEMPTS - _attempts.get(module, 0))


def state_snapshot() -> dict[str, Any]:
    """（诊断/状态栏）自愈运行态。"""
    with _lock:
        return {"running": sorted(_running),
                "attempts": dict(_attempts),
                "last_attempt": dict(_last_attempt),
                "max_auto_attempts": MAX_AUTO_ATTEMPTS,
                "cooldown_s": COOLDOWN_S}


def reset_all() -> None:
    """（测试用）清空运行态。"""
    with _lock:
        _running.clear()
        _last_attempt.clear()
        _attempts.clear()
