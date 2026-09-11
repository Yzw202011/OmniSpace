#!/usr/bin/env python3
"""OmniSpace 桌面壳（正式版，2026-09-08 接线 S1）。

大白话：开一个原生窗口装下 OmniSpace 界面（EdgeChromium/WebView2 内核），
代替系统浏览器。由 boot.py 在启动链就绪后拉起（shell_poc.py 是 S0 验证档，
本文件是其正式化转正）。

关窗语义（ADR-004 吸收 page_guard 方案）：本壳不直接杀后端——关窗后页面
WebSocket 断开，后端 page_guard 按「全部页面关闭 60s 且无任务」倒计时走
正规退出链（/api/quit 由启动页发起）。壳进程自身随窗口关闭自然退出。

用法（boot 调用，也可手动）：
  runtime/py310/OmniSpace-Shell.exe launcher/shell.py --url http://127.0.0.1:5800
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# Win10/11 自带 .NET Framework（netfx）而非 .NET Core（coreclr）；
# pythonnet 默认找 coreclr 必炸，指到 netfx（pywebview 官方 Windows 姿势）
os.environ.setdefault('PYTHONNET_RUNTIME', 'netfx')

import webview  # noqa: E402

LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'logs', 'shell.log')


def _log(msg: str) -> None:
    line = f'{time.strftime("%Y-%m-%d %H:%M:%S")} {msg}'
    # DETACHED/无控制台进程 stdout 句柄无效——print 会抛 OSError 崩进程
    # （免黑窗启动链同款教训，先探句柄再打印）
    try:
        if sys.stdout is not None:
            print(line, flush=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:  # noqa: BLE001 - 日志失败不挡窗口
        pass


def _renderer_process_limit() -> int:
    """WebView2 渲染进程数动态档位（2026-09-08 UI 降载方案③）。

    用户拍板口径：不写死——配置好的机器开到 5（Chromium 默认按站点
    派进程，本应用单源单页常态 1-2 个，5 = 上限宽松到无感）；低配
    收紧省内存。判定口径：物理内存（本产品显存/内存双大户，RAM 是
    webview 进程的直接约束）。
      ≥32GB → 5（高配，无感上限）
      16~32GB → 3（中配）
      <16GB → 2（低配，够主页面+偶发弹层）
    """
    try:
        import ctypes
        class MEMORYSTATUSEX(ctypes.Structure):
            # 字段必须补全到 API 真实长度（dwLength 校验用，短结构
            # 会被 GlobalMemoryStatusEx 拒写——实测踩坑：只声明 3 字段
            # 时 ullTotalPhys 恒 0，档位恒落最低档）
            _fields_ = [('dwLength', ctypes.c_ulong),
                        ('dwMemoryLoad', ctypes.c_ulong),
                        ('ullTotalPhys', ctypes.c_ulonglong),
                        ('ullAvailPhys', ctypes.c_ulonglong),
                        ('ullTotalPageFile', ctypes.c_ulonglong),
                        ('ullAvailPageFile', ctypes.c_ulonglong),
                        ('ullTotalVirtual', ctypes.c_ulonglong),
                        ('ullAvailVirtual', ctypes.c_ulonglong),
                        ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        # 阈值留标称/可用差余量（32GB 标称机 ullTotalPhys≈31.6GB，
        # 卡 32 整会错落中档——实测踩坑）：≥30 视作 32GB 级、≥15 视作
        # 16GB 级
        total_gb = stat.ullTotalPhys / 2 ** 30
        if total_gb >= 30:
            return 5
        if total_gb >= 15:
            return 3
        return 2
    except Exception:  # noqa: BLE001 - 探测失败给中配档
        return 3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', required=True, help='要装载的页面地址')
    ap.add_argument('--width', type=int, default=1440)
    ap.add_argument('--height', type=int, default=900)
    args = ap.parse_args()

    # WebView2 官方环境变量通道：渲染进程上限（必须在 webview 环境
    # 创建前设置；GPU/网络服务进程不受此控，只限页面渲染进程）
    limit = _renderer_process_limit()
    os.environ['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'] = (
        f'--renderer-process-limit={limit}')
    _log(f'壳启动：装载 {args.url}（渲染进程上限 {limit}）')
    window = webview.create_window(
        'OmniSpace AI', args.url,
        width=args.width, height=args.height, min_size=(1024, 680))

    def on_closed() -> None:
        # 关窗=页面 WS 断开 → 后端 page_guard 60s 倒计时正规退出链
        _log('窗口已关闭（后端由 page_guard 倒计时接管，60s 无任务自动退出）')

    assert window is not None  # create_window 类型宽收窄
    window.events.closed += on_closed
    try:
        webview.start(gui='edgechromium')
    except Exception as exc:  # noqa: BLE001 - 壳失败回退提示（boot 侧有浏览器兜底）
        _log(f'壳启动异常：{exc}')
        return 1
    _log('壳正常退出')
    return 0


if __name__ == '__main__':
    sys.exit(main())
# 本项目仅供学习使用，商业授权请+Q 3559331368
