"""OmniSpace AI v2.5.0 硬件监控采集模块（规格 §5.1 采样层）。

使用 psutil 采集 CPU/RAM/磁盘，pynvml 采集 GPU。
所有采集函数均可在依赖缺失时返回默认值，保证不崩溃。

分级采样（v2.3.1，config.yaml scheduler.*_sample_interval_s）：
调度循环以 1s 基频 tick，但低频变化指标不重复采集——
GPU 2s / CPU 5s / 磁盘 10s 内直接返回缓存值，降低 psutil/NVML 开销
（psutil.cpu_percent 带 100ms 阻塞探测，1s 全量采集浪费约 10% 单核）。
内存/电源为纳秒级读数，保持每 tick 实时采集。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368

from __future__ import annotations

import importlib
import os
import time
from collections.abc import Callable
from typing import Any

import psutil

from ...config import (
    CPU_SAMPLE_INTERVAL_S,
    DISK_SAMPLE_INTERVAL_S,
    GPU_SAMPLE_INTERVAL_S,
    ROOT_DIR,
)


# ── 可选依赖加载 ────────────────────────────────────────────────
def _try_import(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except Exception:
        return None


_pynvml = _try_import("pynvml")
_torch = _try_import("torch")


class HardwareMonitor:
    """硬件监控采集器——分级采样（GPU 2s / CPU 5s / 磁盘 10s，其余实时）。"""

    def __init__(self) -> None:
        self._nvml_initialized = False
        # 分级采样缓存：key -> (采样值, 采样时间戳)
        self._cache: dict[str, tuple[dict, float]] = {}
        # 磁盘 IO 上一次采样计数（monotonic 秒, read_time_ms, write_time_ms），
        # 用于计算采样间隔内的磁盘占用率（文档B §4.1：磁盘 IO >80% 延迟写入）
        self._disk_prev: tuple[float, int, int] | None = None

    def _cached(self, key: str, ttl_s: float, producer: Callable[[], dict]) -> dict:
        """TTL 缓存采样：ttl 内返回上次结果，过期重新采样。"""
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None and (now - hit[1]) < ttl_s:
            return hit[0]
        value = producer()
        self._cache[key] = (value, now)
        return value

    def _ensure_nvml(self) -> bool:
        """初始化 pynvml，返回是否可用。"""
        if self._nvml_initialized:
            return True
        if _pynvml is None:
            return False
        try:
            _pynvml.nvmlInit()
            self._nvml_initialized = True
            return True
        except Exception:
            return False

    # ── GPU 采集 ────────────────────────────────────────────────

    def get_gpu(self) -> dict:
        """采集 GPU 状态（TTL 缓存，间隔 GPU_SAMPLE_INTERVAL_S=2s）。

        Returns:
            dict: vram_used_mb, vram_total_mb, util_percent, temp_celsius
        """
        return self._cached("gpu", GPU_SAMPLE_INTERVAL_S, self._sample_gpu)

    def _sample_gpu(self) -> dict:
        # 多卡枚举（显存调度机制批1，2026-09-10）：顶层字段保持主卡
        # （0 号）口径与历史完全一致（analyzer/decision 等下游零波及），
        # 新增 devices 数组 + device_count 供 per-card 消费。此前硬编码
        # 0 号卡——gpu_domains 把 paint/dialog 域分到 1 号卡时，该卡的
        # 温度/显存在调度视野中不存在（方案 §1.2 病灶⑥）。
        # 值类型混合（int/float/None/list[dict]），显式 Any 装载
        result: dict[str, Any] = {
            "vram_used_mb": 0,
            "vram_total_mb": 0,
            "util_percent": 0.0,
            "temp_celsius": 0.0,
            # S5 进程级归因（2026-08-28 V77 根修）：本进程树 SM 利用率
            # 与外部归因利用率；NVML 采样不可用时为 None，analyzer
            # 回退整卡口径（向后兼容）。
            "own_util_percent": None,
            "external_util_percent": None,
        }

        # 优先使用 pynvml
        devices = self._sample_all_gpu_devices()
        if devices:
            devices.sort(key=lambda d: d.get("device", 0))
            result.update(devices[0])
            result["device_count"] = len(devices)
            result["devices"] = devices
            return result

        # 降级：使用 torch（主卡）
        if _torch is not None and _torch.cuda.is_available():
            try:
                result["vram_total_mb"] = int(
                    _torch.cuda.get_device_properties(0).total_memory // (1024 * 1024)
                )
                result["vram_used_mb"] = int(
                    _torch.cuda.memory_allocated(0) // (1024 * 1024)
                )
                result["util_percent"] = 0.0  # torch 无法直接获取利用率
                return result
            except Exception:
                pass

        return result

    def _sample_all_gpu_devices(self) -> list[dict]:
        """逐卡 NVML 采集（返回非空列表；NVML 不可用/全失败返回空）。"""
        if not self._ensure_nvml():
            return []
        count = 1
        try:
            count = max(1, int(_pynvml.nvmlDeviceGetCount()))
        except Exception:  # noqa: BLE001 - 枚举失败按单卡（历史行为）
            count = 1
        devices: list[dict] = []
        for idx in range(count):
            per = self._sample_gpu_nvml(idx)
            if per is not None:
                devices.append(per)
        return devices

    def _sample_gpu_nvml(self, idx: int) -> dict | None:
        """单卡 NVML 采集；任何失败返回 None（调用方降级）。"""
        try:
            handle = _pynvml.nvmlDeviceGetHandleByIndex(idx)
            # 值类型混合（int/float/None），显式 Any 装载
            per: dict[str, Any] = {
                "device": idx,
                "vram_total_mb": 0,
                "vram_used_mb": 0,
                "util_percent": 0.0,
                "temp_celsius": 0.0,
                "own_util_percent": None,
                "external_util_percent": None,
            }
            mem = _pynvml.nvmlDeviceGetMemoryInfo(handle)
            per["vram_total_mb"] = int(mem.total // (1024 * 1024))
            per["vram_used_mb"] = int(mem.used // (1024 * 1024))

            util = _pynvml.nvmlDeviceGetUtilizationRates(handle)
            per["util_percent"] = float(util.gpu)

            # S5 进程级归因：自身生成跑满 GPU（own≈95%）属正常工
            # 况，不应被质量总督判为"高负载"自伤降步；仅外部进程
            # 争抢算力才驱动降参（analyzer 消费 external 字段）。
            own = self._sum_own_process_util(handle)
            if own is not None:
                per["own_util_percent"] = round(own, 1)
                per["external_util_percent"] = round(
                    max(0.0, per["util_percent"] - own), 1)

            try:
                temp = _pynvml.nvmlDeviceGetTemperature(
                    handle, _pynvml.NVML_TEMPERATURE_GPU
                )
                per["temp_celsius"] = float(temp)
            except Exception:
                pass
            return per
        except Exception:  # noqa: BLE001 - 单卡失败不拖垮其余卡
            return None

    @staticmethod
    def _sum_own_process_util(handle: Any) -> float | None:
        """汇总本进程树（后端 + vLLM worker 等子进程）的 SM 利用率。

        nvmlDeviceGetProcessUtilization(handle, 0) 返回驱动侧最近采样
        窗内各进程 SM 利用率百分比（本机 RTX 5070 Ti / driver 610.88
        实测可用，探针 scripts/scratch/archive/cssc/_nvml_proc_util_probe.py）。窗口均值与整
        卡瞬时值口径不同，之和可能略超整卡值，调用方以 max(0,...) 钳
        制。API 异常时返回 None，调用方回退整卡口径。
        """
        try:
            samples = _pynvml.nvmlDeviceGetProcessUtilization(handle, 0)
        except Exception:
            return None
        try:
            pids = {os.getpid()}
            pids.update(
                c.pid for c in psutil.Process().children(recursive=True))
        except Exception:
            pids = {os.getpid()}
        total = 0.0
        for s in samples:
            if s.pid in pids:
                total += float(s.smUtil)
        return min(total, 100.0)

    # ── CPU 采集 ────────────────────────────────────────────────

    def get_cpu(self) -> dict:
        """采集 CPU 状态（TTL 缓存，间隔 CPU_SAMPLE_INTERVAL_S=5s）。

        Returns:
            dict: usage_percent, temp_celsius, cores, threads
        """
        return self._cached("cpu", CPU_SAMPLE_INTERVAL_S, self._sample_cpu)

    def _sample_cpu(self) -> dict:
        result = {
            "usage_percent": psutil.cpu_percent(interval=0.1),
            "temp_celsius": 0.0,
            "cores": psutil.cpu_count(logical=False) or 0,
            "threads": psutil.cpu_count(logical=True) or 0,
        }

        # 温度采集（跨平台，可能不可用）
        try:
            if hasattr(psutil, "sensors_temperatures"):
                temps = psutil.sensors_temperatures()
                if temps:
                    # 取第一个温度传感器的读数
                    for sensor_name in ("coretemp", "cpu_thermal", "k10temp"):
                        if sensor_name in temps and temps[sensor_name]:
                            result["temp_celsius"] = float(temps[sensor_name][0].current)
                            break
                    else:
                        # 取任意第一个传感器
                        first_key = next(iter(temps))
                        if temps[first_key]:
                            result["temp_celsius"] = float(temps[first_key][0].current)
        except Exception:
            pass

        return result

    # ── 内存采集 ────────────────────────────────────────────────

    def get_memory(self) -> dict:
        """采集内存状态。

        Returns:
            dict: available_gb, total_gb, used_percent
        """
        vm = psutil.virtual_memory()
        return {
            "available_gb": round(vm.available / (1024 ** 3), 2),
            "total_gb": round(vm.total / (1024 ** 3), 2),
            "used_percent": vm.percent,
        }

    # ── 电源状态 ────────────────────────────────────────────────

    def get_power(self) -> str:
        """检测电源状态。

        Returns:
            'ac' 或 'battery'
        """
        try:
            battery = psutil.sensors_battery()
            if battery is not None:
                return "battery" if not battery.power_plugged else "ac"
        except Exception:
            pass
        return "ac"

    # ── 磁盘 IO ─────────────────────────────────────────────────

    def get_disk_io(self) -> dict:
        """采集磁盘 IO 延迟（TTL 缓存，间隔 DISK_SAMPLE_INTERVAL_S=10s）。"""
        return self._cached("disk", DISK_SAMPLE_INTERVAL_S, self._sample_disk_io)

    def _sample_disk_io(self) -> dict:
        """采集磁盘余量与 IO 占用率。

        busy_percent = 采样间隔内 (read_time+write_time) 增量 / 间隔时长，
        即磁盘忙碌时间占比（文档B §4.1「磁盘 IO >80%」的可计算口径）。
        首次采样无前一帧，busy_percent 为 0.0。
        """
        free_gb = 0.0
        try:
            usage = psutil.disk_usage(str(ROOT_DIR))
            free_gb = round(usage.free / (1024 ** 3), 2)
        except Exception:  # noqa: BLE001 - 磁盘余量采集失败不阻断监控
            pass
        read_ms = 0
        write_ms = 0
        busy_percent = 0.0
        try:
            counters = psutil.disk_io_counters()
            if counters is not None:
                now = time.monotonic()
                read_ms = int(getattr(counters, "read_time", 0))
                write_ms = int(getattr(counters, "write_time", 0))
                prev = self._disk_prev
                if prev is not None:
                    elapsed_s = now - prev[0]
                    if elapsed_s > 0:
                        delta_ms = (read_ms - prev[1]) + (write_ms - prev[2])
                        busy_percent = round(
                            min(1.0, max(0.0, delta_ms / (elapsed_s * 1000.0))), 4)
                self._disk_prev = (now, read_ms, write_ms)
        except Exception:  # noqa: BLE001 - 部分平台无 IO 计数器，占用率留 0
            pass
        return {
            "free_gb": free_gb,
            "read_ms": read_ms,
            "write_ms": write_ms,
            "busy_percent": busy_percent,
        }

    # ── 综合快照 ────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """返回所有硬件指标的完整快照。"""
        return {
            "gpu": self.get_gpu(),
            "cpu": self.get_cpu(),
            "memory": self.get_memory(),
            "power": self.get_power(),
            "disk": self.get_disk_io(),
        }
