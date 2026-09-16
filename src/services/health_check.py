"""一键体检与修复（自愈批4，2026-09-11 自愈与横切内建方案 §批4，吸收 L1）。

只读体检 ``run_health_check()``：普通用户「不专业也不怕」——出问题先体检，
大白话报告 + 每项给出路；与 POST /system/diagnose（环境级 26 项，startup_check）
互补：本模块管**运行时**健康（显存余量/孤儿进程/模型对账/队列/日志保留/激活）。

修复 ``repair(action)``：**白名单制**，只做零数据风险动作——
  - clean_logs    清 30 天过期日志（复用 event_log.cleanup_expired）
  - rebuild_dirs  重建缺失的系统目录（固定清单，绝不接受路径入参）
  - kill_orphans  清「确认孤儿」的推理子进程（口径与 launcher/stop.py 一致：
                  llama-poc 运行目录 / OmniSpace-LLM.exe；守卫=绝不碰
                  后端本体与任何 python 进程）
其余体检项只报告 + 给出路，不自动修。
"""
from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import DATA_DIR, LOGS_DIR, MODELS_DIR

#: 重建目录固定清单（零数据风险：只 mkdir，永不删除）
_SYSTEM_DIRS: tuple[Path, ...] = (
    DATA_DIR,
    LOGS_DIR,
    MODELS_DIR,
    LOGS_DIR / "events",
    DATA_DIR / "comfyui" / "output",
    DATA_DIR / "comfyui" / "input",
    DATA_DIR / "comfyui" / "temp",
    DATA_DIR / "comfyui" / "user",
)

#: 磁盘余量注意线（GB）；低于即建议清理
_DISK_WARN_GB = 20.0

#: 孤儿推理进程匹配口径（镜像 launcher/stop.py：宁可漏匹配不可误伤）
_ORPHAN_SIGNS = ("llama-poc", "OmniSpace-LLM.exe")

#: 活栈属主进程名特征（父链命中=有主，绝不判孤儿）——2026-09-11 实弹教训：
#: 多实例共存时（测试实例 :5827 vs 生产 :5800），名字口径会把生产后端
#: 合法持有的推理子进程误判成孤儿，一键清理=杀生产的推理引擎
_OWNER_SIGNS = ("python", "omnispace-backend", "omnispace-boot", "omnispace-shell")


def _matched_engine_procs() -> list[tuple[Any, str]]:
    """按名字口径匹配推理进程（不判孤儿，只做初筛）。"""
    import psutil
    matched: list[tuple[Any, str]] = []
    me = psutil.Process().pid
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            if proc.info["pid"] == me:
                continue
            exe = proc.info["exe"] or ""
            name = proc.info["name"] or ""
            cmd = " ".join(proc.info["cmdline"] or [])
            if any(sign in f"{exe} {name} {cmd}" for sign in _ORPHAN_SIGNS):
                matched.append((proc, name))
        except Exception:  # noqa: BLE001 - 单进程读取失败跳过
            continue
    return matched


def _is_owned_by_live_stack(proc: Any, _depth: int = 0) -> bool:
    """父链上有活着的 python/后端栈属主 = 有主，不是孤儿。

    祖先链穿过同类的 OmniSpace-LLM/llama 推理进程继续向上走
    （引擎父子链的根才是属主判定点）；父进程已死 = 真孤儿。
    """
    try:
        if _depth > 8:  # 防御异常进程树成环/过深
            return True  # 判不定=有主（保守：宁可漏修不可误杀）
        parent = proc.parent()
        if parent is None:
            return False  # 父已死=真孤儿
        # 注意：parent() 返回的 Process 没有 .info 属性（只有 process_iter
        # 产出的有）——必须走 name()；此处曾因误读 .info 抛 AttributeError
        # 被兜底吞掉，导致整条父链被误判「有主」（2026-09-11 实弹揪出）
        pname = str(parent.name() or "").lower()
        if any(sign in pname for sign in _OWNER_SIGNS):
            return True  # 活着的属主
        return _is_owned_by_live_stack(parent, _depth + 1)
    except Exception:  # noqa: BLE001 - 判不定按有主处理
        return True


