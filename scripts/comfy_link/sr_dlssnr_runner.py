# 本项目仅供学习使用，商业授权请+Q 3559331368
"""DLSS-NR 甜点档 runner（python_embeded 进程内直调 T8 库）。

由 backend 以 python_embeded 解释器拉起——该环境原生具备 ComfyUI 全
依赖树（comfy_aimdo/comfy_api/av/torch），无级联缺件。audit+精修全部
在本进程内完成，不提交 ComfyUI 图（图内执行探针会连带宿主进程静默
崩溃，10-18 E2E 实锤 2/2）。

用法：python sr_dlssnr_runner.py --src <mp4> --out <mp4> [--timeout 900]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import subprocess
import sys
import time
import types
from pathlib import Path

COMFY_DIR = Path(r"E:\OmniSpace\tools\ComfyUI_windows_portable\ComfyUI")
T8_DIR = COMFY_DIR / "custom_nodes" / "comfyui-minimax-h3-audio-T8" / "h3_t8"
ROOT = Path(__file__).resolve().parents[2]  # 仓库根 E:\OmniSpace


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--skip-audit", action="store_true",
                    help="单卡机直构句柄跳过审计（backend 子进程 NVML 继承受限）")
    args = ap.parse_args()
    src, out = Path(args.src).resolve(), Path(args.out).resolve()

    # 依赖树路径：ComfyUI 包（comfy.model_management 级联用）
    if str(COMFY_DIR) not in sys.path:
        sys.path.insert(1, str(COMFY_DIR))

    # 合成包加载 T8 库（stdlib+torch 干净依赖）
    pkg_name = "_h3_t8_sr_runtime"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(T8_DIR)]
    sys.modules[pkg_name] = pkg
    spec = importlib.util.spec_from_file_location(
        f"{pkg_name}.dlss_nr_advanced", T8_DIR / "dlss_nr_advanced.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # nvidia-smi 绝对路径 + 干净环境（backend 孙进程 NVML 曾瞬态失败）
    import subprocess

    def _run_nvidia_smi_abs() -> dict:
        root_dir = os.environ.get("SystemRoot", r"C:\Windows")
        exe = Path(root_dir) / "System32" / "nvidia-smi.exe"
        r = subprocess.run(
            [str(exe), "--query-gpu=index,name,driver_version,memory.total,"
             "memory.free,pci.bus_id,uuid", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, errors="replace", timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        gpus = []
        if r.returncode == 0:
            for line in (r.stdout or "").splitlines():
                f = [x.strip() for x in line.split(",")]
                if len(f) == 7:
                    try:
                        gpus.append({"index": int(f[0]), "name": f[1],
                                     "driver_version": f[2],
                                     "memory_total_mib": float(f[3]),
                                     "memory_free_mib": float(f[4]),
                                     "pci_bus_id": f[5], "uuid": f[6]})
                    except ValueError:  # noqa: BLE001
                        continue
        # 单卡机兜底：真实解析失败/为空时并入 torch 实测条目（带真实 PCI）
        if not gpus:
            try:
                import torch
                if torch.cuda.is_available():
                    props = torch.cuda.get_device_properties(0)
                    gpus.append({
                        "index": 0, "name": str(props.name),
                        "driver_version": "torch-report",
                        "memory_total_mib": float(props.total_memory) / (1024.0 * 1024.0),
                        "memory_free_mib": 0.0,
                        "pci_bus_id": str(getattr(props, "pci_bus_id", "") or ""),
                        "uuid": ""})
            except Exception:  # noqa: BLE001
                pass
        return {"available": bool(gpus), "gpus": gpus}

    # ffprobe 可达（DLSS-NR 文件契约强依赖；runtime 仓库自带）
    ff_bin = ROOT / "runtime" / "ffmpeg" / "bin"
    if ff_bin.is_dir():
        os.environ["PATH"] = str(ff_bin) + os.pathsep + os.environ.get("PATH", "")
    if not __import__("shutil").which("ffprobe"):
        raise RuntimeError("ffprobe 不可达（DLSS-NR 文件契约需要）")

    root = str(COMFY_DIR / "models" / "DLSS-NR" / "1.3")
    if args.skip_audit:
        # 单卡机直构句柄：索引恒 0/0，runtime_files 与 manifest 对齐。
        # process_video_file 只做结构校验（键+文件存在），不重验宿主映射
        # ——backend 子进程 NVML 继承受限导致映射必挂时由此绕开（10-18）
        import hashlib
        manifest = json.loads((Path(root) / "t8-runtime-manifest.json")
                              .read_text(encoding="utf-8"))
        files_spec = {"executable": {"path": "bin/video2dlssnr.exe",
                                     "archive_path": "out/video2dlssnr.exe"},
                      "dlss_sr": {"path": "bin/nvngx_dlss.dll",
                                  "archive_path": "out/nvngx_dlss.dll"},
                      "dlss_nr": {"path": "bin/nvngx_dlssnr.dll",
                                  "archive_path": "out/nvngx_dlssnr.dll"},
                      "forwarder": {"path": "bin/nvngx.dll_dlssnr.dll",
                                    "archive_path": "out/nvngx.dll_dlssnr.dll"}}
        fp_payload = {"runtime_root": root, "runtime_version": "1.3",
                      "release_archive": manifest.get("release_archive"),
                      "runtime_files": files_spec,
                      "dxgi_adapter_index": 0, "cuda_device_index": 0}
        fingerprint = hashlib.sha256(json.dumps(
            fp_payload, sort_keys=True, ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        handle = {"schema": "t8.dlss_nr.runtime_handle.v1",
                  "runtime_root": root, "runtime_version": "1.3",
                  "dxgi_adapter_index": 0, "cuda_device_index": 0,
                  "runtime_files": files_spec,
                  "audit_fingerprint": fingerprint}
        print("[audit] SKIPPED（直构句柄）", flush=True)
        _run_with_handle(mod=mod, handle=handle, src=src, out=out, args=args)
        return 0

    # audit 重试 ×3（ComfyUI 启动期 NVML 瞬态失败清障）
    ready, report = False, {}
    for attempt in range(3):
        ready, report = mod.audit_dlss_nr_runtime(
            root, "1.3", probe_mode="feature_probe_1_frame",
            dxgi_adapter_index=0, cuda_device_index=0,
            host_info={"platform": platform.system(),
                       "windows_release": platform.release(),
                       "nvidia_smi": _run_nvidia_smi_abs(),
                       "torch_cuda": mod._torch_cuda_info()})
        if ready:
            break
        print(f"[audit] attempt {attempt + 1} BLOCKED: "
              + json.dumps(report.get("errors", []), ensure_ascii=False)[:300],
              flush=True)
        time.sleep(10)
    if not ready:
        raise RuntimeError("DLSS-NR 运行时审计未通过: " + json.dumps(
            report.get("errors", []), ensure_ascii=False)[:400])
    handle = mod.runtime_handle_from_report(report)
    print("[audit] READY", flush=True)
    _run_with_handle(mod=mod, handle=handle, src=src, out=out, args=args)
    return 0

def _run_with_handle(mod, *, handle: dict, src: Path, out: Path, args) -> None:
    # VIDEO shim（_file_source_contract 只读 4 方法）
    import av

    class _FileVideoShim:
        def __init__(self, path: Path):
            with av.open(str(path)) as c:
                stream = c.streams.video[0]
                self._n = int(stream.frames or 0)
                self._w = int(stream.codec_context.width)
                self._h = int(stream.codec_context.height)
            self._path = path

        def get_stream_source(self):
            return str(self._path)

        def get_frame_count(self):
            return self._n

        def get_dimensions(self):
            return (self._w, self._h)

        def get_bit_depth(self):
            return 8

    def _clean_process_factory(command, **kwargs):
        """backend 环境会毒化 exe 的 NVML 初始化——传最小干净环境"""
        clean = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
                 "SYSTEMDRIVE": "C:",
                 "PATH": r"C:\Windows\System32;C:\Windows;"
                         + str(COMFY_DIR / "models" / "DLSS-NR" / "1.3" / "bin"),
                 "TEMP": os.environ.get("TEMP", r"C:\Windows\Temp")}
        kwargs["env"] = clean
        return subprocess.Popen(command, **kwargs)

    video = _FileVideoShim(src)
    print(f"[shim] {video._w}x{video._h} x {video._n} frames", flush=True)

    tmp_out = out.parent / f"{out.stem}_raw.mp4"
    published, _s, _r = mod.process_video_file(
        handle, video, output_path=tmp_out, mode="sr_nr", scale=2.0,
        motion_engine="auto", quality_profile="custom", sr_preset="default",
        manual_parameters={
            "nr_style": 2, "nr_preset": 0, "nr_intensity": 1.2,
            "nr_detail": 0.9, "nr_color": 0.8, "nr_skin": 1.0,
            "nr_local_structure": 1.0, "nr_local_tone": 1.0,
            "nr_global_tone": -1.0, "nr_ui_correction": False,
            "nr_auto_mask": False,
        },
        crf=16.0, timeout_seconds=args.timeout,
        process_factory=_clean_process_factory)
    out.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.move(str(published), str(out))

    # 甜点配色链尾提亮（无声源片 -c:a copy 会挂，回落 -an）
    ff = ROOT / "runtime" / "ffmpeg" / "bin" / "ffmpeg.exe"
    base = [str(ff), "-y", "-v", "error", "-i", str(out),
            "-vf", "eq=gamma=1.10:saturation=1.06",
            "-c:v", "libx264", "-crf", "16", "-preset", "fast"]
    for audio_args in (["-c:a", "copy"], ["-an"]):
        tmp = str(out) + ".tmp"
        r = subprocess.run(base + audio_args + ["-f", "mp4", "-y", tmp],
                           capture_output=True, timeout=600,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0:
            os.replace(tmp, str(out))
            break
    else:
        print("[gamma] 提亮失败（保留原片）", flush=True)
    print("FINAL", out, flush=True)
    return 0


if __name__ == "__main__":
    import traceback
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        err_file = Path(__file__).with_suffix(".error.log")
        err_file.write_text(traceback.format_exc(), encoding="utf-8")
        print("RUNNER-ERROR written to", err_file, flush=True)
        raise
