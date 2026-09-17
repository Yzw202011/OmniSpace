# 本项目仅供学习使用，商业授权请+Q 3559331368
"""沙箱父侧执行器（插件安全模型终态=A，2026-09-17）。

含源码档（user_source）invoke 的进程外执行：派生 sandbox_host 子进程
→ 单行 job 进 → 单行结果出；**超时硬杀 / 内存上限硬杀 / 崩溃隔离**。
按 invoke 派生（无常驻池，~200ms 级开销——当前该档零用户的诚实取舍，
量大再上常驻 worker）。

应急回退闸：config ``plugins.sandbox: false`` 一键回进程内信任
（重启生效）。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("omnispace.services.plugin_runtime.sandbox")

ROOT_DIR = Path(__file__).resolve().parents[3]
SANDBOX_HOST = Path(__file__).resolve().parent / "sandbox_host.py"

DEFAULT_TIMEOUT_S = 180.0
DEFAULT_MEM_CAP_MB = 1024


class SandboxError(RuntimeError):
    """沙箱执行失败（超时/内存超限/崩溃/输出不可解析）。"""


def sandbox_enabled() -> bool:
    """总闸（config plugins.sandbox，默认 true；读取失败保持开）。"""
    try:
        from src.config import get_config
        raw = (get_config().get("plugins") or {}).get("sandbox", True)
        return bool(raw)
    except Exception:  # noqa: BLE001 - 配置异常保持沙箱（保守）
        return True


def _require_jsonable(event: dict[str, Any]) -> None:
    """事件探针：不可 JSON 序列化（如 ndarray）立即报错——沙箱档
    不支持数组直传（帧类插件属出厂档，进程内直跑不受影响）。"""
    try:
        json.dumps(event)
    except (TypeError, ValueError) as exc:
        raise SandboxError(
            f"沙箱档事件须 JSON 可序列化（ndarray 请走出厂档）: {exc}"
        ) from None


def invoke_sandboxed(name: str, source_py: Path,
                     pkg_path: Path | None, event: dict[str, Any],
                     *, timeout_s: float = DEFAULT_TIMEOUT_S,
                     mem_cap_mb: int = DEFAULT_MEM_CAP_MB) -> Any:
    """派生子进程执行单次 on_think；一切失败面收口为 SandboxError。"""
    _require_jsonable(event)
    job = json.dumps({
        "root": str(ROOT_DIR), "name": name,
        "source_path": str(source_py),
        "pkg_path": str(pkg_path) if pkg_path else None,
        "event": event}, ensure_ascii=False, default=str)

    proc = subprocess.Popen(
        [sys.executable, str(SANDBOX_HOST)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    killed: dict[str, str | None] = {"reason": None}

    def _watch_rss() -> None:
        try:
            import psutil
            p = psutil.Process(proc.pid)
            while proc.poll() is None:
                try:
                    if p.memory_info().rss > mem_cap_mb * 1024 * 1024:
                        killed["reason"] = f"内存超限(>{mem_cap_mb}MB)"
                        proc.kill()
                        return
                except Exception:  # noqa: BLE001 - 进程已退等
                    return
                time.sleep(0.5)
        except Exception:  # noqa: BLE001 - 看护线程失败不影响主链
            return

    watcher = threading.Thread(target=_watch_rss, daemon=True)
    watcher.start()
    t0 = time.monotonic()
    try:
        out, err = proc.communicate(
            input=job.encode("utf-8"), timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        raise SandboxError(
            f"沙箱插件超时(>{timeout_s:.0f}s)已被强杀") from None
    finally:
        if proc.poll() is None:
            proc.kill()

    if killed["reason"]:
        raise SandboxError(f"沙箱插件{killed['reason']}已被强杀")

    lines = (out or b"").decode("utf-8", errors="replace").strip().splitlines()
    if proc.returncode != 0 or not lines:
        raise SandboxError(
            f"沙箱插件异常退出 rc={proc.returncode}: "
            f"{(err or b'').decode('utf-8', 'replace')[:500]}")
    try:
        resp = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise SandboxError(
            f"沙箱输出不可解析: {exc}: {lines[-1][:200]}") from None
    if not resp.get("ok"):
        raise SandboxError(
            f"沙箱插件执行失败: {str(resp.get('error'))[:800]}")
    logger.info("沙箱插件完成: %s 耗时 %.2fs", name, time.monotonic() - t0)
    return resp.get("result")
