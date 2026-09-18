"""OmniSpace AI v2.1 日志配置（规格 §7 logging / §6.1 安全架构）。

文件日志 + 控制台日志双输出，敏感字段脱敏，日志轮转（10MB / 5 个文件）。
提供 setup_logging() 函数与脱敏过滤器。

规格引用：
  - §7 logging.level / max_file_size_mb=10 / max_files=5 / sensitive_fields
  - §6.1 安全架构：日志脱敏
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
from typing import Any

from ..config import (
    LOG_LEVEL,
    LOG_MAX_FILE_MB,
    LOG_MAX_FILES,
    LOG_SENSITIVE_FIELDS,
    LOGS_DIR,
)

# 默认敏感字段（从配置读取，补充常见项）
_DEFAULT_SENSITIVE = {"api_key", "password", "token", "secret",
                      "authorization", "private_key", "session_token"}


class SensitiveDataFilter(logging.Filter):
    """日志脱敏过滤器：将敏感字段的值替换为 ***。

    支持两种脱敏场景：
      1. 日志消息中的 JSON 字段（如 {"api_key": "sk-xxx"}）
      2. 键值对格式（如 api_key=sk-xxx）
    """

    def __init__(self, sensitive_fields: set[str] | None = None) -> None:
        super().__init__()
        self._sensitive = sensitive_fields or _DEFAULT_SENSITIVE
        # 构建正则：匹配 "field": "value" 或 field=value
        self._patterns: list[tuple[re.Pattern, str]] = []
        for field in self._sensitive:
            # JSON 格式: "field": "value" 或 'field': 'value'
            json_pat = re.compile(
                rf'(["\'])({re.escape(field)})(["\']\s*:\s*["\'])([^"\']*)(["\'])',
                re.IGNORECASE,
            )
            self._patterns.append((json_pat, r'\1\2\3***\5'))
            # 键值对格式: field=value
            kv_pat = re.compile(
                rf'\b({re.escape(field)})(=)([^\s,;&\]]+)',
                re.IGNORECASE,
            )
            self._patterns.append((kv_pat, r'\1\2***'))

    def filter(self, record: logging.LogRecord) -> bool:
        """对日志消息进行脱敏处理。"""
        msg = record.getMessage()
        for pattern, replacement in self._patterns:
            msg = pattern.sub(replacement, msg)

        # 处理 extra 字典中的敏感字段
        if hasattr(record, "__dict__"):
            for key in list(record.__dict__.keys()):
                if key.lower() in self._sensitive and \
                   isinstance(record.__dict__[key], str):
                    record.__dict__[key] = "***"

        # 重写消息
        record.msg = msg
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    """JSON 格式日志（可选，供结构化日志分析）。"""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, ensure_ascii=False, default=str)


# ═══════════════════════════════════════════════════════════════════
#  日志初始化
# ═══════════════════════════════════════════════════════════════════

_configured = False
_sensitive_filter: SensitiveDataFilter | None = None


def setup_logging(level: str | None = None,
                  use_json: bool = False) -> logging.Logger:
    """初始化全局日志配置。

    Args:
        level: 日志级别（DEBUG/INFO/WARNING/ERROR），默认从配置读取
        use_json: 是否使用 JSON 格式（默认 False，纯文本）

    Returns:
        根 logger
    """
    global _configured, _sensitive_filter

    if _configured:
        return logging.getLogger("omnispace")

    log_level = level or LOG_LEVEL
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # 确保日志目录存在
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # 合并配置中的敏感字段
    sensitive_fields = set(_DEFAULT_SENSITIVE)
    if LOG_SENSITIVE_FIELDS:
        sensitive_fields.update(f.lower() for f in LOG_SENSITIVE_FIELDS)
    _sensitive_filter = SensitiveDataFilter(sensitive_fields)

    # 日志格式
    if use_json:
        fmt = JsonFormatter()
    else:
        fmt = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s:%(lineno)d: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    # ── 文件日志处理器（RotatingFileHandler，10MB / 5 个文件）──
    file_handler = logging.handlers.RotatingFileHandler(
        filename=str(LOGS_DIR / "backend.log"),
        maxBytes=LOG_MAX_FILE_MB * 1024 * 1024,
        backupCount=LOG_MAX_FILES,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(fmt)
    file_handler.addFilter(_sensitive_filter)

    # ── 控制台日志处理器 ──
    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    console_handler.setFormatter(console_fmt)
    console_handler.addFilter(_sensitive_filter)

    # ── 错误日志单独文件 ──
    error_handler = logging.handlers.RotatingFileHandler(
        filename=str(LOGS_DIR / "error.log"),
        maxBytes=LOG_MAX_FILE_MB * 1024 * 1024,
        backupCount=LOG_MAX_FILES,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(fmt)
    error_handler.addFilter(_sensitive_filter)

    # ── 配置根 logger ──
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    # 清除已有处理器防止重复
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(error_handler)

    # 降低第三方库日志级别
    for noisy in ("uvicorn.access", "uvicorn.error", "sqlalchemy.engine",
                  "chromadb", "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    log = logging.getLogger("omnispace")
    log.info("日志系统初始化完成: level=%s, file=%s, rotation=%dMB×%d",
             log_level, LOGS_DIR / "backend.log", LOG_MAX_FILE_MB, LOG_MAX_FILES)
    return log

def mask_value(field_name: str, value: Any) -> Any:
    """工具函数：检查字段名是否敏感，是则返回 *** 否则原值。"""
    if _sensitive_filter and field_name.lower() in _sensitive_filter._sensitive:
        return "***"
    return value
# 本项目仅供学习使用，商业授权请+Q 3559331368
