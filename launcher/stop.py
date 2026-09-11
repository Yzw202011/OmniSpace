#!/usr/bin/env python3
"""OmniSpace 停止脚本（免黑窗启动配套，2026-08-31）。

桌面快捷方式以 pythonw 无窗方式拉起 launcher/boot.py 后，没有控制台
窗口可关、没有 Ctrl+C 可发——本脚本是无窗模式的主动退出通道：

  1. 优雅路径：定位启动页 HTTP 服务（5850-5869，校验 Server 头防误伤
     同区间陌生服务），POST /api/quit 令 boot 自行走 _shutdown（后端
     terminate→kill、ComfyUI 清理、splash 关停），等效于关闭控制台窗口。
  2. 兜底路径：优雅退出超时（boot 卡死/异常态）时，按严格匹配清理本
     启动链进程：boot.py 进程 → 端口 5800-5835 的 backend.main uvicorn
     → 8189 ComfyUI 残留（对齐 launcher/boot 的三道防线口径）。

安全边界：只处理「本启动链」管理的对象。项目内以其他方式直启的实例
（如 :8765 的 uvicorn 及其 vLLM 子进程）只检测提示、绝不触碰。
HTTP 出站仅指向字面量回环主机 127.0.0.1（http.client，不跟随重定向），
端口只接受 psutil 实测 LISTEN 的 int，无任何外部可达面。

用法：
  runtime\\py310\\python.exe launcher\\stop.py [--grace 秒]
"""
from __future__ import annotations

import argparse
import http.client
import os
import re
import sys
import time
from pathlib import Path

import psutil

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:  # noqa: BLE001 - 保底不阻断
            pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECT_MARKER = str(PROJECT_ROOT).lower()
SPLASH_PORT_RANGE = (5850, 5869)
BACKEND_PORT_RANGE = (5800, 5835)
COMFY_PORT = int(os.environ.get('OMNISPACE_COMFYUI_PORT', '8189'))
LOOPBACK_HOST = '127.0.0.1'


def _ports_listening(lo: int, hi: int) -> set[int]:
    try:
        return {c.laddr.port for c in psutil.net_connections(kind='inet')
                if c.status == 'LISTEN' and c.laddr and lo <= c.laddr.port <= hi}
    except Exception:  # noqa: BLE001 - 降级为空集，后续进程匹配仍可用
        return set()


def _splash_port() -> int | None:
    """在启动页端口区间内找 boot 的 splash 服务。

    先经 psutil 筛真实 LISTEN 端口再做 HTTP 校验（本机存在过滤驱动时，
    向未监听回环端口的请求会各烧满超时）；Server 头前缀匹配确保
    /api/quit 只发给 OmniSpace boot，不会误伤同区间的陌生服务。
    """
    for port in sorted(_ports_listening(*SPLASH_PORT_RANGE)):
        conn: http.client.HTTPConnection | None = None
        try:
            conn = http.client.HTTPConnection(LOOPBACK_HOST, port, timeout=1.5)
            conn.request('GET', '/api/status')
            resp = conn.getresponse()
            if (resp.status == 200
                    and resp.getheader('Server', '').startswith('OmniSpaceBoot')):
                return port
        except Exception:
            continue
        finally:
            if conn is not None:
                conn.close()
    return None


def _cmdline(proc: psutil.Process) -> str:
    try:
        return ' '.join(proc.cmdline()).lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return ''


def _boot_processes() -> list[psutil.Process]:
    """本启动链的 boot 进程：python/pythonw 运行 launcher\\boot.py。

    匹配三要素（launcher + boot.py + 项目路径，pythonw 的 argv[0]
    即 runtime 内解释器路径，天然含项目标记），排除同名无关脚本。
    """
    out = []
    for proc in psutil.process_iter(['pid']):
        cl = _cmdline(proc)
        if 'boot.py' in cl and 'launcher' in cl and PROJECT_MARKER in cl:
            out.append(proc)
    return out


def _backend_processes() -> tuple[list[tuple[psutil.Process, int]],
                                  list[tuple[psutil.Process, int | None]]]:
    """区分本启动链的后端（--port 落在 5800-5835）与项目内独立实例。

    boot 经 PortManager 拉起的 uvicorn 端口必在区间内；区间外的
    （如 launcher.py 旧入口默认的 :8765）属于独立实例，只提示不处理。
    """
    ours, others = [], []
    for proc in psutil.process_iter(['pid']):
        cl = _cmdline(proc)
        if 'backend.main:app' not in cl or PROJECT_MARKER not in cl:
            continue
        m = re.search(r'--port\s+(\d+)', cl)
        port = int(m.group(1)) if m else None
        if port is not None and BACKEND_PORT_RANGE[0] <= port <= BACKEND_PORT_RANGE[1]:
            ours.append((proc, port))
        else:
            others.append((proc, port))
    return ours, others


def _find_comfy_leftover() -> psutil.Process | None:
    """8189 监听且 cmdline 严格含 comfyui/main.py 的残留进程（不查 cwd：
    ComfyUI 便携版工作目录在 tools/ 子树，项目根标记不在其 cwd 内）。"""
    try:
        for conn in psutil.net_connections(kind='inet'):
            if (conn.laddr and conn.laddr.port == COMFY_PORT
                    and conn.status == 'LISTEN'):
                try:
                    proc = psutil.Process(conn.pid)
                    if 'comfyui/main.py' in _cmdline(proc):
                        return proc
                except psutil.NoSuchProcess:
                    pass
                break
    except Exception:  # noqa: BLE001
        pass
    return None


