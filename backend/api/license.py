"""激活 API（P5）：状态展示与激活提交（未激活态下的白名单端点）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from .. import license_gate
from ..middleware.error_handler import ApiError, ok

router = APIRouter(tags=["license"])


class ActivateReq(BaseModel):
    code: str


@router.get("/license/status")
def license_status() -> dict[str, Any]:
    return ok(license_gate.status())


@router.post("/license/activate")
def license_activate(req: ActivateReq) -> dict[str, Any]:
    try:
        info = license_gate.activate(req.code)
    except license_gate.GateError as exc:
        raise ApiError("LICENSE_INVALID", str(exc),
                       suggestion="请联系卖家核对激活码，或提供新机器指纹换绑") from exc
    return ok({"activated": True, **info})
