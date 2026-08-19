#!/usr/bin/env python3
"""
硬件性能基准工具（CLI）
- 基于 backend/services/scheduler/monitor.HardwareMonitor 真实采样
  CPU/内存/磁盘/GPU，综合评分并识别硬件等级（文档B §4.2 六档，
  经 backend/data/models.detect_hardware_tier 按 GPU 型号名匹配）
  （审计 BK-048 修复：原引用不存在的 backend.core.gpu_manager）
- 评分结果缓存至 data/hardware_profile.json，供 VRAM 调度策略引用
用法:
  python tools/benchmark.py            完整基准（有缓存则读缓存）
  python tools/benchmark.py --force    强制重测（忽略缓存）
"""
import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend import config  # noqa: E402
from backend.data.models import detect_hardware_tier  # noqa: E402
from backend.services.scheduler.monitor import HardwareMonitor  # noqa: E402

CACHE_PATH = config.DATA_DIR / "hardware_profile.json"


def _gpu_name() -> str:
    """探测 GPU 型号名（pynvml 优先，torch 兜底）。"""
    try:
        import pynvml
        pynvml.nvmlInit()
        name = pynvml.nvmlDeviceGetName(0)
        pynvml.nvmlShutdown()
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        return str(name)
    except Exception:  # noqa: BLE001
        pass
    try:
        import torch
        if torch.cuda.is_available():
            return str(torch.cuda.get_device_name(0))
    except Exception:  # noqa: BLE001
        pass
    return ""


def _disk_write_mb_s() -> float:
    """数据目录写速快测（32MB 单次写，粗略参考值）。"""
    probe = config.DATA_DIR / ".bench_write"
    payload = b"\0" * (1024 * 1024)
    try:
        start = time.perf_counter()
        with open(probe, "wb") as f:
            for _ in range(32):
                f.write(payload)
            f.flush()
        elapsed = max(time.perf_counter() - start, 1e-6)
        return round(32.0 / elapsed, 1)
    except Exception:  # noqa: BLE001
        return 0.0
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def run_benchmark() -> dict:
    """执行完整硬件基准：采样 → 评分 → 硬件等级识别 → 缓存落盘。"""
    monitor = HardwareMonitor()
    gpu = monitor._sample_gpu()       # 绕过 TTL 缓存，取即时样本
    cpu = monitor._sample_cpu()
    mem = monitor.get_memory()
    disk_mb_s = _disk_write_mb_s()

    gpu_name = _gpu_name()
    tier = detect_hardware_tier(gpu_name, int(gpu.get("vram_total_mb", 0)))

    # 综合评分（0-100，粗略加权：显存 40 / 内存 25 / CPU 线程 20 / 磁盘写速 15）
    vram_gb = gpu.get("vram_total_mb", 0) / 1024.0
    score = (
        min(vram_gb / 32.0, 1.0) * 40
        + min(mem.get("total_gb", 0) / 64.0, 1.0) * 25
        + min(cpu.get("threads", 0) / 32.0, 1.0) * 20
        + min(disk_mb_s / 2000.0, 1.0) * 15
    )

    profile = {
        "gpu_name": gpu_name or "none",
        "vram_total_mb": gpu.get("vram_total_mb", 0),
        "cpu_cores": cpu.get("cores", 0),
        "cpu_threads": cpu.get("threads", 0),
        "ram_total_gb": mem.get("total_gb", 0),
        "disk_write_mb_s": disk_mb_s,
        "score": round(score, 1),
        "tier": tier["tier"],
        "tier_label": tier["label"],
        "tier_matched_by": tier["matched_by"],
        "models": tier["models"],
        "learn_tabs": tier["learn_tabs"],
        "measured_at": time.time(),
    }
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(profile, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    return profile


def get_profile() -> dict:
    """读取缓存的基准结果；无缓存则执行一次完整基准。"""
    if CACHE_PATH.is_file():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return run_benchmark()


def main() -> int:
    ap = argparse.ArgumentParser(description="OmniSpace 硬件基准")
    ap.add_argument("--force", action="store_true", help="强制重测")
    args = ap.parse_args()

    print("正在执行硬件基准测试...")
    profile = run_benchmark() if args.force else get_profile()

    print("\n===== 基准结果 =====")
    print(json.dumps(profile, ensure_ascii=False, indent=2, default=str))
    print(f"\n硬件档位: {profile.get('tier', 'unknown')}"
          f"（{profile.get('tier_label', '')}，匹配方式: {profile.get('tier_matched_by', '')}）")
    print(f"结果已缓存: {CACHE_PATH}，供 VRAM 调度引用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