def _classify_orphans() -> tuple[list[dict[str, Any]], list[Any], list[Any]]:
    """匹配口径内的进程 → (真孤儿[(名片, 进程对象)], 孤儿进程对象, 有主进程)。"""
    orphans: list[dict[str, Any]] = []
    orphan_procs: list[Any] = []
    owned: list[Any] = []
    for proc, name in _matched_engine_procs():
        try:
            if _is_owned_by_live_stack(proc):
                owned.append(proc)
                continue
            orphans.append({"pid": proc.info["pid"], "name": name})
            orphan_procs.append(proc)
        except Exception:  # noqa: BLE001
            continue
    return orphans, orphan_procs, owned


def _check_gpu_vram_free() -> dict[str, Any]:
    """显存余量（诚实返回：采集失败=unknown，不编造）。"""
    try:
        import torch  # type: ignore
        if not torch.cuda.is_available():
            return {"key": "vram", "level": "warn",
                    "friendly": "未检测到独立显卡，AI 生成类功能将受限",
                    "fixable": False}
        free_b, total_b = torch.cuda.mem_get_info()
        free_gb = free_b / 1024 ** 3
        total_gb = total_b / 1024 ** 3
        used_pct = (total_b - free_b) / total_b * 100 if total_b else 0.0
        if used_pct >= 95:
            level, note = "warn", "显存几乎占满，正在运行的功能可能失败"
        elif used_pct >= 90:
            level, note = "warn", "显存偏高，建议关闭暂不用的 AI 功能"
        else:
            level, note = "ok", "显存充足"
        return {"key": "vram", "level": level,
                "friendly": f"显存余量 {free_gb:.1f}GB（共 {total_gb:.0f}GB），{note}",
                "fixable": False,
                "detail": {"free_gb": round(free_gb, 1),
                           "total_gb": round(total_gb, 1),
                           "used_pct": round(used_pct, 1)}}
    except Exception as exc:  # noqa: BLE001
        return {"key": "vram", "level": "unknown",
                "friendly": "显存状态暂时读不到（不影响使用）",
                "detail": str(exc)[:200]}


def _check_ram() -> dict[str, Any]:
    try:
        import psutil
        vm = psutil.virtual_memory()
        if vm.percent >= 90:
            level, note = "warn", "内存吃紧，请关闭其他大程序"
        else:
            level, note = "ok", "内存充足"
        return {"key": "ram", "level": level,
                "friendly": f"内存占用 {vm.percent:.0f}%，{note}", "fixable": False}
    except Exception as exc:  # noqa: BLE001
        return {"key": "ram", "level": "unknown",
                "friendly": "内存状态暂时读不到", "detail": str(exc)[:200]}


def _check_disk() -> dict[str, Any]:
    try:
        du = shutil.disk_usage(str(DATA_DIR))
        free_gb = du.free / 1024 ** 3
        if free_gb < _DISK_WARN_GB:
            level = "warn"
            note = "空间偏低，建议清理旧生成结果"
        else:
            level = "ok"
            note = "空间充足"
        return {"key": "disk", "level": level,
                "friendly": f"磁盘剩余 {free_gb:.0f}GB，{note}", "fixable": False}
    except Exception as exc:  # noqa: BLE001
        return {"key": "disk", "level": "unknown",
                "friendly": "磁盘状态暂时读不到", "detail": str(exc)[:200]}


