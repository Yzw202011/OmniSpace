#!/usr/bin/env python3
"""OmniSpace Boot 启动主程序（2026-08-31）。

ADR-002 交付形态（launcher + 系统浏览器）内的统一启动编排器：
双击 启动OmniSpace.bat 拉起本进程，Splash 启动页（系统浏览器）全程
可视化启动过程，后端就绪后即后台预热对话模型——vLLM 冷启动
~157s 在启动页阶段消化，直击 UAT 台账 P0 M-05（冷启动模态突袭）
与 M-08（AI 切分 242s 黑箱等待；漫剧 ai-describe 与对话共用 vLLM）。

启动阶段：
  splash   启动页就绪（<1s 浏览器打开，stdlib HTTP :5850+）
  env      环境自检（复用 launcher.EnvironmentChecker，异步流式上报）
  backend  后端启动（幂等：先探测 5800-5835 健康实例→接管；否则 spawn）
  warmup   预热点火（POST /models/warmup → vLLM 后台加载）
  ready    就绪（启动页 5s 倒计时自动进入主应用）

守护模式（ready 之后持续运行）：
  - 后端心跳/崩溃重启（复用 launcher.BackendProcess，≤5 次）
  - 接管实例死亡 → 自动补位 spawn
  - ComfyUI 可选预热（复用外部实例机制，后端探测 8189 自动复用）
  - 退出时清理 ComfyUI 残留（吸收 watchdog_backend.ps1 兜底职责）

用法：
  runtime\\py310\\python.exe launcher\\boot.py [--port 5800] [--no-browser]
      [--comfy] [--no-warmup]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil

BOOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BOOT_DIR))

# boot 统一日志（2026-09-01 日志机制方案 C）：不再依赖外层重定向产生
# boot_out1/2/3… 编号文件，BootState.log 统一 tee 到 logs/boot.log
# （追加式 + 简单截断保护），/logs/raw 白名单可直接 tail 本文件
BOOT_LOG = BOOT_DIR.parent / 'logs' / 'boot.log'
_boot_log_lock = threading.Lock()
# 轮转阈值（2026-09-16：原「超 20MB 砍前半」无痕丢一半历史且不生成
# 归档；改标准轮转 boot.log → boot.log.1，保留一代完整启动现场）
_BOOT_LOG_ROTATE_BYTES = 10 * 1024 * 1024


def _boot_log_write(line: str) -> None:
    try:
        with _boot_log_lock:
            if (BOOT_LOG.exists()
                    and BOOT_LOG.stat().st_size > _BOOT_LOG_ROTATE_BYTES):
                rotated = BOOT_LOG.parent / (BOOT_LOG.name + '.1')
                try:
                    rotated.unlink(missing_ok=True)
                    BOOT_LOG.replace(rotated)
                except OSError:
                    pass
            with open(BOOT_LOG, 'a', encoding='utf-8') as f:
                f.write(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} {line}\n')
    except Exception:  # noqa: BLE001 - 日志失败绝不阻断启动
        pass

from launcher import (  # noqa: E402
    PROJECT_ROOT,
    BackendProcess,
    EnvironmentChecker,
    LauncherConfig,
    PortManager,
)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        try:
            # line_buffering：重定向到文件/管道时 print 不再整块缓冲，
            # 外层任务捕获的日志才能实时可见（2026-08-31 E2E 实测整段静默）
            _stream.reconfigure(encoding='utf-8', errors='replace',
                                line_buffering=True)
        except Exception:  # noqa: BLE001 - 极端环境下保底不阻断启动
            pass

SPLASH_HTML = BOOT_DIR / 'splash.html'
SPLASH_PORT_RANGE = (5850, 5869)
COMFY_PORT = int(os.environ.get('OMNISPACE_COMFYUI_PORT', '8189'))
AUTO_REDIRECT_S = 5.0


def _eula_file() -> Path:
    return BOOT_DIR.parent / 'data' / '.eula_accepted'
# 后端就绪等待上限：实测冷启动 25s（空闲）~96s（高负载），240s 留足余量；
# 期间每 15s 向启动页推送进度（UAT P0「零进度反馈」的直接对策）
BACKEND_READY_TIMEOUT_S = 240.0
BACKEND_PROGRESS_EVERY_S = 15.0


# ─────────────────────────── 状态容器 ───────────────────────────

@dataclass
class Phase:
    key: str
    label: str
    state: str = 'pending'      # pending | running | done | error | skipped
    detail: str = ''
    t0: float = 0.0
    t1: float = 0.0


class BootState:
    """线程安全的全局状态快照（Splash /api/status 的唯一数据源）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.boot_t0 = time.time()
        self.phases: dict[str, Phase] = {
            p.key: p for p in [
                Phase('splash', '启动页'),
                Phase('env', '环境自检'),
                Phase('backend', '后端服务'),
                Phase('warmup', '模型预热'),
                Phase('ready', '就绪'),
            ]
        }
        self.current = 'splash'
        self.logs: deque[dict] = deque(maxlen=300)
        self._log_seq = 0
        self.backend = {
            'url': '', 'port': 0, 'attached': False,
            'version': '', 'uptime_s': 0.0, 'healthy': False,
        }
        self.vllm: dict = {}
        self.comfy = {'reachable': False, 'warming': False, 'message': ''}
        self.hardware: dict = {}
        self.restart_count = 0
        self.redirect_ready = False
        self.redirect_at = 0.0
        # 首启向导（P7）：EULA 同意标记 + 激活门禁状态（poller 从后端刷新）
        self.eula = _eula_file().exists()
        self.license: dict = {'required': False, 'activated': True,
                              'fingerprints': [], 'reason': ''}

    def log(self, line: str, level: str = 'info') -> None:
        if not line.strip():
            return
        with self._lock:
            self._log_seq += 1
            self.logs.append({
                'seq': self._log_seq, 't': datetime.now().strftime('%H:%M:%S'),
                'line': line[:400], 'level': level,
            })
        try:
            print(f'  {line}')
        except Exception:  # noqa: BLE001 - stdout 断裂（孤儿管道）不得打断状态机
            pass
        _boot_log_write(f'[{level}] {line}')

    def set_phase(self, key: str, state: str, detail: str = '') -> None:
        with self._lock:
            ph = self.phases[key]
            if state == 'running':
                ph.t0 = time.time()
            if state in ('done', 'error', 'skipped'):
                ph.t1 = time.time()
            ph.state = state
            ph.detail = detail
            if state == 'running':
                self.current = key

    def mark_redirect_ready(self) -> None:
        with self._lock:
            self.redirect_ready = True
            self.redirect_at = time.time()

    def snapshot(self, logs_after: int = 0) -> dict:
        with self._lock:
            now = time.time()
            phases = []
            for p in self.phases.values():
                dur = (p.t1 or now) - p.t0 if p.t0 else 0.0
                phases.append({
                    'key': p.key, 'label': p.label, 'state': p.state,
                    'detail': p.detail, 'duration_s': round(dur, 1),
                })
            logs = [entry for entry in self.logs if entry['seq'] > logs_after]
            return {
                'phase': self.current,
                'phases': phases,
                'logs': logs,
                'log_seq': self._log_seq,
                'backend': dict(self.backend),
                'vllm': dict(self.vllm),
                'comfy': dict(self.comfy),
                'hardware': dict(self.hardware),
                'restart_count': self.restart_count,
                'eula': self.eula,
                'license': dict(self.license),
                'redirect': {
                    'ready': self.redirect_ready,
                    'auto_in': max(0.0, AUTO_REDIRECT_S - (now - self.redirect_at))
                    if self.redirect_ready else 0.0,
                },
                'elapsed_s': round(now - self.boot_t0, 1),
            }


