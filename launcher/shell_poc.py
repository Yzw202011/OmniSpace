#!/usr/bin/env python3
"""S0 桌面壳 PoC（ADR-004，封装发行计划 §3 六门槛之①③先行验证）。

大白话：开一个原生窗口装下 OmniSpace 界面，代替系统浏览器。两种模式：
  附加模式（默认）：只开窗指向已在跑的后端，关窗=只关窗，不动后端——
    用来在开发机上对着活实例做 PoC，零风险。
  全链模式 --quit-on-close：关窗时 POST /api/quit 优雅收尾——只对
    PoC 自己拉起的实例用（本脚本不拉起后端，由 boot.py --no-browser 负责，
    该接线属 S1，本脚本验证的是窗口与关闭钩子本身）。

用法：
  runtime/py310/python.exe launcher/shell_poc.py                 # 附加到 5800
  runtime/py310/python.exe launcher/shell_poc.py --port 5801
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

# Win10/11 自带 .NET Framework（netfx）而非 .NET Core（coreclr）；
# pythonnet 默认找 coreclr 必炸，指到 netfx（pywebview 官方 Windows 姿势）
os.environ.setdefault('PYTHONNET_RUNTIME', 'netfx')

import webview  # noqa: E402

POC_LOG = 'shell_poc.log'


def _log(msg: str) -> None:
    line = f'{time.strftime("%H:%M:%S")} {msg}'
    print(line, flush=True)
    with open(POC_LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def _healthy(base: str) -> bool:
    try:
        with urllib.request.urlopen(f'{base}/health', timeout=2) as r:
            return json.loads(r.read()).get('data', {}).get('status') == 'healthy'
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=5800)
    ap.add_argument('--quit-on-close', action='store_true',
                    help='关窗时 POST /api/quit（只对 PoC 自有实例用）')
    args = ap.parse_args()
    base = f'http://127.0.0.1:{args.port}'

    ok = _healthy(base)
    _log(f'PoC 启动：目标 {base} 健康={ok}（不健康也开窗——首启场景本就是'
         '先出窗再等后端就绪）')

    window = webview.create_window(
        'OmniSpace AI', base,
        width=1440, height=900, min_size=(1024, 680))

    def on_closing() -> bool:
        _log('CLOSE_HOOK_FIRED（closing 事件触发）')
        if args.quit_on_close:
            try:
                req = urllib.request.Request(f'{base}/api/quit', method='POST')
                urllib.request.urlopen(req, timeout=5)
                _log('已 POST /api/quit（全链模式）')
            except Exception as exc:  # noqa: BLE001
                _log(f'/api/quit 失败：{exc}')
        return True  # 允许关闭

    def on_closed() -> None:
        _log('WINDOW_DESTROYED（closed 事件，进程树收尾验证位）')

    window.events.closing += on_closing
    window.events.closed += on_closed

    _log('进入 webview.start()（EdgeChromium/WebView2 后端）……')
    webview.start(gui='edgechromium')
    _log('webview.start() 返回，PoC 正常退出')
    return 0


if __name__ == '__main__':
    sys.exit(main())
