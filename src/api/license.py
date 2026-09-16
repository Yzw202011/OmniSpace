"""激活 API（P5）：状态展示与激活提交（未激活态下的白名单端点）。

B8（2026-09-14）：激活失败锁定——连续 5 次失败锁 60 秒（对齐发码台
「5 锁 60s」语义），防枚举式试码；Ed25519 伪造本不可行，此为纵深防御。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import threading
import time
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from .. import license_gate
from ..middleware.error_handler import ApiError, ok

router = APIRouter(tags=["license"])

_FAIL_LIMIT = 5
_FAIL_LOCK_S = 60.0
_fail_lock = threading.Lock()
_fail_times: list[float] = []
_locked_until: float = 0.0


class ActivateReq(BaseModel):
    code: str


def _check_lock(now: float) -> None:
    global _locked_until
    if now < _locked_until:
        remain = int(_locked_until - now) + 1
        raise ApiError("LICENSE_LOCKED",
                       f"激活失败次数过多，请 {remain} 秒后重试",
                       suggestion="核对激活码后重试；多次失败请联系卖家换绑")


def _record_fail(now: float) -> None:
    global _locked_until
    with _fail_lock:
        _fail_times.append(now)
        recent = [t for t in _fail_times if now - t < 300.0]
        _fail_times.clear()
        _fail_times.extend(recent)
        if len(recent) >= _FAIL_LIMIT:
            _locked_until = now + _FAIL_LOCK_S
            _fail_times.clear()


@router.get("/license/status")
def license_status() -> dict[str, Any]:
    return ok(license_gate.status())


@router.post("/license/activate")
def license_activate(req: ActivateReq) -> dict[str, Any]:
    now = time.time()
    _check_lock(now)
    try:
        info = license_gate.activate(req.code)
    except license_gate.GateError as exc:
        _record_fail(now)
        _check_lock(time.time())  # 达到阈值立即进入锁定
        raise ApiError("LICENSE_INVALID", str(exc),
                       suggestion="请联系卖家核对激活码，或提供新机器指纹换绑") from exc
    with _fail_lock:
        _fail_times.clear()  # 成功即清失败史
    return ok({"activated": True, **info})
