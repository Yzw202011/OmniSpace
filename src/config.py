"""OmniSpace AI v2.1 配置模块（规格 §7 配置项完整清单）。

从 config.yaml 加载配置，并提供 Python 常量供各模块引用。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# ── 路径 ──────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
# 兼容别名：backend→src 扁平化重构后，旧代码/外部进程可能仍引用
# config.BACKEND_DIR。指向 SRC_DIR（backend/ 目录已不存在），避免
# 干净环境下 import 期 FileNotFoundError。
BACKEND_DIR = SRC_DIR
FRONTEND_DIR = ROOT_DIR / "frontend"
MODELS_DIR = ROOT_DIR / "models"
DATA_DIR = ROOT_DIR / "data"
LOGS_DIR = ROOT_DIR / "logs"

for _d in (DATA_DIR, LOGS_DIR, MODELS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "omnispace.db"

# ── 加载 YAML ────────────────────────────────────────────────────
_yaml_path = SRC_DIR / "config.yaml"
if not _yaml_path.is_file():
    raise FileNotFoundError(
        f"配置文件不存在: {_yaml_path}（src 扁平化后 config.yaml 位于 src/ 下；"
        "若从旧布局升级请确认已拉取 src/config.yaml）")
with open(_yaml_path, encoding="utf-8") as _f:
    _cfg: dict[str, Any] = yaml.safe_load(_f)

# ── 服务 ─────────────────────────────────────────────────────────
HOST = _cfg["server"]["host"]  # 127.0.0.1，§14约束2
PORT = _cfg["server"]["port"]  # 5800

# ── API 事件日志（自愈批2：慢请求阈值，单一来源 config.yaml `event_log` 节）──
EVENT_LOG_SLOW_MS = int(_cfg.get("event_log", {}).get("slow_request_ms", 3000))

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
RATE_LIMIT = _cfg["server"]["rate_limit"]  # 300/min（config.yaml 实值；旧注释 100 已过期，批0-c 勘误）

# ── 调度 ─────────────────────────────────────────────────────────
SCHEDULER_INTERVAL_MS = _cfg["scheduler"]["sample_interval_ms"]
# 分级采样间隔（秒）：低频指标缓存复用，不随 tick 重复采集
GPU_SAMPLE_INTERVAL_S = float(_cfg["scheduler"].get("gpu_sample_interval_s", 2))
CPU_SAMPLE_INTERVAL_S = float(_cfg["scheduler"].get("cpu_sample_interval_s", 5))
DISK_SAMPLE_INTERVAL_S = float(_cfg["scheduler"].get("disk_sample_interval_s", 10))
HYSTERESIS_SECONDS = _cfg["scheduler"]["hysteresis_seconds"]
# 空闲显存回收（P2 两级空闲回收）：
#   表层 60s → 释放 embed/aux/voice 小模型；深层默认 300s → 卸载非常驻大模型
SHALLOW_RECLAIM_SECONDS = float(_cfg["scheduler"].get("shallow_reclaim_seconds", 60))
IDLE_RECLAIM_SECONDS = float(_cfg["scheduler"].get("idle_reclaim_seconds", 300))
# 对话引擎空闲看门狗阈值（方案A，2026-09-05）：就绪且全系统空闲达此秒数
# → 正规卸载链还显存；0=禁用（见 config.yaml scheduler 注释）
DIALOG_IDLE_UNLOAD_SECONDS = float(_cfg["scheduler"].get("dialog_idle_unload_seconds", 900))
THRESHOLDS = _cfg["scheduler"]["thresholds"]

# ── 磁盘阈值（P2 统一口径：单一来源 config.yaml `disk` 节）─────────
# 语义拆分：launcher 启动预检 / startup_check 含模型运行 / 安装器完整包，
# 消除昔日 launcher 20GB / startup_check 50GB / installer 500GB 三套硬编码。
DISK_START_MIN_GB = float(_cfg["disk"]["start_min_gb"])
DISK_MODELS_MIN_GB = float(_cfg["disk"]["models_min_gb"])

# ── 多卡与设备策略（P3 §5.3）──────────────────────────────────────
# 默认单卡；secondary_offload 显式开启且辅助索引有效时才启用双卡卸载。
GPU_PRIMARY_DEVICE = int(_cfg.get("gpu", {}).get("primary_device", 0))
GPU_AUXILIARY_DEVICE = int(_cfg.get("gpu", {}).get("auxiliary_device", 1))
GPU_SECONDARY_OFFLOAD = bool(_cfg.get("gpu", {}).get("secondary_offload", False))

# 功能→卡分配（批1 多卡地基，2026-09-05）：缺项视为主卡=现状。
# 仅多卡机器生效（单卡强制全主卡，裁决单源见 engines/gpu_domains.py
# resolve_assignments：坏索引回落主卡、paint/video_gen 强制同卡收敛）。
GPU_FEATURE_DEVICES: dict[str, int] = {}
for _fname, _fdev in (_cfg.get("gpu", {}).get("feature_devices") or {}).items():
    _fkey = str(_fname).strip().lower()
    if _fkey in ("dialog", "paint", "video_gen", "training"):
        try:
            GPU_FEATURE_DEVICES[_fkey] = int(_fdev)
        except (TypeError, ValueError):
            log.debug("<module>: 降级忽略", exc_info=True)

# ── 模型缓存 ─────────────────────────────────────────────────────
CACHE_COMPRESSION = _cfg["model_cache"]["compression"]
CACHE_EVICTION = _cfg["model_cache"]["eviction_policy"]

# ── 数据库 ───────────────────────────────────────────────────────
# 诚实标注：不提供 DB_ENCRYPTION 常量——SQLCipher 未启用（死代码已清理），
# 数据库为明文 SQLite，仅本地 127.0.0.1 运行（规格 §14 约束1/2）。
DB_WAL_MODE = _cfg["database"]["wal_mode"]
DB_BUSY_TIMEOUT = _cfg["database"]["busy_timeout_ms"]
# 写同步模式与页缓存上限（WAL 下 NORMAL 为标准推荐，性能优化见 config.yaml 注释）
DB_SYNCHRONOUS = _cfg["database"].get("synchronous", "NORMAL")
DB_CACHE_SIZE_KB = int(_cfg["database"].get("cache_size_kb", 16384))

# ── 视频 ─────────────────────────────────────────────────────────
LTX2_MAX_AUDIO_SYNC = _cfg["video"]["ltx2_max_audio_sync_seconds"]
VIDEO_MAX_DURATION = _cfg["video"]["max_duration_seconds"]

# ── 对话 ─────────────────────────────────────────────────────────
DIALOG_MAX_INPUT_CHARS = _cfg["dialog"].get("max_input_chars", 32768)
# 单次 prefill 输入 token 上限（16GB 显存安全线，见 config.yaml 注释）
DIALOG_MAX_PREFILL_TOKENS = int(_cfg["dialog"].get("max_prefill_tokens", 3072))

# ── 绘画 ─────────────────────────────────────────────────────────

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

APP_VERSION = str(_cfg.get("app", {}).get("version", "2.5.0"))

# 构建号（P1 单一真源方案）：config.yaml 的 app.version 是唯一手写处；
# make_dist.py 出包时在包内生成 src/build_info.py（版本+git 短哈希+日期），
# 开发环境无此文件时回退 "+dev"，用于区分开发跑的还是发行包跑的
try:
    from .build_info import BUILD_ID  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001 - 开发环境无生成文件
    BUILD_ID = f"{APP_VERSION}+dev"


def get_config() -> dict[str, Any]:
    """返回完整配置字典。"""
    return _cfg
