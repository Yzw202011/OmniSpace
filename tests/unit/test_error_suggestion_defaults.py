"""批1 错误出路默认化单测（2026-09-11 自愈与横切内建方案）。

验收口径（docs/未完成清单.md 任务#1）：
  - 全语义码在 SUGGESTION_DEFAULTS 里都有非空默认出路（表完整性）
  - error() 调用方未传 suggestion 时自动补默认出路（信封恒带出路）
  - 调用方显式传入的 suggestion 不被覆盖
  - 未登记码/未知码兜底 SUGGESTION_FALLBACK
  - 历史数字码经兼容层映射后同样带出路
  - raise ApiError → 全局异常处理器路径同样带出路

不碰 GPU、不碰真实服务，error() 直测 + 最小 FastAPI app 验证处理器链。
"""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from src.middleware.error_handler import (
    SEMANTIC_CODES,
    SUGGESTION_DEFAULTS,
    SUGGESTION_FALLBACK,
    ApiError,
    error,
)


def _body(resp: JSONResponse) -> dict:
    return json.loads(bytes(resp.body))


# ── 表完整性：全语义码必配默认出路 ────────────────────────────────

def test_every_semantic_code_has_nonempty_suggestion() -> None:
    missing = [c for c in SEMANTIC_CODES
               if not isinstance(SUGGESTION_DEFAULTS.get(c), str)
               or not SUGGESTION_DEFAULTS[c].strip()]
    assert missing == [], f"缺默认出路的语义码: {missing}"


def test_suggestion_table_no_empty_strings() -> None:
    bad = [k for k, v in SUGGESTION_DEFAULTS.items() if not v.strip()]
    assert bad == []


# ── error() 自动补与覆盖语义 ─────────────────────────────────────

def test_error_without_suggestion_gets_registered_default() -> None:
    body = _body(error("MODEL_NOT_LOADED"))
    assert body["success"] is False
    err = body["error"]
    assert err["suggestion"] == SUGGESTION_DEFAULTS["MODEL_NOT_LOADED"]
    assert err["suggestion"].strip()
    # 出路 ≠ 现象：默认出路不能与 message 同串（否则等于没给出路）
    assert err["suggestion"] != err["message"]


def test_error_explicit_suggestion_wins() -> None:
    body = _body(error("MODEL_NOT_LOADED", suggestion="自定义出路文案"))
    assert body["error"]["suggestion"] == "自定义出路文案"


def test_error_explicit_empty_suggestion_falls_back_to_default() -> None:
    # 显式传空串 = 未给出路，走默认表（而非裸信封）
    body = _body(error("MODEL_NOT_LOADED", suggestion=""))
    assert body["error"]["suggestion"] == SUGGESTION_DEFAULTS["MODEL_NOT_LOADED"]


def test_unregistered_code_uses_fallback() -> None:
    body = _body(error("TOTALLY_UNKNOWN_CODE_XY"))
    assert body["error"]["message"] == "未知错误"
    assert body["error"]["suggestion"] == SUGGESTION_FALLBACK


def test_legacy_numeric_code_retired_conservative_fallback() -> None:
    """批3（2026-09-18）数字码清偿：int 输入=不应再出现的信号，保守兜底。

    旧契约（40004→SYSTEM_PARAM_INVALID 翻译）随 _LEGACY_CODE_MAP 退役；
    新契约：int 一律落 SYSTEM_INTERNAL_ERROR（行为与旧"未映射码"等价）。"""
    body = _body(error(40004))
    assert body["error"]["code"] == "SYSTEM_INTERNAL_ERROR"
    assert body["error"]["suggestion"] == SUGGESTION_DEFAULTS[
        "SYSTEM_INTERNAL_ERROR"]


def test_legacy_dead_mapping_70005_conservative_fallback() -> None:
    """70005 同上：int 兜底不产「未知错误」歧义（SYSTEM_INTERNAL_ERROR 恒带出路）。"""
    body = _body(error(70005))
    assert body["error"]["code"] == "SYSTEM_INTERNAL_ERROR"
    assert body["error"]["message"] != "未知错误"


# ── ApiError → 全局异常处理器链路 ────────────────────────────────

def test_api_error_handler_path_carries_suggestion() -> None:
    # 模拟 main.py 的 ApiError 处理器写法（main.py:397-400 同构），
    # 验证 raise ApiError（不带 suggestion）→ error() 自动补默认出路
    app = FastAPI()

    @app.exception_handler(ApiError)
    async def api_error_handler(_: object, exc: ApiError):  # pragma: no cover - 签名对齐
        from src.middleware.error_handler import error as err
        return err(exc.code, exc.message, exc.detail, suggestion=exc.suggestion)

    @app.get("/boom")
    async def boom() -> None:
        raise ApiError("COMIC_ART_STYLE_NOT_FOUND", detail={"style_id": "s1"})

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/boom")
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "COMIC_ART_STYLE_NOT_FOUND"
    assert body["error"]["suggestion"] == SUGGESTION_DEFAULTS["COMIC_ART_STYLE_NOT_FOUND"]


def test_validation_style_error_via_registered_code() -> None:
    # main.py:402-408 参数校验处理器走 error("SYSTEM_PARAM_INVALID")，
    # 未传 suggestion → 应自动补默认出路
    body = _body(error("SYSTEM_PARAM_INVALID", "参数校验失败：field required"))
    assert body["error"]["suggestion"] == SUGGESTION_DEFAULTS["SYSTEM_PARAM_INVALID"]
# 本项目仅供学习使用，商业授权请+Q 3559331368
