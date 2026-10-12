"""H3 逐帧放大后处理服务（2026-10-01 方案：ESRGAN 接导演台链，用户拍板"动"）。

清晰度线后处理：对已生成段落视频做逐帧软件超分（672/768 生成档 →
2688×1536 交付档[2K，2026-10-07 用户令升级]），内容零改动（2026-10-01
实弹四路对比赢家：边缘锐利发丝可辨零伪影 + 内容保真高，110s/5s 段）。
链路：VHS_LoadVideoPath → 4x-UltraSharp → ImageScale(目标档, lanczos)
→ VHS_VideoCombine（原音轨拼回）。

2K 交付档依据（2026-10-07）：4x-UltraSharp 对 672/768 生成档的原生
输出本就是 2688×1536，此前被 lanczos 缩回 1344×768 丢弃——2K 交付
只多编码不多算（ESRGAN 逐帧推理占 90%+ 耗时且与交付档无关）。失败
自动整段回退 1K（1344×768 旧档）重试，再失败回退原片（零阻断铁律）。

流式分块（2026-10-08 用户令，学 Video2X 架构）：VHS 整段批处理在
2K 档会因「全段帧常驻」爆内存（864×480 源 4x 后 3456×1920×整批帧
数）——改为按 _CHUNK_FRAMES 帧分块循环超分出无声小段、ffmpeg 合段
拼回源音轨，内存峰值 O(整段)→O(几帧)。帧数/fps 由 ffprobe 探测，
探测失败回退整段单趟旧行为。

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

# 交付档（2026-10-12 用户令：2K→1K——源分辨率 480p/720p 下 2K 多为
# 插值像素白耗时，1K 贴源且放大段省 ~3-5min/支；2K 常量保留供回切）
_DELIVERY_2K = (2688, 1536)
_DELIVERY_1K = (1344, 768)
_TARGET_W, _TARGET_H = _DELIVERY_1K
_TIMEOUT_S = 900.0  # 单趟实测 110s/5s 段；分块后每块远小于此
# 流式分块（2026-10-08 用户令，学 Video2X 逐帧流式）：每块帧数=显存
# 峰值旋钮——48 帧 × 3456×1920(4x 中间) × 4B ≈ 1.3GB，安全
_CHUNK_FRAMES = 48

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
    """柔和占比（定档常量 0.55，2026-10-02 调参定案，非配置旋钮）。

    7 档盲调实测（用户真实项目视频，皮肤区+背景细节区双区域视觉模型
    独立评审同指 0.55）：幻觉消失且细节几乎满血的甜点——0.70 残留
    锐化光晕，0.45 起丢最细发丝。调参记录见 _tune/（mix00~mix100）。
    0=全模型锐度（纹理幻觉），1=纯软件拉伸（无失真最软）。
    """
    return 0.55


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
                  progress_cb: Any = None,
                  target: tuple[int, int] = _DELIVERY_1K,
                  blend_factor: float | None = None,
                  out_name: str | None = None) -> Path:
    """单段视频逐帧放大到交付档；任何失败路径返回原片（零阻断）。

    target 默认 2K（2688×1536）；2K 失败自动回退 1K（1344×768）重试
    （2026-10-07 用户令「有问题回退到 1K」），再失败回退原片。

    out_name（2026-10-12 超分视频列）：产物文件名（相对 src 同目录）。
    缺省沿用管线命名 {stem}_fu.mp4；超分列传 {stem}_sr.mp4 与管线产物
    隔离命名（调用方按命名约定探测状态）。

    流式分块（2026-10-08 用户令，学 Video2X 架构）：长段按
    _CHUNK_FRAMES 帧分块循环（VHS frame_load_cap + skip 分批推进），
    每块独立超分出小段，最后 ffmpeg 合段拼回原音轨——内存峰值从
    O(整段) 降到 O(几帧)，2K 不再因批帧规模爆掉（此前 2K ImageScale
    整批 3456×1920 帧 OOM 实证）。帧数探测失败回退整段单趟旧行为。

    自验双哨兵：产物分辨率必须 = 目标交付档，音轨必须存在
    （ffmpeg ffprobe；探测链不可用时不拦，合段默认拼回源音轨）。
    """
    attempts = [target] + ([_DELIVERY_1K] if target != _DELIVERY_1K else [])
    meta = _probe_video_meta(src)
    last_exc: Exception | None = None
    for tw, th in attempts:
        try:
            if meta is not None and meta[0] > _CHUNK_FRAMES:
                return _upscale_chunked(src, meta[0], meta[1], tw, th,
                                        comfy_base=comfy_base,
                                        comfy_input=comfy_input,
                                        comfy_output=comfy_output,
                                        progress_cb=progress_cb,
                                        blend_factor=blend_factor,
                                        out_name=out_name)
            return _run_pass(src, tw, th, comfy_base=comfy_base,
                             comfy_input=comfy_input,
                             comfy_output=comfy_output,
                             progress_cb=progress_cb,
                             blend_factor=blend_factor,
                             out_name=out_name)
        except urllib.error.HTTPError as exc:
            last_exc = exc
            log.warning("逐帧放大 %dx%d HTTP 失败 %s: %s", tw, th,
                        src.name, exc)
        except Exception as exc:  # noqa: BLE001 - 逐档失败降下一档
            last_exc = exc
            log.warning("逐帧放大 %dx%d 失败 %s: %s", tw, th, src.name, exc)
    log.warning("逐帧放大全档失败（回退原片）%s: %s", src.name, last_exc)
    return src


def _ff_bin(name: str) -> str:
    """runtime ffmpeg 组件定位（缺失抛错，供探测/合段显式失败）。"""
    exe = ROOT_DIR / "runtime" / "ffmpeg" / "bin" / f"{name}.exe"
    if exe.is_file():
        return str(exe)
    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError(f"ffmpeg 组件缺失: {name}")


def _probe_video_meta(src: Path) -> tuple[int, int] | None:
    """(总帧数, fps) 探测；失败返回 None（回退整段单趟旧行为）。"""
    try:
        ffp = _ff_bin("ffprobe")
    except Exception:  # noqa: BLE001 - 组件缺失按单趟
        return None
    flags = (subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    try:
        r = subprocess.run(
            [ffp, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=nb_frames,r_frame_rate",
             "-of", "json", str(src)],
            capture_output=True, text=True, timeout=30, creationflags=flags)
        st = ((json.loads(r.stdout or "{}")).get("streams") or [{}])[0]
        nb = int(st.get("nb_frames") or 0)
        num, _, den = (st.get("r_frame_rate") or "").partition("/")
        fps = (float(num) / float(den or 1)) if den else 0.0
        if nb <= 0 or fps <= 0:
            return None
        return nb, max(1, round(fps))
    except Exception:  # noqa: BLE001 - 探测失败按单趟
        return None


def _build_upscale_wf(video_path: str, audio_path: str | None,
                      target_w: int, target_h: int,
                      frame_cap: int, skip: int, prefix: str,
                      fps: int = 24) -> dict:
    """超分工作流装配（整段/分块共用；au_name=None=无声分块）。"""
    wf = {
        "1": {"class_type": "VHS_LoadVideoPath", "inputs": {
            "video": video_path, "force_rate": 0,
            "custom_width": 0, "custom_height": 0,
            "frame_load_cap": frame_cap, "skip_first_frames": skip,
            "select_every_nth": 1, "format": "None"}},
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
            "width": target_w, "height": target_h,
            "crop": "disabled"}},
        "3b": {"class_type": "ImageScale", "inputs": {
            "image": ["1", 0], "upscale_method": "lanczos",
            "width": target_w, "height": target_h,
            "crop": "disabled"}},
        "4": {"class_type": "ImageBlend", "inputs": {
            "image1": ["3a", 0], "image2": ["3b", 0],
            "blend_factor": round(1.0 - _soft_mix(), 2),
            "blend_mode": "normal"}},
    }
    combine = {"images": ["4", 0], "frame_rate": fps, "loop_count": 0,
               "filename_prefix": prefix,
               "format": "video/h264-mp4", "pix_fmt": "yuv420p",
               "crf": 16, "save_metadata": True, "trim_to_audio": False,
               "pingpong": False, "save_output": True}
    if audio_path:
        wf["5"] = {"class_type": "VHS_LoadAudioUpload", "inputs": {
            "audio": audio_path}}
        combine["audio"] = ["5", 0]
    wf["6"] = {"class_type": "VHS_VideoCombine", "inputs": combine}
    return wf


def _wait_product(comfy_base: str, comfy_output: Path, pid: str,
                  target_w: int, target_h: int, src_name: str,
                  out: Path, progress_cb: Any = None,
                  budget_s: float | None = None) -> Path:
    """轮询 /history 至产物落盘并自验，移动到 out 返回。

    budget_s：本趟等待预算。分块路径按块序递增（2026-10-11 挡位战役
    实测同链内逐块衰减：块1 ~90s → 块4 837s+，900s 固定预算长行撞顶
    率 ~50% 且超时后 ComfyUI 进程死亡；2400s 全程两支稳过）。
    """
    budget = float(budget_s or _TIMEOUT_S)
    t0 = time.time()
    while time.time() - t0 < budget:
        time.sleep(10.0)
        if progress_cb:
            progress_cb(min(0.95, (time.time() - t0) / budget),
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
                    _check_output_or_raise(prod, target_w, target_h)
                    shutil.move(str(prod), out)
                    return out
        if status.get("completed"):
            raise RuntimeError("完成但无视频产物")
    raise TimeoutError(f"逐帧放大超时（{budget:.0f}s）")


def _run_pass(src: Path, target_w: int, target_h: int, *,
              comfy_base: str, comfy_input: Path | None,
              comfy_output: Path | None, progress_cb: Any = None,
              frame_cap: int = 0, skip: int = 0,
              with_audio: bool = True, tag: str = "",
              fps: int = 24,
              blend_factor: float | None = None,
              budget_s: float | None = None,
              out_name: str | None = None) -> Path:
    """单趟超分（整段带音轨 / 单个分块无声）。"""
    from .comfy_proc import COMFY_INPUT_DIR as _def_input
    from .comfy_proc import COMFY_OUTPUT_DIR as _def_output
    comfy_input = comfy_input or _def_input
    comfy_output = comfy_output or _def_output

    if not frame_upscale_ready(comfy_base):
        raise RuntimeError("逐帧放大链路未就绪（权重/节点缺失）")

    task_id = uuid.uuid4().hex[:12]
    in_name = f"fu_{task_id}.mp4"
    au_name = f"fu_{task_id}_audio.mp4" if with_audio else None
    comfy_input.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, comfy_input / in_name)
    if au_name:
        shutil.copy(src, comfy_input / au_name)
    prefix = f"frame_upscale/{task_id}" + (f"_{tag}" if tag else "")
    # VHS 校验绝对路径（裸文件名 400：Invalid file path 实锤）
    wf = _build_upscale_wf(str(comfy_input / in_name),
                           str(comfy_input / au_name) if au_name else None,
                           target_w, target_h,
                           frame_cap, skip, prefix, fps=fps)
    resp = _api(comfy_base, "POST", "/prompt",
                body={"prompt": wf, "client_id": f"omnispace-fu-{task_id}"},
                timeout=30.0)
    if resp.get("error") or resp.get("node_errors"):
        detail = json.dumps(resp.get("node_errors") or resp["error"],
                            ensure_ascii=False)[:400]
        raise RuntimeError(f"工作流校验失败: {detail}")
    pid = str(resp["prompt_id"])
    log.info("逐帧放大已提交 (%dx%d, cap=%s skip=%s, prompt_id=%s, src=%s)",
             target_w, target_h, frame_cap, skip, pid, src.name)
    out = src.with_name(out_name or f"{src.stem}_fu{tag}.mp4")
    out = _wait_product(comfy_base, comfy_output, pid, target_w, target_h,
                        src.name, out, progress_cb=progress_cb,
                        budget_s=budget_s)
    log.info("逐帧放大单趟完成: %s → %s", src.name, out.name)
    return out


def _upscale_chunked(src: Path, total_frames: int, fps: int,
                     target_w: int, target_h: int, *,
                     comfy_base: str, comfy_input: Path | None,
                     comfy_output: Path | None,
                     progress_cb: Any = None,
                     blend_factor: float | None = None,
                     out_name: str | None = None) -> Path:
    """流式分块超分（Video2X 式）：每块 _CHUNK_FRAMES 帧独立超分出
    无声小段，块间内存互不相干（O(几帧)），最后 ffmpeg 合段拼回源音轨。
    """
    from .comfy_proc import COMFY_OUTPUT_DIR as _def_output
    comfy_output = comfy_output or _def_output
    n_chunks = (total_frames + _CHUNK_FRAMES - 1) // _CHUNK_FRAMES
    log.info("逐帧放大流式分块: %d 帧 → %d 块（每块 %d 帧，%dx%d）",
             total_frames, n_chunks, _CHUNK_FRAMES, target_w, target_h)
    chunks: list[Path] = []
    for ci in range(n_chunks):
        skip = ci * _CHUNK_FRAMES
        cap = min(_CHUNK_FRAMES, total_frames - skip)
        if progress_cb:
            progress_cb(min(0.95, ci / n_chunks),
                        f"逐帧放大 块 {ci + 1}/{n_chunks}")
        # 块序递增预算（2026-10-11 战役实测：链内逐块衰减 90→257→450→
        # 837s+，块4 撞 900s 顶；块序线性放宽 c1=900 … c6=2400，与
        # 2400s 全程成功样本吻合）
        chunks.append(_run_pass(src, target_w, target_h,
                                comfy_base=comfy_base,
                                comfy_input=comfy_input,
                                comfy_output=comfy_output,
                                progress_cb=progress_cb,
                                frame_cap=cap, skip=skip,
                                with_audio=False,
                                blend_factor=blend_factor,
                                tag=f"{target_w}x{target_h}_c{ci:03d}",
                                fps=fps,
                                budget_s=max(_TIMEOUT_S,
                                             600.0 + 300.0 * (ci + 1))))
    out = src.with_name(out_name or f"{src.stem}_fu.mp4")
    _concat_and_mux(chunks, src, out, fps, target_w=target_w, target_h=target_h,
                    progress_cb=progress_cb)
    # 分块中间产物回收（每块 5~16MB，留存会无限累积）
    for c in chunks:
        try:
            c.unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001 - 回收失败不阻断交付
            log.warning("分块回收失败（忽略）%s: %s", c.name, exc)
    log.info("逐帧放大流式合段完成: %d 块 → %s", len(chunks), out.name)
    return out


def _concat_and_mux(chunks: list[Path], src: Path, out: Path,
                    fps: int, progress_cb: Any = None,
                    target_w: int | None = None,
                    target_h: int | None = None) -> None:
    """无声分块 → concat demuxer 合段（-c:v copy）→ 拼回源音轨。

    终验尺寸=调用方实际交付档（2026-10-12 用户令 1K 交付——旧版此处
    硬编码 *_DELIVERY_2K，切 1K 后合段成品必被毙回原片，实锤于
    720p×10s 探针）。
    """
    ffmpeg = _ff_bin("ffmpeg")
    flags = (subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    list_file = out.with_suffix(".concat.txt")
    lines = [f"file '{str(p.resolve()).replace(chr(39), chr(39) * 2)}'"
             for p in chunks]
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8",
                         newline="\n")  # LF 强制：清单混入回车符即 ffmpeg 打不开路径（E2E 实锤）
    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0",
           "-i", str(list_file), "-i", str(src),
           "-map", "0:v:0", "-map", "1:a:0?",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-shortest", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                       creationflags=flags)
    try:
        list_file.unlink(missing_ok=True)
    except OSError:
        pass
    if r.returncode != 0:
        raise RuntimeError(
            f"分块合段失败: {(r.stderr or '')[-300:]}")
    _check_output_or_raise(out, target_w or _TARGET_W,
                           target_h or _TARGET_H)


def _check_output_or_raise(prod: Path, target_w: int = _TARGET_W,
                           target_h: int = _TARGET_H) -> None:
    """产物自验：分辨率必须到目标交付档（ffprobe；探测器缺失不拦）。"""
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
    if dims != f"{target_w},{target_h}":
        raise RuntimeError(
            f"逐帧放大产物分辨率 {dims} != {target_w}x{target_h}")


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