# ─────────────────────────── ComfyUI 预热 ───────────────────────────

class ComfyManager:
    """ComfyUI 可选预热：boot 拥有进程生命周期，后端经 8189 自动复用外部实例。

    spawn 参数与 src/services/inference/h3_engine.py 对齐（含 ffmpeg
    PATH 前置——宿主 Electron 阉割版 ffmpeg 探测能过、执行报错，UAT M-03）。
    """

    def __init__(self, state: BootState) -> None:
        self.state = state
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def probe(self) -> bool:
        try:
            req = urllib.request.Request(
                f'http://127.0.0.1:{COMFY_PORT}/system_stats')
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def start_prewarm(self) -> str:
        with self._lock:
            if self.probe():
                self.state.comfy['reachable'] = True
                return f'ComfyUI 已在运行（端口 {COMFY_PORT}）'
            if self._proc and self._proc.poll() is None:
                return 'ComfyUI 预热已在进行中'
            comfy_dir = PROJECT_ROOT / 'tools' / 'ComfyUI_windows_portable'
            embed = comfy_dir / 'python_embeded'
            # 品牌化优先（OmniSpace-Engine.exe，2026-09-02），缺失回退原版
            py = embed / 'OmniSpace-Engine.exe'
            if not py.is_file():
                py = embed / 'python.exe'
            main = comfy_dir / 'ComfyUI' / 'main.py'
            if not (py.is_file() and main.is_file()):
                return '未找到 ComfyUI 便携版（tools/ComfyUI_windows_portable）'
            env = dict(os.environ)
            ff_bin = PROJECT_ROOT / 'runtime' / 'ffmpeg' / 'bin'
            if ff_bin.is_dir():
                env['PATH'] = str(ff_bin) + os.pathsep + env.get('PATH', '')
            log_fp = open(  # noqa: SIM115 - 进程生命周期内常驻
                PROJECT_ROOT / 'logs' / 'comfyui_boot.log', 'ab')
            self._proc = subprocess.Popen(
                [str(py), '-s', 'ComfyUI/main.py', '--windows-standalone-build',
                 '--listen', '127.0.0.1', '--port', str(COMFY_PORT)],
                cwd=str(comfy_dir), stdout=log_fp, stderr=subprocess.STDOUT,
                env=env,
                # CREATE_NO_WINDOW：控制台子系统子进程（python/品牌 Engine）
                # 从无窗父进程拉起时系统会新开控制台黑窗，必须显式压掉
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW)
            self.state.comfy['warming'] = True
            self.state.log(f'ComfyUI 预热启动 (PID {self._proc.pid}，冷启动约 25s)')
            threading.Thread(target=self._wait_ready, daemon=True,
                             name='comfy-prewarm').start()
            return 'ComfyUI 预热已启动（冷启动约 25s）'

    def _wait_ready(self) -> None:
        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(2)
            if self._proc and self._proc.poll() is not None:
                self.state.comfy['warming'] = False
                self.state.log('ComfyUI 预热进程异常退出（详见 logs/comfyui_boot.log）',
                               'error')
                return
            if self.probe():
                self.state.comfy['reachable'] = True
                self.state.comfy['warming'] = False
                self.state.log(f'ComfyUI 就绪 (:{COMFY_PORT}，后端将自动复用)')
                return
        self.state.comfy['warming'] = False

    def shutdown(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=8)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            self._proc = None
        # 兜底：外部手动实例/极端场景残留（对齐 launcher.Launcher 同名逻辑）
        try:
            for conn in psutil.net_connections(kind='inet'):
                if conn.laddr.port == COMFY_PORT and conn.status == 'LISTEN':
                    proc = psutil.Process(conn.pid)
                    cmdline = ' '.join(proc.cmdline()).lower()
                    if 'comfyui/main.py' in cmdline:
                        proc.kill()
                        self.state.log(f'已清理 ComfyUI 残留进程 (PID {proc.pid})')
                    break
        except Exception:
            pass


# ─────────────────────────── Splash HTTP 服务 ───────────────────────────

class _SplashServer(ThreadingHTTPServer):
    """禁用 SO_REUSEADDR：Windows 上该选项允许两个进程双绑同一端口，
    后启动的 boot 会与在跑实例互相劫持启动页请求（2026-08-31 E2E 实测，
    第二实例启动页完全不可达）；禁用后冲突即 OSError → 顺延 5851+。"""

    allow_reuse_address = False
    daemon_threads = True


def make_splash_handler(boot: BootOrchestrator) -> type[BaseHTTPRequestHandler]:
    class SplashHandler(BaseHTTPRequestHandler):
        server_version = 'OmniSpaceBoot/1.0'

        def _send(self, code: int, body: bytes,
                  ctype: str = 'application/json; charset=utf-8') -> None:
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: dict, code: int = 200) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False).encode())

        def do_GET(self) -> None:  # noqa: N802 - http.server 约定
            if self.path in ('/', '/index.html'):
                try:
                    html = _apply_splash_skin(SPLASH_HTML.read_bytes(),
                                              _read_ui_theme())
                    self._send(200, html, 'text/html; charset=utf-8')
                except OSError:
                    self._send(500, b'splash.html missing', 'text/plain')
            elif self.path.startswith('/api/status'):
                qs = self.path.split('logs_after=', 1)
                after = int(qs[1].split('&', 1)[0]) if len(qs) > 1 else 0
                payload = boot.state.snapshot(logs_after=after)
                # 跨版本接管闸门配套（09-02）：启动页亮明自己身份，供后来者
                # 判断「同一副本身份」再决定是否复用
                payload['build_id'] = boot._own_build_id()
                self._json(payload)
            else:
                self._send(404, b'not found', 'text/plain')

        def do_POST(self) -> None:  # noqa: N802 - http.server 约定
            try:
                length = int(self.headers.get('Content-Length') or 0)
                if length:
                    self.rfile.read(length)
            except Exception:
                pass
            if self.path == '/api/open':
                url = boot.state.backend.get('url')
                if url:
                    _open_ui(url, boot.opts)  # type: ignore[arg-type]
                    self._json({'ok': True, 'url': url})
                else:
                    self._json({'ok': False, 'message': '后端尚未就绪'}, 409)
            elif self.path == '/api/comfy':
                msg = boot.comfy.start_prewarm()
                self._json({'ok': True, 'message': msg})
            elif self.path == '/api/eula':
                # 首启向导（P7）：同意用户协议——落标记文件，下次启动不再弹。
                # 激活提交不经此处：启动页 JS 直接调后端 /api/v1/license/activate
                # （同机回环，见 backend CORS 白名单 127.0.0.1:58xx）
                try:
                    _eula_file().parent.mkdir(parents=True, exist_ok=True)
                    _eula_file().write_text(
                        time.strftime('%Y-%m-%d %H:%M:%S'), encoding='utf-8')
                    boot.state.eula = True
                    boot.state.log('用户已同意最终用户协议')
                    boot.maybe_redirect_ready()
                    self._json({'ok': True})
                except OSError as exc:
                    self._json({'ok': False, 'message': f'写入失败: {exc}'}, 500)
            elif self.path == '/api/quit':
                self._json({'ok': True})
                boot.request_shutdown('启动页请求退出')
            else:
                self._send(404, b'not found', 'text/plain')

        def log_message(self, *args) -> None:  # 静默访问日志
            pass

    return SplashHandler


