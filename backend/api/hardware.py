"""硬件 API 路由（规格 §4.6 硬件 API）。

端点清单：
- GET       /hardware/info      硬件画像（静态信息：GPU/CPU/RAM/Disk/Power）
- GET       /hardware/realtime  实时遥测（GPU利用率/显存/温度/CPU/内存）
- GET       /hardware/synergy   协同调度聚合状态（scheduler + vram + feature_lock）
- WebSocket /hardware/realtime  实时推送（每 2 秒推送一次遥测）

约定：router 不带 prefix；成功 ok(data)；错误抛 ApiError。服务层不可用时返回模拟数据。
"""
from __future__ import annotations

import asyncio
import logging
import platform
import time

from fastapi import APIRouter, Body, WebSocket, WebSocketDisconnect

from ..config import ROOT_DIR
from ..data.models import HardwareProfile
from ..middleware.error_handler import ApiError, ok

router = APIRouter()
log = logging.getLogger("omnispace.api.hardware")


def _try_psutil():
    """惰性尝试导入 psutil；不可用返回 None。"""
    try:
        import psutil  # type: ignore
        return psutil
    except Exception:
        return None


def _detect_gpu_info() -> dict:
    """检测 GPU 静态信息（名称/显存/驱动）。

    优先 pynvml（真实硬件），降级 torch.cuda，最终回退未检测到独显。
    """
    try:
        import pynvml  # type: ignore
        pynvml.nvmlInit()
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="replace")
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            try:
                driver = pynvml.nvmlSystemGetDriverVersion()
                if isinstance(driver, bytes):
                    driver = driver.decode("utf-8", errors="replace")
            except Exception:
                driver = ""
            try:
                cc_major, cc_minor = pynvml.nvmlDeviceGetCudaComputeCapability(handle)
                cc = f"{cc_major}.{cc_minor}"
            except Exception:
                cc = ""
            return {
                "vendor": "nvidia", "name": name,
                "vram_total_mb": int(mem.total // (1024 * 1024)),
                "vram_free_mb": int(mem.free // (1024 * 1024)),
                "compute_capability": cc, "driver_version": driver,
            }
        finally:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass
    except Exception:
        pass
    # 降级：torch.cuda（无驱动级信息）
    try:
        import torch  # type: ignore
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            return {
                "vendor": "nvidia", "name": props.name,
                "vram_total_mb": int(props.total_memory // (1024 * 1024)),
                "vram_free_mb": 0,
                "compute_capability": f"{props.major}.{props.minor}",
                "driver_version": "",
            }
    except Exception:
        pass
    return {"vendor": "none", "name": "未检测到独立显卡",
            "vram_total_mb": 0, "vram_free_mb": 0,
            "compute_capability": "", "driver_version": ""}


def _build_hardware_profile() -> dict:
    """构建硬件画像（优先真实数据，缺失时回退模拟值）。"""
    psutil = _try_psutil()
    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            cpu_info = {
                "name": platform.processor() or "Unknown CPU",
                "cores": psutil.cpu_count(logical=False) or 0,
                "threads": psutil.cpu_count(logical=True) or 0,
                "usage_percent": psutil.cpu_percent(interval=None),
                "temp_celsius": 0.0,
            }
            ram_info = {
                "total_gb": round(vm.total / (1024 ** 3), 1),
                "available_gb": round(vm.available / (1024 ** 3), 1),
                "total_mb": int(vm.total / (1024 ** 2)),
                "available_mb": int(vm.available / (1024 ** 2)),
                "usage_percent": vm.percent,
            }
            import shutil
            disk = shutil.disk_usage(str(ROOT_DIR))
            disk_info = {"path": str(ROOT_DIR),
                         "total_gb": round(disk.total / (1024 ** 3), 1),
                         "free_gb": round(disk.free / (1024 ** 3), 1),
                         "percent": round(disk.used / disk.total * 100, 1) if disk.total else 0.0}
            power = "ac"
            try:
                battery = psutil.sensors_battery()
                if battery is not None and not battery.power_plugged:
                    power = "battery"
            except Exception:
                pass
            gpu_info = _detect_gpu_info()
            return {"gpu": gpu_info, "cpu": cpu_info, "ram": ram_info,
                    "disk": disk_info, "power": power}
        except Exception as exc:  # noqa: BLE001 - 降级到模拟数据
            log.warning("psutil 采集失败，降级模拟数据：%s", exc)

    # 模拟硬件画像兜底
    return {
        "gpu": {"vendor": "none", "name": "未检测到独立显卡",
                "vram_total_mb": 0, "vram_free_mb": 0,
                "compute_capability": "", "driver_version": ""},
        "cpu": {"name": "Mock CPU", "cores": 8, "threads": 16,
                "usage_percent": 12.0, "temp_celsius": 45.0},
        "ram": {"total_gb": 32.0, "available_gb": 16.0,
                "total_mb": 32768, "available_mb": 16384, "usage_percent": 50.0},
        "disk": {"path": str(ROOT_DIR), "total_gb": 512.0,
                 "free_gb": 100.0, "percent": 80.5},
        "power": "ac",
    }


_monitor = None


def _realtime_gpu() -> dict:
    """实时 GPU 遥测：优先调度引擎 HardwareMonitor（pynvml/torch），失败回退零值。"""
    global _monitor
    try:
        from ..services.scheduler.monitor import HardwareMonitor
        if _monitor is None:
            _monitor = HardwareMonitor()
        gpu = _monitor.get_gpu()
        if gpu and gpu.get("vram_total_mb", 0) > 0:
            total = int(gpu.get("vram_total_mb", 0))
            used = int(gpu.get("vram_used_mb", 0))
            return {
                "usage_percent": float(gpu.get("util_percent", 0.0)),
                "vram_used_mb": used,
                "vram_total_mb": total,
                "vram_free_mb": max(0, total - used),
                "temp_celsius": float(gpu.get("temp_celsius", 0.0)),
            }
    except Exception:
        pass
    return {"usage_percent": 0.0, "vram_used_mb": 0,
            "vram_total_mb": 0, "vram_free_mb": 0, "temp_celsius": 0.0}


def _realtime_data() -> dict:
    """采集当前时刻实时遥测数据。"""
    psutil = _try_psutil()
    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            return {
                "gpu": _realtime_gpu(),
                "cpu": {"usage_percent": psutil.cpu_percent(interval=None)},
                "ram": {"total_gb": round(vm.total / (1024 ** 3), 1),
                        "available_gb": round(vm.available / (1024 ** 3), 1),
                        "usage_percent": vm.percent},
                "timestamp": time.time(),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("实时遥测采集失败，降级模拟数据：%s", exc)
    return {
        "gpu": {"usage_percent": 35.0, "vram_used_mb": 8192,
                "vram_total_mb": 24576, "vram_free_mb": 16384, "temp_celsius": 52.0},
        "cpu": {"usage_percent": 12.0},
        "ram": {"total_gb": 32.0, "available_gb": 16.0, "usage_percent": 50.0},
        "timestamp": time.time(),
    }


@router.get("/hardware/info")
def hardware_info():
    """硬件画像（规格 §4.6 GET /v1/hardware/info）。

    响应额外携带 tier 字段（文档B §4.2，审计 BK-011）：
    按 GPU 型号名匹配六档硬件等级（RTX 5090/4090/4070Ti/3060/
    RX6600/纯CPU），未命中按显存保守降档，供前端展示当前
    自适应档位与模型路由建议。
    """
    profile = _build_hardware_profile()
    # 用 HardwareProfile 校验结构后回吐 dict
    hp = HardwareProfile(**profile)
    data = hp.model_dump()
    from ..data.models import get_effective_tier, read_tier_override
    gpu = profile.get("gpu", {})
    data["tier"] = get_effective_tier(
        str(gpu.get("name", "")), int(gpu.get("vram_total_mb", 0)))
    data["tier_override"] = read_tier_override()
    return ok(data)


@router.put("/hardware/tier")
def hardware_tier_set(body: dict = Body(default_factory=dict)):
    """手动设置硬件档位（SET-008）：{"tier": "auto"|六档之一}。

    覆盖自动探测结果（持久化 system_settings 表，重启保持）；
    tier="auto" 恢复自动探测。生效语义：后续档位解析点
    （/hardware/info、对话模型路由、学习标签配额）立即按覆盖档
    返回；已加载模型不热切换。
    """
    from ..data.models import HARDWARE_TIER_TABLE, get_effective_tier, write_tier_override
    tier = str(body.get("tier", "auto") or "auto").strip().lower()
    valid = {e["tier"] for e in HARDWARE_TIER_TABLE} | {"auto"}
    if tier not in valid:
        raise ApiError("SYSTEM_PARAM_INVALID",
                       f"tier 仅支持 {sorted(valid)}")
    write_tier_override(tier)
    effective = get_effective_tier()  # 覆盖生效后的解析结果
    return ok({"tier_override": tier,
               "effective": effective}, message="硬件档位已更新")


@router.get("/hardware/realtime")
def hardware_realtime():
    """实时遥测（规格 §4.6 GET /v1/hardware/realtime）。"""
    return ok(_realtime_data())


@router.get("/hardware/synergy")
def hardware_synergy():
    """协同调度聚合状态（规格 §4.6 GET /v1/hardware/synergy）。

    返回 {scheduler, vram, feature_lock, thermal_guard} 四段聚合
    （对齐前端 SynergyState）：
    - scheduler:     调度引擎快照（current_mode/running 等）
    - vram:          实时显存 + 常驻/缓存模型列表
    - feature_lock:  功能互斥锁快照（规格 §6.1）
    - thermal_guard: GPU 温度保护状态机快照（规格 §4.1.3：
                     state/paused/throttling/util_cap/连续高温计数）
    调度引擎不可用时降级为空模式并记 warning（规格 §4.1 容错降级）。
    """
    # 1. 功能互斥锁快照（含空闲时长，供前端展示空闲回收倒计时）
    try:
        from ..middleware.feature_lock import get_feature_lock
        _lock_mgr = get_feature_lock()
        lock = _lock_mgr.status()
        lock["idle_seconds"] = round(_lock_mgr.idle_seconds, 1)
    except Exception:  # noqa: BLE001
        lock = {}
    feature_lock = {
        "active_feature": lock.get("active_feature"),
        "holder_task_id": lock.get("task_id"),
        "idle_seconds": lock.get("idle_seconds", 0.0),
    }

    # 2. VRAM 实时状态
    gpu = _realtime_gpu()
    total_mb = gpu["vram_total_mb"]
    used_mb = gpu["vram_used_mb"]
    vram = {
        "used_mb": used_mb,
        "total_mb": total_mb,
        "free_mb": gpu["vram_free_mb"],
        "percent": round(used_mb / total_mb * 100, 1) if total_mb else 0.0,
        "resident_models": [],
        "cached_models": [],
    }

    # 3. 调度引擎快照
    scheduler: dict = {"current_mode": None, "running": False}
    try:
        from ..services.scheduler import get_scheduler
        snap = get_scheduler().get_state()
        scheduler = {
            "current_mode": snap.get("mode"),
            "running": True,
        }
        if snap.get("active_model"):
            vram["resident_models"] = [snap["active_model"]]
        vram["cached_models"] = snap.get("cached_models", []) or []
    except Exception as exc:  # noqa: BLE001 - 降级而非崩溃
        log.warning("调度引擎状态获取失败: %s", exc)

    # 4. 热保护状态快照（规格 §4.1.3：状态经 /hardware 暴露给前端，
    #    thermal_guard.get_status() 原生承载；获取失败降级为空 dict）
    thermal_guard: dict = {}
    try:
        from ..services.thermal_guard import get_thermal_guard
        thermal_guard = get_thermal_guard().get_status()
    except Exception as exc:  # noqa: BLE001 - 降级而非崩溃
        log.warning("热保护状态获取失败: %s", exc)

    return ok({"scheduler": scheduler, "vram": vram,
               "feature_lock": feature_lock, "thermal_guard": thermal_guard})


@router.websocket("/hardware/realtime")
async def hardware_realtime_ws(websocket: WebSocket):
    """实时推送（规格 §4.6 WebSocket /v1/hardware/realtime）。

    每 2 秒推送一次遥测数据，直到客户端断开。
    消息格式：{"type": "system_status", "data": {...}}
    """
    from ..middleware.cors import ws_origin_guard
    if not await ws_origin_guard(websocket):  # 审计 R3-SEC1：防 CSWSH
        return
    await websocket.accept()
    log.info("硬件实时监控 WebSocket 已连接")
    try:
        while True:
            data = _realtime_data()
            await websocket.send_json({"type": "system_status", "data": data})
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        log.info("硬件实时推送 WebSocket 已断开")
    except Exception as exc:  # noqa: BLE001
        log.warning("硬件实时推送异常：%s", exc)
