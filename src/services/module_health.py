"""功能舱壁（批2 P31，2026-09-19）：每模块独立健康账本。

大白话：四个重舱（对话/绘画/视频/训练）各有本账——只记「连续失败」，
连续 3 次才判 degraded（防单次抖动误报）；任一成功即清零恢复。
舱壁语义：一个舱坏了只报这个舱、只治这个舱，其余舱照常干活（各引擎
本就进程隔离，本账本把「坏了」这件事看见+说清+联动自愈）。

- record_failure 由错误信封中间件统一喂（语义码前缀映射模块）
- record_success 由各模块成功落点喂（对话发送/关键帧落库/视频完成/训练步进）
- 状态变化经 ws_hub 广播 module_health；degraded 触发 self_heal 自动恢复
"""
from __future__ import annotations

import logging
import threading
import time
from copy import deepcopy
from typing import Any

log = logging.getLogger("omnispace.module_health")

MODULES = ("dialog", "paint", "video", "training")
MODULE_LABELS = {"dialog": "AI对话", "paint": "绘画出图", "video": "视频生成",
                 "training": "训练"}
DEGRADE_THRESHOLD = 3

_lock = threading.Lock()
_state: dict[str, dict[str, Any]] = {
    m: {"consecutive_failures": 0, "state": "ok", "last_error": "",
        "last_error_at": 0.0, "degraded_since": 0.0}
    for m in MODULES}

# 语义码前缀 → 模块（顺序敏感：更具体的在前）
_CODE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("MANGA_VIDEO", "video"), ("VIDEO", "video"),
    ("LORA", "training"), ("TRAIN", "training"),
    ("COMFY", "paint"), ("PAINT", "paint"), ("DRAW", "paint"),
    ("DIALOG", "dialog"), ("VLLM", "dialog"),
    ("MODEL_", "dialog"),
)


def module_for_code(sem: str) -> str:
    """错误语义码 → 归属模块（空=不属于任何重舱，不计账）。"""
    u = (sem or "").upper()
    for prefix, mod in _CODE_PREFIXES:
        if u.startswith(prefix):
            return mod
    return ""


def _broadcast(module: str) -> None:
    """状态变化广播（锁外调用；无连接时静默，前端初拉走 REST 兜底）。"""
    try:
        from .ws_hub import WsHub
        WsHub.instance().broadcast({
            "type": "module_health",
            "data": {"module": module, **deepcopy(_state[module]),
                     "label": MODULE_LABELS.get(module, module)},
        })
    except Exception:  # noqa: BLE001 - 广播失败不影响账本
        log.debug("module_health 广播失败", exc_info=True)


def record_failure(sem: str, message: str = "") -> None:
    """记一次失败；达到阈值（连续 3 次）→ degraded + 触发自愈。"""
    mod = module_for_code(sem)
    if not mod:
        return
    newly_degraded = False
    with _lock:
        st = _state[mod]
        st["consecutive_failures"] += 1
        st["last_error"] = f"{sem}: {message}"[:300]
        st["last_error_at"] = time.time()
        if (st["consecutive_failures"] >= DEGRADE_THRESHOLD
                and st["state"] == "ok"):
            st["state"] = "degraded"
            st["degraded_since"] = time.time()
            newly_degraded = True
    if newly_degraded:
        log.warning("舱壁告警：%s 连续失败 %d 次 → degraded（%s）",
                    MODULE_LABELS.get(mod, mod), DEGRADE_THRESHOLD,
                    _state[mod]["last_error"])
        _broadcast(mod)
        try:  # 触发自动恢复（延迟导入防环）
            from . import self_heal
            self_heal.try_recover(mod, trigger=sem)
        except Exception:  # noqa: BLE001 - 自愈启动失败不影响主链
            log.warning("自愈启动失败: %s", mod, exc_info=True)


def record_success(module: str) -> None:
    """记一次成功：连续失败清零；degraded → ok 并广播恢复。"""
    if module not in MODULES:
        return
    recovered = False
    with _lock:
        st = _state[module]
        if st["consecutive_failures"] or st["state"] != "ok":
            st["consecutive_failures"] = 0
            if st["state"] != "ok":
                st["state"] = "ok"
                st["degraded_since"] = 0.0
                recovered = True
    if recovered:
        log.info("舱壁恢复：%s 恢复正常", MODULE_LABELS.get(module, module))
        _broadcast(module)


def snapshot() -> dict[str, Any]:
    """全模块健康快照（REST 兜底 + 状态栏初拉用）。"""
    with _lock:
        mods = {m: {**v, "label": MODULE_LABELS.get(m, m)}
                for m, v in _state.items()}
    return {"modules": mods,
            "degraded": [m for m, v in mods.items() if v["state"] != "ok"],
            "threshold": DEGRADE_THRESHOLD}


def reset_all() -> None:
    """（测试用）清空全部账本。"""
    with _lock:
        for st in _state.values():
            st.update(consecutive_failures=0, state="ok", last_error="",
                      last_error_at=0.0, degraded_since=0.0)