# ─────────────────────────── 编排器 ───────────────────────────

class BootBackend(BackendProcess):
    """BackendProcess 的 Boot 变体：stdout/stderr 逐行回流启动页日志。"""

    def __init__(self, config: LauncherConfig, state: BootState) -> None:
        super().__init__(config)
        self.state = state
        self.on_output = self._on_output

    def _on_output(self, line: str) -> None:
        level = 'error' if 'ERROR' in line.upper()[:60] else (
            'warn' if any(k in line.upper()[:60]
                          for k in ('WARN', 'DEGRADED')) else 'info')
        self.state.log(line, level)


@dataclass
class BootOptions:
    port: int = 5800
    no_browser: bool = False
    comfy: bool = False
    warmup: bool = True
    # 强制系统浏览器（开发调试）：覆盖设置页 launch_mode 的桌面壳选择
    force_browser: bool = False
    # 通道 A（拖入）：把 models 文件夹拖到启动 exe/快捷方式上，Windows
    # 以该路径为命令行参数拉起 exe，桩（omnispace_exe.c）原样透传到这
    dropped: list = field(default_factory=list)


def _read_launch_mode() -> str:
    """读设置页的界面打开方式（shell/browser，2026-09-08 壳接线）。

    直接只读 SQLite（boot 阶段后端可能未起，不走 HTTP）；读取失败/
    无记录按默认 shell。key 与 api/system.py._SETTINGS_KEY 对齐。
    """
    try:
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'data', 'omnispace.db')
        if not os.path.isfile(db_path):
            return 'shell'
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=2)
        try:
            row = conn.execute(
                "SELECT value FROM system_settings WHERE key='system.settings'"
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return 'shell'
        mode = (json.loads(row[0]) or {}).get('launch_mode', 'shell')
        return 'browser' if mode == 'browser' else 'shell'
    except Exception:  # noqa: BLE001 - 读取失败按默认壳
        return 'shell'


# 治愈系主题联动（2026-09-11，docs/治愈系主题方案-2026-09-11.md §1.4）：
# 前端 useAppStore.setTheme 会把 theme 回写 system.settings（白名单六值）；
# 启动页按该值注入 data-skin/data-skinmode 换肤。主题值 → (皮肤, 亮暗)。
# 缺省/无效/读取失败 → 不注入 = 经典 HUD 青（旧库/旧版本零观感变化）。
_THEME_SKINS = {
    'sakura': ('sakura', None),
    'light': ('sakura', 'light'),
    'tech': ('tech', None),
    'tech-light': ('tech', 'light'),
    'dali': ('dali', None),
    'dali-light': ('dali', 'light'),
}


def _read_ui_theme() -> str:
    """读设置里的界面主题（六值白名单，2026-09-11 启动页联动）。

    与 _read_launch_mode 同款只读 SQLite 模式（boot 阶段后端可能未起）；
    读取失败/无记录/脏值返回 'sakura' 之外的哨兵空串由调用方判定——
    这里直接返回白名单内的值或 ''（'' = 不注入，保持经典 HUD）。
    """
    try:
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'data', 'omnispace.db')
        if not os.path.isfile(db_path):
            return ''
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=2)
        try:
            row = conn.execute(
                "SELECT value FROM system_settings WHERE key='system.settings'"
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return ''
        theme = (json.loads(row[0]) or {}).get('theme', '')
        return theme if theme in _THEME_SKINS else ''
    except Exception:  # noqa: BLE001 - 读取失败按经典 HUD
        return ''


def _apply_splash_skin(html: bytes, theme: str) -> bytes:
    """按主题值给启动页 HTML 注入 data-skin/data-skinmode 属性。

    只替换首个 <html lang="zh-CN"> 开标签；找不到锚点原样返回
    （未来 splash.html 改版时无害降级为主题缺省观感）。
    """
    skin = _THEME_SKINS.get(theme)
    if not skin:
        return html
    skin_name, skinmode = skin
    attr = f'data-skin="{skin_name}"'
    if skinmode:
        attr += f' data-skinmode="{skinmode}"'
    return html.replace(b'<html lang="zh-CN">',
                        f'<html lang="zh-CN" {attr}>'.encode(), 1)


def _has_integrated_gpu() -> bool:
    """是否存在核显（钉卡前提，2026-09-08 UI 降载方案①）。

    双卡机（核显+独显）才把 WebView2 钉到核显省独显；单卡机（无
    核显，如 Intel F 系列/独立显卡台式老平台）不动图形偏好——把
    WebView2 钉到不存在的卡会导致回退行为不可控。

    判定口径（2026-09-08 实测校准）：显示类注册表里存在「非独显、
    非虚拟驱动」的适配器即视为核显席位——含核显驱动未装全时显示
    为 "Microsoft Basic Display Adapter" 的情况（本机实况：核显以
    Basic 驱动在跑合成，正则匹配 Intel 型号会漏判）。虚拟驱动
    （远程 Idd 等）与 Microsoft 基本驱动本身不构成席位，但 Basic
    适配器 + 独显并存 = 双卡拓扑成立。
    """
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r'SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-'
            r'bfc1-08002be10318}')
        for i in range(16):
            try:
                sub = winreg.OpenKey(key, f'{i:04d}')
            except OSError:
                break
            try:
                desc = str(winreg.QueryValueEx(sub, 'DriverDesc')[0] or '')
                name = desc.lower()
                # 虚拟显示驱动不算（向日葵/Parsec/ToDesk 等 Idd）
                if 'idd' in name or 'virtual' in name or 'parsec' in name:
                    continue
                # 独显不算
                if 'nvidia' in name or 'amd' in name or 'radeon' in name:
                    continue
                # 剩余候选：Intel 系（xe/uhd/iris/arc）、或核显驱动
                # 未装时的 Microsoft Basic（双卡拓扑下的核显席位）
                if 'microsoft basic display adapter' in name \
                        or 'intel' in name or 'iris' in name \
                        or 'uhd' in name or 'arc' in name \
                        or name.strip().endswith('xe'):
                    return True
            except OSError:
                pass
        return False
    except Exception:  # noqa: BLE001 - 检测失败按无核显（不动偏好）
        return False


def _reg_has_value(key, name: str) -> bool:
    import winreg
    try:
        winreg.QueryValueEx(key, name)
        return True
    except OSError:
        return False


