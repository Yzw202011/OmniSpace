"""H3 FaceRefine 后处理服务（2026-09-30 方案A，用户拍板 A+B）。

远景脸修复 pass：对已生成段落视频跑 Carasibana/ComfyUI-H3-FaceRefine
v2v 链（脸跟踪裁切 → H3 AV-latent 注入重生成 → 羽化缝回），音频经
MiniMaxH3NativeAudioLock 原样锁回。2026-09-30 实弹验证通过（音频保活
ffprobe 实证 + 远景脸修复视觉模型双时点实证，401s/5s 段）。

工作流模板 = h3_face_refine_workflow.json（验证版固化，23 节点）；
参数化仅五处：源片路径 / 两张身份参考图 / 每镜 prompt / 输出前缀。

降级铁律：节点/权重缺失、执行失败、产物无音轨——一律回退原片并
warning，绝不阻断导演台生成主流程（config 开关另有一层总门）。

2026-10-17 挂死根修（用户令 D）：轮询从「只看 /history」升级为
重启指纹 + 丢任务探测 + mark_busy 护闸 + 一次自愈重试——旧代码在
ComfyUI 中途重启（历史清空）后把「查无此任务」当「还在跑」盲等
1500s（10-16 扫荡战役 2/10 次实测中招）。

实弹五坑（模板内已固化规避，换机重装时对照）：
  ① NativeAudioLock 是外部包（Shrek3OnVH5 仓的 custom_nodes 子目录）
  ② SAM 权重目录是 models/sams（复数）；detector 用平铺文件名
  ③ canvas 必须 manual 768×768（auto 会缩 512 → latent mismatch）
  ④ 源片脸 <20px 时 YOLO 检不到（No face detected → 本层降级原片）
  ⑤ 调试脚本勿 import h3_engine（atexit 会杀 ComfyUI 子进程）
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
import uuid
from pathlib import Path
from typing import Any

from ...config import ROOT_DIR

log = logging.getLogger("omnispace.inference.h3_face_refine")

_COMFY_DIR = ROOT_DIR / "tools" / "ComfyUI_windows_portable"
_COMFY_MODELS = _COMFY_DIR / "ComfyUI" / "models"
_WORKFLOW_TEMPLATE = Path(__file__).with_name("h3_face_refine_workflow.json")

# SSRF 边界：本模块只服务本机 ComfyUI，基址为回环字面量常量
_COMFY_BASE = "http://127.0.0.1:8189"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# FaceRefine 链三关键节点（缺失任一即预检不过）
_REQUIRED_NODES = ("H3FaceTrackCrop", "MiniMaxH3NativeAudioLock",
                   "VHS_LoadVideoPath")
# 链上权重三件（sams 复数目录坑见模块头注）
_REQUIRED_WEIGHTS = (
    _COMFY_MODELS / "sams" / "sam_vit_b_01ec64.pth",
    _COMFY_MODELS / "ultralytics" / "bbox" / "face_yolov8m.pt",
    _COMFY_MODELS / "loras" /
    "minimax_h3_fl2v_turbo_8step_v1.0_768p_comfyui_"
    "resized_avg_rank_20_bf16.safetensors",
)

_READY_CACHE: bool | None = None
_TIMEOUT_S = 1500.0  # 8 步 turbo 实测 401s/5s 段；留分段变长余量
_LOST_CHECKS_LIMIT = 3  # 连续 3 次（约 30s）查无此任务判丢失


def face_refine_enabled() -> bool:
    """总门（config comfyui.h3_face_refine.enabled，默认 false——
    +~400s/段，只应在远景/全景镜需要修脸时手动开）。"""
    try:
        from ...config import get_config
        return bool(((get_config().get("comfyui") or {})
                     .get("h3_face_refine") or {}).get("enabled", False))
    except Exception:  # noqa: BLE001 - 配置异常按关闭（保守）
        return False


def facerefine_ready() -> bool:
    """链路就绪预检（节点注册 × 权重在位）。

    权重/节点结论缓存一次（安装态不随进程变）；ComfyUI 探活失败
    （未启动/超时）返回 False 但**不写缓存**——调用方通常在段生成后
    调本 pass（彼时 ComfyUI 已活），探活失败是暂态。
    """
    global _READY_CACHE
    if _READY_CACHE is not None:
        return _READY_CACHE
    if not all(p.is_file() for p in _REQUIRED_WEIGHTS):
        log.warning("FaceRefine 权重缺失（sams/ultralytics/loras），"
                    "pass 将降级跳过；安装清单见模块头注")
        _READY_CACHE = False
        return False
    try:
        oi = _api("GET", "/object_info", timeout=10.0)
    except Exception:  # noqa: BLE001 - ComfyUI 暂不可达：不缓存暂态
        return False
    ok = all(n in (oi or {}) for n in _REQUIRED_NODES)
    if not ok:
        log.warning("FaceRefine 节点缺失（%s），pass 将降级跳过；"
                    "安装清单见模块头注与记忆 h3-secondary-dev-research",
                    "、".join(n for n in _REQUIRED_NODES if n not in (oi or {})))
    _READY_CACHE = ok
    return ok


def _api(method: str, path: str,
         body: dict | None = None, timeout: float = 10.0) -> Any:
    # SSRF 边界：基址 = 模块级回环字面量；路径仅允许站内绝对路径；
    # 拼接后再解析校验 scheme/host 必须落回环（防参数拼接逃逸）
    if not path.startswith("/"):
        raise ValueError(f"FaceRefine API path 必须以 / 起始: {path!r}")
    url = urllib.parse.urljoin(_COMFY_BASE, path)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in _LOOPBACK_HOSTS:
        raise ValueError(f"FaceRefine 仅允许回环 URL: {url!r}")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


class _JobLost(RuntimeError):
    """ComfyUI 重启/任务蒸发——历史不跨重启保留，blind 轮询只会空转。"""


def _comfy_manager():
    """comfy_proc 管理器（懒加载；失败返回 None 走无守护降级路径）。"""
    try:
        from .comfy_proc import get_comfy_proc
        return get_comfy_proc()
    except Exception:  # noqa: BLE001 - 管理器不可用不阻断降级铁律
        return None


def _pid_in_queue(queue: dict, pid: str) -> bool:
    for key in ("queue_running", "queue_pending"):
        for item in queue.get(key) or []:
            try:
                # /queue 条目 = [执行序号, prompt_id, prompt, ...]，
                # 两个槽都查以兼容形状漂移（2026-10-17 E2E 实测：查
                # item[0] 永不匹配 → 把运行中任务误判丢失）
                for slot in (item[0], item[1] if len(item) > 1 else None):
                    if slot is not None and str(slot) == pid:
                        return True
            except Exception:  # noqa: BLE001 - 队列项形状异常按不匹配
                continue
    return False


def _build_workflow(src: Path, prompt: str, ref_images: list[Any],
                    comfy_input: Path, task_id: str) -> dict:
    wf = json.loads(_WORKFLOW_TEMPLATE.read_text(encoding="utf-8"))
    # 参数化五处（模板其余值均为 2026-09-30 验证版固化）
    wf["1"]["inputs"]["video"] = str(src)
    refs = [ref_images[0], ref_images[min(1, len(ref_images) - 1)]]
    for slot, img in (("6", refs[0]), ("7", refs[1])):
        name = f"fr_{task_id}_{slot}.png"
        comfy_input.mkdir(parents=True, exist_ok=True)
        img.save(comfy_input / name, format="PNG")
        wf[slot]["inputs"]["image"] = name
    wf["9"]["inputs"]["prompt"] = prompt
    wf["23"]["inputs"]["filename_prefix"] = f"face_refine/{task_id}"
    return wf


def _submit_and_wait(wf: dict, *, src: Path,
                     comfy_output: Path, task_id: str,
                     progress_cb: Any = None) -> Path:
    """提交 + 轮询一次；ComfyUI 重启/任务蒸发抛 _JobLost（上层重试）。"""
    cm = _comfy_manager()
    try:
        if cm is not None:
            cm.mark_busy()  # 精修加载 H3+SAM 权重可达数分钟，护住空闲看门狗
    except Exception:  # noqa: BLE001
        cm = None
    try:
        pid0 = cm.pid() if cm is not None else None
        try:
            resp = _api("POST", "/prompt",
                        body={"prompt": wf,
                              "client_id": f"omnispace-fr-{task_id}"},
                        timeout=30.0)
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            raise _JobLost(f"提交时 ComfyUI 不可达: {exc}") from exc
        if resp.get("error") or resp.get("node_errors"):
            detail = json.dumps(resp.get("node_errors") or resp["error"],
                                ensure_ascii=False)[:400]
            raise RuntimeError(f"工作流校验失败: {detail}")
        pid = str(resp["prompt_id"])
        log.info("FaceRefine 已提交 (prompt_id=%s, src=%s, comfy_pid=%s)",
                 pid, src.name, pid0)

        t0 = time.time()
        lost_checks = 0
        last_note = 0.0
        while time.time() - t0 < _TIMEOUT_S:
            time.sleep(10.0)
            if progress_cb is not None:
                progress_cb(min(0.95, (time.time() - t0) / _TIMEOUT_S),
                            "FaceRefine 脸修复")
            # 重启指纹：管理器在管 pid 变更/消失，或进程已死（外部
            # taskkill 后 Popen.pid 号不变，必须 poll 验尸）= 历史必清空
            if cm is not None and pid0 is not None:
                cur = cm.pid()
                dead = cm.poll() is not None
                if dead or cur != pid0:
                    raise _JobLost(f"ComfyUI 进程消亡/变更 {pid0}→{cur}")
            try:
                hist = _api("GET", f"/history/{pid}", timeout=10.0)
                entry = (hist or {}).get(pid)
            except Exception:  # noqa: BLE001 - 暂态网络错按仍在执行
                continue
            if time.time() - last_note >= 60.0:
                last_note = time.time()
                log.info("FaceRefine 轮询中 (%.0fs): %s",
                         time.time() - t0, src.name)
            if entry is None:
                # 不在历史：必须还在队列里才算活着
                try:
                    queue = _api("GET", "/queue", timeout=10.0)
                    alive = _pid_in_queue(queue or {}, pid)
                except Exception:  # noqa: BLE001 - 探测失败按在队（不误杀）
                    alive = True
                lost_checks = lost_checks + 1 if not alive else 0
                if lost_checks >= _LOST_CHECKS_LIMIT:
                    raise _JobLost("任务既不在队列也不在历史")
                continue
            lost_checks = 0
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
                        _check_audio_or_raise(prod)
                        out = src.with_name(f"{src.stem}_fr.mp4")
                        shutil.move(str(prod), out)
                        log.info("FaceRefine 完成 (%.0fs): %s → %s",
                                 time.time() - t0, src.name, out.name)
                        return out
            if status.get("completed"):
                raise RuntimeError("完成但无视频产物")
        raise TimeoutError(f"FaceRefine 超时（{_TIMEOUT_S:.0f}s）")
    finally:
        if cm is not None:
            try:
                cm.mark_idle()
            except Exception:  # noqa: BLE001
                pass


def _wait_comfy_ready(timeout_s: float = 120.0) -> bool:
    """冷启动就绪等待（/object_info 探活；spawn 返回 ≠ 服务已起）。"""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            _api("GET", "/object_info", timeout=5.0)
            return True
        except Exception:  # noqa: BLE001 - 未就绪继续等
            time.sleep(2.0)
    return False


def refine_video(src: Path, *, prompt: str,
                 ref_images: list[Any],
                 comfy_input: Path | None = None,
                 comfy_output: Path | None = None,
                 progress_cb: Any = None) -> Path:
    """单段视频远景脸修复；任何失败路径返回原片路径（零阻断）。

    Args:
        src: 段产物 mp4（绝对路径，VHS_LoadVideoPath 直读）
        prompt: 该镜六段式 prompt（脸部重生成的语义上下文）
        ref_images: 身份参考图（PIL.Image，取前两张；不足两张时重复
            首图——ReferenceToVideo 的 ref_image_0/1 两槽必须喂）
    Returns:
        修复后 mp4 路径；失败时原样返回 src
    """
    from .comfy_proc import COMFY_INPUT_DIR as _def_input
    from .comfy_proc import COMFY_OUTPUT_DIR as _def_output
    comfy_input = comfy_input or _def_input
    comfy_output = comfy_output or _def_output

    if not facerefine_ready():
        return src
    if not ref_images:
        log.warning("FaceRefine 无身份参考图，跳过（返回原片）: %s", src.name)
        return src

    for attempt in (1, 2):  # 第 2 次 = ComfyUI 重启后的自愈重试
        task_id = uuid.uuid4().hex[:12]
        try:
            wf = _build_workflow(src, prompt, ref_images, comfy_input,
                                 task_id)
            return _submit_and_wait(wf, src=src, comfy_output=comfy_output,
                                    task_id=task_id, progress_cb=progress_cb)
        except _JobLost as exc:
            log.warning("FaceRefine 任务丢失（第 %d 次）: %s (%s)",
                        attempt, src.name, exc)
            if attempt == 1:
                cm = _comfy_manager()
                if cm is not None:
                    try:
                        cm.spawn("comfyui_h3.log")  # 拉回活实例（幂等）
                    except Exception:  # noqa: BLE001
                        log.warning("FaceRefine 重拉 ComfyUI 失败",
                                    exc_info=True)
                # spawn 返回 ≠ 服务就绪：冷启动 20-60s，盲 POST 必吃闭门羹
                if not _wait_comfy_ready():
                    log.warning("FaceRefine 重拉后 ComfyUI 仍未就绪"
                                "（回退原片）: %s", src.name)
                    break
                continue
            log.warning("FaceRefine 重试后仍丢失（回退原片）: %s", src.name)
        except urllib.error.HTTPError as exc:
            log.warning("FaceRefine HTTP 失败（回退原片）%s: %s", src.name, exc)
            break
        except Exception as exc:  # noqa: BLE001 - 全失败路径回退原片
            log.warning("FaceRefine 失败（回退原片）%s: %s", src.name, exc)
            break
    return src


def _check_audio_or_raise(prod: Path) -> None:
    """音频保活自验：产物无 aac 音轨视为失败（实弹坑：vrs17 前版本
    剥音频；本链 1.1.x+AudioLock 实证保活，此为回归哨兵）。"""
    import subprocess
    import sys
    ffp_path = ROOT_DIR / "runtime" / "ffmpeg" / "bin" / "ffprobe.exe"
    ffp: str = (str(ffp_path) if ffp_path.is_file()
                else (shutil.which("ffprobe") or ""))
    if not ffp:
        return  # 探测器缺失不拦（ComfyUI VHS 组装默认带音轨）
    flags = (subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    r = subprocess.run(
        [ffp, "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(prod)],
        capture_output=True, text=True, timeout=30, creationflags=flags)
    if "audio" not in (r.stdout or ""):
        raise RuntimeError("FaceRefine 产物无音轨（音频保活哨兵失败）")


def refine_segments(seg_paths: list[Path], *, conv: dict,
                    assets: list[dict] | None,
                    asset_images: dict,
                    progress_cb=None) -> list[Path]:
    """导演台段批量脸修复（API 层薄封装入口；config 总门在本模块）。

    身份参考图 = 绑定角色资产前两张（file_path ↔ asset_files 值反查
    别名再取 asset_images）；每镜 prompt 取工程包对应 shot。
    任何失败路径返回原片列表（refine_video 内部兜底）。
    """
    if not face_refine_enabled() or not seg_paths:
        return seg_paths
    char_refs: list = []
    for a in assets or []:
        if a.get("kind") != "character" or not a.get("file_path"):
            continue
        alias = next((k for k, v in (conv.get("asset_files") or {}).items()
                      if v == a.get("file_path")), None)
        if alias and alias in asset_images:
            char_refs.append(asset_images[alias])
    shot_prompts = [s.get("prompt") or ""
                    for s in conv["project"].get("shots") or []]
    refined: list[Path] = []
    for i, sp in enumerate(seg_paths):
        if progress_cb:
            progress_cb(0.90, f"FaceRefine {i + 1}/{len(seg_paths)}")
        refined.append(refine_video(
            sp, prompt=shot_prompts[i] if i < len(shot_prompts) else "",
            ref_images=char_refs))
    return refined
