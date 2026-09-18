"""ComfyUI 绘画工作流引擎：ComfyUI 子进程 + HTTP API 桥接（2026-08-27）。

定位：paint_engine 的并列推理后端（接口对齐 generate/img2img →
{"images": [PIL.Image]}），承接一致性模式生图（gen_router 选路）。
扩散执行委托给 ComfyUI headless 服务（/prompt API）；A/B/C 提示词
协议、seed 策略、资产绑定、后处理仍在我方层（调用方）完成。

与 H3 视频共用同一个 ComfyUI 子进程（port 8189，串行互斥——跨引擎
互斥由 feature_lock/model_manager 调用侧裁决，本引擎 _gen_lock 串行
自身任务）。权重经硬链接挂接 ComfyUI/models 标准目录（零磁盘副本）。

与 paint_engine 的关键差异：ComfyUI 的 LoRA 是工作流内节点而非引擎
状态，每次生成可独立指定 lora_name/lora_scale，无需 attach/detach。
"""
from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

from PIL import Image

from ...config import ROOT_DIR
from ...middleware.error_handler import ApiError
from .comfy_proc import COMFY_INPUT_DIR as _COMFY_INPUT
from .comfy_proc import COMFY_OUTPUT_DIR as _COMFY_OUTPUT
from .comfy_proc import get_comfy_proc

log = logging.getLogger("omnispace.inference.comfy_paint")

# ── ComfyUI 便携版落位（与 H3 同一实例）────────────────────────
_COMFY_DIR = ROOT_DIR / "tools" / "ComfyUI_windows_portable"
_COMFY_PY = _COMFY_DIR / "python_embeded" / "python.exe"
_COMFY_MAIN = _COMFY_DIR / "ComfyUI" / "main.py"
_COMFY_PORT = int(os.environ.get("OMNISPACE_COMFYUI_PORT", "8189"))
_COMFY_BASE = f"http://127.0.0.1:{_COMFY_PORT}"
_COMFY_MODELS = _COMFY_DIR / "ComfyUI" / "models"
# 输入/输出目录 2026-09-02 起统一收编 data/comfyui（单源 comfy_proc，
# 与子进程启动参数 --input/--output-directory 同源，引擎树内不再落产物）

# 绘画权重（硬链接挂接于 ComfyUI/models 标准目录；源仓
# models/paint/flux2-klein-9b*，TE 为 diffusers 4 分片流式合并产物）
_PAINT_FILES = {
    "unet": "flux-2-klein-9b-fp8.safetensors",
    "clip": "qwen3_8b.safetensors",
    "vae": "flux2-vae.safetensors",
}

# Z-Image-Turbo 权重族（2026-09-15 Z1 接入，Apache-2.0 可随包发行）。
# TE 与 klein-4b 同源（Qwen3-4B comfy 格式；CLIPLoader type=lumina2
# 且非 flux 分支时 sd.py 按 TEModel.QWEN3_4B 走 z_image TE）——零额外下载。
# 实测（tools/scratch/zimg_verify.py）：四视图 720p 14.1s / 512² 6.1s，
# 中文字渲染 klein 不可比；参数矩阵（zimg_matrix2）证官方基线最优。
_Z_IMAGE_FILES = {
    "unet": "z_image_turbo_bf16.safetensors",
    "clip": "qwen_3_4b.safetensors",
    "vae": "z_image_ae.safetensors",
}
_Z_IMAGE_MODEL_ID = "z-image-turbo"
# TextEncodeZImageOmni 原生参考条件上限（image1~3）
_Z_IMAGE_MAX_REFS = 3

# PuLID-Flux2 身份注入（2026-08-28 P1）：klein 原生权重 v2
# （iFayens/ComfyUI-PuLID-Flux2 节点 + Fayens/Pulid-Flux2 权重 +
# antelopev2 人脸分析；EVA-CLIP 首跑经 open_clip 自动下载）
_PULID_FILE = "pulid_flux2_klein_v2.safetensors"

# ── sampler 名跨栈别名（B2 实弹验收 2026-09-14 揪出的既有缺陷）──────
# draw 链默认 DEFAULT_SAMPLER="euler_a"（paint_engine.py legacy diffusers
# 常量）会随请求传入 comfy 工作流，而 ComfyUI 0.34 的 KSamplerSelect
# 列表（44 项）无 euler_a（新版名 euler_ancestral）→ 工作流校验失败、
# 绘画页默认参数生图全断。显式映射 legacy 名 → ComfyUI 标准名；
# 未知名直传（由 ComfyUI 校验兜底）。
_COMFY_SAMPLER_ALIAS = {
    "euler_a": "euler_ancestral",
}


def _comfy_sampler_name(raw: str) -> str:
    return _COMFY_SAMPLER_ALIAS.get(raw, raw)

# 每步每百万像素耗时（2026-08-27 实测：1280x720=0.92MP @36步 euler
# fp8 90s/镜 → ~2.7 s/步/MPix @ RTX 5070 Ti 16GB）
_ETA_SEC_PER_STEP_PER_MPIX = 2.7
_STARTUP_TIMEOUT_S = 150.0          # 冷启动含 torch import
_TASK_TIMEOUT_MARGIN_S = 240.0      # 任务超时 = ETA + 固定余量（含模型加载）

# ReferenceLatent 链上限（2026-08-28 P1 多参考：FLUX.2 官方多参考
# 上限 = 用户口径 8 张；调用方序 = 重要性序，超限按序截断）
_MAX_WF_REFS = 8


def _preset_name(params: dict) -> str:
    """绘画步数/CFG 档位（批1a 2026-09-12；2026-09-16 拍板扩三档）：
    fast = klein 蒸馏系原生 4 步 + cfg1.0（叠 SageAttention = 双重
    加速）；balanced = 8 步 / cfg4.0（**默认档，2026-09-16 拍板**：
    A/B 实证 8 步与 36 步目验平齐 8.2 分、快 4.2×）；quality = 历史
    36 步 / cfg4.0 原样。调用方显式传 steps/cfg 时优先级最高（不破坏
    既有调用与单测）；config paint.preset 门控。"""
    preset = str(params.get("preset") or "").strip().lower()
    if preset in ("fast", "quality", "balanced"):
        return preset
    try:
        from src.config import get_config
        _cfg = str((get_config().get("paint") or {}).get(
            "preset", "balanced")).strip().lower()
        return _cfg if _cfg in ("fast", "quality", "balanced") else "balanced"
    except Exception:  # noqa: BLE001 - 配置异常按 balanced 处理
        return "balanced"