def _ensure_webview_gpu_preference() -> None:
    """WebView2 图形偏好自适应钉卡（2026-09-08 UI 降载方案①）。

    双卡机把 msedgewebview2.exe 钉到「省电」（核显）——壳的渲染
    永不抢占独显；幂等（已写且值正确跳过）；无核显机器零动作。
    失败静默（不影响启动链，Windows 自行调度兜底）。
    """
    if not _has_integrated_gpu():
        return
    try:
        import winreg
        path = r'Software\Microsoft\DirectX\UserGpuPreferences'
        want = 'msedgewebview2.exe=GpuPreference=1;'
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                             winreg.KEY_READ)
        existing = ''
        try:
            existing = winreg.QueryValueEx(key, 'msedgewebview2.exe')[0]
        except OSError:
            pass
        finally:
            winreg.CloseKey(key)
        if existing == 'GpuPreference=1;':
            return
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                             winreg.KEY_SET_VALUE)
        try:
            winreg.SetValueEx(key, 'msedgewebview2.exe', 0,
                              winreg.REG_SZ, 'GpuPreference=1;')
        finally:
            winreg.CloseKey(key)
        print(f'[壳] 双卡机检测到核显：WebView2 已钉省电核显（{want}）')
    except Exception as exc:  # noqa: BLE001 - 注册表失败静默
        print(f'[壳] 图形偏好设置跳过：{exc}')


def _open_ui(url: str, opts: BootOptions) -> None:
    """统一界面入口（2026-09-08 壳接线）：按设置拉桌面壳或开浏览器。

    壳失败（依赖缺失等）自动回退系统浏览器——绝不让用户黑屏。
    """
    _ensure_webview_gpu_preference()
    if opts.force_browser or _read_launch_mode() == 'browser':
        webbrowser.open(url)
        return
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # B1（2026-09-13 拍板项 0=A）：主链升级 3.12——py312 为主、py310
        # 并存保留为回退锚点与打包基线。OmniSpace-Shell 品牌化 exe 尚未
        # 复制到 py312（brand_exe 重打待收尾批），暂以 pythonw 兜底。
        shell_exe = os.path.join(root, 'runtime', 'py312', 'OmniSpace-Shell.exe')
        pythonw = os.path.join(root, 'runtime', 'py312', 'pythonw.exe')
        runner = shell_exe if os.path.isfile(shell_exe) else pythonw
        cmd = [runner, os.path.join(root, 'launcher', 'shell.py'),
               '--url', url]
        subprocess.Popen(cmd, cwd=root,
                         creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0)
                         | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
        print(f'[壳] 已拉起桌面窗口装载 {url}')
    except Exception as exc:  # noqa: BLE001 - 壳失败回退浏览器
        print(f'[壳] 拉起失败回退浏览器：{exc}')
        webbrowser.open(url)