def _llama_leftovers() -> list[psutil.Process]:
    """llama-server / vLLM 推理子进程孤儿（UAT 2026-09-10 发现：优雅退出后
    llama-server 持 8.6GB 显存残留——它们由后端派生但可能逃逸 Job Object）。

    匹配口径：exe/cmdline 含 llama-poc（llama.cpp 运行时目录），或进程名
    OmniSpace-LLM.exe（vLLM py313 专属运行时，命名唯一不误伤）。"""
    found: list[psutil.Process] = []
    me = psutil.Process().pid
    for proc in psutil.process_iter(['pid', 'name', 'exe', 'cmdline']):
        try:
            if proc.info['pid'] == me:
                continue
            exe = proc.info['exe'] or ''
            name = proc.info['name'] or ''
            cmd = ' '.join(proc.info['cmdline'] or [])
            if 'llama-poc' in f'{exe} {name} {cmd}' or name == 'OmniSpace-LLM.exe':
                found.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def _graceful_quit(port: int) -> None:
    conn = http.client.HTTPConnection(LOOPBACK_HOST, port, timeout=3)
    try:
        conn.request('POST', '/api/quit')
        resp = conn.getresponse()
        resp.read()
        if resp.status != 200:
            raise RuntimeError(f'/api/quit 响应异常: HTTP {resp.status}')
    finally:
        conn.close()


def _stack_cleared() -> bool:
    """优雅退出完成判据：splash、boot 进程、区间后端监听全部消失。

    不把 ComfyUI 计入——它经 Job Object 随后端异步退出，末尾统一兜底。
    """
    return (not _boot_processes()
            and not _ports_listening(*BACKEND_PORT_RANGE)
            and _splash_port() is None)


def _terminate(procs: list[psutil.Process], label: str) -> bool:
    if not procs:
        return False
    for proc in procs:
        try:
            proc.terminate()
            print(f'  已终止{label} (PID {proc.pid})')
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(procs, timeout=5)
    for proc in alive:
        try:
            proc.kill()
            print(f'  已强制终止{label} (PID {proc.pid})')
        except psutil.NoSuchProcess:
            pass
    return True


def _report_others(others: list[tuple[psutil.Process, int | None]]) -> None:
    for proc, port in others:
        where = f':{port}' if port else '(端口未知)'
        print(f'提示：检测到项目内独立后端实例 {where} (PID {proc.pid})，'
              f'非本启动链管理，未处理。')


def main() -> int:
    parser = argparse.ArgumentParser(description='OmniSpace 停止脚本')
    parser.add_argument('--grace', type=float, default=30.0,
                        help='优雅退出等待秒数（默认 30）')
    args = parser.parse_args()

    print('=' * 60)
    print('OmniSpace AI · 停止')
    print('=' * 60)

    # 重试扫描数秒：覆盖「启动后立即点停止」的竞态（splash 尚未 bind）
    splash = None
    for _ in range(6):
        splash = _splash_port()
        if splash is not None:
            break
        time.sleep(0.5)

    boots = _boot_processes()
    ours, others = _backend_processes()
    comfy = _find_comfy_leftover()

    if splash is None and not boots and not ours and comfy is None:
        print('未发现运行中的 OmniSpace 启动链（启动页 / 后端 5800-5835 / ComfyUI 均无）。')
        _report_others(others)
        return 0

    # ① 优雅路径：让 boot 自行收尾（与关控制台窗口等效且更干净）
    if splash is not None:
        print(f'发现启动页服务 :{splash}，发送优雅退出请求…')
        try:
            _graceful_quit(splash)
            print('已请求退出，等待启动链收尾（后端停止 + ComfyUI 清理）…')
        except Exception as e:  # noqa: BLE001 - 转兜底
            print(f'优雅退出请求失败: {e}')
        deadline = time.time() + args.grace
        while time.time() < deadline:
            if _stack_cleared():
                print('✓ 已优雅退出，收尾完成。')
                # 推理子进程孤儿兜底（llama-server/vLLM 可能逃逸 Job Object，
                # UAT 2026-09-10 实测残留 8.6GB 显存）
                leftovers = _llama_leftovers()
                if leftovers:
                    _terminate(leftovers, ' 推理子进程孤儿(llama/vLLM)')
                _report_others(others)
                return 0
            time.sleep(1.0)
        print(f'{args.grace:.0f}s 内未完全退出，转兜底清理。')
    else:
        print('未发现启动页服务（boot 不在或已异常），直接兜底清理。')

    # ② 兜底路径：先杀 boot（否则其心跳会把后端当崩溃拉起），再后端，
    #    最后 ComfyUI（正常情况下已随后端 Job Object 共生死，此处兜残留）
    _terminate(_boot_processes(), ' boot 进程')
    time.sleep(1)
    ours_now, _ = _backend_processes()
    _terminate([p for p, _ in ours_now], ' 后端进程')
    if comfy is not None:
        try:
            comfy.kill()
            print(f'  已清理 ComfyUI 残留 (PID {comfy.pid})')
        except psutil.NoSuchProcess:
            pass
    llama = _llama_leftovers()
    _terminate(llama, ' 推理子进程孤儿(llama/vLLM)')

    ok = _stack_cleared() and _find_comfy_leftover() is None and not _llama_leftovers()
    print('✓ 兜底清理完成。' if ok else '✗ 清理后仍有残留，请检查任务管理器。')
    _report_others(others)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
# 本项目仅供学习使用，商业授权请+Q 3559331368
