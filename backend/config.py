"""OmniSpace AI v2.1 配置模块（规格 §7 配置项完整清单）。

从 config.yaml 加载配置，并提供 Python 常量供各模块引用。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# ── 路径 ──────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
FRONTEND_DIR = ROOT_DIR / "frontend"
MODELS_DIR = ROOT_DIR / "models"
DATA_DIR = ROOT_DIR / "data"
LOGS_DIR = ROOT_DIR / "logs"

for _d in (DATA_DIR, LOGS_DIR, MODELS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "omnispace.db"

# ── 加载 YAML ────────────────────────────────────────────────────
_yaml_path = BACKEND_DIR / "config.yaml"
with open(_yaml_path, encoding="utf-8") as _f:
    _cfg: dict[str, Any] = yaml.safe_load(_f)

# ── 服务 ─────────────────────────────────────────────────────────
HOST = _cfg["server"]["host"]  # 127.0.0.1，§14约束2
PORT = _cfg["server"]["port"]  # 5800

# ── 回环绑定闸门（TASK-P0-05，规格 §14 约束2）────────────────────
# API 无认证体系，非回环绑定 = 局域网数据裸奔。原 main.py lifespan 告警
# 发生在 uvicorn 绑定 socket 之后（为时已晚），闸门前移到配置导入期：
# 本模块被 import 时即校验，早于任何绑定动作，无论经 launcher 还是手动
# uvicorn 启动都无法绕过。显式豁免：环境变量 OMNISPACE_ALLOW_LAN=1。

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def assert_loopback_host(host: str, allow_lan: str | None = None) -> None:
    """非回环 host 且未豁免时抛 RuntimeError（拒绝启动）。"""
    import os
    if host in _LOOPBACK_HOSTS:
        return
    if (allow_lan if allow_lan is not None else os.environ.get("OMNISPACE_ALLOW_LAN")) == "1":
        import logging
        logging.getLogger("omnispace.config").warning(
            "!" * 60 + "\nOMNISPACE_ALLOW_LAN=1 已豁免回环校验：当前绑定 %s，"
            "API 无认证体系，请确保处于可信网络\n" + "!" * 60, host)
        return
    raise RuntimeError(
        f"拒绝启动：config.yaml server.host={host} 为非回环地址（规格 §14 约束2）。"
        "API 无认证体系，绑定局域网等于数据裸奔。确需局域网访问请设置环境变量 "
        "OMNISPACE_ALLOW_LAN=1 后重试（自担风险），或将 host 改回 127.0.0.1。")


assert_loopback_host(HOST)

API_PREFIX = "/api/v1"  # 文档B/D/E 统一基线（ADR-03：由历史 /v1 迁移）
RATE_LIMIT = _cfg["server"]["rate_limit"]  # 100/min

# ── 调度 ─────────────────────────────────────────────────────────
SCHEDULER_INTERVAL_MS = _cfg["scheduler"]["sample_interval_ms"]
# 分级采样间隔（秒）：低频指标缓存复用，不随 tick 重复采集
GPU_SAMPLE_INTERVAL_S = float(_cfg["scheduler"].get("gpu_sample_interval_s", 2))
CPU_SAMPLE_INTERVAL_S = float(_cfg["scheduler"].get("cpu_sample_interval_s", 5))
DISK_SAMPLE_INTERVAL_S = float(_cfg["scheduler"].get("disk_sample_interval_s", 10))
HYSTERESIS_SECONDS = _cfg["scheduler"]["hysteresis_seconds"]
# 空闲显存回收：无功能锁活动超过该秒数后卸载非常驻大模型（默认 300s）
IDLE_RECLAIM_SECONDS = float(_cfg["scheduler"].get("idle_reclaim_seconds", 300))
THRESHOLDS = _cfg["scheduler"]["thresholds"]

# ── 模型缓存 ─────────────────────────────────────────────────────
CACHE_COMPRESSION = _cfg["model_cache"]["compression"]
CACHE_EVICTION = _cfg["model_cache"]["eviction_policy"]

# ── 数据库 ───────────────────────────────────────────────────────
# 诚实标注：不提供 DB_ENCRYPTION 常量——SQLCipher 未启用（死代码已清理），
# 数据库为明文 SQLite，仅本地 127.0.0.1 运行（规格 §14 约束1/2）。
DB_WAL_MODE = _cfg["database"]["wal_mode"]
DB_BUSY_TIMEOUT = _cfg["database"]["busy_timeout_ms"]

# ── 视频 ─────────────────────────────────────────────────────────
VIDEO_DEFAULT_CODEC = _cfg["video"]["default_codec"]
VIDEO_SUPPORTED_CODECS = _cfg["video"]["supported_codecs"]
LTX2_MAX_AUDIO_SYNC = _cfg["video"]["ltx2_max_audio_sync_seconds"]
VIDEO_MAX_DURATION = _cfg["video"]["max_duration_seconds"]

# ── 对话 ─────────────────────────────────────────────────────────
DIALOG_MAX_CONTEXT = _cfg["dialog"]["max_context_tokens"]
DIALOG_MAX_INPUT_CHARS = _cfg["dialog"].get("max_input_chars", 32768)
# 单次 prefill 输入 token 上限（16GB 显存安全线，见 config.yaml 注释）
DIALOG_MAX_PREFILL_TOKENS = int(_cfg["dialog"].get("max_prefill_tokens", 3072))
DIALOG_STREAM_CHUNK = _cfg["dialog"]["stream_chunk_size"]

# ── 绘画 ─────────────────────────────────────────────────────────
PAINT_DEFAULT_STEPS = _cfg["paint"]["default_steps"]
PAINT_DEFAULT_GUIDANCE = _cfg["paint"]["default_guidance"]
PAINT_MAX_BATCH = _cfg["paint"]["max_batch_size"]

# ── 漫剧 ─────────────────────────────────────────────────────────
STORYBOARD_MAX_ROWS = _cfg["manga"]["max_storyboard_rows"]
PANORAMA_RESOLUTIONS = _cfg["manga"]["panorama_resolutions"]
CAMERA_PRESETS = _cfg["manga"]["camera_presets"]
POSE_PRESETS_COUNT = _cfg["manga"]["pose_presets_count"]
VOICE_PRESET_EMOTIONS = _cfg["manga"]["voice_preset_emotions"]

# ── 日志 ─────────────────────────────────────────────────────────
LOG_LEVEL = _cfg["logging"]["level"]
LOG_MAX_FILE_MB = _cfg["logging"]["max_file_size_mb"]
LOG_MAX_FILES = _cfg["logging"]["max_files"]
LOG_SENSITIVE_FIELDS = _cfg["logging"]["sensitive_fields"]

# ── UI ───────────────────────────────────────────────────────────
UI_THEME = _cfg["ui"]["theme"]
UI_PRIMARY_COLOR = _cfg["ui"]["primary_color"]
UI_FONT = _cfg["ui"]["font"]
UI_AI_GENERATED_BG = _cfg["ui"]["ai_generated_bg"]

APP_VERSION = str(_cfg.get("app", {}).get("version", "2.3.1"))


def get_config() -> dict[str, Any]:
    """返回完整配置字典。"""
    return _cfg
