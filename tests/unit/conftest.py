"""pytest 全局配置：保证项目根在 sys.path，提供通用 fixture。"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import logging
import os
import sys
import tempfile
from logging import handlers as _lh
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── B9（2026-09-13）测试日志隔离 ─────────────────────────────────────
# 病灶（两轮实锤）：测试进程内 setup_logging() 一旦被触发，Rotating
# FileHandler 就会把测试期日志写进生产 logs/backend.log（pydeps/后端
# 排障数据源被毒化）。本 patch 在任何 backend import 之前拦住 handler
# 构造：pytest 进程内、指向项目 logs/ 的文件日志一律重定向到系统临时
# 目录（OMNISPACE_PYTEST_LOGDIR 可覆盖）。仅本 conftest 生效（pytest
# 专属入口），生产运行零影响。

_PYTEST_LOGDIR = Path(
    os.environ.get("OMNISPACE_PYTEST_LOGDIR")
    or (Path(tempfile.gettempdir()) / "omnispace-pytest-logs"))


def _redirect(filename: str) -> str:
    try:
        p = Path(filename).resolve()
        if p.is_relative_to(ROOT / "logs"):
            _PYTEST_LOGDIR.mkdir(parents=True, exist_ok=True)
            return str(_PYTEST_LOGDIR / p.name)
    except Exception:  # noqa: BLE001 - 重定向失败按原样（宁直写不炸测试）
        pass
    return filename


def _patch_handler_init(cls: type, orig) -> None:  # type: ignore[no-untyped-def]
    """包装 __init__：filename 经 _redirect 后再走原构造。

    注记：曾试「重定向子类替换模块属性」，smoke 全量下 41 errors——
    logging 内部对 Handler 构造参数的兼容面比签名更宽（位置/关键字
    混用 + delay 语义），包装 __init__ 透传 *args/**kwargs 才是零
    假设的写法。回退此版（717 全绿实证）。"""
    def patched(self, filename, *args, **kwargs):  # type: ignore[no-untyped-def]
        orig(self, _redirect(filename), *args, **kwargs)
    cls.__init__ = patched  # type: ignore[misc]


_patch_handler_init(_lh.RotatingFileHandler, _lh.RotatingFileHandler.__init__)
_patch_handler_init(logging.FileHandler, logging.FileHandler.__init__)


# ── 模块级全局态隔离（2026-09-15 审计修复）───────────────────────────
# 对话排队位次表（api/dialog.py _DIALOG_LOCK_WAITERS）是模块级可变
# dict——测试失败路径泄漏的条目会污染同进程后续测试（全量跑「偶发
# 1 挂、单跑即过」的病根之一）。autouse 每用例前后各清一次。
@pytest.fixture(autouse=True)
def _isolate_module_globals():
    def _clear() -> None:
        try:
            from src.api import dialog as _dialog
            _dialog._DIALOG_LOCK_WAITERS.clear()
        except Exception:  # noqa: BLE001 - 模块未载/重构改名时跳过
            pass
        # 批5 专项根修（2026-09-19）：对话引擎单例经引擎提供的惰性
        # 复位口复位——绝不构造（有实例才清），根治当年「get_ 复位
        # 反致每测试造线程」的反例（历史教训见 dialog_engine.
        # reset_instance docstring）。看门狗在 pytest 进程内本就不
        # 启动（PYTEST_CURRENT_TEST 闸），本口兜底显式构造过的引擎，
        # 防其状态/线程跨测试泄漏。
        try:
            from src.services.inference import dialog_engine as _de
            _de.reset_instance()
        except Exception:  # noqa: BLE001 - 引擎未载/接口重构时跳过
            pass
    _clear()
    yield
    _clear()
