"""H3 逐帧放大后处理服务（2026-10-01 方案：ESRGAN 接导演台链，用户拍板"动"）。

清晰度线后处理：对已生成段落视频做逐帧软件超分（672/768 生成档 →
1344×768 交付档），内容零改动（2026-10-01 实弹四路对比赢家：边缘锐利
发丝可辨零伪影 + 内容保真高，110s/5s 段）。链路：
VHS_LoadVideoPath → 4x-UltraSharp → ImageScale(1344×768, lanczos)
→ VHS_VideoCombine（原音轨拼回）。

降级铁律同 h3_face_refine：权重/节点缺失、执行失败、产物分辨率或
音轨不自验——一律回退原片并 warning，绝不阻断生成主流程。

与 h3_face_refine 的时序约定：脸修复在前（吃生成原分辨率），逐帧
放大殿后（保证交付前最后一步是高清化）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from ...config import ROOT_DIR

log = logging.getLogger("omnispace.inference.h3_frame_upscale")

_COMFY_DIR = ROOT_DIR / "tools" / "ComfyUI_windows_portable"
_ESRGAN_WEIGHT = (_COMFY_DIR / "ComfyUI" / "models" / "upscale_models"
                  / "4x-UltraSharp.pth")
_REQUIRED_NODES = ("VHS_LoadVideoPath", "UpscaleModelLoader",
                   "ImageUpscaleWithModel", "VHS_VideoCombine")

# 交付档（H3 16GB 档原生上限；图像域 lanczos 回缩去 4x 锯齿）
_TARGET_W, _TARGET_H = 1344, 768
_TIMEOUT_S = 900.0  # 实测 110s/5s 段；留分段变长余量

_READY_CACHE: bool | None = None


def frame_upscale_enabled() -> bool:
    """总门（config comfyui.h3_frame_upscale.enabled，默认 false——
    +~110s/段，只在需要高清交付时手动开）。"""
    try:
        from ...config import get_config
        return bool(((get_config().get("comfyui") or {})
                     .get("h3_frame_upscale") or {}).get("enabled", False))
    except Exception:  # noqa: BLE001 - 配置异常按关闭（保守）
        return False


def _soft_mix() -> float:
    """柔和占比（config comfyui.h3_frame_upscale.soft_mix，默认 0.45）。

    0=全模型锐度（纹理幻觉风险），1=纯软件拉伸（无失真最软）。
    用户报「放大后失真怪怪的」后引入的调和旋钮（2026-10-02）。
    """
    try:
        from ...config import get_config
        v = float(((get_config().get("comfyui") or {})
                   .get("h3_frame_upscale") or {}).get("soft_mix", 0.45))
        return min(1.0, max(0.0, v))
    except Exception:  # noqa: BLE001 - 配置异常按默认
        return 0.45


def frame_upscale_ready(comfy_base: str = "http://127.0.0.1:8189") -> bool:
    """链路就绪预检（权重结论缓存；ComfyUI 探活失败=暂态不缓存，
    同 h3_face_refine.facerefine_ready 的分级约定）。"""
    global _READY_CACHE
    if _READY_CACHE is not None:
        return _READY_CACHE
    if not _ESRGAN_WEIGHT.is_file():
        log.warning("逐帧放大权重缺失（%s），pass 将降级跳过；"
                    "下载源 hf-mirror.com/lokCX/4x-Ultrasharp", _ESRGAN_WEIGHT)
        _READY_CACHE = False
        return False
    try:
        oi = json.loads(urllib.request.urlopen(
            f"{comfy_base}/object_info", timeout=10).read())
    except Exception:  # noqa: BLE001 - ComfyUI 暂不可达：不缓存暂态
        return False
    ok = all(n in oi for n in _REQUIRED_NODES)
    if not ok:
        log.warning("逐帧放大节点缺失（%s），pass 将降级跳过（VHS 包）",
                    "、".join(n for n in _REQUIRED_NODES if n not in oi))
    _READY_CACHE = ok
    return ok


def _api(base: str, method: str, path: str,
         body: dict | None = None, timeout: float = 10.0) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{base}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def upscale_video(src: Path, *,
                  comfy_base: str = "http://127.0.0.1:8189",
                  comfy_input: Path | None = None,
                  comfy_output: Path | None = None,
                  progress_cb: Any = None) -> Path:
    """单段视频逐帧放大到交付档；任何失败路径返回原片（零阻断）。

    自验双哨兵：产物分辨率必须 = 1344×768，音轨必须存在
    （ffmpeg ffprobe；探测链不可用时不拦，VHS 组装默认带音轨）。
    """
    from .comfy_proc import COMFY_INPUT_DIR as _def_input
    from .comfy_proc import COMFY_OUTPUT_DIR as _def_output
    comfy_input = comfy_input or _def_input
    comfy_output = comfy_output or _def_output

    if not frame_upscale_ready(comfy_base):
        return src

    task_id = uuid.uuid4().hex[:12]
    in_name = f"fu_{task_id}.mp4"
    au_name = f"fu_{task_id}_audio.mp4"
    try:
        comfy_input.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, comfy_input / in_name)
        shutil.copy(src, comfy_input / au_name)
        wf = {
            "1": {"class_type": "VHS_LoadVideoPath", "inputs": {
                "video": str(comfy_input / in_name), "force_rate": 0,
                "custom_width": 0, "custom_height": 0, "frame_load_cap": 0,
                "skip_first_frames": 0, "select_every_nth": 1,
                "format": "None"}},
            "2": {"class_type": "UpscaleModelLoader", "inputs": {
                "model_name": "4x-UltraSharp.pth"}},
            "3": {"class_type": "ImageUpscaleWithModel", "inputs": {
                "upscale_model": ["2", 0], "image": ["1", 0]}},
            # 双路调和（2026-10-02 用户报放大后失真：4x-UltraSharp 对
            # 软质 3D 二次元风有皮肤纹理幻觉+过度锐化）——模型锐利版与
            # lanczos 柔和版按 soft_mix 调和：0=纯柔和（无幻觉），
            # 1=全模型锐度。默认 0.55。
            "3a": {"class_type": "ImageScale", "inputs": {
                "image": ["3", 0], "upscale_method": "lanczos",
                "width": _TARGET_W, "height": _TARGET_H,
                "crop": "disabled"}},
            "3b": {"class_type": "ImageScale", "inputs": {
                "image": ["1", 0], "upscale_method": "lanczos",
                "width": _TARGET_W, "height": _TARGET_H,
                "crop": "disabled"}},
            "4": {"class_type": "ImageBlend", "inputs": {
                "image1": ["3a", 0], "image2": ["3b", 0],
                "blend_factor": 1.0 - _soft_mix(),
                "blend_mode": "normal"}},
            "5": {"class_type": "VHS_LoadAudioUpload", "inputs": {
                "audio": au_name}},
            "6": {"class_type": "VHS_VideoCombine", "inputs": {
                "images": ["4", 0], "audio": ["5", 0], "frame_rate": 24,
                "loop_count": 0,
                "filename_prefix": f"frame_upscale/{task_id}",
                "format": "video/h264-mp4", "pix_fmt": "yuv420p",
                "crf": 16, "save_metadata": True, "trim_to_audio": False,
                "pingpong": False, "save_output": True}},
        }
        resp = _api(comfy_base, "POST", "/prompt",
                    body={"prompt": wf, "client_id": f"omnispace-fu-{task_id}"},
                    timeout=30.0)
        if resp.get("error") or resp.get("node_errors"):
            detail = json.dumps(resp.get("node_errors") or resp["error"],
                                ensure_ascii=False)[:400]
            raise RuntimeError(f"工作流校验失败: {detail}")
        pid = str(resp["prompt_id"])
        log.info("逐帧放大已提交 (prompt_id=%s, src=%s)", pid, src.name)

        t0 = time.time()
        while time.time() - t0 < _TIMEOUT_S:
            time.sleep(10.0)
            if progress_cb:
                progress_cb(min(0.95, (time.time() - t0) / _TIMEOUT_S),
                            "逐帧放大")
            try:
                hist = _api(comfy_base, "GET", f"/history/{pid}", timeout=10.0)
            except Exception:  # noqa: BLE001 - 404=执行中
                continue
            entry = (hist or {}).get(pid) or {}
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                msgs = json.dumps(status.get("messages") or [],
                                  ensure_ascii=False)[:400]
                raise RuntimeError(f"执行失败: {msgs}")
            for node_out in (entry.get("outputs") or {}).values():
                for item in (node_out.get("gifs")
                             or node_out.get("images") or []):
                    fn = str(item.get("filename") or "")
                    if fn.endswith((".mp4", ".webm")) or item.get("animated"):
                        sub = str(item.get("subfolder") or "")
                        prod = comfy_output / (Path(sub) / fn if sub else fn)
                        if not prod.is_file():
                            continue
                        _check_output_or_raise(prod)
                        out = src.with_name(f"{src.stem}_fu.mp4")
                        shutil.move(str(prod), out)
                        log.info("逐帧放大完成 (%.0fs): %s → %s",
                                 time.time() - t0, src.name, out.name)
                        return out
            if status.get("completed"):
                raise RuntimeError("完成但无视频产物")
        raise TimeoutError(f"逐帧放大超时（{_TIMEOUT_S:.0f}s）")
    except urllib.error.HTTPError as exc:
        log.warning("逐帧放大 HTTP 失败（回退原片）%s: %s", src.name, exc)
    except Exception as exc:  # noqa: BLE001 - 全失败路径回退原片
        log.warning("逐帧放大失败（回退原片）%s: %s", src.name, exc)
    return src


def _check_output_or_raise(prod: Path) -> None:
    """产物自验：分辨率必须到交付档（ffprobe；探测器缺失不拦）。"""
    ffp_path = ROOT_DIR / "runtime" / "ffmpeg" / "bin" / "ffprobe.exe"
    ffp: str = (str(ffp_path) if ffp_path.is_file()
                else (shutil.which("ffprobe") or ""))
    if not ffp:
        return
    flags = (subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    r = subprocess.run(
        [ffp, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0",
         str(prod)],
        capture_output=True, text=True, timeout=30, creationflags=flags)
    dims = (r.stdout or "").strip()
    if dims != f"{_TARGET_W},{_TARGET_H}":
        raise RuntimeError(
            f"逐帧放大产物分辨率 {dims} != {_TARGET_W}x{_TARGET_H}")


def upscale_segments(seg_paths: list[Path], *,
                     progress_cb=None) -> list[Path]:
    """导演台段批量逐帧放大（API 层薄封装入口；config 总门在本模块）。

    时序约定：调用点必须排在 FaceRefine pass 之后（交付前最后一步
    高清化）。全失败路径返回原片列表。
    """
    if not frame_upscale_enabled() or not seg_paths:
        return seg_paths
    refined: list[Path] = []
    for i, sp in enumerate(seg_paths):
        if progress_cb:
            progress_cb(0.95, f"逐帧放大 {i + 1}/{len(seg_paths)}")
        refined.append(upscale_video(sp))
    return refined
