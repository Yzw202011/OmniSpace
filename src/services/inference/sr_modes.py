# 本项目仅供学习使用，商业授权请+Q 3559331368
"""多模式超分服务（2026-10-18 用户令）：模型设置「超分模型」下拉的真切换后端。

三档新管线（与 ESRGAN `h3_frame_upscale.upscale_video` 并列，契约一致：
(src, out, progress_cb) → 成功返回 out，失败抛异常由调用方如实进作业表）：
  - run_dlssnr:  NVIDIA DLSS-NR 2x（外部 video2dlssnr v1.3 运行时，甜点
    配色 custom/Cinematic/1.2/1.0/0.8 + 链尾 gamma1.10/sat1.06 已并入管线）
  - run_rbvsr:   RealBasicVSR 原尺寸时序修复（3.2min 级，极速）
  - run_seedvr2: SeedVR2 3B 一步扩散修复（fp8，resolution 随超分分辨率档）

公共骨架：拉起 ComfyUI → 提交图 → 轮询历史产物 → 回捞到 out。
输出分辨率：dlssnr 固定 2x（2752×1536）；rbvsr 原尺寸（1376×768）；
seedvr2 短边档（1k→1376×768，2k→2752×1536）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import json
import logging
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("omnispace.inference.sr_modes")

# SSRF 边界：基址 = 模块级回环字面量；拼接后解析校验 scheme/host 白名单
_COMFY_BASE = "http://127.0.0.1:8189"
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

ProgressCB = Callable[[float, str], None]


def _api(method: str, path: str, body: dict | None = None,
         timeout: float = 15.0) -> Any:
    if not path.startswith("/") or ".." in path:
        raise ValueError(f"非法 API 路径: {path!r}")
    url = urllib.parse.urljoin(_COMFY_BASE, path)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in _ALLOWED_HOSTS:
        raise ValueError(f"仅允许本机回环 URL: {url!r}")
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def _ensure_comfy(progress_cb: ProgressCB | None) -> None:
    """ComfyUI 冷启动兜底（SR 可能先于任何生成任务到达）。"""
    from .h3_engine import get_h3_engine
    get_h3_engine()._ensure_running(lambda f, s: None)
    t0 = time.time()
    while time.time() - t0 < 120:
        try:
            _api("GET", "/object_info", timeout=5.0)
            return
        except Exception:  # noqa: BLE001 - 未就绪继续等
            time.sleep(2)
    raise RuntimeError("ComfyUI 120s 未就绪（超分前置失败）")


def _submit_and_wait(wf: dict, *, out: Path, comfy_output: Path,
                     timeout_s: float, progress_cb: ProgressCB | None,
                     stage: str) -> Path:
    """提交图并轮询产物；VHS/DLSS 两类出口都兜（history 优先，目录扫描兜底）。"""
    prefix = None
    for node in wf.values():
        fp = node.get("inputs", {}).get("filename_prefix")
        if isinstance(fp, str):
            prefix = fp
    resp = _api("POST", "/prompt", body={"prompt": wf, "client_id": "omnispace-srmodes"},
                timeout=30.0)
    if resp.get("error") or resp.get("node_errors"):
        raise RuntimeError("SR 工作流校验失败: " + json.dumps(
            resp.get("node_errors") or resp["error"], ensure_ascii=False)[:400])
    pid = str(resp["prompt_id"])
    log.info("[sr_modes] 已提交 (%s, prompt_id=%s)", stage, pid)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        time.sleep(5)
        try:
            hist = _api("GET", f"/history/{pid}", timeout=10.0)
            entry = (hist or {}).get(pid)
        except Exception:  # noqa: BLE001 - 暂态轮询错继续
            continue
        if entry is None:
            if progress_cb is not None:
                progress_cb(min(0.9, 0.2 + (time.time() - t0) / timeout_s * 0.7), stage)
            continue
        st = entry.get("status") or {}
        if st.get("status_str") == "error":
            raise RuntimeError("SR 执行失败: " + json.dumps(
                st.get("messages") or [], ensure_ascii=False)[:400])
        for node_out in (entry.get("outputs") or {}).values():
            sp = node_out.get("saved_path")
            if isinstance(sp, str) and sp and Path(sp).is_file():
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(sp, out)
                return out
            for item in (node_out.get("gifs") or node_out.get("images") or []):
                fn = str(item.get("filename") or "")
                sub = str(item.get("subfolder") or "")
                if fn.endswith(".mp4"):
                    prod = comfy_output / (Path(sub) / fn if sub else fn)
                    if prod.is_file():
                        out.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(prod), out)
                        return out
        if st.get("completed"):
            # history 无产物：按前缀扫输出目录兜底（节点自存路径）
            if prefix:
                subdir = comfy_output / prefix.split("/")[0]
                if subdir.is_dir():
                    cands = sorted(subdir.glob(f"*{Path(prefix).name}*.mp4"),
                                   key=lambda p: p.stat().st_mtime)
                    if cands:
                        out.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(cands[-1]), out)
                        return out
            raise RuntimeError("SR 完成但未找到产物文件")
    raise TimeoutError(f"SR 超时（{timeout_s:.0f}s）")


def _load_video_node(src: Path, node_id: str, wf: dict, use_input_copy: bool,
                     comfy_input: Path) -> None:
    if use_input_copy:
        comfy_input.mkdir(parents=True, exist_ok=True)
        local = f"srmodes_{int(time.time())}.mp4"
        shutil.copy2(src, comfy_input / local)
        wf[node_id] = {"class_type": "LoadVideo", "inputs": {"file": local}}
    else:
        wf[node_id] = {"class_type": "VHS_LoadVideoPath", "inputs": {
            "video": str(src), "force_rate": 0, "custom_width": 0,
            "custom_height": 0, "frame_load_cap": 0, "skip_first_frames": 0,
            "select_every_nth": 1, "format": "None"}}


def run_dlssnr(src: Path, out: Path, *,
               progress_cb: ProgressCB | None = None) -> Path:
    """DLSS-NR 2x（甜点配色）：python_embeded 子进程内直调 T8 库。

    ComfyUI 图内执行探针会连带宿主进程静默崩溃、backend 进程内执行会
    级联缺 comfy 依赖树——两者都实测翻车（10-18）。python_embeded 原生
    具备 ComfyUI 全依赖树，子进程隔离 NVML 怪癖，是最稳执行位。
    """
    import subprocess

    pe = Path(__file__).resolve().parents[3] / "tools" /         "ComfyUI_windows_portable" / "python_embeded" / "python.exe"
    runner = Path(__file__).resolve().parents[3] / "scripts" / "comfy_link" /         "sr_dlssnr_runner.py"
    if not pe.is_file() or not runner.is_file():
        raise RuntimeError("DLSS-NR 执行环境缺失（python_embeded/runner）")
    # ComfyUI 冷启动竞态防护（10-18 实锤：ComfyUI 加载模型期与 DLSS-NR
    # exe 抢 GPU 会导致 exe 崩溃 BrokenPipe）——等 HTTP 就绪再稳 20 秒
    _ensure_comfy(progress_cb)
    time.sleep(20)
    if progress_cb is not None:
        progress_cb(0.1, "DLSS-NR 提交")
    t0 = time.time()

    def _poll() -> None:
        while (out.parent / (out.stem + "_raw.mp4")).exists() and                 time.time() - t0 < 15 * 60:
            if progress_cb is not None:
                progress_cb(min(0.9, 0.2 + (time.time() - t0) / 90 * 0.7),
                            "DLSS-NR 处理")
            time.sleep(5)

    import threading
    th = threading.Thread(target=_poll, daemon=True)
    th.start()
    r = None
    for _attempt in range(2):  # 首跑遇 ComfyUI 冷启动竞态时重试一次
        r = subprocess.run([str(pe), str(runner), "--skip-audit",
                            "--src", str(src), "--out", str(out)],
                           capture_output=True, text=True, errors="replace",
                           timeout=16 * 60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0 and out.is_file():
            break
        time.sleep(15)
    th.join(timeout=5)
    if r is None or r.returncode != 0 or not out.is_file():
        tail = ((r.stdout or "")[-400:] + " || " + (r.stderr or "")[-400:]) if r else "no run"
        raise RuntimeError(f"DLSS-NR 子进程失败 rc={getattr(r, 'returncode', '?')}: {tail}")
    if progress_cb is not None:
        progress_cb(0.95, "DLSS-NR 完成")
    return out


def run_rbvsr(src: Path, out: Path, *,
              progress_cb: ProgressCB | None = None) -> Path:
    """RealBasicVSR 原尺寸时序修复（极速档）。"""
    from .comfy_proc import COMFY_INPUT_DIR, COMFY_OUTPUT_DIR
    _ensure_comfy(progress_cb)
    if progress_cb is not None:
        progress_cb(0.1, "RealBasicVSR 提交")
    wf: dict[str, Any] = {}
    _load_video_node(src, "1", wf, use_input_copy=False, comfy_input=COMFY_INPUT_DIR)
    wf["2"] = {"class_type": "MiniMaxH3RealBasicVSRRestoreT8Advanced", "inputs": {
        "frames": ["1", 0], "audio": ["1", 2],
        "model_name": "RealBasicVSR_x4.pth",
        "output_mode": "native_size_restore",
        "strength": 0.30, "chunk_frames": 8, "overlap_frames": 2,
        "precision": "auto", "checkpoint_branch": "prefer_ema",
        "release_policy": "offload_after"}}
    wf["3"] = {"class_type": "VHS_VideoCombine", "inputs": {
        "images": ["2", 0], "audio": ["2", 2], "frame_rate": 24,
        "loop_count": 0, "filename_prefix": "rbvsr/sr_modes",
        "format": "video/h264-mp4", "pix_fmt": "yuv420p", "crf": 16,
        "save_metadata": True, "trim_to_audio": False, "pingpong": False,
        "save_output": True}}
    return _submit_and_wait(wf, out=out, comfy_output=COMFY_OUTPUT_DIR,
                            timeout_s=15 * 60, progress_cb=progress_cb,
                            stage="RealBasicVSR 处理")


def run_seedvr2(src: Path, out: Path, *, resolution_name: str = "1k",
                progress_cb: ProgressCB | None = None) -> Path:
    """SeedVR2 3B 一步扩散修复（fp8；resolution 随超分分辨率档 1k/2k）。"""
    from .comfy_proc import COMFY_OUTPUT_DIR
    _ensure_comfy(progress_cb)
    if progress_cb is not None:
        progress_cb(0.1, "SeedVR2 提交")
    resolution = 1536 if resolution_name == "2k" else 768
    wf = {
        "1": {"class_type": "VHS_LoadVideoPath", "inputs": {
            "video": str(src), "force_rate": 0, "custom_width": 0,
            "custom_height": 0, "frame_load_cap": 0, "skip_first_frames": 0,
            "select_every_nth": 1, "format": "None"}},
        "2": {"class_type": "SeedVR2LoadDiTModel", "inputs": {
            "model": "seedvr2_ema_3b_fp8_e4m3fn.safetensors",
            "device": "cuda:0", "blocks_to_swap": 0,
            "swap_io_components": False, "offload_device": "none",
            "cache_model": False, "attention_mode": "sdpa"}},
        "3": {"class_type": "SeedVR2LoadVAEModel", "inputs": {
            "model": "ema_vae_fp16.safetensors", "device": "cuda:0",
            "encode_tiled": True, "encode_tile_size": 1024,
            "encode_tile_overlap": 128, "decode_tiled": True,
            "decode_tile_size": 1024, "decode_tile_overlap": 128,
            "tile_debug": "false", "offload_device": "none"}},
        "4": {"class_type": "SeedVR2VideoUpscaler", "inputs": {
            "image": ["1", 0], "dit": ["2", 0], "vae": ["3", 0],
            "seed": 42, "resolution": resolution, "max_resolution": 0,
            "batch_size": 5, "uniform_batch_size": True,
            "temporal_overlap": 2, "prepend_frames": 2,
            "color_correction": "lab", "input_noise_scale": 0.0,
            "latent_noise_scale": 0.0, "offload_device": "none",
            "enable_debug": False}},
        "5": {"class_type": "VHS_VideoCombine", "inputs": {
            "images": ["4", 0], "audio": ["1", 2], "frame_rate": 24,
            "loop_count": 0, "filename_prefix": "seedvr2/sr_modes",
            "format": "video/h264-mp4", "pix_fmt": "yuv420p", "crf": 16,
            "save_metadata": True, "trim_to_audio": False, "pingpong": False,
            "save_output": True}},
    }
    timeout = 240 * 60 if resolution_name == "2k" else 40 * 60
    return _submit_and_wait(wf, out=out, comfy_output=COMFY_OUTPUT_DIR,
                            timeout_s=timeout, progress_cb=progress_cb,
                            stage="SeedVR2 处理")


def _ffmpeg_eq(video: Path, filters: str) -> None:
    """链尾色调（甜点配色 gamma1.10/sat1.06）；失败保留原片。"""
    import subprocess
    ff_path = Path(__file__).resolve().parents[3] / "runtime" / "ffmpeg" / "bin" / "ffmpeg.exe"
    ff = str(ff_path) if ff_path.is_file() else "ffmpeg"
    tmp = video.with_name(video.stem + "_eq.mp4")
    r = subprocess.run([ff, "-y", "-v", "error", "-i", str(video),
                        "-vf", filters, "-c:v", "libx264", "-crf", "16",
                        "-preset", "fast", "-c:a", "copy", str(tmp)],
                       capture_output=True, timeout=600)
    if r.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 1000:
        shutil.move(str(tmp), str(video))
        log.info("[sr_modes] 链尾色调完成 (%s)", filters)
    else:
        tmp.unlink(missing_ok=True)
        log.warning("[sr_modes] 链尾色调失败（保留原片）")
