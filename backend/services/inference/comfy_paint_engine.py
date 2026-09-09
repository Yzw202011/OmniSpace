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

logger = logging.getLogger("omnispace.inference.comfy_paint")

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

# PuLID-Flux2 身份注入（2026-08-28 P1）：klein 原生权重 v2
# （iFayens/ComfyUI-PuLID-Flux2 节点 + Fayens/Pulid-Flux2 权重 +
# antelopev2 人脸分析；EVA-CLIP 首跑经 open_clip 自动下载）
_PULID_FILE = "pulid_flux2_klein_v2.safetensors"

# 每步每百万像素耗时（2026-08-27 实测：1280x720=0.92MP @36步 euler
# fp8 90s/镜 → ~2.7 s/步/MPix @ RTX 5070 Ti 16GB）
_ETA_SEC_PER_STEP_PER_MPIX = 2.7
_STARTUP_TIMEOUT_S = 150.0          # 冷启动含 torch import
_TASK_TIMEOUT_MARGIN_S = 240.0      # 任务超时 = ETA + 固定余量（含模型加载）

# ReferenceLatent 链上限（2026-08-28 P1 多参考：FLUX.2 官方多参考
# 上限 = 用户口径 8 张；调用方序 = 重要性序，超限按序截断）
_MAX_WF_REFS = 8


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


def comfy_paint_available() -> bool:
    """ComfyUI 绘画管线是否就绪（便携版 + 三件权重硬链接齐全）。"""
    if not (_COMFY_PY.is_file() and _COMFY_MAIN.is_file()):
        return False
    checks = [
        _COMFY_MODELS / "diffusion_models" / _PAINT_FILES["unet"],
        _COMFY_MODELS / "text_encoders" / _PAINT_FILES["clip"],
        _COMFY_MODELS / "vae" / _PAINT_FILES["vae"],
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
            pass
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
                        ref_megapixels: list[float] | None = None) -> dict:
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
        steps = int(params.get("steps") or 36)
        cfg = float(params.get("cfg") or 4.0)
        width = int(params.get("width") or 1280)
        height = int(params.get("height") or 720)
        seed = int(params.get("seed") or 0)
        lora_name = str(params.get("lora_name") or "")
        lora_scale = float(params.get("lora_scale") or 1.0)

        wf: dict[str, dict] = {
            "unet": {"class_type": "UNETLoader", "inputs": {
                "unet_name": _PAINT_FILES["unet"],
                "weight_dtype": "default"}},
            "clip": {"class_type": "CLIPLoader", "inputs": {
                "clip_name": _PAINT_FILES["clip"],
                "type": "flux2", "device": "default"}},
            "vae": {"class_type": "VAELoader", "inputs": {
                "vae_name": _PAINT_FILES["vae"]}},
            "noise": {"class_type": "RandomNoise", "inputs": {
                "noise_seed": seed}},
            "sampler": {"class_type": "KSamplerSelect", "inputs": {
                "sampler_name": str(params.get("sampler") or "euler")}},
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
                ref_megapixels: list[float] | None = None) -> dict:
        """参考条件生图（ReferenceLatent 注入，非像素初始化 img2img）。

        ref_image: 构图/环境参考图（P1 多参考：传 PIL 图列表即多图
        链式注入，≤_MAX_WF_REFS，列表序 = 重要性序，超限按序截断）。
        pulid_image: 身份参考图（恒建议面部特写资产——InsightFace
        检测稳定，与 ReferenceLatent 的构图参考职责互补）。
        ref_megapixels: 逐图分辨率预算（D-2 2026-09-10：角色参考
        1.0MP 保一致性锚定力、场景/道具低档控 token 总量；不传则
        走 _ref_megapixels 均匀分档旧行为）。
        """
        return self._run(params, ref_image, pulid_image,
                         ref_megapixels=ref_megapixels)

    def _run(self, params: dict,
             ref_image: Image.Image | None,
             pulid_image: Image.Image | None = None,
             ref_megapixels: list[float] | None = None) -> dict:
        # 生成期间标记忙碌：空闲自动关闭计时暂停（mark/mark_idle 配对）
        proc_mgr = get_comfy_proc()
        proc_mgr.mark_busy()
        try:
            with self._gen_lock:
                return self._run_locked(params, ref_image, pulid_image,
                                        ref_megapixels=ref_megapixels)
        finally:
            proc_mgr.mark_idle()

    def _run_locked(self, params: dict,
                    ref_image: Image.Image | None,
                    pulid_image: Image.Image | None = None,
                    ref_megapixels: list[float] | None = None) -> dict:
        task_id = uuid.uuid4().hex[:12]
        t0 = time.perf_counter()

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
            logger.warning("参考图超上限 %d > %d，按序截断",
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

        try:
            self._ensure_running()
            wf = self._build_workflow(params, ref_names,
                                      filename_prefix=f"paint/{task_id}",
                                      pulid_image_name=pulid_name,
                                      ref_megapixels=ref_megapixels)
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
            logger.info("绘画任务已提交 (prompt_id=%s, %dx%d, %d步, lora=%s@%.2f, refs=%d/%s, pulid=%s@%.2f)",
                        prompt_id, int(params.get("width") or 1280),
                        int(params.get("height") or 720),
                        int(params.get("steps") or 36),
                        params.get("lora_name") or "-",
                        float(params.get("lora_scale") or 0.0),
                        len(ref_names), ref_mode,
                        "Y" if pulid_name else "-",
                        float(params.get("pulid_strength") or 0.0))

            images = self._poll_history(prompt_id, params)
            logger.info("ComfyUI 绘画完成: %d 图, %.1fs",
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
                        pass

    def _poll_history(self, prompt_id: str,
                      params: dict) -> list[Image.Image]:
        """轮询 /history 至完成，产物读出为 PIL（源文件随即清除）。"""
        steps = int(params.get("steps") or 36)
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
                        pass
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
