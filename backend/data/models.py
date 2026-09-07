"""OmniSpace AI v2.1 后端数据模型（规格 §3.2）。

定义所有 Pydantic 模型、枚举和路由表。
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# 资产名称/项目 ID 路径安全（审计 P1-1 修复，2026-08-29）：这些值
# 会被拼进 DATA_DIR 下的落盘目录（comic_assets/{project_id}/…/{name}）。
# 拒绝路径分隔符/盘符/Windows 保留字符/控制符与「..」；中文、空格、
# 中英文数字与常用标点放行（存量资产名均为中文，不受影响）。
_UNSAFE_NAME_PAT = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _check_safe_name(v: str) -> str:
    if _UNSAFE_NAME_PAT.search(v or "") or ".." in (v or ""):
        raise ValueError("名称含路径非法字符（/ \\ : * ? \" < > | ..）")
    return v

# ═══════════════════════════════════════════════════════════════════
#  枚举
# ═══════════════════════════════════════════════════════════════════

class ModelCategory(str, Enum):
    DIALOG = "dialog"
    VIDEO = "video"
    VOICE = "voice"
    VISION = "vision"
    LANGUAGE = "language"
    THREE_D = "3d"
    AUXILIARY = "auxiliary"
    # 视觉语音大模型（Omni 全模态，2026-08-21 用户裁定新增）：
    # 文字/图片/视频/音频四模态输入，文字+自然语音输出
    #（代表：Qwen2.5-Omni，Thinker-Talker 架构）
    OMNI = "omni"


class SynergyMode(str, Enum):
    GPU_PRIMARY = "gpu_primary"
    CPU_ASSIST = "cpu_assist"
    GPU_ASSIST_CPU = "gpu_assist_cpu"
    MEMORY_PRESSURE = "memory_pressure"
    ALL_TENSE = "all_tense"
    ALL_IDLE = "all_idle"


class VideoModel(str, Enum):
    LTX2 = "ltx-2"
    # MiniMax H3 33B（NVFP4 DiT + Qwen3-VL-32B int4 convrot 编码器，
    # ComfyUI 子进程管线，2026-08-25 接入）：DynamicVRAM 分时换载，
    # 采样期峰值 ~12GB，16GB 卡可跑；原生音画（32kHz 立体声）
    MINIMAX_H3 = "minimax-h3"
    WAN21_14B_FP8 = "wan2.1-14b-fp8"
    WAN21_14B_INT4 = "wan2.1-14b-int4"
    WAN21_1_3B = "wan2.1-1.3b"
    # Wan2.2-TI2V-5B：单 ckpt 原生 T2V+I2V+TI2V 双条件统一底座
    # （2026-08-23 混合架构裁定：视频侧统一到该权重，I2V 与文+图
    # 生视频共用；目录名 wan22-ti2v-5b 与发现层约定一致）
    WAN22_TI2V_5B = "wan22-ti2v-5b"
    LTX_VIDEO_095 = "ltx-video-0.9.5"  # 2B diffusers，T5 int8 量化加载
    COGVIDEOX_2B = "cogvideox-2b"
    COGVIDEOX_2B_CPU = "cogvideox-2b-cpu"
    ANIMATELCM = "AnimateLCM"


class ModelStatus(str, Enum):
    READY = "ready"
    LOADING = "loading"
    ERROR = "error"
    NOT_INSTALLED = "not_installed"


class GenerationStatus(str, Enum):
    PENDING = "pending"
    GENERATING = "generating"
    DONE = "done"
    ERROR = "error"


class TrainStatus(str, Enum):
    QUEUED = "queued"
    TRAINING = "training"
    EVALUATING = "evaluating"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"  # 审计 R3-BE1：/learn/tasks/{id}/cancel 落库状态


class ActiveFeature(str, Enum):
    DIALOG = "dialog"
    PAINT = "paint"
    VIDEO_GEN = "video_gen"
    TRAINING = "training"


# ═══════════════════════════════════════════════════════════════════
#  路由表（规格 §3.2）
#  2026-08-29 模型裁剪（用户裁定）：
#    - 对话只保留 Qwen3-VL 4B/8B 家族（8B 含 AWQ int4 量化形态）；
#      omni / 2B / CPU 兜底条目移除。DeepSeek-R1-14B 保留注册表与
#      引擎候选（漫剧·文字槽专用，经 manga-dialog 槽显式点名）。
#    - 绘画只保留 FLUX2-Klein-9B + Qwen-Image-2512（AI 绘画模块）；
#      flux2-klein-4b 保留引擎候选（漫剧角色生图 comic_gen 专用底座，
#      不入绘画路由表）；sdxl 系保留引擎候选（LoRA 训练底座）。
#    - 视频只保留 MiniMax H3（12GB 档 5s 段 ~10.6GB 实测可跑）。
# ═══════════════════════════════════════════════════════════════════

VIDEO_ROUTING_TABLE = [
    # H3 NVFP4：33B 顶格质量 + 原生音画。门槛 12 = 实测采样峰值
    # ~10.6GB（125 帧 5s 段）+ 余量；16GB 卡首选，12GB 档跑短段
    {"min_vram_gb": 12, "model": VideoModel.MINIMAX_H3},
]

DIALOG_ROUTING_TABLE = [
    # 09-02 清理：qwen3-vl-8b 完整版本为空壳已隔离（拍板清单 D3），
    # 16GB+ 档位由 awq 档承接
    {"min_vram_gb": 12, "model": "qwen3-vl-8b-awq"},
    {"min_vram_gb": 8, "model": "qwen3-vl-4b"},
]

PAINT_ROUTING_TABLE = [
    # FLUX.2 Klein 9B（quanto float8 / GGUF Q6_K，质量优于 4B，
    # 16GB 卡自动首选，2026-08-25 接入）
    {"min_vram_gb": 10, "model": "flux2-klein-9b"},
    # Qwen-Image-2512（20B MMDiT + Qwen2.5-VL 编码器，中文原生理解/
    # 中英文字渲染开源第一）：GGUF 流式推理（量化权重常驻 CPU，
    # GPU 峰值 ~1GB + 编码器逐层 + VAE，实测 2.15s/步）——
    # 12GB 档位"高精度模式"底座，RAM 需 28GB+（2026-08-22 接入）
    {"min_vram_gb": 6, "model": "qwen-image-2512"},
]


# ═══════════════════════════════════════════════════════════════════
#  硬件等级自适应（文档B §4.2，审计 BK-011：按 GPU 型号名六档匹配）
# ═══════════════════════════════════════════════════════════════════
# 文档B §4.2 权威映射（显存路由无法区分 4070Ti 12GB 与 3060 12GB，
# 必须按型号名匹配）；型号名未命中时按显存保守降档回退。
# 模型列为内部路由表模型 id（dialog/paint/video）+ 学习标签数配额。
HARDWARE_TIER_TABLE: list[dict] = [
    # ── RTX 50 系列 (Blackwell, CC 12.0) ──
    {
        "tier": "rtx5090", "label": "RTX 5090 32GB",
        "name_patterns": ["rtx 5090", "5090"],
        "min_vram_gb": 30,
        "models": {"dialog": "qwen3-vl-8b-awq", "paint": "flux2-klein-9b",
                   "video": "minimax-h3"},
        "learn_tabs": 5,
    },
    {
        "tier": "rtx5080", "label": "RTX 5080 16GB",
        "name_patterns": ["rtx 5080", "5080"],
        "min_vram_gb": 14,
        "models": {"dialog": "qwen3-vl-8b-awq", "paint": "flux2-klein-9b",
                   "video": "minimax-h3"},
        "learn_tabs": 4,
    },
    {
        "tier": "rtx5070ti", "label": "RTX 5070 Ti 16GB",
        "name_patterns": ["rtx 5070 ti", "5070 ti", "5070ti"],
        "min_vram_gb": 14,
        "models": {"dialog": "qwen3-vl-8b-awq", "paint": "flux2-klein-9b",
                   "video": "minimax-h3"},
        "learn_tabs": 4,
    },
    {
        "tier": "rtx5070", "label": "RTX 5070 12GB",
        "name_patterns": ["rtx 5070", "5070"],
        "min_vram_gb": 10,
        "models": {"dialog": "qwen3-vl-4b", "paint": "flux2-klein-9b",
                   "video": "minimax-h3"},
        "learn_tabs": 3,
    },
    {
        "tier": "rtx5060ti", "label": "RTX 5060 Ti 16GB",
        "name_patterns": ["rtx 5060 ti", "5060 ti", "5060ti"],
        "min_vram_gb": 14,
        "models": {"dialog": "qwen3-vl-8b-awq", "paint": "flux2-klein-9b",
                   "video": "minimax-h3"},
        "learn_tabs": 3,
    },
    {
        "tier": "rtx5060", "label": "RTX 5060 8GB",
        "name_patterns": ["rtx 5060", "5060"],
        "min_vram_gb": 6,
        "models": {"dialog": "qwen3-vl-4b", "paint": "qwen-image-2512",
                   "video": ""},
        "learn_tabs": 2,
    },
    # ── RTX 40 系列 (Ada Lovelace, CC 8.9) ──
    {
        "tier": "rtx4090", "label": "RTX 4090 24GB",
        "name_patterns": ["rtx 4090", "4090"],
        "min_vram_gb": 20,
        "models": {"dialog": "qwen3-vl-8b-awq", "paint": "flux2-klein-9b",
                   "video": "minimax-h3"},
        "learn_tabs": 5,
    },
    {
        "tier": "rtx4080s", "label": "RTX 4080 Super 16GB",
        "name_patterns": ["rtx 4080 super", "4080 super", "4080s"],
        "min_vram_gb": 14,
        "models": {"dialog": "qwen3-vl-8b-awq", "paint": "flux2-klein-9b",
                   "video": "minimax-h3"},
        "learn_tabs": 4,
    },
    {
        "tier": "rtx4080", "label": "RTX 4080 16GB",
        "name_patterns": ["rtx 4080", "4080"],
        "min_vram_gb": 14,
        "models": {"dialog": "qwen3-vl-8b-awq", "paint": "flux2-klein-9b",
                   "video": "minimax-h3"},
        "learn_tabs": 4,
    },
    {
        "tier": "rtx4070tis", "label": "RTX 4070 Ti Super 16GB",
        "name_patterns": ["rtx 4070 ti super", "4070 ti super", "4070tis"],
        "min_vram_gb": 14,
        "models": {"dialog": "qwen3-vl-8b-awq", "paint": "flux2-klein-9b",
                   "video": "minimax-h3"},
        "learn_tabs": 4,
    },
    {
        "tier": "rtx4070ti", "label": "RTX 4070 Ti 12GB",
        "name_patterns": ["rtx 4070 ti", "4070 ti", "4070ti"],
        "min_vram_gb": 0,  # 仅按型号名命中（12GB 档由 3060 兜底）
        "models": {"dialog": "qwen3-vl-4b", "paint": "flux2-klein-9b",
                   "video": "minimax-h3"},
        "learn_tabs": 3,
    },
    {
        "tier": "rtx4070s", "label": "RTX 4070 Super 12GB",
        "name_patterns": ["rtx 4070 super", "4070 super", "4070s"],
        "min_vram_gb": 10,
        "models": {"dialog": "qwen3-vl-4b", "paint": "flux2-klein-9b",
                   "video": "minimax-h3"},
        "learn_tabs": 3,
    },
    {
        "tier": "rtx4070", "label": "RTX 4070 12GB",
        "name_patterns": ["rtx 4070", "4070"],
        "min_vram_gb": 10,
        "models": {"dialog": "qwen3-vl-4b", "paint": "flux2-klein-9b",
                   "video": "minimax-h3"},
        "learn_tabs": 3,
    },
    {
        "tier": "rtx4060ti16g", "label": "RTX 4060 Ti 16GB",
        "name_patterns": ["rtx 4060 ti 16", "4060 ti 16"],
        "min_vram_gb": 14,
        "models": {"dialog": "qwen3-vl-8b-awq", "paint": "flux2-klein-9b",
                   "video": "minimax-h3"},
        "learn_tabs": 2,
    },
    {
        "tier": "rtx4060ti", "label": "RTX 4060 Ti 8GB",
        "name_patterns": ["rtx 4060 ti", "4060 ti", "4060ti"],
        "min_vram_gb": 6,
        "models": {"dialog": "qwen3-vl-4b", "paint": "qwen-image-2512",
                   "video": ""},
        "learn_tabs": 2,
    },
    {
        "tier": "rtx4060", "label": "RTX 4060 8GB",
        "name_patterns": ["rtx 4060", "4060"],
        "min_vram_gb": 6,
        "models": {"dialog": "qwen3-vl-4b", "paint": "qwen-image-2512",
                   "video": ""},
        "learn_tabs": 2,
    },
    # ── 入门档 ──
    {
        "tier": "rtx3060", "label": "RTX 3060 12GB",
        "name_patterns": ["rtx 3060", "3060"],
        "min_vram_gb": 10,
        "models": {"dialog": "qwen3-vl-4b", "paint": "flux2-klein-9b",
                   "video": "minimax-h3"},
        "learn_tabs": 2,
    },
    {
        "tier": "rx6600", "label": "AMD RX 6600",
        "name_patterns": ["rx 6600", "rx6600", "radeon rx 6600"],
        "min_vram_gb": 6,
        "models": {"dialog": "qwen3-vl-4b", "paint": "qwen-image-2512",
                   "video": ""},
        "learn_tabs": 1,
    },
    {
        "tier": "cpu", "label": "纯 CPU",
        "name_patterns": [],
        "min_vram_gb": 0,
        # 2026-08-29 模型裁剪：CPU 降级变体（int4-cpu / sdxl-cpu）随
        # 路由表裁剪移除——纯 CPU 档对话/绘画/视频均不可用（诚实置空）。
        "models": {"dialog": "", "paint": "", "video": ""},
        "learn_tabs": 1,
    },
]


def detect_hardware_tier(gpu_name: str = "", vram_total_mb: int = 0) -> dict:
    """按 GPU 型号名识别硬件等级（文档B §4.2），未命中按显存保守降档。

    覆盖 RTX 50/40/30 全系列 + AMD + 纯CPU，按型号名精确匹配；
    同族内按 Ti Super → Ti → Super → 基础 的顺序排列（首个匹配命中）。

    Args:
        gpu_name:      GPU 型号名（pynvml/torch 报告，如 "NVIDIA GeForce RTX 5070 Ti"）
        vram_total_mb: 显存总量（MB），仅在型号名未命中时用于降档

    Returns:
        {"tier", "label", "models": {dialog,paint,video}, "learn_tabs",
         "matched_by": "name" | "vram" | "none"}
    """
    name = (gpu_name or "").strip().lower()
    if name and name not in ("none", "unknown"):
        for entry in HARDWARE_TIER_TABLE:
            for pat in entry["name_patterns"]:
                if pat in name:
                    return {**entry, "matched_by": "name"}
        # 型号名未登记：按显存保守降档（12GB 档取 4070 而非 4070Ti Super，
        # 与文档B §4.2「显存相同按低档路由」的保守语义一致）
        vram_gb = vram_total_mb / 1024.0
        if vram_gb > 0:
            fallback_order = (
                "rtx5090",        # 30GB
                "rtx4090",        # 20GB
                "rtx4080",        # 14GB → 16GB 卡回退
                "rtx4070",        # 10GB → 12GB 卡回退
                "rtx4060",        # 6GB  → 8GB 卡回退
                "rx6600",         # 6GB 兜底
            )
            by_tier = {e["tier"]: e for e in HARDWARE_TIER_TABLE}
            for tid in fallback_order:
                entry = by_tier[tid]
                if vram_gb >= entry["min_vram_gb"] and entry["min_vram_gb"] > 0:
                    return {**entry, "matched_by": "vram"}
            return {**by_tier["rx6600"], "matched_by": "vram"}
    cpu_tier = HARDWARE_TIER_TABLE[-1]
    return {**cpu_tier, "matched_by": "none" if not name else "vram"}


# ── 手动档位覆盖（SET-008）─────────────────────────────────────────
# PUT /hardware/tier 写入 system_settings 表 key="hardware.tier_override"，
# 本函数在所有自动探测调用点之上叠加覆盖语义（5s 缓存，避免热路径
# 每次读库）。override="auto" 时回退自动探测。
_TIER_OVERRIDE_KEY = "hardware.tier_override"
_tier_override_cache: dict = {"value": None, "ts": 0.0}


def read_tier_override() -> str:
    """读取持久化的手动档位覆盖（无覆盖/异常 → "auto"）。"""
    import time as _t

    now = _t.time()
    if now - _tier_override_cache["ts"] < 5.0 \
            and _tier_override_cache["value"] is not None:
        return _tier_override_cache["value"]
    value = "auto"
    try:
        from .database import get_db_safe
        db = get_db_safe()
        if db is not None:
            row = db.query_one(
                "SELECT value FROM system_settings WHERE key=?",
                (_TIER_OVERRIDE_KEY,))
            if row:
                import json as _json
                raw = _json.loads(row["value"])
                value = str(raw if isinstance(raw, str)
                            else raw.get("tier", "auto"))
    except Exception:  # noqa: BLE001 - 读库失败按自动探测
        value = "auto"
    valid = {e["tier"] for e in HARDWARE_TIER_TABLE}
    if value not in valid:
        value = "auto"
    _tier_override_cache.update({"value": value, "ts": now})
    return value


def write_tier_override(tier: str) -> None:
    """持久化手动档位覆盖并刷新缓存。"""
    import json as _json
    import time as _t

    from .database import get_db_safe
    db = get_db_safe()
    if db is not None:
        db.sql(
            "INSERT INTO system_settings (key, value, updated_at)"
            " VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET"
            " value=excluded.value, updated_at=excluded.updated_at",
            (_TIER_OVERRIDE_KEY, _json.dumps(tier), _t.time()))
    _tier_override_cache.update({"value": tier, "ts": _t.time()})


def get_effective_tier(gpu_name: str = "", vram_total_mb: int = 0) -> dict:
    """档位解析入口：手动覆盖优先，其次自动探测（matched_by=manual）。"""
    override = read_tier_override()
    if override != "auto":
        for entry in HARDWARE_TIER_TABLE:
            if entry["tier"] == override:
                return {**entry, "matched_by": "manual"}
    return detect_hardware_tier(gpu_name, vram_total_mb)


# ═══════════════════════════════════════════════════════════════════
#  请求/响应模型
# ═══════════════════════════════════════════════════════════════════

class ApiResponse(BaseModel):
    code: int = 0
    message: str = "ok"
    data: dict | None = None


class ApiErrorDetail(BaseModel):
    code: int
    message: str
    detail: dict | None = None
    suggestion: str = ""


# ── 硬件 ──────────────────────────────────────────────────────────

class GpuInfo(BaseModel):
    vendor: str = "none"  # nvidia | amd | none
    name: str = ""
    vram_total_mb: int = 0
    vram_free_mb: int = 0
    compute_capability: str = ""
    driver_version: str = ""


class CpuInfo(BaseModel):
    name: str = ""
    cores: int = 0
    threads: int = 0
    usage_percent: float = 0.0
    temp_celsius: float = 0.0


class RamInfo(BaseModel):
    total_gb: float = 0.0
    available_gb: float = 0.0
    total_mb: int = 0
    available_mb: int = 0
    usage_percent: float = 0.0


class DiskInfo(BaseModel):
    total_gb: float = 0.0
    free_gb: float = 0.0
    percent: float = 0.0


class HardwareProfile(BaseModel):
    gpu: GpuInfo = GpuInfo()
    cpu: CpuInfo = CpuInfo()
    ram: RamInfo = RamInfo()
    disk: DiskInfo = DiskInfo()
    power: str = "ac"  # ac | battery


class SchedulerState(BaseModel):
    mode: SynergyMode = SynergyMode.GPU_PRIMARY
    gpu_usage: float = 0.0
    cpu_usage: float = 0.0
    mem_available_gb: float = 0.0
    active_model: str | None = None
    cached_models: list[str] = []
    last_switch_ms: int = 0


# ── 模型 ──────────────────────────────────────────────────────────

class ModelInfo(BaseModel):
    id: str
    name: str
    category: ModelCategory
    purpose: str = ""
    size_gb: float = 0.0
    params: str = ""
    min_vram_gb: float = 0.0
    associated_features: list[str] = []
    status: ModelStatus = ModelStatus.NOT_INSTALLED
    file_path: str = ""
    sha256: str = ""


class ModelImportRequest(BaseModel):
    path: str


class ModelSelectRequest(BaseModel):
    feature: str  # dialog/paint/video/voice
    model_id: str


# ── 对话 ──────────────────────────────────────────────────────────

class DialogSendRequest(BaseModel):
    session_id: str
    content: str
    attachments: list[dict] | None = None


class DialogSessionCreate(BaseModel):
    title: str = "新对话"
    model: str | None = None


class DialogMessage(BaseModel):
    id: str
    role: str  # user | assistant
    content: str
    attachments: list[dict] | None = None
    timestamp: float
    model_used: str = ""


# ── 绘画 ──────────────────────────────────────────────────────────

class DrawRequest(BaseModel):
    prompt: str
    negative_prompt: str = ""
    width: int = Field(default=1024, ge=512, le=2688)
    height: int = Field(default=1024, ge=512, le=2688)
    steps: int = Field(default=20, ge=4, le=50)
    guidance_scale: float = Field(default=7.5, ge=1.0, le=20.0)
    model: str | None = None
    controlnet: dict | None = None
    lora: list[dict] | None = None
    seed: int = -1
    batch_size: int = Field(default=1, ge=1, le=4)


class DrawResponse(BaseModel):
    images: list[str]  # base64
    model_used: str
    generation_time_ms: int
    seed: int


# ── 漫剧：分镜表 ─────────────────────────────────────────────────

class StoryboardRow(BaseModel):
    id: str
    shot_number: int
    original_dialogue: str = ""
    description: str = ""
    characters: list[str] = []
    scene: str = ""
    props: list[str] = []
    voice_id: str = ""
    voice_emotion: str = "默认"
    director_stage_done: bool = False
    generation_status: GenerationStatus = GenerationStatus.PENDING
    is_ai_generated: bool = False
    sort_index: int = 0        # R2-B06 拖拽排序位序（升序展示）


class StoryboardCreate(BaseModel):
    project_id: str


class StoryboardRowUpdate(BaseModel):
    original_dialogue: str | None = None
    description: str | None = None
    characters: list[str] | None = None
    scene: str | None = None
    props: list[str] | None = None
    voice_id: str | None = None
    voice_emotion: str | None = None
    is_ai_generated: bool | None = None
    sort_index: int | None = None   # R2-B06 支持单行拖拽落位
    # 批 1.3 导演字段（枚举/范围校验在路由层，模型层放行 Optional）
    camera_type: str | None = None      # 8 枚举
    camera_angle: str | None = None     # 5 枚举
    camera_movement: str | None = None  # 9 枚举
    duration: float | None = None       # 1~60s
    transition: str | None = None       # 6 枚举
    speed: float | None = None          # 0.5~2.0
    volume: float | None = None         # -12~0 dB
    music_path: str | None = None
    asset_id: str | None = None
    # 竞品对齐改造：多资产绑定 + 行锁定
    asset_ids: list[str] | None = None  # 多资产 id 列表（写库序列化为 JSON）
    is_locked: bool | None = None       # 行锁定：批量操作跳过


class AiDescribeRequest(BaseModel):
    """AI 画面描述请求（R2-B07）：row_id 与 dialogue 至少其一。

    prompt_prefix 有值时拼接到内置提示词模板前部，不改变默认行为。
    model_override 有值时覆盖默认对话模型路由（G2 工序弹窗）。
    """
    row_id: str | None = None
    dialogue: str | None = None
    project_id: str | None = None
    prompt_prefix: str | None = Field(default=None, max_length=500)
    model_override: str | None = None   # G2：覆盖默认模型


# ── 漫剧：项目 CRUD / 资产 / 关键帧 / DSL 上传（修复任务清单 批 1）──────

class WorkMode(str, Enum):
    REGULAR = "regular"      # 普通漫剧 5 步
    NARRATIVE = "narrative"  # 解说漫剧 6 步


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    template: str | None = None          # comic_drama=漫剧模板（预置 5 分镜）
    project_id: str | None = None        # 指定 id（缺省自动生成）
    work_mode: WorkMode = Field(default=WorkMode.REGULAR)  # 作品类型
    art_style: str = Field(default="", max_length=40)      # 预置画风 key（空=未选择）
    project_type: str = Field(default="manga", max_length=20)  # manga(漫剧) | comic(漫画页)

    @field_validator("project_id")
    @classmethod
    def _safe_pid(cls, v: str | None) -> str | None:
        # 指定 id 会成为 comic_assets 落盘目录名——同样过路径安全校验
        return _check_safe_name(v) if v else v

    @field_validator("project_type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        # 产品面类型白名单：漫剧库与漫画页共用生成底座，只区分前端入口
        if v not in ("manga", "comic"):
            raise ValueError("project_type 仅支持 manga/comic")
        return v


class ProjectUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    # 漫画页 M1「换风格重生成」：项目级画风可在运行中切换（关键帧生成
    # 时经 _project_style_pack 实时读取，改完即对后续生成生效）
    art_style: str | None = Field(default=None, max_length=40)


class ProjectBatchDelete(BaseModel):
    """批量删除项目请求体（COMIC-004 扩展：一次最多 500 个）。"""
    project_ids: list[str] = Field(min_length=1, max_length=500)


class ArtStyleCreate(BaseModel):
    """自定义作品风格创建（2026-08-24：新建作品选画风，预置之外可自定义）。

    2026-08-31：pack_def 必填——自定义风格必须导入风格包 JSON
    （底座偏好/后处理档位/风格词块），纯文本自定义不再接受。
    """
    name: str = Field(min_length=1, max_length=30)
    prompt: str = Field(default="", max_length=500)   # 生图提示词关键词
    pack_def: str = Field(default="", max_length=4000)  # 风格包 JSON 原文


class SessionBatchDelete(BaseModel):
    """批量删除对话会话请求体（2026-08-20：单批最多 100，
    前端超量自动分批提交）。"""
    session_ids: list[str] = Field(min_length=1, max_length=100)


# ── G1 解说漫剧：故事级生词/生图/视频生词 ──────────────────────────────

class StoryNarrativeRequest(BaseModel):
    """故事生词（解说漫剧第 3 步）：跨分镜聚合生成连贯描述词。"""
    project_id: str
    row_ids: list[str] = Field(default_factory=list)   # 空=全部
    scope: str = "all"                                  # all | missing
    model_override: str | None = None
    prompt_prefix: str | None = Field(default=None, max_length=500)


class StoryKeyframeRequest(BaseModel):
    """故事生图（解说漫剧第 4 步）：跨分镜一致性风格图。"""
    project_id: str
    row_ids: list[str] = Field(default_factory=list)
    scope: str = "all"
    model_override: str | None = None
    resolution: str = "2560x1440"                       # 2560x1440 | 1024x1024 | 1024x576 | 576x1024


class VideoNarrativeRequest(BaseModel):
    """视频生词（解说漫剧第 5 步）：为视频生成写专属描述词。"""
    project_id: str
    row_ids: list[str] = Field(default_factory=list)
    scope: str = "all"
    model_override: str | None = None
    prompt_prefix: str | None = Field(default=None, max_length=500)


class AssetGenerateRequest(BaseModel):
    project_id: str
    name: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=2000)
    # 出图统一规格（2026-08-14 铁律）：资产图 2560×1440（16:9）
    width: int = Field(default=2560, ge=256, le=2560)
    height: int = Field(default=1440, ge=256, le=2560)
    transparent: bool = False               # 道具：透明背景 PNG

    @field_validator("name", "project_id")
    @classmethod
    def _safe_name(cls, v: str) -> str:
        return _check_safe_name(v)


class AssetBatchGenerateRequest(BaseModel):
    project_id: str
    kind: str = "character"                 # character/scene/prop
    items: list[dict]                       # [{name, prompt, ...}]


class AssetTurnaroundRequest(BaseModel):
    """角色多视图（四视图）生成请求（规格：2560×1440 横排 4 格）。

    正面/侧面/背面/特写四格一次成图，后端自动裁切、做色调一致性
    校验并入库 portrait.png + portrait_views/ 目录结构。
    """
    project_id: str
    name: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=2000)
    seed: int = -1
    transparent: bool = False               # 四视图一键去背（PIL 降级）


class AssetRegenerateViewRequest(BaseModel):
    """四视图资产单视图重生请求（竞品对齐：每张视图可单独重生）。

    view 为目标视图；prompt 缺省时沿用资产现有描述词。
    """
    view: Literal["front", "side", "back", "closeup"]
    prompt: str | None = Field(default=None, max_length=2000)


class AssetBindRequest(BaseModel):
    asset_id: str
    row_id: str                             # 分镜行 id


class AssetAdoptRequest(BaseModel):
    """资产库资产引入项目（竞品「全部可用角色」对齐）。"""
    asset_id: str                           # 资产库中的源资产 id
    project_id: str                         # 引入的目标项目 id


class AssetUpdateRequest(BaseModel):
    """资产元信息更新（竞品对齐）：仅更新非 None 字段。"""
    name: str | None = Field(default=None, max_length=100)
    prompt: str | None = Field(default=None, max_length=2000)


class AssetInferRequest(BaseModel):
    """从分镜行推断实体资产桩请求（竞品对齐）。"""
    project_id: str


class KeyframeGenerateRequest(BaseModel):
    row_id: str
    project_id: str | None = None
    prompt: str | None = None            # 缺省用分镜行 description
    # 出图统一规格（2026-08-14 铁律）：分镜图 2560×1440（16:9）
    width: int = Field(default=2560, ge=256, le=2560)
    height: int = Field(default=1440, ge=256, le=2560)
    # V37 seed 固定兜底（2026-08-27）：显式指定基础种子复用已验证
    # 结果；None = 沿用该行最近版本的已存 seed（无则随机）。逐镜派生
    # base_seed+shot_index，跨镜既有共享分量又有镜间差异
    seed: int | None = Field(default=None, ge=0)
    # 显式强制重抽（忽略已存 seed 与 VLM 重试沿用）——v36 抽卡失败
    # 场景用户主动点「重新生成」时前端置 True
    force_new_seed: bool = False
    # P2-B 逐镜重抽（2026-08-28 暴露到端点）：仅重生成指定镜号
    #（1-based），其余镜复用当前版本已落盘首帧与 seed——整版重抽
    # 会给已达标镜引入新方差（V48 教训）；None=整版生成（原行为）
    only_shots: list[int] | None = None
    # 竞品文本优先模式（2026-08-29 竞品对齐实验）：True 时跳过 C 段
    # 道具颜色词/角色外貌词剥除（_strip_prop_colors/_strip_char_appearance）
    # ——竞品样张实测为「描述词文本赢过资产图」（高马尾/白衬衫/白色
    # 行李箱均按 C 段原文渲染，尽管资产图为披肩发/黑色行李箱）。默认
    # False 保持项目学说（资产图为准，v12/v22 事故裁定）；手部泛红
    # 剥除（v64 渲染缺陷修复）不受此开关影响，恒生效。
    text_priority: bool = False
    # 推理后端选路（2026-08-27 双链路对比）：diffusers=本地
    # paint_engine；comfy=ComfyUI 子进程工作流（与 H3 共用 8189
    # 实例，与 diffusers 显存互斥——选 comfy 时先卸载本地管线，
    # 反之亦然）；auto=None（P2-C 2026-08-28 默认）=PuLID 就绪且
    # 有角色资产绑定时自动 comfy+PuLID 身份注入（质量优先，
    # ~110s/镜），否则回落 diffusers；显式 diffusers 可指定快链路
    engine: str | None = Field(
        default=None, pattern="^(diffusers|comfy|auto)$")


class KeyframeBatchRequest(BaseModel):
    row_ids: list[str]
    project_id: str | None = None
    model_override: str | None = None   # G2：覆盖默认绘画模型
    resolution: str = "2560x1440"          # G2：分辨率 2560x1440|1024x1024|1024x576|576x1024


class SceneObjectUpdate(BaseModel):
    project_id: str
    object_id: str
    name: str | None = None
    position: dict | None = None
    rotation: dict | None = None
    scale: dict | None = None


class EmotionDetectRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class TextTo3DRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    project_id: str | None = None


# ── 漫剧：导演台 ─────────────────────────────────────────────────

class PanoramaRequest(BaseModel):
    scene_id: str
    resolution: int = Field(default=2048, ge=1024, le=8192)


class ScreenshotRequest(BaseModel):
    scene_id: str
    camera_ids: list[str]  # 必须恰好4个


class CharacterPositionUpdate(BaseModel):
    character_id: str
    position: dict  # {x, y, z}
    rotation: dict | None = None
    scale: float | None = None


class CameraAdd(BaseModel):
    name: str
    position: dict
    rotation: dict
    fov: int = 60


class CameraUpdate(BaseModel):
    name: str | None = None
    position: dict | None = None
    rotation: dict | None = None
    fov: int | None = None


class CharacterLock(BaseModel):
    character_id: str


# ── 漫剧：视频生成 ───────────────────────────────────────────────

class VideoGenerateRequest(BaseModel):
    storyboard_row_id: str
    description: str
    screenshot_4in1: str  # base64
    character_assets: list[str] = []
    audio_path: str | None = None
    resolution: str = "1080p"  # 720p/1080p/2k/4k
    fps: int = Field(default=24, ge=1, le=48)
    duration_seconds: float = Field(default=5.0, ge=1, le=20)
    codec: str = "h264"  # h264/h265/vp9/av1
    model_override: str | None = None
    # STYLE-026：风格 LoRA 挂载（训练成果应用到视频生成管线）。
    # 当前 Ken Burns 降级管线接收并落库/回显，LTX-2 就绪后实际生效。
    style_lora_version: str | None = None
    style_strength: float = Field(default=1.0, ge=0.0, le=1.0)


class VideoGenResult(BaseModel):
    id: str
    file_path: str
    model_used: str
    duration_seconds: float
    resolution: str
    generation_time_ms: int
    has_audio_sync: bool


# ── 漫剧：音色 ───────────────────────────────────────────────────

class VoiceProfile(BaseModel):
    id: str
    name: str
    character_id: str
    is_preset: bool = False


class VoiceBindRequest(BaseModel):
    character_id: str
    voice_id: str


class VoiceEmotionUpdate(BaseModel):
    emotion_label: str


class VoicePreviewRequest(BaseModel):
    voice_id: str
    text: str
    emotion: str = "默认"


# ── 知识学习 ─────────────────────────────────────────────────────

class TrainTaskCreate(BaseModel):
    """LoRA 训练任务创建请求。

    审计 P0-6 超参边界（硬件安全）：
      - epochs 1~50：过多 epoch 导致 GPU 长时间满载过热
      - lora_rank 4~64：rank 过大显存爆炸（16GB 基线约束）
      - learning_rate (0, 1e-3]：过大 lr 训练发散且浪费算力
    越界直接 422 由 RequestValidationError 处理器转 40004。
    """
    base_model: str
    lora_rank: int = Field(default=16, ge=4, le=64)
    lora_alpha: int = 32
    learning_rate: float = Field(default=1e-4, gt=0, le=1e-3)
    epochs: int = Field(default=3, ge=1, le=50)
    # 可选：留空时自动从知识库+行为偏好构建训练集（见 learn.py learn_train 文档）
    dataset_path: str = ""


class TrainTask(BaseModel):
    id: str
    base_model: str
    lora_rank: int = 16
    lora_alpha: int = 32
    learning_rate: float = 1e-4
    epochs: int = 3
    dataset_path: str
    status: TrainStatus = TrainStatus.QUEUED
    progress: float = 0.0


# ── 系统 ─────────────────────────────────────────────────────────

class SystemSettings(BaseModel):
    theme: str = "sakura"
    font_size: int = 14
    auto_model_select: bool = True
    default_video_codec: str = "h264"
    default_resolution: str = "1080p"
    # 界面打开方式（2026-09-08 桌面壳接线）：shell=原生桌面窗口
    # （pywebview，默认）/ browser=系统浏览器。boot 启动链读取决定
    # 拉壳还是开浏览器；本次会话不热切，下次启动生效。
    launch_mode: str = "shell"
    # 界面效果（2026-09-08 UI 降载方案②）：full=完整动效（默认）/
    # lite=性能模式（关动态粒子/流光/模糊，AI 生成期间更稳）。
    # 前端 html.ui-lite 全局类驱动，即时生效。
    ui_performance: str = "full"
    # 远程推理服务器（批3 D3 2026-09-05）：对话由远端专业卡服务器
    # （Linux vLLM，OpenAI 兼容 API）承载，本地零显存。api_key 可选。
    remote_dialog_enabled: bool = False
    remote_dialog_base_url: str = ""
    remote_dialog_api_key: str = ""
    remote_dialog_model: str = ""
    remote_dialog_timeout_s: float = 300.0


class ProjectExport(BaseModel):
    project_id: str


class ProjectImport(BaseModel):
    file_path: str