def _fast_preset(params: dict) -> bool:
    """fast 档判定（保留原签名——test_keyframe_multiref 的 shim 提取
    依赖此名）。"""
    return _preset_name(params) == "fast"


def _effective_steps_cfg(params: dict) -> tuple[int, float]:
    """档位感知的有效 (steps, cfg)：fast = 4 步 / cfg1.0；balanced =
    8 步 / cfg4.0（默认，2026-09-16 拍板）；quality = 36 步 / cfg4.0
    （历史口径）；调用方显式传值优先。所有消费 steps/cfg 默认值的
    点位（工作流构造 / 日志 / ETA 超时）统一走此函数，避免档位间
    口径漂移。"""
    defaults = {"fast": (4, 1.0), "balanced": (8, 4.0),
                "quality": (36, 4.0)}[_preset_name(params)]
    return (int(params.get("steps") or defaults[0]),
            float(params.get("cfg") or defaults[1]))


def _ref_megapixels(n_refs: int) -> float:
    """多参考逐图分辨率预算（总参考 latent token 预算≈拼图时代）。

    拼图时代整张参考 ≤1MP；多图若每图仍 1MP，8 图 = 8MP 参考
    token（采样显著变慢 + 文本信号被稀释）。按图数分档缩幅：
    1-2 图 1.0MP、3-4 图 0.5MP、5-8 图 0.35MP（总预算 1.0~2.8MP）。
    """
    if n_refs <= 2:
        return 1.0
    if n_refs <= 4:
        return 0.5
    return 0.35


def comfy_paint_available(model_id: str = "") -> bool:
    """ComfyUI 绘画管线是否就绪（便携版 + 对应引擎槽权重齐全）。

    model_id 空串/klein 系 → 查 klein 三件套；_Z_IMAGE_MODEL_ID → 查
    Z 三件套（2026-09-15 审计修复：旧实现恒查 klein 三件，Z2 gate 拿
    它当「z 可用」探测——z 权重缺失时闸仍绿，直到 ComfyUI 工作流校验
    才炸再走异常回退，白付一次冷启动）。
    """
    if not (_COMFY_PY.is_file() and _COMFY_MAIN.is_file()):
        return False
    if model_id == _Z_IMAGE_MODEL_ID:
        files = _Z_IMAGE_FILES
    else:
        files = _PAINT_FILES
    checks = [
        _COMFY_MODELS / "diffusion_models" / files["unet"],
        _COMFY_MODELS / "text_encoders" / files["clip"],
        _COMFY_MODELS / "vae" / files["vae"],
    ]
    return all(p.is_file() for p in checks)


def pulid_available() -> bool:
    """PuLID-Flux2 身份注入是否就绪（节点 + 权重 + antelopev2）。

    antelopev2 为 buffalo_l 命名套件（scrfd_10g_bnkps 检测 +
    glintr100 识别；insightface 1.0.1 按内容识别加载，不写死名）。
    """
    if not (_COMFY_DIR / "ComfyUI" / "custom_nodes" / "ComfyUI-PuLID-Flux2"
            / "pulid_flux2.py").is_file():
        return False
    if not (_COMFY_MODELS / "pulid" / _PULID_FILE).is_file():
        return False
    return (_COMFY_MODELS / "insightface" / "models" / "antelopev2"
            / "glintr100.onnx").is_file()