def _check_orphan_engines() -> dict[str, Any]:
    """孤儿推理进程（名字口径 + 父链守卫，见 _classify_orphans）。"""
    try:
        orphans, _procs, _owned = _classify_orphans()
    except Exception as exc:  # noqa: BLE001
        return {"key": "orphan_engines", "level": "unknown",
                "friendly": "进程状态暂时读不到", "detail": str(exc)[:200]}
    if orphans:
        names = "、".join(f"{p['name']}(pid {p['pid']})" for p in orphans[:5])
        return {"key": "orphan_engines", "level": "warn",
                "friendly": f"检测到 {len(orphans)} 个残留的推理进程（{names}），"
                            "可能还占着显存，可一键清理",
                "fixable": True, "fix_action": "kill_orphans",
                "detail": {"processes": orphans}}
    return {"key": "orphan_engines", "level": "ok",
            "friendly": "没有残留的推理进程", "fixable": False}


def _check_models_manifest() -> dict[str, Any]:
    """模型登记 vs 磁盘对账（复用发行就绪总检，只读）。"""
    try:
        from .model_manager.readiness import compute_readiness
        r = compute_readiness(MODELS_DIR, MODELS_DIR / "models_manifest.json")
        if not r.get("manifest_found"):
            return {"key": "models_manifest", "level": "warn",
                    "friendly": "模型清单缺失（软件包可能不完整），"
                                "请到「模型管理」页查看",
                    "fixable": False}
        missing = int(r.get("missing_count") or 0)
        if missing:
            return {"key": "models_manifest", "level": "warn",
                    "friendly": f"有 {missing} 个模型文件不在盘上，"
                                "受影响的功能会不可用；恢复方式见「模型管理」页",
                    "fixable": False,
                    "detail": {"missing_count": missing}}
        return {"key": "models_manifest", "level": "ok",
                "friendly": "模型登记与磁盘对账一致", "fixable": False}
    except Exception as exc:  # noqa: BLE001
        return {"key": "models_manifest", "level": "unknown",
                "friendly": "模型对账暂时跑不了", "detail": str(exc)[:200]}


def _check_queues() -> dict[str, Any]:
    """生成队列状态（信息性：排队/运行中数量）。"""
    try:
        from .image_queue import ImageTaskQueue
        from .video_queue import VideoTaskQueue
        img = ImageTaskQueue.instance().snapshot()
        vid = VideoTaskQueue.instance().snapshot()
        pending = int(img.get("pending", 0) or 0) + int(vid.get("pending", 0) or 0)
        running = int(img.get("running", 0) or 0) + int(vid.get("running", 0) or 0)
        if pending + running:
            friendly = (f"生成队列：{running} 个进行中，{pending} 个排队"
                        "（会自动依次完成）")
        else:
            friendly = "生成队列空闲"
        return {"key": "queues", "level": "ok", "friendly": friendly,
                "fixable": False,
                "detail": {"image": img, "video": vid}}
    except Exception as exc:  # noqa: BLE001
        return {"key": "queues", "level": "unknown",
                "friendly": "队列状态暂时读不到", "detail": str(exc)[:200]}


def _check_logs_retention() -> dict[str, Any]:
    """日志保留：30 天外过期文件数（可一键清理）。"""
    try:
        from .event_log import EVENTS_DIR
        if not EVENTS_DIR.is_dir():
            return {"key": "logs_retention", "level": "ok",
                    "friendly": "暂无历史日志，无需清理", "fixable": False}
        cutoff = time.time() - 30 * 86400
        expired = [f for f in EVENTS_DIR.glob("events-*.jsonl")
                   if f.stat().st_mtime < cutoff]
        if expired:
            return {"key": "logs_retention", "level": "warn",
                    "friendly": f"有 {len(expired)} 个超过 30 天的过期日志文件，"
                                "可一键清理",
                    "fixable": True, "fix_action": "clean_logs",
                    "detail": {"expired": [f.name for f in expired]}}
        return {"key": "logs_retention", "level": "ok",
                "friendly": "日志都在保留期内，无需清理", "fixable": False}
    except Exception as exc:  # noqa: BLE001
        return {"key": "logs_retention", "level": "unknown",
                "friendly": "日志状态暂时读不到", "detail": str(exc)[:200]}


