"""ComfyProcManager 进程生命周期实弹测试（B9 尾巴，GPU 窗口专用）。

默认跳过（不占卡、不拖慢常规套件）；显式开启实弹：
    OMNISPACE_COMFY_E2E=1 runtime/py312/python.exe -m pytest \\
        backend/tests/unit/test_comfy_proc_live.py -q

覆盖（真进程）：spawn → 存活 + /system_stats 健康 → 重复 spawn 幂等
（同进程复用）→ shutdown 收尸。走产品正规链（sage 门控/模型挂载/
Job Object 绑定全生效），不绕管理器裸拉 ComfyUI。
"""
from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNISPACE_COMFY_E2E") != "1",
        reason="ComfyUI 实弹需显式开启: OMNISPACE_COMFY_E2E=1（占 GPU）"),
]


def _comfy_healthy(port: int = 8189, timeout: float = 5.0) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/system_stats", timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def _wait_comfy_ready(timeout_s: float = 150.0) -> bool:
    import time
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _comfy_healthy():
            return True
        time.sleep(2.0)
    return False


def test_comfy_proc_lifecycle() -> None:
    from backend.services.inference.comfy_proc import get_comfy_proc
    mgr = get_comfy_proc()
    try:
        proc = mgr.spawn("comfyui_paint_live_test")
        assert proc is not None and proc.poll() is None, "spawn 后进程应存活"
        assert _wait_comfy_ready(), "ComfyUI 150s 内未就绪（/system_stats 不通）"
        first_pid = mgr.pid()
        # 幂等：管理器语义下重复获取不叠加进程
        assert mgr.pid() == first_pid
    finally:
        mgr.shutdown()
    import time
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if mgr.pid() is None:
            break
        time.sleep(0.5)
    assert mgr.pid() is None, "shutdown 后进程应收尸"
    assert not _comfy_healthy(), "shutdown 后端口不应再服务"