class ComfyPaintEngine:
    """ComfyUI 子进程托管 + Flux2 Klein 绘画工作流提交（线程安全单例）。

    生成调用发生在调用方工作线程（同步阻塞合法）；ComfyUI 进程在
    首个任务时按需冷启动（或复用外部手动起的实例）；权重驻留语义
    与 paint_engine 一致——任务后由调用方显式 unload()。
    """

    _instance: ComfyPaintEngine | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()   # 保护进程启动/停止
        self._gen_lock = threading.Lock()    # 生成串行（单任务约束）
        self._log_fp = None

    @classmethod
    def get(cls) -> ComfyPaintEngine:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ── HTTP 基础（urllib，零依赖；线程内同步调用）──────────────

    def _api(self, method: str, path: str,
             body: dict | None = None, timeout: float = 10.0) -> dict | None:
        url = f"{_COMFY_BASE}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None  # /history/{id} 未完成 —— 调用方按执行中处理
            # 非 404 HTTP 错误（如工作流校验 400+node_errors）：解析错误体
            # 交调用方转成带细节的 ApiError——不让裸 HTTPError 漏到用户
            body = self._parse_error_body(exc)
            if body is not None:
                return body
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            return None  # 进程未起/端口不通 —— 探活语义

    @staticmethod
    def _parse_error_body(exc: urllib.error.HTTPError) -> dict | None:
        """解析 HTTP 错误响应体为 dict；无体/非 JSON 返回兜底错误 dict。

        仅在非 404 分支使用——保底也返回 dict 而非 None（None=执行中语义，
        不能让错误被误判成执行中）。"""
        try:
            raw = exc.read()
            body = json.loads(raw) if raw else {}
            if isinstance(body, dict):
                return body
        except Exception:  # noqa: BLE001 - 错误体不是 JSON 时走兜底
            log.debug("_parse_error_body: 降级忽略", exc_info=True)
        return {"error": {"message": f"HTTP {exc.code} {exc.reason}"}}

    # ── 进程生命周期 ──────────────────────────────────────────────

    def is_alive(self) -> bool:
        """ComfyUI 端口是否可服务（含外部手动起的实例复用）。"""
        return self._api("GET", "/object_info", timeout=5.0) is not None

    def _spawn(self) -> None:
        """冷启动委托统一进程管理器（2026-08-31 治理）。

        单一所有者 + Job Object 共生死 + 空闲自动关闭；
        --deterministic / HF 镜像 / ffmpeg PATH 前置等启动参数
        统一收敛在 comfy_proc.py（两引擎共用同一实例）。
        """
        self._proc = get_comfy_proc().spawn("comfyui_paint.log")

    def ensure_running(self) -> None:
        """公开预热入口（W3-C 2026-09-13，/models/warmup feature=paint
        comfy 档消费）：确保 ComfyUI 服务可用（幂等，复用探测）。"""
        self._ensure_running()

    def _ensure_running(self) -> None:
        """确保 ComfyUI 服务可用（复用探测 + 单次冷启动等待）。"""
        if self.is_alive():
            return
        with self._proc_lock:
            if self.is_alive():
                return
            if self._proc is not None and self._proc.poll() is None:
                self._wait_ready(_STARTUP_TIMEOUT_S)
                return
            if not comfy_paint_available():
                raise ApiError(
                    code=60003,
                    message="ComfyUI 绘画管线未就绪（便携版或权重缺失）",
                    suggestion="请确认 tools/ComfyUI_windows_portable 与 "
                               "ComfyUI/models 绘画权重硬链接完整",
                )
            self._spawn()
            self._wait_ready(_STARTUP_TIMEOUT_S)

    def _wait_ready(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.is_alive():
                return
            if self._proc is not None and self._proc.poll() is not None:
                raise ApiError(
                    code=60003,
                    message=f"ComfyUI 子进程异常退出 (code={self._proc.returncode})",
                    suggestion="查看 logs/comfyui_paint.log 排查",
                )
            time.sleep(2.0)
        raise ApiError(code=60003, message="ComfyUI 启动超时",
                       suggestion="查看 logs/comfyui_paint.log 排查")

    def unload(self) -> None:
        """卸载 ComfyUI 内驻留权重（进程保留热启动）。"""
        self._api("POST", "/free",
                  body={"unload_models": True, "free_memory": True},
                  timeout=60.0)

    def shutdown(self) -> None:
        """终止 ComfyUI 子进程树（委托统一管理器，幂等——无论哪个
        引擎 spawn 的实例都可杀；外部手动起的实例不受影响）。"""
        with self._proc_lock:
            self._proc = None
        get_comfy_proc().shutdown()

    # ── 工作流构造（Flux2 Klein + 可选 LoRA + 可选参考图注入）─────

    @staticmethod
    def _resolve_reflatent(params: dict,
                           pulid_active: bool) -> str:
        """ReferenceLatent 注入模式解析（2026-08-28 P0-1）。

        模式（params["reflatent"]，缺省 "auto"）：
          both —— 正/负双侧注入（旧行为）
          pos  —— 仅正侧注入（负侧保留纯文本——v64/v65 事故：双侧
                  注入把负向瞳色锚稀释失效）
          off  —— 完全不注入（身份走 PuLID attention 硬锁，构图走
                  文本；v61/v62 链式误差复利与双 latent 通道稀释文
                  本信号的根治）
          auto —— PuLID 激活 → off；否则 both（无 PuLID 时参考图
                  是唯一一致性通道，保持旧行为）
        """
        mode = str(params.get("reflatent") or "auto").lower()
        if mode in ("both", "pos", "off"):
            return mode
        return "off" if pulid_active else "both"

    def _build_workflow(self, params: dict,
                        ref_names: list[str] | None,
                        filename_prefix: str,
                        pulid_image_name: str | None = None,
                        ref_megapixels: list[float] | None = None,
                        pulid_image_b_name: str | None = None,
                        pulid_strength_b: float = 0.0) -> dict:
        """API 格式工作流（官方 Image Edit Klein 蓝图节点级还原）。

        参考图注入链（P1 多参考）：每图 LoadImage →
        ImageScaleToTotalPixels（逐图分档预算，见 _ref_megapixels）→
        VAEEncode → ReferenceLatent——该节点向 conditioning 追加一条
        reference latent，N 图 = N 节点级联（FLUX.2 原生多参考语义，
        上限 _MAX_WF_REFS）；both 模式正/负两侧对称级联。
        PuLID 链（P1）：LoadImage(身份图) → 512² 缩放 → ApplyPuLIDFlux2
        （InsightFace embedding + EVA-CLIP → IDFormer token 注入
        attention，采样期 wrapper 挂载/卸载）。
        """
        prompt = str(params.get("prompt") or "")
        negative = str(params.get("negative") or "")
        steps, cfg = _effective_steps_cfg(params)
        width = int(params.get("width") or 1280)
        height = int(params.get("height") or 720)
        seed = int(params.get("seed") or 0)
        lora_name = str(params.get("lora_name") or "")
        lora_scale = float(params.get("lora_scale") or 1.0)

        # D-LoRA（2026-09-10）：角色 LoRA（klein-4b 架构训练）在场时
        # 底座自动切 klein-4b（unet+TE 同切——4B 的 TE 是 Qwen3-4B 而非
        # 9B 的 Qwen3-8B；LoRA 跨底座挂载会键不匹配静默失效）。身份改由
        # LoRA 权重硬锁，PuLID 链保持原逻辑（调用方在 LoRA 在场时应置
        # pulid_strength=0，避免 4B 上未验证的 PuLID patch）。
        unet_name = _PAINT_FILES["unet"]
        clip_name = _PAINT_FILES["clip"]
        if lora_name and str(params.get("lora_base") or "") == "4b":
            unet_name = "flux-2-klein-4b.safetensors"
            clip_name = "qwen_3_4b.safetensors"

        # B7+（2026-09-14）：model 参数切换底座单文件（管理员 paint 槽
        # default 或用户显式指定）——4b 组装件已在 diffusion_models，
        # 9b-fp8 为默认。只在值非默认时覆盖，不影响已有流程。
        model_id = str(params.get("model") or "").strip()
        if model_id == "flux2-klein-4b":
            unet_name = "flux-2-klein-4b.safetensors"
            clip_name = "qwen_3_4b.safetensors"
        elif model_id == "flux2-klein-9b":
            unet_name = "flux-2-klein-9b-fp8.safetensors"
            clip_name = "qwen3_8b.safetensors"

        wf: dict[str, dict] = {
            "unet": {"class_type": "UNETLoader", "inputs": {
                "unet_name": unet_name,
                "weight_dtype": "default"}},
            "clip": {"class_type": "CLIPLoader", "inputs": {
                "clip_name": clip_name,
                "type": "flux2", "device": "default"}},
            "vae": {"class_type": "VAELoader", "inputs": {
                "vae_name": _PAINT_FILES["vae"]}},
            "noise": {"class_type": "RandomNoise", "inputs": {
                "noise_seed": seed}},
            "sampler": {"class_type": "KSamplerSelect", "inputs": {
                "sampler_name": _comfy_sampler_name(
                    str(params.get("sampler") or "euler"))}},
            "sigmas": {"class_type": "Flux2Scheduler", "inputs": {
                "steps": steps, "width": width, "height": height}},
            "latent": {"class_type": "EmptyFlux2LatentImage", "inputs": {
                "width": width, "height": height, "batch_size": 1}},
            "decode": {"class_type": "VAEDecode", "inputs": {
                "samples": ["sample", 0], "vae": ["vae", 0]}},
            "save": {"class_type": "SaveImage", "inputs": {
                "images": ["decode", 0],
                "filename_prefix": filename_prefix}},
        }

        model_src = ["unet", 0]
        clip_src = ["clip", 0]
        if lora_name:
            wf["lora"] = {"class_type": "LoraLoader", "inputs": {
                "model": model_src, "clip": clip_src,
                "lora_name": lora_name,
                "strength_model": lora_scale,
                "strength_clip": 0.0}}
            model_src = ["lora", 0]
            clip_src = ["lora", 1]

        # PuLID 身份注入（P1）：LoRA 之后 patch 模型（attention 级
        # 身份 token 注入；官方工作流身份图缩至 512² 喂 EVA-CLIP）
        pulid_strength = float(params.get("pulid_strength") or 0.0)
        if pulid_image_name and pulid_strength > 0:
            wf["pulid_load"] = {"class_type": "LoadImage", "inputs": {
                "image": pulid_image_name}}
            wf["pulid_scale"] = {"class_type": "ImageScaleToTotalPixels",
                                 "inputs": {
                                     "image": ["pulid_load", 0],
                                     "upscale_method": "lanczos",
                                     "megapixels": 0.26,
                                     "resolution_steps": 1}}
            wf["pulid_model"] = {"class_type": "PuLIDModelLoader",
                                 "inputs": {"pulid_file": _PULID_FILE}}
            wf["pulid_face"] = {"class_type": "PuLIDInsightFaceLoader",
                                "inputs": {"provider": "CPU"}}
            wf["pulid_eva"] = {"class_type": "PuLIDEVACLIPLoader",
                               "inputs": {}}
            wf["pulid_apply"] = {"class_type": "ApplyPuLIDFlux2",
                                 "inputs": {
                                     "model": model_src,
                                     "pulid_model": ["pulid_model", 0],
                                     "strength": pulid_strength,
                                     "eva_clip": ["pulid_eva", 0],
                                     "face_analysis": ["pulid_face", 0],
                                     "image": ["pulid_scale", 0]}}
            model_src = ["pulid_apply", 0]
        # 双 PuLID 级联（Step B PoC 2026-09-10）：第二身份 attention
        # 注入——多人镜头 B 角色身份硬锁实验；链式 patch 语义未官方
        # 背书，PoC 验证后决定是否转正（方案 docs/多角色一致性路由方案）
        if pulid_image_b_name and pulid_strength_b > 0:
            wf["pulid_load_b"] = {"class_type": "LoadImage", "inputs": {
                "image": pulid_image_b_name}}
            wf["pulid_scale_b"] = {"class_type": "ImageScaleToTotalPixels",
                                   "inputs": {
                                       "image": ["pulid_load_b", 0],
                                       "upscale_method": "lanczos",
                                       "megapixels": 0.26,
                                       "resolution_steps": 1}}
            wf["pulid_apply_b"] = {"class_type": "ApplyPuLIDFlux2",
                                   "inputs": {
                                       "model": model_src,
                                       "pulid_model": ["pulid_model", 0],
                                       "strength": pulid_strength_b,
                                       "eva_clip": ["pulid_eva", 0],
                                       "face_analysis": ["pulid_face", 0],
                                       "image": ["pulid_scale_b", 0]}}
            model_src = ["pulid_apply_b", 0]

        wf["pos"] = {"class_type": "CLIPTextEncode", "inputs": {
            "clip": clip_src, "text": prompt}}
        wf["neg"] = {"class_type": "CLIPTextEncode", "inputs": {
            "clip": clip_src, "text": negative}}

        pos_src = ["pos", 0]
        neg_src = ["neg", 0]
        reflatent = self._resolve_reflatent(params, pulid_image_name is not None)
        ref_names = ref_names or []
        if ref_names and reflatent != "off":
            uniform_mp = _ref_megapixels(len(ref_names))
            for i, name in enumerate(ref_names):
                # D-2（2026-09-10）：逐图分辨率——调用方按 kind 给角色
                # 参考 1.0MP（一致性锚定主力），None 走均匀分档
                mp = uniform_mp
                if (ref_megapixels and i < len(ref_megapixels)
                        and ref_megapixels[i] is not None):
                    mp = ref_megapixels[i]
                suffix = "" if i == 0 else str(i)
                wf[f"load_img{suffix}"] = {"class_type": "LoadImage",
                                           "inputs": {"image": name}}
                wf[f"scale_img{suffix}"] = {
                    "class_type": "ImageScaleToTotalPixels",
                    "inputs": {
                        "image": [f"load_img{suffix}", 0],
                        "upscale_method": "nearest-exact",
                        "megapixels": mp,
                        "resolution_steps": 1}}
                wf[f"ref_encode{suffix}"] = {"class_type": "VAEEncode",
                                             "inputs": {
                                                 "pixels": [f"scale_img{suffix}", 0],
                                                 "vae": ["vae", 0]}}
                wf[f"ref_pos{suffix}"] = {"class_type": "ReferenceLatent",
                                          "inputs": {
                                              "conditioning": pos_src,
                                              "latent": [f"ref_encode{suffix}",
                                                         0]}}
                pos_src = [f"ref_pos{suffix}", 0]
                if reflatent == "both":
                    wf[f"ref_neg{suffix}"] = {
                        "class_type": "ReferenceLatent",
                        "inputs": {"conditioning": neg_src,
                                   "latent": [f"ref_encode{suffix}", 0]}}
                    neg_src = [f"ref_neg{suffix}", 0]

        wf["guider"] = {"class_type": "CFGGuider", "inputs": {
            "model": model_src, "positive": pos_src,
            "negative": neg_src, "cfg": cfg}}
        wf["sample"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0], "guider": ["guider", 0],
            "sampler": ["sampler", 0], "sigmas": ["sigmas", 0],
            "latent_image": ["latent", 0]}}
        return wf

    def _build_workflow_z_image(self, params: dict,
                                ref_names: list[str] | None,
                                filename_prefix: str) -> dict:
        """Z-Image-Turbo 工作流族（2026-09-15 Z1，params.model=z-image-turbo）。

        参数固化官方基线（社区二开矩阵 tools/scratch/zimg_matrix2.py 实测
        无增益、cfg>1 反致卡通化/约束失守）：8 步/cfg1/res_multistep/
        simple + ModelSamplingAuraFlow(shift=3)。蒸馏模型 cfg=1 下负向
        语义无效 → 负向恒 ConditioningZeroOut（同官方模板）。参考图走
        TextEncodeZImageOmni 原生参考条件（VAE 参考隐空间，≤3 张，
        auto_resize 1MP），与 klein 的 ReferenceLatent 链不同族。
        角色 LoRA（Z4）：lora_name/lora_scale 走 LoraLoader（模型侧，
        strength_clip=0；TE 未包裹）。不支持：PuLID（flux2 专属）、inpaint。
        """
        prompt = str(params.get("prompt") or "")
        width = int(params.get("width") or 1280)
        height = int(params.get("height") or 720)
        seed = int(params.get("seed") or 0)
        refs = list(ref_names or [])[:_Z_IMAGE_MAX_REFS]

        wf: dict[str, dict] = {
            "unet": {"class_type": "UNETLoader", "inputs": {
                "unet_name": _Z_IMAGE_FILES["unet"],
                "weight_dtype": "default"}},
            "clip": {"class_type": "CLIPLoader", "inputs": {
                "clip_name": _Z_IMAGE_FILES["clip"],
                "type": "lumina2", "device": "default"}},
            "vae": {"class_type": "VAELoader", "inputs": {
                "vae_name": _Z_IMAGE_FILES["vae"]}},
        }

        pos_src = ["pos", 0]
        model_src = ["unet", 0]
        clip_src = ["clip", 0]
        # Z-Image 角色 LoRA（Z4）：底座冻结、仅模型侧适配（TE 未包裹，
        # strength_clip=0）。lora_name 相对 ComfyUI models/loras 目录。
        lora_name = str(params.get("lora_name") or "")
        lora_scale = float(params.get("lora_scale") or 1.0)
        if lora_name:
            wf["lora"] = {"class_type": "LoraLoader", "inputs": {
                "model": model_src, "clip": clip_src,
                "lora_name": lora_name,
                "strength_model": lora_scale,
                "strength_clip": 0.0}}
            model_src = ["lora", 0]
            clip_src = ["lora", 1]
        wf["ms"] = {"class_type": "ModelSamplingAuraFlow", "inputs": {
            "model": model_src, "shift": 3}}
        wf["pos"] = {"class_type": "CLIPTextEncode", "inputs": {
            "clip": clip_src, "text": prompt}}
        pos_src = ["pos", 0]
        if refs:
            # 参考隐空间必须与采样隐空间同形（Z-Image 参考是编辑语义：
            # 输出=输入分辨率；异形即 reshape 崩）→ 参考图中心裁剪
            # 精确缩放到出图尺寸，关闭节点内 1MP 自动缩放
            enc_inputs: dict = {"clip": clip_src, "prompt": prompt,
                                "vae": ["vae", 0],
                                "auto_resize_images": False}
            for i, name in enumerate(refs, start=1):
                wf[f"load_img{i}"] = {"class_type": "LoadImage",
                                      "inputs": {"image": name}}
                wf[f"scale_img{i}"] = {"class_type": "ImageScale", "inputs": {
                    "image": [f"load_img{i}", 0],
                    "upscale_method": "lanczos",
                    "width": width, "height": height, "crop": "center"}}
                enc_inputs[f"image{i}"] = [f"scale_img{i}", 0]
            wf["z_pos"] = {"class_type": "TextEncodeZImageOmni",
                           "inputs": enc_inputs}
            pos_src = ["z_pos", 0]
        wf["neg"] = {"class_type": "ConditioningZeroOut", "inputs": {
            "conditioning": pos_src}}
        wf["latent"] = {"class_type": "EmptySD3LatentImage", "inputs": {
            "width": width, "height": height, "batch_size": 1}}
        wf["sample"] = {"class_type": "KSampler", "inputs": {
            "model": ["ms", 0], "positive": pos_src,
            "negative": ["neg", 0], "latent_image": ["latent", 0],
            "seed": seed, "steps": 8, "cfg": 1.0,
            "sampler_name": "res_multistep", "scheduler": "simple",
            "denoise": 1.0}}
        wf["decode"] = {"class_type": "VAEDecode", "inputs": {
            "samples": ["sample", 0], "vae": ["vae", 0]}}
        wf["save"] = {"class_type": "SaveImage", "inputs": {
            "images": ["decode", 0], "filename_prefix": filename_prefix}}
        return wf

    # ── 对外接口（对齐 paint_engine）─────────────────────────────

    def generate(self, params: dict,
                 pulid_image: Image.Image | None = None) -> dict:
        """文生图。params: prompt/negative/steps/cfg/width/height/seed/
        lora_name/lora_scale/sampler/pulid_strength/timeout_s
        pulid_image: 身份参考图（pulid_strength>0 时经 PuLID 注入）"""
        return self._run(params, None, pulid_image)

    def img2img(self, params: dict,
                ref_image: Image.Image | list[Image.Image],
                pulid_image: Image.Image | None = None,
                ref_megapixels: list[float] | None = None,
                pulid_image_b: Image.Image | None = None) -> dict:
        """参考条件生图（ReferenceLatent 注入，非像素初始化 img2img）。

        ref_image: 构图/环境参考图（P1 多参考：传 PIL 图列表即多图
        链式注入，≤_MAX_WF_REFS，列表序 = 重要性序，超限按序截断）。
        pulid_image: 身份参考图（恒建议面部特写资产——InsightFace
        检测稳定，与 ReferenceLatent 的构图参考职责互补）。
        ref_megapixels: 逐图分辨率预算（D-2 2026-09-10：角色参考
        1.0MP 保一致性锚定力、场景/道具低档控 token 总量；不传则
        走 _ref_megapixels 均匀分档旧行为）。
        pulid_image_b: 第二身份图（Step B PoC 2026-09-10 多角色双硬
        锁实验，配合 params["pulid_strength_b"]）。
        """
        return self._run(params, ref_image, pulid_image,
                         ref_megapixels=ref_megapixels,
                         pulid_image_b=pulid_image_b)

    def _run(self, params: dict,
             ref_image: Image.Image | None,
             pulid_image: Image.Image | None = None,
             ref_megapixels: list[float] | None = None,
             pulid_image_b: Image.Image | None = None) -> dict:
        # 种子契约（draw.py：随机种子 -1 各任务独立随机）：ComfyUI
        # noise_seed 下限 0，负值进工作流前解析为真随机
        if int(params.get("seed") or 0) < 0:
            params["seed"] = random.randint(0, 2 ** 31 - 1)
        # 生成期间标记忙碌：空闲自动关闭计时暂停（mark/mark_idle 配对）
        proc_mgr = get_comfy_proc()
        proc_mgr.mark_busy()
        try:
            with self._gen_lock:
                return self._run_locked(params, ref_image, pulid_image,
                                        ref_megapixels=ref_megapixels,
                                        pulid_image_b=pulid_image_b)
        finally:
            proc_mgr.mark_idle()

    def _run_locked(self, params: dict,
                    ref_image: Image.Image | None,
                    pulid_image: Image.Image | None = None,
                    ref_megapixels: list[float] | None = None,
                    pulid_image_b: Image.Image | None = None) -> dict:
        task_id = uuid.uuid4().hex[:12]
        t0 = time.perf_counter()

        # Z-Image-Turbo 模式（Z1 2026-09-15）：PuLID 是 flux2 专属
        # patch，z 底座诚实拒绝（身份锚定走参考图条件/Z4 角色 LoRA）
        z_mode = str(params.get("model") or "") == _Z_IMAGE_MODEL_ID
        if z_mode and (pulid_image is not None
                       or pulid_image_b is not None):
            raise ApiError(
                code=60003,
                message="Z-Image 底座暂不支持 PuLID 身份锁"
                        "（PuLID 为 flux2 专属 patch）",
                suggestion="身份锚定请改用参考图条件（img2img 传参考图）"
                           "或等待 Z-Image 角色 LoRA（规划 Z4）")
        # 准入闸（2026-09-15 审计修复）：z 任务此前向 gpu-budget 申报
        # need=0.0G 裸发，19.6GB staged 全家桶靠 ComfyUI dynamic offload
        # 硬扛（首跑即 96% 显存越线+RAM 危急连环告警）。空闲低于 unet
        # 权重体积（11.5GB）时诚实早拒——宁可早拒不让装到一半死。
        if z_mode:
            try:
                import torch
                if torch.cuda.is_available():
                    _free_b, _ = torch.cuda.mem_get_info(0)
                    if _free_b < 11.5 * 1024 ** 3:
                        raise ApiError(
                            code=60003,
                            message=(f"Z-Image 需近乎空卡（unet 11.5GB 起"
                                     f"+TE/VAE 靠 offload），实测空闲仅"
                                     f" {_free_b / 1024 ** 3:.1f}GB"),
                            suggestion="关闭占显存应用后重试，或改用"
                                       " klein-9b 引擎槽")
            except ApiError:
                raise
            except Exception:  # noqa: BLE001 - 探测失败放行（ComfyUI 侧自会报）
                log.debug("_run_locked: 降级忽略", exc_info=True)

        # ReferenceLatent 模式提前解析：off 时不落参考图（省 IO，
        # 工作流不建 ref 链）
        pulid_will = (pulid_image is not None
                      and float(params.get("pulid_strength") or 0.0) > 0)
        ref_mode = self._resolve_reflatent(params, pulid_will)

        # 参考图写入 ComfyUI/input/（LoadImage 节点按名引用）；P1
        # 多参考：列表直传，超上限按序截断（调用方序 = 重要性序）
        ref_imgs: list[Image.Image] = (
            [im for im in ref_image if im is not None]
            if isinstance(ref_image, list)
            else ([] if ref_image is None else [ref_image]))
        if len(ref_imgs) > _MAX_WF_REFS:
            log.warning("参考图超上限 %d > %d，按序截断",
                           len(ref_imgs), _MAX_WF_REFS)
            ref_imgs = ref_imgs[:_MAX_WF_REFS]
        ref_names: list[str] = []
        if ref_imgs and ref_mode != "off":
            _COMFY_INPUT.mkdir(parents=True, exist_ok=True)
            for i, im in enumerate(ref_imgs):
                name = (f"paint_ref_{task_id}.png" if i == 0
                        else f"paint_ref_{task_id}_{i}.png")
                im.save(_COMFY_INPUT / name, format="PNG")
                ref_names.append(name)
        pulid_name: str | None = None
        if pulid_image is not None and float(
                params.get("pulid_strength") or 0.0) > 0:
            if not pulid_available():
                raise ApiError(
                    code=60003,
                    message="PuLID 管线未就绪（节点/权重/antelopev2 缺失）",
                    suggestion="确认 custom_nodes/ComfyUI-PuLID-Flux2 与 "
                               "models/pulid、models/insightface 挂载完整")
            pulid_name = f"paint_pulid_{task_id}.png"
            _COMFY_INPUT.mkdir(parents=True, exist_ok=True)
            pulid_image.save(_COMFY_INPUT / pulid_name, format="PNG")
        # 双 PuLID PoC（Step B）：第二身份图落盘
        pulid_b_name: str | None = None
        pulid_strength_b = float(params.get("pulid_strength_b") or 0.0)
        if pulid_image_b is not None and pulid_strength_b > 0:
            if not pulid_available():
                raise ApiError(
                    code=60003,
                    message="PuLID 管线未就绪（节点/权重/antelopev2 缺失）",
                    suggestion="确认 custom_nodes/ComfyUI-PuLID-Flux2 与 "
                               "models/pulid、models/insightface 挂载完整")
            pulid_b_name = f"paint_pulid_b_{task_id}.png"
            _COMFY_INPUT.mkdir(parents=True, exist_ok=True)
            pulid_image_b.save(_COMFY_INPUT / pulid_b_name, format="PNG")

        try:
            self._ensure_running()
            if z_mode:
                wf = self._build_workflow_z_image(
                    params, ref_names,
                    filename_prefix=f"paint/{task_id}")
            else:
                wf = self._build_workflow(params, ref_names,
                                          filename_prefix=f"paint/{task_id}",
                                          pulid_image_name=pulid_name,
                                          ref_megapixels=ref_megapixels,
                                          pulid_image_b_name=pulid_b_name,
                                          pulid_strength_b=pulid_strength_b)
            resp = self._api("POST", "/prompt",
                             body={"prompt": wf,
                                   "client_id": f"omnispace-{task_id}"},
                             timeout=15.0)
            if resp is None:
                raise ApiError(code=60003, message="ComfyUI 不可达（提交失败）")
            if resp.get("error") or resp.get("node_errors"):
                detail = json.dumps(resp.get("node_errors") or resp["error"],
                                    ensure_ascii=False)[:500]
                raise ApiError(code=60003,
                               message=f"绘画工作流校验失败: {detail}")
            prompt_id = str(resp["prompt_id"])
            _log_steps, _ = (_effective_steps_cfg(params)
                             if not z_mode else (8, 1.0))
            log.info("绘画任务已提交 (prompt_id=%s, model=%s, %dx%d, %d步, lora=%s@%.2f, refs=%d/%s, pulid=%s@%.2f)",
                        prompt_id,
                        _Z_IMAGE_MODEL_ID if z_mode else "klein-9b-fp8",
                        int(params.get("width") or 1280),
                        int(params.get("height") or 720),
                        _log_steps,
                        params.get("lora_name") or "-",
                        float(params.get("lora_scale") or 0.0),
                        len(ref_names), ref_mode,
                        "Y" if pulid_name else "-",
                        float(params.get("pulid_strength") or 0.0))

            images = self._poll_history(prompt_id, params)
            log.info("ComfyUI 绘画完成: %d 图, %.1fs",
                        len(images), time.perf_counter() - t0)
            return {"images": images, "prompt_id": prompt_id,
                    "elapsed_s": time.perf_counter() - t0,
                    "engine": "comfy"}
        finally:
            for name in (*ref_names, pulid_name):
                if name is not None:
                    try:
                        (_COMFY_INPUT / name).unlink(missing_ok=True)
                    except OSError:
                        log.debug("_run_locked: 降级忽略", exc_info=True)

    def inpaint(self, params: dict, image: Image.Image,
                mask: Image.Image) -> dict:
        """潜空间 mask 修复（W3-C 步4-c 2026-09-13）。

        配方：VAEEncode(原图) → SetLatentNoiseMask → 采样。noise_mask
        是 ComfyUI 内核采样机制（仅 mask 区注入噪声、未 mask 区每步
        从原 latent 恢复=逐像素保留）；ReferenceLatent 额外注入整图
        latent 作条件，给重绘区未遮罩上下文引导。mask 口径与 legacy
        PaintEngine.inpaint 一致（L 模式，白=重绘）。steps/cfg 不传走
        paint.preset 档；显式 ≥20 步压缩到 8 步/cfg1.0（与
        _ComfyGenAdapter 重映射同口径：蒸馏 9B 不吃 legacy 步数）。
        返回 {images, prompt_id, elapsed_s, engine:"comfy"}。
        """
        from PIL import Image as _PILImage

        if str(params.get("model") or "") == _Z_IMAGE_MODEL_ID:
            # Z-Image 工作流族暂无 inpaint 配方（SetLatentNoiseMask
            # 依赖 flux2 潜空间口径），诚实拒绝优于静默走错底座
            raise ApiError(
                code=60003,
                message="Z-Image 底座暂不支持局部重绘（inpaint）",
                suggestion="局部重绘请切 klein 底座（模型选型选 "
                           "flux2-klein-9b/4b）后重试")

        if not comfy_paint_available():
            raise ApiError("PAINT_ENGINE_NOT_READY",
                           "ComfyUI klein 出图栈不可用（便携版或权重缺失）")
        image = image.convert("RGB")
        mask = mask.convert("L")
        if mask.size != image.size:
            mask = mask.resize(image.size, _PILImage.NEAREST)  # type: ignore[attr-defined]
        w = image.size[0] // 8 * 8
        h = image.size[1] // 8 * 8
        if (w, h) != image.size:  # VAE 需 /8 对齐：居中裁
            image = image.crop(((image.size[0] - w) // 2,
                                (image.size[1] - h) // 2,
                                (image.size[0] - w) // 2 + w,
                                (image.size[1] - h) // 2 + h))
            mask = mask.crop(((mask.size[0] - w) // 2,
                              (mask.size[1] - h) // 2,
                              (mask.size[0] - w) // 2 + w,
                              (mask.size[1] - h) // 2 + h))

        p = dict(params)
        if int(p.get("steps") or 0) >= 20:
            p["steps"] = 8
            p["cfg"] = 1.0
        steps, cfg = _effective_steps_cfg(p)
        # 同 _run 种子契约：inpaint 独立建工作流，负种子在此解析
        seed = int(p.get("seed") or 0)
        if seed < 0:
            seed = random.randint(0, 2 ** 31 - 1)
        prompt = str(p.get("prompt") or "")
        negative = str(p.get("negative") or "")

        task_id = uuid.uuid4().hex[:12]
        t0 = time.perf_counter()
        _COMFY_INPUT.mkdir(parents=True, exist_ok=True)
        img_name = f"paint_ip_img_{task_id}.png"
        mask_name = f"paint_ip_mask_{task_id}.png"
        image.save(_COMFY_INPUT / img_name, format="PNG")
        mask.save(_COMFY_INPUT / mask_name, format="PNG")

        wf = {
            "unet": {"class_type": "UNETLoader", "inputs": {
                "unet_name": _PAINT_FILES["unet"],
                "weight_dtype": "default"}},
            "clip": {"class_type": "CLIPLoader", "inputs": {
                "clip_name": _PAINT_FILES["clip"],
                "type": "flux2", "device": "default"}},
            "vae": {"class_type": "VAELoader", "inputs": {
                "vae_name": _PAINT_FILES["vae"]}},
            "img_load": {"class_type": "LoadImage", "inputs": {
                "image": img_name}},
            "img_encode": {"class_type": "VAEEncode", "inputs": {
                "pixels": ["img_load", 0], "vae": ["vae", 0]}},
            "mask_load": {"class_type": "LoadImage", "inputs": {
                "image": mask_name}},
            "mask_conv": {"class_type": "ImageToMask", "inputs": {
                "image": ["mask_load", 0], "channel": "red"}},
            "mask_set": {"class_type": "SetLatentNoiseMask", "inputs": {
                "samples": ["img_encode", 0], "mask": ["mask_conv", 0]}},
        }
        # ReferenceLatent 需要 conditioning 先建——正/负条件节点：
        wf["pos"] = {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["clip", 0], "text": prompt}}
        wf["neg"] = {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["clip", 0], "text": negative}}
        wf["ref"] = {"class_type": "ReferenceLatent", "inputs": {
            "conditioning": ["pos", 0],
            "latent": ["img_encode", 0]}}
        wf["noise"] = {"class_type": "RandomNoise", "inputs": {
            "noise_seed": seed}}
        wf["sampler"] = {"class_type": "KSamplerSelect", "inputs": {
            "sampler_name": _comfy_sampler_name(
                str(p.get("sampler") or "euler"))}}
        wf["sigmas"] = {"class_type": "Flux2Scheduler", "inputs": {
            "steps": steps, "width": w, "height": h}}
        wf["guider"] = {"class_type": "CFGGuider", "inputs": {
            "model": ["unet", 0], "positive": ["ref", 0],
            "negative": ["neg", 0], "cfg": cfg}}
        wf["sample"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0], "guider": ["guider", 0],
            "sampler": ["sampler", 0], "sigmas": ["sigmas", 0],
            "latent_image": ["mask_set", 0]}}
        wf["decode"] = {"class_type": "VAEDecode", "inputs": {
            "samples": ["sample", 0], "vae": ["vae", 0]}}
        wf["save"] = {"class_type": "SaveImage", "inputs": {
            "images": ["decode", 0], "filename_prefix": f"paint_ip/{task_id}"}}

        try:
            self._ensure_running()
            resp = self._api("POST", "/prompt",
                             body={"prompt": wf,
                                   "client_id": f"omnispace-{task_id}"},
                             timeout=15.0)
            if resp is None:
                raise ApiError(code=60003, message="ComfyUI 不可达（提交失败）")
            if resp.get("error") or resp.get("node_errors"):
                detail = json.dumps(resp.get("node_errors") or resp["error"],
                                    ensure_ascii=False)[:500]
                raise ApiError(code=60003,
                               message=f"修复工作流校验失败: {detail}")
            prompt_id = str(resp["prompt_id"])
            log.info("潜空间修复已提交 (prompt_id=%s, %dx%d, %d步)",
                        prompt_id, w, h, steps)
            eta_params = {**p, "width": w, "height": h}
            images = self._poll_history(prompt_id, eta_params)
            log.info("潜空间修复完成: %.1fs",
                        time.perf_counter() - t0)
            if not images:
                raise ApiError(code=60003, message="修复采样无产物")
            # 软边回贴（与 legacy PaintEngine.inpaint 的 composite 语义
            # 对齐）：未遮罩区逐像素保留原原图，仅修复区取采样结果；
            # VAE 往返损耗（实测 ~4.8% 像素差）不进入交付。
            from PIL import ImageFilter
            out = images[0].convert("RGB")
            if out.size != image.size:
                out = out.resize(image.size, _PILImage.LANCZOS)  # type: ignore[attr-defined]
            soft = mask.filter(ImageFilter.GaussianBlur(6))
            blended = _PILImage.composite(out, image, soft)
            return {"images": [blended], "prompt_id": prompt_id,
                    "elapsed_s": time.perf_counter() - t0,
                    "engine": "comfy"}
        finally:
            for name in (img_name, mask_name):
                try:
                    (_COMFY_INPUT / name).unlink(missing_ok=True)
                except OSError:
                    log.debug("inpaint: 降级忽略", exc_info=True)

    def _poll_history(self, prompt_id: str,
                      params: dict) -> list[Image.Image]:
        """轮询 /history 至完成，产物读出为 PIL（源文件随即清除）。"""
        steps, _ = _effective_steps_cfg(params)
        width = int(params.get("width") or 1280)
        height = int(params.get("height") or 720)
        eta = (steps * (width * height / 1e6) * _ETA_SEC_PER_STEP_PER_MPIX
               + 45.0)
        timeout_s = float(params.get("timeout_s")
                          or (eta + _TASK_TIMEOUT_MARGIN_S))
        deadline = time.monotonic() + timeout_s

        while time.monotonic() < deadline:
            time.sleep(3.0)
            hist = self._api("GET", f"/history/{prompt_id}", timeout=5.0)
            if hist is None:
                continue  # 执行中（404 = 未入 history）
            entry = hist.get(prompt_id) or {}
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                messages = status.get("messages") or []
                detail = json.dumps(messages, ensure_ascii=False)[:600]
                raise ApiError(code=60003, message=f"ComfyUI 执行失败: {detail}")
            outputs = entry.get("outputs") or {}
            images: list[Image.Image] = []
            for node_out in outputs.values():
                for item in node_out.get("images") or []:
                    sub = str(item.get("subfolder") or "")
                    fn = str(item.get("filename") or "")
                    src = (_COMFY_OUTPUT / sub / fn) if sub else (
                        _COMFY_OUTPUT / fn)
                    if not src.is_file():
                        continue
                    with Image.open(src) as im:
                        images.append(im.convert("RGB"))
                    try:
                        src.unlink(missing_ok=True)
                    except OSError:
                        log.debug("_poll_history: 降级忽略", exc_info=True)
            if images:
                # 清理任务子目录（paint/<task_id> 前缀隔离）
                return images
            if status.get("completed"):
                raise ApiError(code=60003,
                               message="ComfyUI 执行完成但无图像产物")
        raise ApiError(code=60003,
                       message=f"ComfyUI 任务超时 ({timeout_s:.0f}s)")


_ENGINE: ComfyPaintEngine | None = None


def get_comfy_paint_engine() -> ComfyPaintEngine:
    return ComfyPaintEngine.get()
# 本项目仅供学习使用，商业授权请+Q 3559331368
