"""SH-GAP-10 错误聚合分桶单测（2026-10-21 拍板「做」）。

参数校验类 api_failed（客户端 4xx 性质，learn=1678/style=786 噪声大户）
单独归入 api_param_invalid 桶——与真失败分榜，防按 count 排序时
噪声淹没真故障；数据不删只分榜。
"""
from __future__ import annotations

from src.api.logs import _error_group_key


def test_param_validation_failure_goes_to_own_bucket() -> None:
    assert _error_group_key({
        "module": "learn", "event": "api_failed",
        "friendly": "知识学习接口调用失败：参数校验失败：body.lora_rank …",
    }) == ("learn", "api_param_invalid")


def test_real_failure_stays_in_api_failed() -> None:
    assert _error_group_key({
        "module": "manga", "event": "api_failed",
        "friendly": "视频生成失败：ComfyUI 引擎异常退出",
    }) == ("manga", "api_failed")


def test_non_api_failed_untouched() -> None:
    assert _error_group_key({
        "module": "system", "event": "system_crash_detected",
        "friendly": "参数校验失败字样出现在别的事件里不改判定",
    }) == ("system", "system_crash_detected")
