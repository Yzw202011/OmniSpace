"""MiniMax H3 视频引擎：ComfyUI 子进程 + HTTP API 桥接（2026-08-25）。

架构裁定（技术评估结论）：
  本地 diffusers 无 H3 支持（0.39/0.40 均未发版，H3 在 main 分支
  PR #14355）；官方仓仅 bf16（~144GB 唯一权重，16GB 卡不可行）；
  NVFP4/convrot 量化 kernel 深度绑定 ComfyUI（PR #15224）。因此
  H3 走 ComfyUI 便携版子进程：后端托管进程生命周期，经 /prompt
  API 提交工作流，轮询 /history 取产物回搬 VIDEO_OUT_DIR。

显存模型（DynamicVRAM 分时换载，2026-08-25 实测）：
  文本编码器 14.3GB Staged + DiT 11.9GB Staged 分时驻留，采样期
  单时刻峰值 ~12GB；任务完成后 POST /free 卸载全部权重（ComfyUI
  进程保留热启动，空载仅 ~0.4GB CUDA context）。

工作流（官方模板 video_minimax_h3_t2v.json 的 subgraph 节点级还原）：
  UNETLoader(NVFP4 DiT) + CLIPLoader(int4 convrot, type=minimax)
  + VAELoader×2 → MiniMaxH3ImageToVideo(可选首帧 I2V)
  → RandomNoise + KSamplerSelect(res_multistep)
  + BasicScheduler(simple, 20步) + BasicGuider
  → SamplerCustomAdvanced → VAEDecode + VAEDecodeAudio
  → CreateVideo(24fps) → SaveVideo

帧数网格：24fps 下 17k+5（n%17==5）对齐，训练范围 124~362 帧
（约 5~15 秒）。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from ...config import ROOT_DIR
from ...middleware.error_handler import ApiError
from .comfy_proc import COMFY_INPUT_DIR as _COMFY_INPUT
from .comfy_proc import COMFY_OUTPUT_DIR as _COMFY_OUTPUT
from .comfy_proc import get_comfy_proc

logger = logging.getLogger("omnispace.inference.h3")

# ── ComfyUI 便携版落位（tools/ComfyUI_windows_portable） ──────────
_COMFY_DIR = ROOT_DIR / "tools" / "ComfyUI_windows_portable"
_COMFY_PY = _COMFY_DIR / "python_embeded" / "python.exe"
_COMFY_MAIN = _COMFY_DIR / "ComfyUI" / "main.py"
_COMFY_PORT = int(os.environ.get("OMNISPACE_COMFYUI_PORT", "8189"))
_COMFY_BASE = f"http://127.0.0.1:{_COMFY_PORT}"
_COMFY_MODELS = _COMFY_DIR / "ComfyUI" / "models"
# 输入/输出目录 2026-09-02 起统一收编 data/comfyui（单源 comfy_proc，
# 与子进程启动参数 --input/--output-directory 同源，引擎树内不再落产物）

# H3 权重（硬链接挂接于 ComfyUI/models 标准目录，源仓
# Abiray/Minimax-H3-nvfp4-INT4-INT8-Convrot）
_H3_FILES = {
    "unet": "MiniMax_H3_FL2VA_pruned_nvfp4.safetensors",
    "clip": "qwen3vl_32b_minimax_h3_int4_convrot.safetensors",
    "video_vae": "minimax_h3_video_vae_fp16.safetensors",
    "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
}

# 每步每百万像素耗时（2026-08-25 实测：864x480=0.41MP @20步
# res_multistep 5.47s/it → 13.3 s/步/MPix；1344x768≈13.6s/it）
_ETA_SEC_PER_STEP_PER_MPIX = 13.5
_DEFAULT_STEPS = 20
_H3_FPS = 24
_STARTUP_TIMEOUT_S = 150.0          # 冷启动含 torch import（实测 ~20s，留余量）
_TASK_TIMEOUT_MARGIN_S = 180.0      # 任务超时 = ETA + 固定余量

# P3 显存标定（2026-08-29 E2E 实测，1344×768@20 步）：采样期需求
# ≈ 7.4 + 0.0255×帧数 GB。243 帧(10.1s) 实测 15.2GB 擦线通过；
# 373 帧(15.5s) OOM（SamplerCustomAdvanced 需 17.1GB > 16GB）。
# 按硬件总显存档位钳制单段帧数（>10s 为 H3 训练域外 + 实测 OOM）。
_SEG_FRAME_LIMITS: tuple[tuple[int, int], ...] = ((16, 243), (12, 125))

ProgressCB = Callable[[float, str], None]


def _seg_frame_limit() -> int:
    """按 GPU 总显存档位返回单段帧数上限（检测失败取最保守档）。"""
    try:
        import pynvml  # type: ignore

        pynvml.nvmlInit()
        try:
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            total_gb = pynvml.nvmlDeviceGetMemoryInfo(h).total / (1024 ** 3)
        finally:
            pynvml.nvmlShutdown()
    except Exception:  # noqa: BLE001
        return _SEG_FRAME_LIMITS[-1][1]
    for min_gb, frames in _SEG_FRAME_LIMITS:
        if total_gb >= min_gb:
            return frames
    return _SEG_FRAME_LIMITS[-1][1]


def h3_available() -> bool:
    """H3 管线是否就绪（ComfyUI 便携版 + 四件权重硬链接齐全）。"""
    if not (_COMFY_PY.is_file() and _COMFY_MAIN.is_file()):
        return False
    checks = [
        _COMFY_MODELS / "diffusion_models" / _H3_FILES["unet"],
        _COMFY_MODELS / "text_encoders" / _H3_FILES["clip"],
        _COMFY_MODELS / "vae" / _H3_FILES["video_vae"],
        _COMFY_MODELS / "vae" / _H3_FILES["audio_vae"],
    ]
    return all(p.is_file() for p in checks)


def align_h3_frames(seconds: float) -> int:
    """秒 → 24fps 帧数，向上对齐 17k+5 网格（n % 17 == 5）。"""
    n = max(5, round(seconds * _H3_FPS))
    return n + (5 - n % 17) % 17


class H3Engine:
    """ComfyUI 子进程托管 + H3 工作流提交 + 产物回搬（线程安全单例）。

    生成调用发生在 video_worker 后台线程（同步阻塞合法）；ComfyUI
    进程在首个任务时按需冷启动，任务完成后 /free 卸载权重（进程
    保留热启动），后端退出时 atexit 终止。
    """

    _instance: H3Engine | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()   # 保护进程启动/停止
        self._gen_lock = threading.Lock()    # 生成串行（16GB 单任务约束）
        self._log_fp = None

    @classmethod
    def get(cls) -> H3Engine:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ── HTTP 基础（urllib，零依赖；线程内同步调用） ──────────────

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
            # /history/{id} 未完成时返回 404 —— 调用方按"执行中"处理
            if exc.code == 404:
                return None
            # 非 404 HTTP 错误（如工作流校验 400+node_errors）：解析错误体
            # 交调用方转成带细节的 ApiError——不让裸 HTTPError 漏到用户
            body = self._parse_error_body(exc)
            if body is not None:
                return body
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            return None  # 进程未起/端口不通 —— 探活语义

    # ── 进程生命周期 ──────────────────────────────────────────────

    def is_alive(self) -> bool:
        """ComfyUI 端口是否可服务（含外部手动起的实例复用）。

        探活用 /object_info（0.33.1 实测 /system_info 已 404）；
        响应就绪同时意味着节点注册与模型目录扫描完成。
        """
        return self._api("GET", "/object_info", timeout=5.0) is not None

    def _spawn(self) -> None:
        """冷启动委托统一进程管理器（2026-08-31 治理）。

        单一所有者 + Job Object 共生死 + 空闲自动关闭；启动参数
        （--deterministic / ffmpeg PATH 前置等）统一收敛在
        comfy_proc.py（与 comfy_paint_engine 共用同一实例）。
        """
        self._proc = get_comfy_proc().spawn("comfyui_h3.log")

    def _ensure_running(self, progress_cb: ProgressCB | None) -> None:
        """确保 ComfyUI 服务可用（复用探测 + 单次冷启动等待）。"""
        if self.is_alive():
            return
        with self._proc_lock:
            if self.is_alive():
                return
            if self._proc is not None and self._proc.poll() is None:
                # 前一实例仍在初始化（端口未就绪）——等待其就绪
                self._wait_ready(_STARTUP_TIMEOUT_S, progress_cb)
                return
            if not h3_available():
                raise ApiError(
                    code=60003,
                    message="MiniMax H3 管线未就绪（ComfyUI 或权重缺失）",
                    suggestion="请确认 tools/ComfyUI_windows_portable 与 "
                               "models/video_gen/h3 权重完整",
                )
            self._spawn()
            self._wait_ready(_STARTUP_TIMEOUT_S, progress_cb)

    def _wait_ready(self, timeout_s: float,
                    progress_cb: ProgressCB | None) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.is_alive():
                return
            if self._proc is not None and self._proc.poll() is not None:
                raise ApiError(
                    code=60003,
                    message=f"ComfyUI 子进程异常退出 (code={self._proc.returncode})",
                    suggestion="查看 logs/comfyui_h3.log 排查",
                )
            if progress_cb is not None:
                progress_cb(0.02, "ComfyUI 启动中")
            time.sleep(2.0)
        raise ApiError(code=60003, message="ComfyUI 启动超时",
                       suggestion="查看 logs/comfyui_h3.log 排查")

    def unload(self) -> None:
        """卸载 ComfyUI 内驻留权重（进程保留热启动；force_unload 语义）。"""
        self._api("POST", "/free",
                  body={"unload_models": True, "free_memory": True},
                  timeout=60.0)

    def shutdown(self) -> None:
        """终止 ComfyUI 子进程树（委托统一管理器，幂等——无论哪个
        引擎 spawn 的实例都可杀；外部手动起的实例不受影响）。"""
        with self._proc_lock:
            self._proc = None
        get_comfy_proc().shutdown()

    # ── 工作流构造 ────────────────────────────────────────────────

    def _build_workflow(self, *, prompt: str, width: int, height: int,
                        length: int, seed: int, steps: int,
                        first_frame_name: str | None,
                        filename_prefix: str) -> dict:
        """构造 API 格式 H3 工作流（官方 T2V 模板 subgraph 节点级还原）。"""
        i2v: dict = {}
        if first_frame_name is not None:
            # LoadImage 节点（首帧图已写入 ComfyUI/input/）
            i2v = {
                "1": {"class_type": "LoadImage",
                      "inputs": {"image": first_frame_name}},
            }
        wf: dict = {
            "6": {"class_type": "UNETLoader",
                  "inputs": {"unet_name": _H3_FILES["unet"],
                             "weight_dtype": "default"}},
            "13": {"class_type": "CLIPLoader",
                   "inputs": {"clip_name": _H3_FILES["clip"],
                              "type": "minimax", "device": "default"}},
            "11": {"class_type": "VAELoader",
                   "inputs": {"vae_name": _H3_FILES["video_vae"]}},
            "24": {"class_type": "VAELoader",
                   "inputs": {"vae_name": _H3_FILES["audio_vae"]}},
            "104": {"class_type": "MiniMaxH3ImageToVideo",
                    "inputs": {"clip": ["13", 0], "vae": ["11", 0],
                               "prompt": prompt, "width": width,
                               "height": height, "length": length}},
            "15": {"class_type": "RandomNoise",
                   "inputs": {"noise_seed": seed}},
            "17": {"class_type": "KSamplerSelect",
                   "inputs": {"sampler_name": "res_multistep"}},
            "9": {"class_type": "BasicScheduler",
                  "inputs": {"model": ["6", 0], "scheduler": "simple",
                             "steps": steps, "denoise": 1.0}},
            "16": {"class_type": "BasicGuider",
                   "inputs": {"model": ["6", 0],
                              "conditioning": ["104", 0]}},
            "14": {"class_type": "SamplerCustomAdvanced",
                   "inputs": {"noise": ["15", 0], "guider": ["16", 0],
                              "sampler": ["17", 0], "sigmas": ["9", 0],
                              "latent_image": ["104", 1]}},
            "10": {"class_type": "VAEDecode",
                   "inputs": {"samples": ["14", 0], "vae": ["11", 0]}},
            "23": {"class_type": "VAEDecodeAudio",
                   "inputs": {"samples": ["14", 0], "vae": ["24", 0]}},
            "91": {"class_type": "CreateVideo",
                   "inputs": {"images": ["10", 0], "audio": ["23", 0],
                              "fps": _H3_FPS}},
            "8": {"class_type": "SaveVideo",
                  "inputs": {"video": ["91", 0],
                             "filename_prefix": filename_prefix,
                             "format": "auto", "codec": "auto"}},
        }
        wf.update(i2v)
        if first_frame_name is not None:
            wf["104"]["inputs"]["first_frame"] = ["1", 0]
        return wf

    # ── 生成主流程 ────────────────────────────────────────────────

    def generate(self, *, prompt: str, width: int, height: int,
                 seconds: float, out_path: Path,
                 first_frame: Image.Image | None = None,
                 steps: int = _DEFAULT_STEPS,
                 progress_cb: ProgressCB | None = None) -> Path:
        """执行 H3 生成（同步阻塞，video_worker 线程内调用）。

        Args:
            prompt: 中文/英文提示词（Qwen3-VL-32B 编码器中文原生）
            width/height: 32 倍数画幅（短边 ≤768，面积 ≤768×1344）
            seconds: 目标时长（内部 clamp 5~15s + 17k+5 帧对齐）
            out_path: 产物落位（回搬至此）
            first_frame: 可选首帧（I2V）
            steps: 采样步数（官方 20）
            progress_cb: (fraction, stage)；回调异常 = 取消信号
                （POST /interrupt 后原样穿透）

        Returns:
            out_path（写盘完成）

        Raises:
            ApiError: 启动/提交/执行失败或超时
        """
        with self._gen_lock:
            # 生成期间标记忙碌：空闲自动关闭计时暂停（mark 配对）
            get_comfy_proc().mark_busy()
            try:
                return self._generate_locked(
                    prompt=prompt, width=width, height=height, seconds=seconds,
                    out_path=out_path, first_frame=first_frame, steps=steps,
                    progress_cb=progress_cb)
            finally:
                get_comfy_proc().mark_idle()

    def _generate_locked(self, *, prompt: str, width: int, height: int,
                         seconds: float, out_path: Path,
                         first_frame: Image.Image | None, steps: int,
                         progress_cb: ProgressCB | None) -> Path:
        task_id = uuid.uuid4().hex[:12]
        seconds = min(max(seconds, 5.0), 15.0)
        length = align_h3_frames(seconds)
        seed = int.from_bytes(uuid.uuid4().bytes[:8], "big") % (2 ** 31)

        def _report(frac: float, stage: str) -> None:
            if progress_cb is not None:
                progress_cb(min(frac, 0.99), stage)

        # 首帧图写入 ComfyUI/input/（LoadImage 节点按名引用）
        frame_name: str | None = None
        if first_frame is not None:
            frame_name = f"h3_{task_id}.png"
            _COMFY_INPUT.mkdir(parents=True, exist_ok=True)
            first_frame.save(_COMFY_INPUT / frame_name, format="PNG")

        prompt_id: str | None = None
        try:
            self._ensure_running(progress_cb)
            _report(0.06, "提交 H3 工作流")

            wf = self._build_workflow(
                prompt=prompt, width=width, height=height, length=length,
                seed=seed, steps=steps, first_frame_name=frame_name,
                filename_prefix=f"h3/{task_id}")
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
                               message=f"H3 工作流校验失败: {detail}")
            prompt_id = str(resp["prompt_id"])
            logger.info("H3 任务已提交 (prompt_id=%s, %dx%d, %d帧, %d步)",
                        prompt_id, width, height, length, steps)

            out_path.parent.mkdir(parents=True, exist_ok=True)
            video_rel = self._poll_history(
                prompt_id, width=width, height=height, steps=steps,
                progress_cb=progress_cb)

            # 产物回搬：ComfyUI/output/<subfolder>/<filename> → out_path
            src = _COMFY_OUTPUT / video_rel
            if not src.is_file():
                raise ApiError(code=60003,
                               message=f"H3 产物缺失: {video_rel}")
            shutil.move(str(src), out_path)
            _report(1.0, "完成")
            logger.info("H3 视频已回搬: %s (%.1f MB)",
                        out_path.name, out_path.stat().st_size / 1048576)
            return out_path
        finally:
            # 权重即时卸载（ComfyUI 进程保留热启动）—— video_gen 锁
            # 释放后显存归零，避免与 dialog/paint 挤兑
            self.unload()
            if frame_name is not None:
                try:
                    (_COMFY_INPUT / frame_name).unlink(missing_ok=True)
                except OSError:
                    pass

    def _poll_history(self, prompt_id: str, *, width: int, height: int,
                      steps: int,
                      progress_cb: ProgressCB | None) -> str:
        """轮询 /history 至完成，返回产物相对路径（subfolder/filename）。

        采样进度按 ETA 线性平滑（无 WebSocket 细粒度）；progress_cb
        异常视为取消：POST /interrupt 后穿透。
        """
        eta = (steps * (width * height / 1e6) * _ETA_SEC_PER_STEP_PER_MPIX
               + 45.0)
        deadline = time.monotonic() + eta + _TASK_TIMEOUT_MARGIN_S
        start = time.monotonic()

        def _report(frac: float, stage: str) -> None:
            if progress_cb is not None:
                progress_cb(min(frac, 0.99), stage)

        while time.monotonic() < deadline:
            time.sleep(3.0)
            _report(0.08 + 0.8 * min(1.0,
                     (time.monotonic() - start) / eta), "H3 采样中")
            hist = self._api("GET", f"/history/{prompt_id}", timeout=5.0)
            if hist is None:
                continue  # 执行中（404 = 未入 history）
            entry = hist.get(prompt_id) or {}
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                messages = status.get("messages") or []
                detail = json.dumps(messages, ensure_ascii=False)[:600]
                raise ApiError(code=60003,
                               message=f"H3 执行失败: {detail}")
            outputs = entry.get("outputs") or {}
            for node_out in outputs.values():
                for item in (node_out.get("images")
                             or node_out.get("videos") or []):
                    sub = str(item.get("subfolder") or "")
                    fn = str(item.get("filename") or "")
                    if fn.endswith(".mp4") or item.get("animated"):
                        rel = f"{sub}/{fn}" if sub else fn
                        _report(0.95, "解码保存")
                        return rel
            # status completed 但无 mp4 —— 视为失败
            if status.get("completed"):
                raise ApiError(code=60003,
                               message="H3 执行完成但无视频产物")
        raise ApiError(code=60004, message="H3 生成超时",
                       suggestion="请重试或降低分辨率/时长")


_engine: H3Engine | None = None
_engine_lock = threading.Lock()


def get_h3_engine() -> H3Engine:
    """H3Engine 单例访问（懒初始化 + atexit 终止注册）。"""
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = H3Engine.get()
            import atexit
            atexit.register(_engine.shutdown)
        return _engine


# ══ 导演台模式（V7.2 TheodoreDirector，2026-08-29 P1 接线） ═════════

# H3Adapter 输出槽位（custom_nodes/ComfyUI_Theodore_Director/nodes.py
# 输出顺序：H3 prompt / H3 frames / delivery frames / reference map /
# ref_image_0..8 / ref_video_0..2 / ref_video_audio_0..2 / ref_audio_0..2）
_ADAPTER_OUT_PROMPT = 0
_ADAPTER_OUT_FRAMES = 1
_ADAPTER_OUT_REF_IMG0 = 4

# Theodore 节点输入口径（nodes.py schema；SelectShot widgets:
# queue_index / base_seed / resume_mode）
_SELECT_OUT_SEED = 4       # SHOT(0) prompt(1) neg(2) duration(3) seed(4)

# 网格画幅（对齐 V7.2 工作流 0.4MP 16:9 档；ref_image_size=match
# 会把网格格 1280×720 等比缩到生成面积）
_DIRECTOR_WIDTH = 1344
_DIRECTOR_HEIGHT = 768
# 每镜连 3 个 ref 槽（<Picture 1>=首帧(fixed) + 角色资产 + 场景资产）
_DIRECTOR_REF_SLOTS = 3


def _build_director_workflow(*, plan_json: str, queue_index: int,
                             base_seed: int, steps: int,
                             filename_prefix: str,
                             relay_tail: str | None = None) -> dict:
    """构造 API 格式单段导演台工作流（V7.2 首采样链节点级还原）。

    链路：Project(plan_json) → SelectShot(queue_index) → H3Adapter
    → MiniMaxH3ReferenceToVideo(prompt/length/ref_image_0..2) →
    官方采样链（RandomNoise/KSamplerSelect/BasicScheduler/BasicGuider/
    SamplerCustomAdvanced）→ VAEDecode+VAEDecodeAudio → CreateVideo
    → SaveVideo。Impact 队列环与二采链不进入 API 图（后端 worker
    自带逐镜编排与裁时拼接）。

    P2 latentRelay（2026-08-29）：relay_tail 非 None 时注入续接链
    LoadVideo → GetVideoComponents → MiniMaxH3AddGuide（上一段尾部
    22 帧画面 + 音轨锚定在新段第 0 帧，每步重注入、永不参与去噪）。
    与 V7.2 MotionContext 同语义（像素域实现：本 ComfyUI 快照无
    MiniMaxH3MotionContext* 节点，AddGuide 为官方等价机制，仅多一次
    22 帧的 VAE 往返）；接续段长度由 H3Adapter 按 latentRelay 自动
    扩 22 帧上下文（duration_mode=final_output），头部重复帧由
    video.py 裁时剥离。
    """
    wf = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": _H3_FILES["unet"],
                         "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": _H3_FILES["clip"],
                         "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader",
              "inputs": {"vae_name": _H3_FILES["video_vae"]}},
        "4": {"class_type": "VAELoader",
              "inputs": {"vae_name": _H3_FILES["audio_vae"]}},
        "5": {"class_type": "TheodoreDirector_Project",
              "inputs": {"plan_json": plan_json}},
        "6": {"class_type": "TheodoreDirector_SelectShot",
              "inputs": {"plan": ["5", 0], "queue_index": queue_index,
                         "base_seed": base_seed, "resume_mode": "resume"}},
        "7": {"class_type": "TheodoreDirector_H3Adapter",
              "inputs": {"plan": ["5", 0], "shot": ["6", 0]}},
        "8": {"class_type": "MiniMaxH3ReferenceToVideo",
              "inputs": {"clip": ["2", 0], "vae": ["3", 0],
                         "audio_vae": ["4", 0],
                         "prompt": ["7", _ADAPTER_OUT_PROMPT],
                         "length": ["7", _ADAPTER_OUT_FRAMES],
                         "width": _DIRECTOR_WIDTH,
                         "height": _DIRECTOR_HEIGHT,
                         "ref_image_size": "match",
                         **{f"ref_images.ref_image_{k}":
                            ["7", _ADAPTER_OUT_REF_IMG0 + k]
                            for k in range(_DIRECTOR_REF_SLOTS)}}},
        "9": {"class_type": "RandomNoise",
              "inputs": {"noise_seed": ["6", _SELECT_OUT_SEED]}},
        "10": {"class_type": "KSamplerSelect",
               "inputs": {"sampler_name": "res_multistep"}},
        "11": {"class_type": "BasicScheduler",
               "inputs": {"model": ["1", 0], "scheduler": "simple",
                          "steps": steps, "denoise": 1.0}},
        "12": {"class_type": "BasicGuider",
               "inputs": {"model": ["1", 0],
                          "conditioning": ["8", 0] if not relay_tail
                          else ["20", 0]}},
        "13": {"class_type": "SamplerCustomAdvanced",
               "inputs": {"noise": ["9", 0], "guider": ["12", 0],
                          "sampler": ["10", 0], "sigmas": ["11", 0],
                          "latent_image": ["8", 1]}},
        "14": {"class_type": "VAEDecode",
               "inputs": {"samples": ["13", 0], "vae": ["3", 0]}},
        "15": {"class_type": "VAEDecodeAudio",
               "inputs": {"samples": ["13", 0], "vae": ["4", 0]}},
        "16": {"class_type": "CreateVideo",
               "inputs": {"images": ["14", 0], "audio": ["15", 0],
                          "fps": _H3_FPS}},
        "17": {"class_type": "SaveVideo",
               "inputs": {"video": ["16", 0],
                          "filename_prefix": filename_prefix,
                          "format": "auto", "codec": "auto"}},
    }
    if relay_tail:
        # 续接链：上一段尾部片段（ComfyUI input 目录）→ 帧+音轨 →
        # AddGuide 锚定 frame_idx=0（22 帧 = 17×1+5 合法剪辑长度）
        wf["18"] = {"class_type": "LoadVideo",
                    "inputs": {"file": relay_tail}}
        wf["19"] = {"class_type": "GetVideoComponents",
                    "inputs": {"video": ["18", 0]}}
        wf["20"] = {"class_type": "MiniMaxH3AddGuide",
                    "inputs": {"positive": ["8", 0], "latent": ["8", 1],
                               "frame_idx": 0, "vae": ["3", 0],
                               "audio_vae": ["4", 0],
                               "image": ["19", 0], "audio": ["19", 1]}}
    return wf


def _extract_relay_tail(src: Path, dst: Path, ctx_frames: int) -> None:
    """ffmpeg 截取上一段尾部 ctx+2 帧（多裁 2 帧让 AddGuide 收敛到
    17k+5=22 帧合法剪辑长度），H264/AAC 重编码保证 LoadVideo 可读。"""
    from ..encoder_service import get_encoder_service

    enc = get_encoder_service()
    if not enc.available:
        raise ApiError(code=60003,
                       message="latentRelay 续接需要 FFmpeg（当前不可用）")
    tail_s = (ctx_frames + 2) / _H3_FPS
    flags = (subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    r = subprocess.run(
        [enc.ffmpeg_path, "-y", "-sseof", f"-{tail_s:.4f}", "-i", str(src),
         "-t", f"{tail_s:.4f}",
         "-c:v", "libx264", "-preset", "fast", "-crf", "18",
         "-pix_fmt", "yuv420p", "-r", str(_H3_FPS),
         "-c:a", "aac", "-ar", "44100", "-ac", "2",
         str(dst)],
        capture_output=True, text=True, timeout=120, creationflags=flags)
    if r.returncode != 0 or not dst.is_file():
        raise ApiError(code=60003,
                       message="续接尾帧提取失败: "
                               f"{(r.stderr or '')[-300:]}")


def _poll_director_history(engine: H3Engine, prompt_id: str, *,
                           seconds: float, steps: int,
                           frames: int,
                           progress_cb: ProgressCB | None) -> str:
    """导演台单段轮询（P3 校准：帧数线性 ETA + 队列活性检测）。

    E2E 实测（2026-08-29，16GB 显存 + 31GB 权重 offload）：141 帧
    @1344×768@20 步，同规格两跑实测 912s ↔ 1242s（6.5 ↔ 8.8 s/帧，
    offload 换页抖动为主变量，历史观测可恶化至 60s/步）——固定常量
    ETA 曾两次击杀真实任务，故按帧数线性 + 900s 抖动余量。
    活性检测：prompt 不在 /queue 且 history 无产物 → 被外部中断，
    立即报错而非傻等超时。
    """
    eta = max(seconds * 3.0, frames * 5.5 + steps * 5.0)
    deadline = time.monotonic() + eta + 900.0
    start = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(3.0)
        if progress_cb is not None:
            progress_cb(min(0.99, (time.monotonic() - start) / eta),
                        "H3 导演台采样中")
        hist = engine._api("GET", f"/history/{prompt_id}", timeout=5.0)
        if hist is None:
            continue
        entry = hist.get(prompt_id) or {}
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            detail = json.dumps(status.get("messages") or [],
                                ensure_ascii=False)[:600]
            raise ApiError(code=60003, message=f"H3 导演台执行失败: {detail}")
        for node_out in (entry.get("outputs") or {}).values():
            for item in (node_out.get("images")
                         or node_out.get("videos") or []):
                fn = str(item.get("filename") or "")
                if fn.endswith(".mp4") or item.get("animated"):
                    sub = str(item.get("subfolder") or "")
                    if progress_cb is not None:
                        progress_cb(1.0, "解码保存")
                    return f"{sub}/{fn}" if sub else fn
        if status.get("completed"):
            raise ApiError(code=60003, message="H3 导演台完成但无视频产物")
        # 活性检测：既不在队列也无产物 → 被 /interrupt 等外部中断
        q = engine._api("GET", "/queue", timeout=5.0)
        if q is not None:
            running = {str(it[1]) for it in q.get("queue_running") or []}
            pending = {str(it[1]) for it in q.get("queue_pending") or []}
            if prompt_id not in running and prompt_id not in pending:
                raise ApiError(
                    code=60004,
                    message="H3 导演台任务已中断（不在执行队列且无产物）")
    raise ApiError(code=60004, message="H3 导演台生成超时",
                   suggestion="请重试或降低分段时长")


def generate_director(*, project: dict, plan: list[dict],
                      asset_images: dict, out_dir: Path,
                      steps: int = _DEFAULT_STEPS,
                      progress_cb: ProgressCB | None = None) -> list[Path]:
    """导演台模式：逐镜提交 V7.2 导播台工作流并回收分段视频。

    Args:
        project: h3_convert 产出的 TheodoreDirector 工程包（深拷贝后
            改写 assets[].path 指向本次任务 input 子目录）
        plan: h3_convert 裁时计划（逐镜模式每段单镜；P2 合并模式
            段内多镜 + latentRelay 接续，头部 22 帧上下文由 video.py
            裁时剥离）
        asset_images: {别名: PIL.Image}——项目绑定资产 + 各镜网格首帧
        out_dir: 分段产物落位目录（段文件名 = {shot_id}.mp4）
        steps: 每段采样步数（官方 20）
        progress_cb: (fraction, stage)，fraction 跨段累计

    Returns:
        各段 mp4 落盘路径列表（顺序 = plan 顺序）

    Raises:
        ApiError: ComfyUI 未就绪 / 提交校验失败 / 执行失败 / 超时
    """
    engine = get_h3_engine()
    if not plan:
        raise ApiError(code=60003, message="导演台计划为空")
    with engine._gen_lock:
        return _generate_director_locked(
            project=project, plan=plan, asset_images=asset_images,
            out_dir=out_dir, steps=steps, progress_cb=progress_cb)


def _generate_director_locked(*, project: dict, plan: list[dict],
                              asset_images: dict, out_dir: Path,
                              steps: int, progress_cb) -> list[Path]:
    import copy

    engine = get_h3_engine()
    task_id = uuid.uuid4().hex[:12]
    input_sub = f"omnispace_{task_id}"
    total = len(plan)

    def _report(seg_done: float, frac: float, stage: str) -> None:
        if progress_cb is not None:
            overall = (seg_done + max(0.0, min(1.0, frac))) / total
            progress_cb(min(overall, 0.99), stage)

    engine._ensure_running(
        lambda f, s: _report(0.0, f, s))
    _report(0.0, 0.03, "提交导演台工作流")

    # 工程包深拷贝：素材路径改写为本次任务 input 子目录相对路径
    proj = copy.deepcopy(project)
    for a in proj.get("assets") or []:
        if a.get("path") and not a["path"].startswith(f"{input_sub}/"):
            a["path"] = f"{input_sub}/{a['path']}"
    plan_json = json.dumps(proj, ensure_ascii=False)

    # 素材落盘 ComfyUI/input/<sub>/{alias}.png（media.py 经
    # folder_paths.get_annotated_filepath 解析 → input 根）
    input_dir = _COMFY_INPUT / input_sub
    input_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        for alias, img in asset_images.items():
            fp = input_dir / f"{alias}.png"
            img.save(fp, format="PNG")
            written.append(fp)

        out_dir.mkdir(parents=True, exist_ok=True)
        seg_paths: list[Path] = []
        for gi, seg in enumerate(plan):
            shot_id = str(seg.get("shot_id") or f"shot_{gi + 1:03d}")
            dur = float(seg.get("gen_seconds") or 5.0)
            frames = align_h3_frames(dur)
            limit = _seg_frame_limit()
            if frames > limit:
                raise ApiError(
                    code=60004,
                    message=f"{shot_id} 生成段 {dur:.1f}s（{frames} 帧）"
                            f"超出当前显存档位上限 {limit} 帧"
                            "（>10s 已实测 OOM）",
                    suggestion="减少段内合并镜数以缩短生成段")
            _report(float(gi), 0.05, f"{shot_id} 提交")
            # P2 latentRelay：接续段先截上一段尾部（22+2 帧）作锚，
            # 经 AddGuide 注入新段第 0 帧（工作流条件链节点级接线）
            relay_tail = None
            if gi > 0 and seg.get("latent_relay") and seg_paths:
                ctx = int(seg.get("ctx_frames") or 0)
                if ctx > 0:
                    tail = input_dir / f"{seg_paths[-1].stem}_relay_tail.mp4"
                    _extract_relay_tail(seg_paths[-1], tail, ctx)
                    written.append(tail)
                    relay_tail = f"{input_sub}/{tail.name}"
            wf = _build_director_workflow(
                plan_json=plan_json, queue_index=gi,
                base_seed=int((proj.get("defaults") or {}).get("baseSeed")
                              or 0),
                steps=steps,
                filename_prefix=f"h3_director/{task_id}/{shot_id}",
                relay_tail=relay_tail)
            resp = engine._api("POST", "/prompt",
                               body={"prompt": wf,
                                     "client_id": f"omnispace-{task_id}"},
                               timeout=15.0)
            if resp is None:
                raise ApiError(code=60003,
                               message="ComfyUI 不可达（导演台提交失败）")
            if resp.get("error") or resp.get("node_errors"):
                detail = json.dumps(resp.get("node_errors") or resp["error"],
                                    ensure_ascii=False)[:500]
                raise ApiError(code=60003,
                               message=f"导演台工作流校验失败: {detail}")
            prompt_id = str(resp["prompt_id"])
            logger.info("导演台段已提交 (prompt_id=%s, %s, %.1fs)",
                        prompt_id, shot_id, dur)
            video_rel = _poll_director_history(
                engine, prompt_id, seconds=dur, steps=steps,
                frames=align_h3_frames(dur),
                progress_cb=lambda f, s, _gi=gi: _report(_gi, f, s))
            src = _COMFY_OUTPUT / video_rel
            if not src.is_file():
                raise ApiError(code=60003,
                               message=f"导演台产物缺失: {video_rel}")
            dst = out_dir / f"{shot_id}.mp4"
            shutil.move(str(src), dst)
            seg_paths.append(dst)
            _report(float(gi + 1), 1.0, f"{shot_id} 完成")
            logger.info("导演台段已回收: %s (%.1f MB)",
                        dst.name, dst.stat().st_size / 1048576)
        return seg_paths
    finally:
        # 段间输入图清理 + 权重卸载（生成毕即卸，对齐项目铁律）
        for fp in written:
            try:
                fp.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            import shutil as _sh
            _sh.rmtree(input_dir, ignore_errors=True)
        except OSError:
            pass
        engine.unload()