def _check_license() -> dict[str, Any]:
    try:
        from ..license_gate import is_activated
        activated, reason = is_activated()
        if activated:
            return {"key": "license", "level": "ok",
                    "friendly": "产品已激活", "fixable": False}
        return {"key": "license", "level": "warn",
                "friendly": f"产品尚未激活（{reason}）；部分功能可能受限，"
                            "请在启动页输入激活码",
                "fixable": False}
    except Exception as exc:  # noqa: BLE001
        return {"key": "license", "level": "unknown",
                "friendly": "激活状态暂时读不到", "detail": str(exc)[:200]}


#: 体检项注册表（顺序即报告顺序）
CHECKS: tuple[Callable[[], dict[str, Any]], ...] = (
    _check_gpu_vram_free,
    _check_ram,
    _check_disk,
    _check_orphan_engines,
    _check_models_manifest,
    _check_queues,
    _check_logs_retention,
    _check_license,
)

#: 修复动作白名单（零数据风险；键=fix_action，值=执行器）
REPAIR_ACTIONS: dict[str, Callable[[], dict[str, Any]]] = {}


def _repair_clean_logs() -> dict[str, Any]:
    from .event_log import cleanup_expired
    r = cleanup_expired()
    return {"action": "clean_logs",
            "friendly": f"已清理 {len(r['deleted'])} 个过期日志文件，"
                        f"释放 {r['freed_bytes'] / 1024 / 1024:.1f}MB",
            **r}


def _repair_rebuild_dirs() -> dict[str, Any]:
    created = []
    for d in _SYSTEM_DIRS:
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            created.append(str(d))
    return {"action": "rebuild_dirs",
            "friendly": (f"已重建 {len(created)} 个缺失目录" if created
                         else "系统目录齐全，无需重建"),
            "created": created}


def _repair_kill_orphans() -> dict[str, Any]:
    """清确认孤儿：名字口径 + 父链守卫（有主进程绝不碰）。"""
    orphans, procs, _owned = _classify_orphans()
    killed: list[int] = []
    for entry, proc in zip(orphans, procs, strict=True):
        name = str(entry["name"] or "").lower()
        if name.startswith("python"):
            continue  # 守卫：任何 python 进程绝不碰
        try:
            proc.terminate()
            killed.append(int(entry["pid"]))
        except Exception:  # noqa: BLE001 - 单进程失败跳过
            continue
    return {"action": "kill_orphans",
            "friendly": (f"已清理 {len(killed)} 个残留推理进程（pid {killed}）"
                         if killed else "没有需要清理的残留进程"),
            "killed": killed}


REPAIR_ACTIONS.update({
    "clean_logs": _repair_clean_logs,
    "rebuild_dirs": _repair_rebuild_dirs,
    "kill_orphans": _repair_kill_orphans,
})


def run_health_check() -> dict[str, Any]:
    """只读体检：跑全部 CHECKS，汇成人话总评。"""
    items = []
    for fn in CHECKS:
        try:
            items.append(fn())
        except Exception as exc:  # noqa: BLE001 - 单项失败不影响整卷
            items.append({"key": fn.__name__, "level": "unknown",
                          "friendly": "该项检查暂时跑不了",
                          "detail": str(exc)[:200]})
    warn = sum(1 for i in items if i.get("level") == "warn")
    fail = sum(1 for i in items if i.get("level") == "fail")
    if fail:
        summary = f"发现 {fail} 个问题需要处理"
    elif warn:
        summary = f"基本健康，有 {warn} 项建议处理"
    else:
        summary = "一切正常"
    return {"items": items, "summary": summary,
            "counts": {"warn": warn, "fail": fail,
                       "total": len(items)},
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def repair(action: str) -> dict[str, Any]:
    """执行白名单修复动作；白名单外一律拒绝。"""
    fn = REPAIR_ACTIONS.get(action)
    if fn is None:
        from ..middleware.error_handler import ApiError
        raise ApiError("SYSTEM_PARAM_INVALID",
                       f"不支持的修复动作: {action}",
                       detail={"allowed": sorted(REPAIR_ACTIONS)})
    return fn()
