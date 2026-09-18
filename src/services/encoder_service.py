"""OmniSpace AI v2.3 AV1/H.264 视频编码服务（TASK-013 / 规格 §3.5 AV1 编码调度）。

职责：
- FFmpeg 发现：runtime/ffmpeg/bin/ffmpeg.exe → tools/downloads/ffmpeg*/** → PATH
- 启动时探测可用编码器（av1_nvenc / h264_nvenc / hevc_nvenc / libsvtav1 / libx264）
- 硬件调度（规格 §3.5）：
    RTX 50系  → av1_nvenc 硬编（速度最快）
    RTX 30/40 → h264_nvenc 硬编
    AMD       → libsvtav1 软编（多线程）
    纯 CPU    → libsvtav1 软编（低速 preset）
  编码失败自动降级链 AV1 → H.264（按候选链逐一重试）。
- 编码参数（规格 §3.5）：CRF 23、preset medium、音频 AAC 256kbps。
- 进度回调：解析 ffmpeg stderr 的 frame=/time= 字段换算 0.0~1.0 进度。
- 输出校验：文件存在且 >=1KB（MIN_OUTPUT_BYTES），ffprobe 可用时校验时长 > 0
  （时长探测带 3 次短重试，抵御高负载下新文件被杀软/索引短暂独占的误判）。

友好降级：ffmpeg 缺失时 EncoderService.available=False，所有编码方法
抛出 EncoderUnavailableError（API 层捕获后返回友好错误，绝不崩溃）。

单例用法::

    from src.services.encoder_service import get_encoder_service
    enc = get_encoder_service()
    if enc.available:
        enc.encode_frames_to_video(frame_dir, out_path, fps=24, resolution="1080p")
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368

from __future__ import annotations

import importlib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import DATA_DIR, ROOT_DIR

log = logging.getLogger("omnispace.encoder")

# ── 类型别名 ────────────────────────────────────────────────────
# 进度回调：fn(progress: float 0..1, message: str)
ProgressCallback = Callable[[float, str], None]

# ── 常量（规格 §3.5 编码参数）──────────────────────────────────
DEFAULT_CRF = 23                 # 质量：CRF 23（可调）
DEFAULT_PRESET = "medium"        # x264/svt 预设
AUDIO_BITRATE = "256k"           # AAC 256kbps
MIN_OUTPUT_BYTES = 1024          # 输出校验：>1KB（短时长/平坦画面合法小文件
                                 # 亦可过检，如 1s 720p 纯色≈4KB；有效性由
                                 # ffprobe 时长>0 兜底，空容器仅数百字节被拦截）
DEFAULT_TIMEOUT_S = 1800         # 单次编码超时（30 分钟）

# 关注的编码器清单（探测解析目标）
TRACKED_ENCODERS = ("av1_nvenc", "h264_nvenc", "hevc_nvenc", "libsvtav1", "libx264")

# 分辨率字符串 → (宽, 高)
RESOLUTION_MAP: dict[str, tuple[int, int]] = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "2k": (2560, 1440),
    "4k": (3840, 2160),
}

# concat_clips 质量档 → CRF
QUALITY_CRF: dict[str, int] = {"high": 18, "medium": 23, "low": 28}

# ffmpeg -encoders 输出行： " V..... av1_nvenc  NVIDIA NVENC av1 encoder (codec av1)"
_ENCODER_LINE_RE = re.compile(r"^\s*[VAS]\S*\s+(\S+)")
# ffmpeg stderr 进度字段： frame=  123 ... time=00:00:05.12 ...
_FRAME_RE = re.compile(r"frame=\s*(\d+)")
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


class EncoderUnavailableError(RuntimeError):
    """ffmpeg 不可用（未找到二进制）。"""


class EncodeError(RuntimeError):
    """编码执行失败（所有候选编码器均失败/超时/输出校验不通过）。"""


def _try_import(name: str) -> Any:
    """容错导入可选依赖。"""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


_pynvml = _try_import("pynvml")


class EncoderService:
    """视频编码服务——FFmpeg 子进程封装 + 硬件调度 + 降级链。

    线程安全；所有外部调用带超时；依赖缺失时 available=False 友好降级。
    """

    _instance: EncoderService | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._ffmpeg: str | None = None
        self._ffprobe: str | None = None
        self._encoders: dict[str, bool] = {name: False for name in TRACKED_ENCODERS}
        self._hw_class: str = "cpu"           # rtx50 / rtx40 / rtx30 / nvidia_other / amd / cpu
        self._gpu_name: str = ""
        self._probed = False
        self._probe_lock = threading.Lock()

        self._ffmpeg = self.discover_ffmpeg()
        if self._ffmpeg:
            self._ffprobe = self._sibling_tool(self._ffmpeg, "ffprobe")
            self._hw_class, self._gpu_name = self.detect_hardware()
            self._probe_encoders()
        else:
            log.warning("未找到 FFmpeg，编码服务不可用（友好降级）")

    # ── 单例 ────────────────────────────────────────────────────

    @classmethod
    def instance(cls) -> EncoderService:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ── 属性 ────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """ffmpeg 是否可用。"""
        return self._ffmpeg is not None

    @property
    def ffmpeg_path(self) -> str:
        return self._ffmpeg or ""

    @property
    def ffprobe_path(self) -> str:
        """ffprobe 路径（空串=不可用）。供流探测（音频轨判定等）。"""
        return self._ffprobe or ""

    @property
    def hardware_class(self) -> str:
        return self._hw_class

    def status(self) -> dict:
        """编码服务状态快照（供 /v1 状态端点使用）。"""
        return {
            "available": self.available,
            "ffmpeg": self._ffmpeg or "",
            "ffprobe": self._ffprobe or "",
            "encoders": dict(self._encoders),
            "hardware_class": self._hw_class,
            "gpu_name": self._gpu_name,
        }

    # ═══════════════════════════════════════════════════════════
    #  FFmpeg 发现（TASK-013：runtime → tools/downloads → PATH）
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def discover_ffmpeg() -> str | None:
        """按优先级发现 ffmpeg 可执行文件，找不到返回 None。

        搜索顺序：
          1. {ROOT}/runtime/ffmpeg/bin/ffmpeg.exe
          2. {ROOT}/tools/downloads/ffmpeg*/**/ffmpeg.exe（glob 递归）
          3. 系统 PATH（shutil.which）
        """
        exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"

        # 1) runtime/ffmpeg/bin
        candidate = ROOT_DIR / "runtime" / "ffmpeg" / "bin" / exe
        if candidate.is_file():
            log.info("FFmpeg 发现于 runtime: %s", candidate)
            return str(candidate)

        # 2) tools/downloads/ffmpeg*/**（限定深度避免扫描整棵树）
        downloads = ROOT_DIR / "tools" / "downloads"
        if downloads.is_dir():
            try:
                for sub in sorted(downloads.iterdir()):
                    if not (sub.is_dir() and sub.name.lower().startswith("ffmpeg")):
                        continue
                    # 常见布局：ffmpeg*/bin/ffmpeg.exe 与 ffmpeg*/ffmpeg.exe
                    for rel in (Path("bin") / exe, Path(exe)):
                        cand = sub / rel
                        if cand.is_file():
                            log.info("FFmpeg 发现于 downloads: %s", cand)
                            return str(cand)
                    # 再往下探一层（ffmpeg*/**/bin/ffmpeg.exe）
                    for cand in sub.glob(f"*/*/bin/{exe}"):
                        if cand.is_file():
                            log.info("FFmpeg 发现于 downloads(深层): %s", cand)
                            return str(cand)
            except OSError as exc:
                log.debug("扫描 tools/downloads 失败: %s", exc)

        # 3) PATH
        found = shutil.which("ffmpeg")
        if found:
            log.info("FFmpeg 发现于 PATH: %s", found)
            return found
        return None

    @staticmethod
    def _sibling_tool(ffmpeg_path: str, tool: str) -> str | None:
        """在 ffmpeg 同目录寻找姊妹工具（如 ffprobe）。"""
        exe = f"{tool}.exe" if os.name == "nt" else tool
        sibling = Path(ffmpeg_path).parent / exe
        if sibling.is_file():
            return str(sibling)
        return shutil.which(tool)

    # ═══════════════════════════════════════════════════════════
    #  编码器探测
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def parse_encoders_output(text: str) -> dict[str, bool]:
        """解析 `ffmpeg -hide_banner -encoders` 输出，返回关注编码器的可用性。

        输出格式示例::
            Encoders:
             V..... = Video
             ------
             V....D av1_nvenc            NVIDIA NVENC av1 encoder (codec av1)
             V....D libsvtav1            SVT-AV1 encoder (codec av1)
        """
        found = {name: False for name in TRACKED_ENCODERS}
        for line in text.splitlines():
            m = _ENCODER_LINE_RE.match(line)
            if not m:
                continue
            name = m.group(1)
            if name in found:
                found[name] = True
        return found

    def _probe_encoders(self) -> None:
        """运行 ffmpeg -encoders 并解析可用编码器（一次性，带锁）。"""
        with self._probe_lock:
            if self._probed or not self._ffmpeg:
                return
            try:
                proc = subprocess.run(
                    [self._ffmpeg, "-hide_banner", "-encoders"],
                    capture_output=True, text=True, timeout=30,
                    encoding="utf-8", errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW
                    if os.name == "nt" else 0,
                )
                self._encoders = self.parse_encoders_output(proc.stdout or "")
                log.info(
                    "编码器探测: %s",
                    {k: v for k, v in self._encoders.items() if v} or "（无硬件编码器）",
                )
            except Exception as exc:  # noqa: BLE001 - 探测失败降级为全不可用
                log.warning("编码器探测失败，按仅软件编码器处理: %s", exc)
                self._encoders = {name: False for name in TRACKED_ENCODERS}
            self._probed = True

    def has_encoder(self, name: str) -> bool:
        return self._encoders.get(name, False)

    # ═══════════════════════════════════════════════════════════
    #  硬件检测（规格 §3.5 调度矩阵）
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def classify_gpu(gpu_name: str) -> str:
        """根据 GPU 名称分类：rtx50 / rtx40 / rtx30 / nvidia_other / amd / cpu。"""
        n = (gpu_name or "").lower()
        if not n:
            return "cpu"
        if "amd" in n or "radeon" in n:
            return "amd"
        # RTX 系列代数识别（5070/5080/5090 → rtx50；40xx → rtx40；30xx → rtx30）
        m = re.search(r"rtx\s*(\d{2})", n)
        if m:
            gen = int(m.group(1))
            if gen >= 50:
                return "rtx50"
            if gen >= 40:
                return "rtx40"
            if gen >= 30:
                return "rtx30"
            return "nvidia_other"
        if "nvidia" in n or "geforce" in n or "gtx" in n or "quadro" in n:
            return "nvidia_other"
        return "cpu"

    @staticmethod
    def detect_hardware() -> tuple[str, str]:
        """检测 GPU，返回 (硬件分类, GPU 名称)。pynvml → torch → 纯 CPU 降级。"""
        # 1) pynvml
        if _pynvml is not None:
            try:
                _pynvml.nvmlInit()
                handle = _pynvml.nvmlDeviceGetHandleByIndex(0)
                raw = _pynvml.nvmlDeviceGetName(handle)
                name = raw.decode() if isinstance(raw, bytes) else str(raw)
                return EncoderService.classify_gpu(name), name
            except Exception as exc:
                log.debug("pynvml 检测 GPU 失败: %s", exc)
        # 2) torch
        torch = _try_import("torch")
        if torch is not None:
            try:
                if torch.cuda.is_available():
                    name = torch.cuda.get_device_name(0)
                    return EncoderService.classify_gpu(name), name
            except Exception as exc:
                log.debug("torch 检测 GPU 失败: %s", exc)
        return "cpu", ""

    # ═══════════════════════════════════════════════════════════
    #  编码计划（硬件调度 + 降级链）
    # ═══════════════════════════════════════════════════════════

    def _encoder_candidates(self, codec: str, quality: str) -> list[tuple[str, list[str]]]:
        """按硬件调度矩阵构造 (编码器, 参数) 候选链，首个为最优选择。

        规格 §3.5：
          RTX 50系 → av1_nvenc；RTX 30/40 → h264_nvenc；
          AMD → libsvtav1；纯 CPU → libsvtav1（低速 preset）。
        失败降级链：AV1 → H.264（逐个候选重试，含软编兜底）。
        """
        crf = QUALITY_CRF.get(quality, DEFAULT_CRF)
        hw = self._hw_class
        chain: list[tuple[str, list[str]]] = []

        def nvenc_args(extra_cq: bool = True) -> list[str]:
            # NVENC 用 -cq 近似 CRF；preset p4 ≈ medium
            args = ["-preset", "p4"]
            if extra_cq:
                args += ["-cq", str(crf)]
            return args

        def svt_args(slow: bool = False) -> list[str]:
            # SVT-AV1 preset 0(最慢/最优)~13(最快)；低速 preset=4，标准=6
            return ["-crf", str(crf), "-preset", "4" if slow else "6"]

        def x264_args() -> list[str]:
            return ["-crf", str(crf), "-preset", DEFAULT_PRESET]

        want = (codec or "h264").lower()
        if want == "av1":
            if hw == "rtx50":
                chain.append(("av1_nvenc", nvenc_args()))
            if hw in ("rtx50", "amd", "cpu", "nvidia_other", "rtx40", "rtx30"):
                # AMD/CPU 首选 SVT-AV1；NVIDIA 非 50 系时作为 AV1 软编候选
                chain.append(("libsvtav1", svt_args(slow=(hw == "cpu"))))
            # 降级链 AV1 → H.264
            if hw.startswith("rtx") or hw == "nvidia_other":
                chain.append(("h264_nvenc", nvenc_args()))
            chain.append(("libx264", x264_args()))
        elif want in ("h265", "hevc"):
            if hw.startswith("rtx") or hw == "nvidia_other":
                chain.append(("hevc_nvenc", nvenc_args()))
            chain.append(("libx264", x264_args()))  # 无 hevc 软编跟踪时降级 H.264
        else:  # h264 默认
            if hw.startswith("rtx") or hw == "nvidia_other":
                chain.append(("h264_nvenc", nvenc_args()))
            chain.append(("libx264", x264_args()))

        # 仅保留探测可用的编码器；若全部不可用则保留链尾软编（由执行期报错）
        usable = [c for c in chain if self._encoders.get(c[0], False)]
        return usable or chain[-1:]

    # ═══════════════════════════════════════════════════════════
    #  公开 API
    # ═══════════════════════════════════════════════════════════

    def encode_frames_to_video(
        self,
        frame_dir: str | Path,
        out_path: str | Path,
        fps: int = 24,
        resolution: str = "1080p",
        progress_cb: ProgressCallback | None = None,
        codec: str = "h264",
        timeout_s: int = DEFAULT_TIMEOUT_S,
        frame_pattern: str = "frame_%05d.png",
        audio_path: str | None = None,
    ) -> dict:
        """将帧序列编码为视频文件。

        Args:
            frame_dir: 帧目录（帧命名默认 frame_%05d.png）
            out_path: 输出视频路径
            fps: 帧率
            resolution: 720p/1080p/2k/4k（帧尺寸不符时 scale）
            progress_cb: 进度回调 fn(0..1, message)
            codec: av1/h264/h265（硬件调度矩阵决定实际编码器）
            timeout_s: 超时秒数
            frame_pattern: 帧文件名 printf 模式
            audio_path: 可选音轨（存在时混入 AAC 256kbps）

        Returns:
            {"output": str, "encoder": str, "duration_s": float,
             "size_bytes": int, "elapsed_s": float}

        Raises:
            EncoderUnavailableError: ffmpeg 不可用
            EncodeError: 所有候选编码器均失败
        """
        self._ensure_available()
        frame_dir = Path(frame_dir)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        frames = sorted(frame_dir.glob(frame_pattern.replace("%05d", "*")))
        total_frames = len(frames)
        if total_frames == 0:
            raise EncodeError(f"帧目录为空: {frame_dir}")

        def on_progress(fraction: float, msg: str) -> None:
            self._safe_progress(progress_cb, fraction, msg)

        # 审计 09-10 P2-11：audio_path 直喂 ffmpeg——限制在产品数据目录
        # 内（语音/TTS 产物均在 data/ 下），拒任意本机/UNC 路径外带
        if audio_path:
            audio_resolved = Path(audio_path).resolve()
            if not audio_resolved.is_relative_to(DATA_DIR.resolve()):
                raise EncodeError(
                    f"audio_path 越界（须在数据目录内）: {audio_path}")

        t0 = time.time()
        errors: list[str] = []
        for encoder, enc_args in self._encoder_candidates(codec, "medium"):
            args = [self._ffmpeg, "-y", "-framerate", str(fps),
                    "-i", str(frame_dir / frame_pattern)]
            if audio_path and Path(audio_path).is_file():
                args += ["-i", str(audio_path), "-shortest"]
            args += ["-c:v", encoder, *enc_args, "-pix_fmt", "yuv420p"]
            wh = RESOLUTION_MAP.get((resolution or "").lower())
            if wh:
                args += ["-vf", f"scale={wh[0]}:{wh[1]}"]
            if audio_path and Path(audio_path).is_file():
                args += ["-c:a", "aac", "-b:a", AUDIO_BITRATE]
            args += [str(out_path)]

            on_progress(0.0, f"编码开始（{encoder}）")
            ok, err = self._run_ffmpeg(
                args, timeout_s=timeout_s,
                total_frames=total_frames, progress_cb=on_progress,
            )
            if ok and self._verify_output(out_path):
                duration = self._probe_duration(out_path)
                on_progress(1.0, "编码完成")
                return {
                    "output": str(out_path), "encoder": encoder,
                    "duration_s": duration,
                    "size_bytes": out_path.stat().st_size,
                    "elapsed_s": round(time.time() - t0, 2),
                }
            errors.append(f"{encoder}: {err or '输出校验失败'}")
            log.warning("编码器 %s 失败，尝试降级链下一个: %s", encoder, err)
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                log.debug("encode_frames_to_video: 降级忽略", exc_info=True)

        raise EncodeError("全部候选编码器失败 → " + " | ".join(errors))

    def concat_clips(
        self,
        clips: list[str | Path],
        out_path: str | Path,
        format: str = "h264",
        quality: str = "medium",
        progress_cb: ProgressCallback | None = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> dict:
        """拼接多个视频片段并整体重编码导出。

        使用 ffmpeg concat demuxer（-f concat -safe 0 -i list.txt），
        按 format（av1/h264/h265）+ quality（high/medium/low）重编码，
        音频统一 AAC 256kbps。

        Returns / Raises 同 encode_frames_to_video。
        """
        self._ensure_available()
        if not clips:
            raise EncodeError("片段列表为空")
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # 构造 concat 清单（临时文件，放在输出目录旁）
        list_file = out_path.with_suffix(".concat.txt")
        try:
            lines = []
            for c in clips:
                p = Path(c)
                if not p.is_file():
                    raise EncodeError(f"片段不存在: {p}")
                # concat demuxer 要求单引号包裹路径，转义内部单引号
                lines.append("file '" + str(p).replace("'", "'\\''") + "'")
            list_file.write_text("\n".join(lines), encoding="utf-8")

            total_duration = sum(self._probe_duration(Path(c)) for c in clips)
            crf = QUALITY_CRF.get(quality, DEFAULT_CRF)

            def on_progress(fraction: float, msg: str) -> None:
                self._safe_progress(progress_cb, fraction, msg)

            errors: list[str] = []
            for encoder, enc_args in self._encoder_candidates(format, quality):
                args = [self._ffmpeg, "-y", "-f", "concat", "-safe", "0",
                        "-i", str(list_file),
                        "-c:v", encoder, *enc_args, "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
                        str(out_path)]
                on_progress(0.0, f"拼接导出开始（{encoder}, crf={crf}）")
                ok, err = self._run_ffmpeg(
                    args, timeout_s=timeout_s,
                    total_duration_s=max(total_duration, 0.1),
                    progress_cb=on_progress,
                )
                if ok and self._verify_output(out_path):
                    on_progress(1.0, "拼接导出完成")
                    return {
                        "output": str(out_path), "encoder": encoder,
                        "duration_s": self._probe_duration(out_path),
                        "size_bytes": out_path.stat().st_size,
                        "clips": len(clips),
                    }
                errors.append(f"{encoder}: {err or '输出校验失败'}")
                log.warning("拼接编码器 %s 失败，降级: %s", encoder, err)
                try:
                    out_path.unlink(missing_ok=True)
                except OSError:
                    log.debug("concat_clips: 降级忽略", exc_info=True)
            raise EncodeError("拼接导出失败 → " + " | ".join(errors))
        finally:
            try:
                list_file.unlink(missing_ok=True)
            except OSError:
                log.debug("concat_clips: 降级忽略", exc_info=True)

    # ═══════════════════════════════════════════════════════════
    #  内部：子进程执行 / 进度解析 / 输出校验
    # ═══════════════════════════════════════════════════════════

    def _ensure_available(self) -> None:
        if not self.available:
            raise EncoderUnavailableError(
                "FFmpeg 不可用：未在 runtime/ffmpeg、tools/downloads 或 PATH 找到；"
                "视频导出功能已降级")

    def _run_ffmpeg(
        self,
        args: list[str],
        timeout_s: int,
        progress_cb: ProgressCallback | None = None,
        total_frames: int = 0,
        total_duration_s: float = 0.0,
    ) -> tuple[bool, str]:
        """运行 ffmpeg 子进程并解析 stderr 进度。

        Returns:
            (成功与否, 失败原因)
        """
        start = time.time()
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW
                if os.name == "nt" else 0,
                text=True, encoding="utf-8", errors="replace",
            )
        except OSError as exc:
            return False, f"子进程启动失败: {exc}"

        tail: list[str] = []
        try:
            assert proc.stderr is not None
            deadline = start + timeout_s
            for line in proc.stderr:
                if time.time() > deadline:
                    proc.kill()
                    return False, f"编码超时（>{timeout_s}s）"
                line = line.strip()
                if not line:
                    continue
                tail.append(line)
                if len(tail) > 12:
                    tail.pop(0)
                fraction = self._parse_progress(line, total_frames, total_duration_s)
                if fraction is not None and progress_cb:
                    progress_cb(min(0.99, fraction), "encoding")
            rc = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            return False, "等待进程退出超时"
        finally:
            try:
                if proc.stderr:
                    proc.stderr.close()
            except Exception:
                log.debug("_run_ffmpeg: 降级忽略", exc_info=True)

        if rc != 0:
            snippet = tail[-1] if tail else f"返回码 {rc}"
            return False, f"ffmpeg 返回码 {rc}: {snippet[:200]}"
        return True, ""

    @staticmethod
    def _parse_progress(
        line: str, total_frames: int, total_duration_s: float,
    ) -> float | None:
        """从 ffmpeg stderr 行解析进度（0..1）。优先 frame=，其次 time=。"""
        if total_frames > 0:
            m = _FRAME_RE.search(line)
            if m:
                return min(1.0, int(m.group(1)) / total_frames)
        if total_duration_s > 0:
            m = _TIME_RE.search(line)
            if m:
                h, mnt, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                secs = h * 3600 + mnt * 60 + s
                return min(1.0, secs / total_duration_s)
        return None

    def _verify_output(self, path: Path) -> bool:
        """输出校验：文件存在、>=MIN_OUTPUT_BYTES、（ffprobe 可用时）时长 > 0。

        失败原因如实记日志（2026-08-09 高负载下校验秒失败的归因需求）：
        文件缺失 / 体积过小 / 时长探测为 0 三分支分别标注。
        """
        try:
            if not path.is_file():
                log.warning("输出校验失败：文件不存在 %s", path)
                return False
            size = path.stat().st_size
            if size < MIN_OUTPUT_BYTES:
                log.warning("输出校验失败：文件过小 %s（%dB < %dB）",
                               path, size, MIN_OUTPUT_BYTES)
                return False
        except OSError as exc:
            log.warning("输出校验失败：stat 异常 %s: %s", path, exc)
            return False
        if self._ffprobe:
            duration = self._probe_duration(path)
            if duration <= 0:
                log.warning("输出校验失败：ffprobe 时长=%.2f %s",
                               duration, path)
                return False
        return True

    def _probe_duration(self, path: Path) -> float:
        """用 ffprobe 读取媒体时长（秒）；最终失败返回 0.0。

        高负载下新产出文件可能被杀毒软件/索引服务短暂独占，ffprobe
        打不开时 stdout 为空被解析成 0.0 造成误判（实测 RTX5070Ti 高负载
        窗口 ffmpeg rc=0 且文件合法，但即时探测返回 0）。因此对"异常或
        零时长"做短重试（3 次 × 间隔 0.5s），仍失败才按 0.0 处理并记日志。
        """
        if not self._ffprobe:
            return 0.0
        last = ""
        for attempt in range(3):
            try:
                proc = subprocess.run(
                    [self._ffprobe, "-v", "quiet", "-show_entries",
                     "format=duration", "-of", "csv=p=0", str(path)],
                    capture_output=True, text=True, timeout=20,
                    encoding="utf-8", errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW
                    if os.name == "nt" else 0,
                )
                duration = max(0.0, float((proc.stdout or "").strip() or 0.0))
                if duration > 0:
                    return duration
                last = (f"rc={proc.returncode} duration=0 "
                        f"stderr={((proc.stderr or '').strip())[:120]}")
            except (ValueError, subprocess.SubprocessError, OSError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            if attempt < 2:
                time.sleep(0.5)
        log.warning("ffprobe 时长探测 3 次均失败（按 0 处理）: %s | %s",
                       path, last)
        return 0.0

    @staticmethod
    def _safe_progress(cb: ProgressCallback | None, fraction: float, msg: str) -> None:
        """进度回调容错（回调异常不影响编码主流程）。"""
        if cb is None:
            return
        try:
            cb(max(0.0, min(1.0, fraction)), msg)
        except Exception:  # noqa: BLE001
            log.debug("_safe_progress: 降级忽略", exc_info=True)


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

def get_encoder_service() -> EncoderService:
    """获取编码服务单例。"""
    return EncoderService.instance()