class BootOrchestrator:
    def __init__(self, opts: BootOptions) -> None:
        self.opts = opts
        self.config = LauncherConfig(backend_port=opts.port)
        self.state = BootState()
        self.backend = BootBackend(self.config, self.state)
        self.backend.on_status_change = self._on_backend_status
        self.env_checker = EnvironmentChecker(self.config)
        self.port_manager = PortManager(self.config)
        self.comfy = ComfyManager(self.state)
        self._stop = threading.Event()
        self._owns_backend = False
        # 单实例守卫命中标记：让位退出（exit 0），区别于启动失败（exit 1，
        # bat 入口会据 errorlevel 提示「启动异常退出」）
        self._deferred_to_existing = False
        self._splash: ThreadingHTTPServer | None = None
        self._splash_port = 0
        self._monitor: threading.Thread | None = None

    # ── 对外动作 ──

    def request_shutdown(self, reason: str) -> None:
        self.state.log(f'正在退出：{reason}')
        self._stop.set()

    # ── 阶段①：Splash ──

    def _existing_splash(self) -> int | None:
        """单实例守卫（2026-08-31 实测缺陷修复）：桌面快捷方式每次双击
        都会驻留一个 boot 守护进程（用户连点 4 次 = 4 个 pythonw 常驻）。
        启动前先探测区间内已在服务的 OmniSpaceBoot 启动页，命中则复用。
        先经 psutil 筛 LISTEN 再做 HTTP 校验（过滤驱动环境下裸探测会各烧 3s）；
        HTTP 仅指向字面量回环主机（http.client，不跟随重定向）。
        """
        import http.client
        lo, hi = SPLASH_PORT_RANGE
        try:
            listening = {
                c.laddr.port for c in psutil.net_connections(kind='inet')
                if c.status == 'LISTEN' and c.laddr and lo <= c.laddr.port <= hi
            }
        except Exception:  # noqa: BLE001 - 探测失败不阻断启动
            return None
        for port in sorted(listening):
            conn: http.client.HTTPConnection | None = None
            try:
                conn = http.client.HTTPConnection('127.0.0.1', port, timeout=1.5)
                conn.request('GET', '/api/status')
                resp = conn.getresponse()
                if resp.headers.get('Server', '').startswith('OmniSpaceBoot'):
                    # 跨版本闸门（09-02）：只复用同一副本身份的启动页——
                    # build_id 严格相等才算自己人（开发版=空串、发行包=清单
                    # 构建号）；对不上或旧版无此字段一律不复用，否则点发行包
                    # exe 会直接打开开发环境的启动页（替身陷阱第三层）
                    try:
                        info = json.loads(resp.read().decode('utf-8'))
                    except Exception:
                        info = {}
                    if info.get('build_id') != self._own_build_id():
                        continue
                    return port
            except Exception:
                continue
            finally:
                if conn is not None:
                    conn.close()
        return None

    def _start_splash(self) -> bool:
        existing = self._existing_splash()
        # OMNISPACE_ALLOW_MULTI=1：双实例联调（异目录可移植性验收）时
        # 不让位给既有启动页，与后端互斥体的同名豁免语义一致
        if existing is not None and os.environ.get('OMNISPACE_ALLOW_MULTI') != '1':
            url = f'http://127.0.0.1:{existing}'
            print(f'检测到已在运行的启动页 {url}，复用并退出本实例（单实例守卫）')
            self._deferred_to_existing = True
            if not self.opts.no_browser:
                # 就绪后来客直开主界面，不路过启动页（2026-09-14 实测定性）：
                # 后端已就绪时 HUD 的 redirect.auto_in=0，页面一加载就
                # location.replace 跳主界面，撞 WebView2 早期导航竞态——
                # 壳窗永久白屏（shell.log 23:25 现场 + 直装/跳转 2×2 对照
                # 矩阵实锤）；启动页是启动期临时 HUD，只有真的还在启动中
                # 才值得带给用户看
                backend_port = self._probe_existing()
                if backend_port is not None:
                    url = f'http://{self.config.backend_host}:{backend_port}'
            _open_ui(url, self.opts)
            return False
        self.state.set_phase('splash', 'running')
        handler = make_splash_handler(self)
        for port in range(SPLASH_PORT_RANGE[0], SPLASH_PORT_RANGE[1] + 1):
            try:
                self._splash = _SplashServer(('127.0.0.1', port), handler)
                self._splash_port = port
                break
            except OSError:
                continue
        if self._splash is None:
            self.state.set_phase('splash', 'error', '端口区间全部被占用')
            return False
        threading.Thread(target=self._splash.serve_forever, daemon=True,
                         name='splash-http').start()
        url = f'http://127.0.0.1:{self._splash_port}'
        self.state.set_phase('splash', 'done', url)
        self.state.log(f'启动页就绪 {url}')
        if not self.opts.no_browser:
            _open_ui(url, self.opts)
        return True

    # ── 阶段②：环境自检 ──

    # ── 拖入识别（通道 A：models 文件夹拖到 exe/快捷方式上）──

    def _import_dropped_models(self) -> None:
        """拖入接线（2026-09-02 体验流定稿第一步）。

        exe 桩透传 argv（launcher/omnispace_exe.c）；拖到图标上的外部
        models 根 → 以其为源挂接（同卷直接生效，跨卷提示移盘）；未拖入
        但安装目录 models/ 已有内容（通道 B 收件箱）→ 同样接一遍给启动页
        反馈；两者皆空 → 一句指引。失败绝不阻断启动：ComfyUI 每次冷启前
        comfy_proc._mount_models_before_spawn 还会兜底重挂。
        """
        dropped_root = None
        for arg in self.opts.dropped or []:
            p = Path(arg)
            if (p / 'models').is_dir():
                p = p / 'models'   # 拖的是包含 models 的外层文件夹
            if p.is_dir():
                dropped_root = p
                break
        try:
            import importlib.util
            root = BOOT_DIR.parent
            link_dir = next(
                (d for d in (root / 'scripts' / 'comfy_link',
                             root / 'modelxiazai')
                 if (d / 'comfy_model_map.json').is_file()), None)
            if link_dir is None or not (link_dir / 'comfy_mount.py').is_file():
                if dropped_root is not None:
                    self.state.log('拖入已收到，但包内缺挂接器'
                                   '（scripts/comfy_link）——模型已就位，'
                                   '引擎接线待补', 'warn')
                return
            spec = importlib.util.spec_from_file_location(
                '_omnispace_comfy_mount_boot', link_dir / 'comfy_mount.py')
            mod = importlib.util.module_from_spec(spec)
            # 先注册再 exec（dataclass 字符串注解按模块名查 sys.modules，
            # 未注册会在导入期崩，被 except 吞成接线异常）
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)

            if dropped_root is not None:
                ok, why = mod.validate_models_root(dropped_root)
                if not ok:
                    self.state.log(f'拖入的文件夹不像大模型包（{why}），已忽略'
                                   '——请拖入整个 models 文件夹', 'warn')
                    return
                models_root = dropped_root
                self.state.log(f'检测到拖入的模型包：{dropped_root}'
                               f'（{why}），开始接线…')
            elif (root / 'models').is_dir() and any(
                    (root / 'models').iterdir()):
                models_root = root / 'models'   # 通道 B：安装目录收件箱
            else:
                self.state.log('未检测到大模型：把 models 文件夹拖到启动图标上'
                               '（或放入安装目录 models\\），绘画/视频功能即可解锁')
                return
            report = mod.ensure_mounted(
                models_root=models_root,
                comfy_models=root / 'tools' / 'ComfyUI_windows_portable'
                / 'ComfyUI' / 'models')
            if not report.total:
                self.state.log('模型挂接：包内无映射表，跳过（对话/漫剧关键帧'
                               '不依赖挂接；绘画引擎依赖）', 'warn')
                return
            self.state.log(f'模型接线完成：{report.summary_line()}')
            for line in report.details[:8]:
                self.state.log(f'接线明细：{line}',
                               'warn' if ('冲突' in line or '失败' in line)
                               else 'info')
            yaml_path = (root / 'data' / 'comfyui'
                         / 'extra_model_paths.yaml')
            marker = root / 'data' / 'models_external.json'
            if report.cross_volume:
                # 2b 跨盘降级：yaml 通道（绘画引擎）+ 外部登记标记（后端启动
                # 时按 models_manifest 幂等回填 file_path——boot 阶段激活门禁
                # 拦着 /api/v1，登记只能放后端启动期）
                fr = mod.cross_volume_fallback(
                    models_root=models_root,
                    comfy_models=root / 'tools' / 'ComfyUI_windows_portable'
                    / 'ComfyUI' / 'models',
                    yaml_path=yaml_path)
                try:
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text(json.dumps(
                        {'root': str(models_root)}, ensure_ascii=False),
                        encoding='utf-8')
                except OSError:
                    pass
                self.state.log(f'模型在别的磁盘，已启用跨盘降级：'
                               f'{fr.summary_line()}（对话/漫剧走外部路径，'
                               '绘画引擎走跨盘通道）')
                for rel in fr.unresolvable[:5]:
                    self.state.log(f'跨盘用不了的件（移到同盘即好）：{rel}',
                                   'warn')
                self.state.log('最佳体验：把 models 文件夹移动到软件所在磁盘'
                               '（同盘移动瞬间完成）后重启，自动回到满血模式',
                               'warn')
            else:
                if marker.is_file():
                    try:
                        marker.unlink()
                        self.state.log('检测到模型已与软件同盘，已撤销跨盘登记')
                    except OSError:
                        pass
                if report.missing_src:
                    self.state.log(f'还有 {report.missing_src} 件模型未放入'
                                   '（补齐后下次启动引擎自动接上）')
        except Exception as e:  # noqa: BLE001 - 接线失败不阻断启动
            self.state.log(f'模型接线异常（不阻断启动，引擎冷启时重试）：{e}',
                           'warn')

    def _run_env_checks(self) -> bool:
        self.state.set_phase('env', 'running')
        ok_all, results = self.env_checker.check_all()
        labels = {'os': '操作系统', 'python': 'Python', 'disk_space': '磁盘空间',
                  'cuda': 'CUDA', 'dependencies': '依赖',
                  'hypervisor': '虚拟化层', 'torch_contract': 'torch 契约'}
        for name, r in results.items():
            mark = '✓' if r['passed'] else '✗'
            level = 'info' if r['passed'] else (
                'warn' if name in ('cuda', 'path_ascii', 'torch_contract')
                else 'error')
            self.state.log(f'[{labels.get(name, name)}] {mark} {r["message"]}', level)
        # CUDA 不可用 / 路径非 ASCII 仅警告（诚实降级，后端自行处理）；
        # path_ascii：vLLM(tvm-ffi) 窄字符加载器不支持非 ASCII 路径，
        # 对话引擎会挂但其余功能可用，黄灯提示移目录，不阻断
        blocking = {k: v for k, v in results.items()
                    if not v['passed'] and k not in ('cuda', 'path_ascii')}
        if blocking:
            self.state.set_phase('env', 'error', '关键检查未通过')
            return False
        self.state.set_phase('env', 'done',
                             'CUDA 降级运行' if not results.get('cuda', {}).get('passed')
                             else '全部通过')
        return True

    # ── 阶段③：后端（幂等接管 / spawn）──

    def _probe_existing(self) -> int | None:
        # 先经 psutil 筛出区间内真实 LISTEN 的端口再做健康检查：
        # 本机存在过滤驱动（代理类软件）时，向未监听回环端口的 SYN
        # 会被静默丢弃而非立即拒绝，逐端口 HTTP 探测会各烧满 3s 超时
        # （2026-08-31 实测：36 个候选端口拖慢探测近 2 分钟）
        lo, hi = self.config.port_range
        try:
            listening = {
                c.laddr.port for c in psutil.net_connections(kind='inet')
                if c.status == 'LISTEN' and c.laddr and lo <= c.laddr.port <= hi
            }
        except Exception:  # noqa: BLE001 - 降级回全区间探测
            listening = set(range(lo, hi + 1))
        for port in sorted(listening):
            if self.backend.is_healthy(port):
                return port
        return None

    # ── 跨版本接管闸门的判据三件套（2026-09-02）──

    def _remote_copy_id(self, port: int) -> str:
        """读运行中后端 /health 的 copy_id 字段（旧后端无此字段→''，
        与本副本指纹必不相等=保守拒绝接管）。"""
        try:
            with urllib.request.urlopen(
                    f'http://{self.config.backend_host}:{port}/health',
                    timeout=3) as resp:
                data = json.loads(resp.read().decode('utf-8')).get('data') or {}
            return str(data.get('copy_id') or '')
        except Exception:
            return ''

    def _remote_build_id(self, port: int) -> str:
        """读运行中后端 /health 的 build 字段（读不到按 dev 处理，宁可拒接）"""
        try:
            with urllib.request.urlopen(
                    f'http://{self.config.backend_host}:{port}/health',
                    timeout=3) as resp:
                data = json.loads(resp.read().decode('utf-8')).get('data') or {}
            return str(data.get('build') or '')
        except Exception:
            return ''

    def _own_build_id(self) -> str:
        """本副本身份：包根 dist_manifest.json 的 build_id；开发环境无此文件→''"""
        try:
            return str(json.loads(
                (BOOT_DIR.parent / 'dist_manifest.json').read_text('utf-8')
            ).get('build_id', ''))
        except Exception:
            return ''

    def _copy_fingerprint(self) -> str:
        """副本路径指纹（批2-4 2026-09-18）：包根绝对路径 md5 前 8 位。

        审计 1-1：开发副本 build_id 恒空串=所有开发目录互认"自己人"，
        后启动者复用他人 splash 并接管他人后端（看到对方数据）。指纹经
        环境变量 OMNISPACE_COPY_ID 注入自拉的后端，/health 回显，接管
        闸据此拒绝跨副本收编。"""
        import hashlib
        return hashlib.md5(
            str(BOOT_DIR.parent).encode('utf-8')).hexdigest()[:8]

    @staticmethod
    def _edition(build_id: str) -> str:
        # 空（开发环境）或形如 2.3.1+dev → dev；带 +g<hash>+日期 → release
        if not build_id or build_id.endswith('+dev'):
            return 'dev'
        return 'release'

    def _start_backend(self) -> bool:
        self.state.set_phase('backend', 'running')
        # OMNISPACE_ALLOW_MULTI=1：双实例联调时不接管他人后端，强制自己拉起
        existing = (None if os.environ.get('OMNISPACE_ALLOW_MULTI') == '1'
                    else self._probe_existing())
        if existing is not None:
            # 跨版本接管闸门（2026-09-02）：开发版与发行包不得互相收编——
            # 否则开发机上点发行包 exe 会静默接管开发版后端，看到的全是
            # 开发数据（「验收跑了替身」事故再现）。判据：/health 的 build
            # 字段 vs 包根 dist_manifest.json 的 build_id（开发环境无清单=dev）
            remote_edition = self._edition(self._remote_build_id(existing))
            if remote_edition != self._edition(self._own_build_id()):
                theirs = '开发版' if remote_edition == 'dev' else '发行包'
                mine = '发行包' if theirs == '开发版' else '开发版'
                msg = (f'本机正在运行{theirs}实例（:{existing}）。'
                       f'{mine}不接管{theirs}后端——请先在{theirs}的启动页完全退出，'
                       '再启动本程序。')
                self.state.set_phase('backend', 'error', msg)
                self.state.log(msg, 'error')
                return False
            # 批2-4 同版异副本闸：开发版比对 /health 回显的 copy_id 指纹；
            # 发行包比对 build 全串（防旧版包静默接管新版后端）。对不上
            # 一律拒绝——宁可让用户先退出旧实例，不看错数据
            remote_bid = self._remote_build_id(existing)
            if self._edition(self._own_build_id()) == 'dev':
                remote_copy = self._remote_copy_id(existing)
                if remote_copy != self._copy_fingerprint():
                    msg = (f'本机 :{existing} 是另一副本的开发实例'
                           '（copy_id 不匹配）。不跨副本接管——请先在该'
                           '副本完全退出；双实例联调请设'
                           ' OMNISPACE_ALLOW_MULTI=1。')
                    self.state.set_phase('backend', 'error', msg)
                    self.state.log(msg, 'error')
                    return False
            elif remote_bid != self._own_build_id():
                msg = (f'本机 :{existing} 是不同构建的发行实例'
                       f'（{remote_bid or "?"}）。不跨版本接管——请先'
                       '在该实例完全退出再启动本程序。')
                self.state.set_phase('backend', 'error', msg)
                self.state.log(msg, 'error')
                return False
            url = f'http://{self.config.backend_host}:{existing}'
            self.state.backend.update({
                'url': url, 'port': existing, 'attached': True, 'healthy': True})
            self.state.set_phase('backend', 'done', f'接管运行中实例 :{existing}')
            self.state.log(f'检测到健康后端实例，直接接管 {url}（不重复拉起）')
            self.state.log(
                '接管模式提示：关闭本窗口不会停止被接管的后端（属本副本'
                '存量实例）；如需完全停止请运行 停止OmniSpace.bat')
            return True

        try:
            port, msg = self.port_manager.resolve_port_conflict(
                self.config.backend_port,
                allow_kill=os.environ.get('OMNISPACE_ALLOW_MULTI') != '1')
        except RuntimeError as e:
            self.state.set_phase('backend', 'error', str(e))
            return False
        self.state.log(f'端口决策：{msg}')
        self._owns_backend = True
        # 页面守卫（2026-09-03 方案A）：把自己的启动页端口传给后端——
        # 全部页面关闭且无任务时，后端经 /api/quit 正规退出（等价
        # stop.py，看门狗随启动页收尾放行）。仅自有的后端才传；接管
        # 模式不传（守卫不激活，不代他人链做退出决策）
        os.environ['OMNISPACE_SPLASH_PORT'] = str(self._splash_port)
        # 批2-4：副本指纹随链注入（/health 回显供接管闸比对）
        os.environ['OMNISPACE_COPY_ID'] = self._copy_fingerprint()
        # 端口先行记录：失败路径下 poller 也能探测真实端口（而非 :0）
        self.state.backend['port'] = port
        if not self.backend.start(port):
            self.state.set_phase('backend', 'error', '进程启动失败')
            return False
        self.state.log(f'后端进程已拉起 (uvicorn :{port})，等待就绪…')
        if not self._wait_backend_ready(port, timeout=BACKEND_READY_TIMEOUT_S):
            alive = self.backend.process and self.backend.process.poll() is None
            self.state.set_phase(
                'backend', 'error',
                '进程仍在运行' if alive else '进程已退出（详见 logs/backend_stderr.log）')
            return False
        url = f'http://{self.config.backend_host}:{port}'
        self.state.backend.update({
            'url': url, 'port': port, 'attached': False, 'healthy': True})
        self.state.set_phase('backend', 'done', url)
        self.state.log(f'后端服务就绪 {url}')
        return True

    def _wait_backend_ready(self, port: int, timeout: float) -> bool:
        """等待后端就绪：自带进度流（区别于 BackendProcess.wait_until_ready 的静默等待，
        每 15s 向启动页报一次已耗时——冷启动 25s~2min 期间页面不再死寂）。"""
        start = time.time()
        next_note = start + BACKEND_PROGRESS_EVERY_S
        while True:
            if self.backend.is_healthy(port):
                return True
            proc = self.backend.process
            if proc is not None and proc.poll() is not None:
                return False
            # 停止脚本（/api/quit）在冷启动等待期也要即时生效：免黑窗模式下
            # 没有 Ctrl+C/关窗通道，这里不检查会让退出等满整个冷启动周期
            if self._stop.is_set():
                self.state.log('等待就绪期间收到退出请求，中止启动')
                return False
            now = time.time()
            if now - start > timeout:
                self.state.log(f'后端 {timeout:.0f}s 内未就绪', 'error')
                return False
            if now >= next_note:
                self.state.log(f'仍在启动中… 已等待 {now - start:.0f}s'
                               f'（加载路由与模型，冷启动约 30s~2min）')
                next_note = now + BACKEND_PROGRESS_EVERY_S
            time.sleep(1.0)

    # ── 阶段④：预热 ──

    def _trigger_warmup(self) -> None:
        self.state.set_phase('warmup', 'running')
        if not self.opts.warmup:
            self.state.set_phase('warmup', 'skipped', '已按参数跳过')
            return
        base = self.state.backend['url']
        try:
            body = json.dumps({'feature': 'dialog'}).encode()
            req = urllib.request.Request(
                f'{base}/api/v1/models/warmup', data=body,
                headers={'Content-Type': 'application/json'}, method='POST')
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode())
            msg = (payload.get('meta') or {}).get('message') or ''
            started = (payload.get('data') or {}).get('started')
            if started:
                self.state.log(f'对话模型预热已点火：{msg}（vLLM 冷启动约 2~3 分钟，'
                               f'后台加载，可先进应用）')
                self.state.set_phase('warmup', 'done', '后台预热中（vLLM）')
            else:
                self.state.log(f'对话模型无需预热：{msg}')
                self.state.set_phase('warmup', 'done', msg)
        except Exception as e:  # noqa: BLE001 - 预热失败不阻断进入
            self.state.log(f'预热请求失败（不阻断启动）：{e}', 'warn')
            self.state.set_phase('warmup', 'skipped', '预热请求失败')

    # ── 服务状态轮询（守护期持续）──

    # ── 首启向导（P7）：门禁状态刷新与放行判定 ──────────────────

    def refresh_license(self) -> None:
        """从后端拉取激活门禁状态（仅本进程自产的后端：字面量回环+端口校验）。"""
        import http.client
        port = int(self.state.backend.get('port') or 0)
        if not (5800 <= port <= 5835):
            return
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
        try:
            conn.request('GET', '/api/v1/license/status')
            data = json.loads(
                conn.getresponse().read().decode('utf-8')).get('data')
        except Exception:  # noqa: BLE001 - 后端未就绪时静默跳过
            return
        finally:
            conn.close()
        if not isinstance(data, dict):
            return
        with self.state._lock:
            self.state.license = {
                'required': bool(data.get('gate_enabled')),
                'activated': bool(data.get('activated')),
                'fingerprints': data.get('fingerprints') or [],
                'reason': data.get('reason') or '',
                # 激活加固批（2026-09-19）：授权明细透传给启动页展示
                # （type/expires/permanent/days_left/generation）
                'info': data.get('license') or {},
            }
        self.maybe_redirect_ready()

    def maybe_redirect_ready(self) -> None:
        """EULA 已同意且（门禁未启用或已激活）才放行自动进入。"""
        if self.state.redirect_ready or not self.state.eula:
            return
        lic = self.state.license
        if lic.get('required') and not lic.get('activated'):
            return
        self.state.mark_redirect_ready()

    def _start_poller(self) -> None:
        def _poll() -> None:
            tick = 0
            while not self._stop.is_set():
                tick += 1
                base = self.state.backend.get('url')
                if base:
                    self.state.backend['healthy'] = self.backend.is_healthy(
                        self.state.backend['port'])
                    if self.state.backend['healthy']:
                        if tick % 3 == 1:
                            health = (self._get_json(f'{base}/health')
                                      or {}).get('data', {})
                            self.state.backend['version'] = str(
                                health.get('version', ''))
                            self.state.backend['uptime_s'] = float(
                                health.get('uptime_s', 0))
                        vllm = self._get_json(
                            f'{base}/api/v1/models/vllm/status')
                        if vllm is not None:
                            self.state.vllm = vllm.get('data') or {}
                        if tick % 3 == 1:
                            # 首启向导（P7）：门禁状态刷新（激活完成后放行进入）
                            self.refresh_license()
                        if tick % 3 == 1:
                            hw = self._get_json(f'{base}/api/v1/hardware/info')
                            if hw is not None:
                                self.state.hardware = hw.get('data') or {}
                if tick % 3 == 0:
                    self.state.comfy['reachable'] = self.comfy.probe()
                self._stop.wait(2.0)

        threading.Thread(target=_poll, daemon=True, name='boot-poller').start()

    def _get_json(self, url: str) -> dict | None:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            return None

    # ── 接管实例监控（attached 模式死亡 → 补位 spawn）──

    def _start_attached_monitor(self) -> None:
        def _watch() -> None:
            port = self.state.backend['port']
            fail = 0
            while (not self._stop.is_set() and not self._owns_backend
                   and self.state.backend.get('attached')):
                time.sleep(5)
                if self._stop.is_set() or self._owns_backend:
                    break
                if self.backend.is_healthy(port):
                    fail = 0
                    continue
                fail += 1
                if fail < 2:
                    continue
                self.state.log(f'接管的后端实例失联（:{port}），尝试补位重启…', 'warn')
                if self.port_manager.is_port_in_use(port):
                    time.sleep(8)
                    if self.backend.is_healthy(port):
                        fail = 0
                        continue
                self._owns_backend = True
                self.state.backend['attached'] = False
                self.state.restart_count += 1
                self.backend.start(port)
                if self.backend.wait_until_ready(port, timeout=60):
                    self.state.log('补位后端已就绪')
                    self.state.backend['healthy'] = True
                else:
                    self.state.log('补位后端未就绪（详见日志）', 'error')

        threading.Thread(target=_watch, daemon=True,
                         name='attached-monitor').start()

    def _on_backend_status(self, status: str, message: str) -> None:
        self.state.log(f'[后端] {message}',
                       'error' if 'permanent' in status else 'warn')
        if 'restarting' in status:
            self.state.restart_count += 1

    # ── 主流程 ──

    def _recover_interrupted_upgrade(self) -> None:
        """升级中断自愈（升级机制批3，docs/升级机制方案-2026-09-08.md §2.4）。

        读 updates/state.json：phase 处于中断态（backing_up/applying/
        verifying_start）说明上次升级被打断——从最近一次备份还原旧版
        文件（含 DB 快照），启动页日志说明后照常启动旧版。
        正常路径只读一个 json，零开销。
        """
        import importlib.util
        root = BOOT_DIR.parent
        state_file = root / 'updates' / 'state.json'
        if not state_file.is_file():
            return
        try:
            state = json.loads(state_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        phase = str(state.get('phase') or '')
        if phase not in ('backing_up', 'applying', 'verifying_start'):
            return
        backup_root = root / 'updates' / 'backup'
        stamps = sorted((d for d in backup_root.iterdir() if d.is_dir()),
                        key=lambda d: d.name, reverse=True) \
            if backup_root.is_dir() else []
        if not stamps:
            self.state.log('检测到上次升级中断，但找不到备份目录——'
                           '按原样继续启动（如异常请查 updates/logs）', 'error')
            return
        # 动态加载 updater/restore.py（stdlib-only；先例=comfy_mount 挂接器）
        restore_py = root / 'updater' / 'restore.py'
        if not restore_py.is_file():
            self.state.log('检测到上次升级中断，但 updater/restore.py 缺失，'
                           '无法自动还原——按原样继续启动', 'error')
            return
        spec = importlib.util.spec_from_file_location(
            '_omnispace_upgrade_restore', restore_py)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        restored, errs = mod.restore_backup(root, stamps[0])
        to_version = str(state.get('to_version') or '?')
        if errs:
            self.state.log(f'升级中断还原完成但有 {len(errs)} 个文件失败'
                           f'（还原 {restored} 个）——详见 updates/logs', 'error')
        else:
            self.state.log(f'检测到上次升级中断，已自动还原为 v{to_version}'
                           f'（还原 {restored} 个文件），照常启动旧版', 'warn')
        # 状态机落定：还原完成，不再是中断态
        state['phase'] = 'rolled_back'
        state['reason'] = f'boot 恢复钩子还原（{restored} 文件, 错误 {len(errs)}）'
        state['updated_at'] = time.time()
        try:
            state_file.write_text(json.dumps(state, ensure_ascii=False,
                                             indent=1), encoding='utf-8')
        except OSError:
            pass

    def run(self) -> int:
        print('=' * 60)
        print('OmniSpace AI · 启动主程序 (Boot)')
        print('=' * 60)
        _boot_log_write('========== OmniSpace Boot 会话开始 ==========')
        # ── 升级中断自愈钩子（升级机制批3，2026-09-11）──────────
        # 上次升级若被打断（backing_up/applying/verifying_start），
        # 先从 updates/backup 还原旧版再照常启动（方案 §2.4 断电自愈）。
        try:
            self._recover_interrupted_upgrade()
        except Exception as _recover_exc:
            try:
                self.state.log(f'升级恢复检查异常（忽略继续启动）: {_recover_exc}',
                               'error')
            except Exception:  # noqa: BLE001
                pass

        if not self._start_splash():
            return 0 if self._deferred_to_existing else 1
        # 体验流第一步（通道 A/B 模型接线）——先于环境自检，接线结果直接
        # 进启动页日志；让位分支（已在运行实例）不会走到这里，无重复挂接
        self._import_dropped_models()
        if not self._run_env_checks():
            self.state.log('环境自检未通过，启动中止（启动页保持打开供查看原因）',
                           'error')
            self._hold_until_quit()
            return 1
        # 环境自检后即收到退出请求（如用户秒点停止）：不再进入后端拉起，
        # 直接收尾退出——与 _hold_until_quit 同样保证 _shutdown 收尾
        if self._stop.is_set():
            self.state.log('收到退出请求，中止启动')
            self._shutdown()
            return 0
        if not self._start_backend():
            self._hold_until_quit()
            return 1
        self._trigger_warmup()
        if self.opts.comfy:
            self.comfy.start_prewarm()
        self.state.set_phase('ready', 'running')
        self.state.set_phase('ready', 'done', self.state.backend['url'])
        # 首启向导（P7）：就绪后先刷一次门禁状态，再决定是否放行自动进入
        self.refresh_license()
        self.maybe_redirect_ready()
        if self.state.redirect_ready:
            self.state.log(f'启动完成！{AUTO_REDIRECT_S:.0f} 秒后自动进入，'
                           f'或点击启动页按钮立即进入')
        else:
            waiting = ('同意用户协议' if not self.state.eula
                       else '完成产品激活')
            self.state.log(f'启动完成。请在启动页完成「{waiting}」后进入')
        self.state.log('提示：关闭本控制台窗口 = 退出整个 OmniSpace（含后端）')

        self._start_poller()
        if self.state.backend.get('attached'):
            self._start_attached_monitor()

        try:
            while not self._stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            self.state.log('收到 Ctrl+C')
        finally:
            self._shutdown()
        return 0

    def _hold_until_quit(self) -> None:
        """失败态保持启动页可查（用户看完点退出/关窗），不自动退出。"""
        self._start_poller()
        try:
            while not self._stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        self.state.log('正在关闭 OmniSpace…')
        try:
            self.backend.stop()
        except Exception:
            pass
        # 计划内停机代后端摘除崩溃取证心跳：backend.stop() 走 terminate
        # 强杀，lifespan 收尾（含 heartbeat.stop()）不执行，残留心跳会被
        # 下次启动误判为「异常退出」（2026-09-01 实测误报修复）
        try:
            (BOOT_DIR.parent / 'logs' / '.heartbeat').unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 - 摘除失败按崩溃处理（保守）
            pass
        self.comfy.shutdown()
        if self._splash is not None:
            threading.Thread(target=self._splash.shutdown, daemon=True).start()
        self.state.log('已全部关闭')


def main() -> int:
    parser = argparse.ArgumentParser(description='OmniSpace AI 启动主程序')
    parser.add_argument('--port', type=int, default=5800, help='后端端口')
    parser.add_argument('--no-browser', action='store_true', help='不自动打开界面')
    parser.add_argument('--browser', action='store_true',
                        help='强制系统浏览器打开（覆盖设置页的桌面窗口选择）')
    parser.add_argument('--comfy', action='store_true', help='启动时预热 ComfyUI')
    parser.add_argument('--no-warmup', action='store_true', help='跳过对话模型预热')
    # 位置参数＝拖入透传（通道 A）：拖文件夹到 exe 上，Windows 以路径为
    # 参数启动；不定义此项 argparse 会因多余参数直接报错退出
    parser.add_argument('dropped', nargs='*',
                        help='拖入的 models 文件夹（透传，勿手填）')
    args = parser.parse_args()

    opts = BootOptions(port=args.port, no_browser=args.no_browser,
                       comfy=args.comfy, warmup=not args.no_warmup,
                       force_browser=args.browser,
                       dropped=list(args.dropped or []))
    return BootOrchestrator(opts).run()


if __name__ == '__main__':
    sys.exit(main())
# 本项目仅供学习使用，商业授权请+Q 3559331368
