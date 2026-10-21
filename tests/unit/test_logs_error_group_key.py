"""SH-GAP-10 错误聚合分桶单测（2026-10-21 拍板「做」；同日活体验证后
扩口径：字面量窄判据接不住「请求体无法解析/请求过于频繁/不支持的文件
类型」等防护拒绝——改语义码+人话标记双轨，桶名 api_client_error）。
"""
from __future__ import annotations

from src.api.logs import _error_group_key


def test_param_validation_failure_goes_to_own_bucket() -> None:
    assert _error_group_key({
        "module": "learn", "event": "api_failed",
        "friendly": "知识学习接口调用失败：参数校验失败：body.lora_rank …",
    }) == ("learn", "api_client_error")


def test_json_decode_and_rate_limit_markers() -> None:
    assert _error_group_key({
        "module": "learn", "event": "api_failed",
        "friendly": "知识学习接口调用失败：请求体无法解析（客户端发送的数据"
                    "不是合法 UTF-8 JSON）",
    }) == ("learn", "api_client_error")
    assert _error_group_key({
        "module": "hardware", "event": "api_failed",
        "friendly": "硬件接口调用失败：请求过于频繁，请稍后再试",
    }) == ("hardware", "api_client_error")


def test_client_code_via_detail() -> None:
    assert _error_group_key({
        "module": "system", "event": "api_failed",
        "friendly": "系统接口调用失败：未知日志文件: ../../../data/x.db",
        "detail": "GET /api/v1/logs/raw → HTTP 200 code=SYSTEM_PARAM_INVALID",
    }) == ("system", "api_client_error")


def test_real_failure_stays_in_api_failed() -> None:
    assert _error_group_key({
        "module": "manga", "event": "api_failed",
        "friendly": "视频生成失败：ComfyUI 引擎异常退出",
        "detail": "POST /api/v1/manga/video/generate → HTTP 200 "
                  "code=COMFY_GENERATION_FAILED",
    }) == ("manga", "api_failed")


def test_non_api_failed_untouched() -> None:
    assert _error_group_key({
        "module": "system", "event": "system_crash_detected",
        "friendly": "参数校验失败字样出现在别的事件里不改判定",
    }) == ("system", "system_crash_detected")

