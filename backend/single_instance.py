"""全机单实例限制（2026-08-31 落地）。

一台电脑同一时间只允许跑一个 OmniSpace 后端进程。动机是实测教训：
双栈叠载会把 16GB 显存塞爆——2026-08-29 蓝屏的诱因之一；2026-08-31
再次复现：旧 :8765 实例与新 :5800 实例并存，关键帧采样从 ~1 分钟被拖到
20+ 分钟直至任务超时失败。

机制：Windows 命名互斥体（Local\\ 命名空间，交互会话内全机唯一）。
进程正常退出、崩溃或被强杀时操作系统自动回收句柄，不存在锁文件那样
的残留假锁问题。

覆盖范围：所有启动入口（boot.py / 旧 launcher.py / 直接 uvicorn）最终
都会拉起 backend.main，此处是最终防线；boot.py 的 splash 单实例守卫
只覆盖新启动链自己的重复双击场景。

豁免：pytest 运行态（TestClient 会触发 lifespan，不能与真实实例互斥）
与显式 OMNISPACE_ALLOW_MULTI=1（调试双开自担风险）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import ctypes
import os
import sys

_MUTEX_NAME = r'Local\OmniSpace.backend.singleinstance'
_ERROR_ALREADY_EXISTS = 183
_handle: int | None = None


def _exempt() -> bool:
    if os.environ.get('OMNISPACE_ALLOW_MULTI') == '1':
        return True
    # PYTEST_VERSION 在整个 pytest 进程生命周期内存在（含 fixture 阶段），
    # 比 PYTEST_CURRENT_TEST（仅测试函数执行期）覆盖更完整
    return bool(os.environ.get('PYTEST_VERSION')) or 'pytest' in sys.argv[0]


def acquire() -> bool:
    """尝试获取全机单实例锁。

    True = 本进程持有（或处于豁免/接口失败放行态）；
    False = 本机已有 OmniSpace 后端实例在运行，应拒绝启动。
    """
    global _handle
    if _handle is not None:
        return True
    if _exempt():
        return True
    if sys.platform != 'win32':
        return True  # 项目本身仅支持 Windows（launcher 环境自检同口径）
    try:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        if handle == 0:
            # Win32 API 失败不拦截启动：宁可冒双开风险也不误杀正常使用
            return True
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        _handle = handle  # 持有到进程退出，由 OS 自动回收
        return True
    except Exception:
        return True
