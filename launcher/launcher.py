#!/usr/bin/env python3
"""
OmniSpace AI Launcher 守护进程
- 环境自检（OS, CUDA, 磁盘空间≥5GB）
- 端口冲突三级递进处理
- 模型完整性校验 + 断点续传下载
- 启动/监控后端主进程 + 心跳检测 + 崩溃自动重启
- 就绪后打开系统浏览器访问后端同源前端（src.main 静态托管 frontend/dist，
  无独立前端端口；实际端口经 --port 传入 uvicorn，默认 8765）
- 系统托盘图标 + 气泡通知
- 首次安装引导（动画/轮播/偏好问卷）
- Launcher与主程序WebSocket双向通信
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368

import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
import yaml

# stdout/stderr 强制 UTF-8（2026-08-26 实测：Start-Process 重定向或
# 非 UTF-8 管道场景下 Python 回退 GBK，print「✓」等字符直接
# UnicodeEncodeError 令 launcher 在环境检查阶段崩溃退出）
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 极端环境下保底不阻断启动
            pass

# 添加项目路径
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── 崩溃取证（2026-09-16 布控）─────────────────────────────────
# 09-16 审计定性：看门狗「只记重启次数、不记退出码、不留现场」= 无声死
# 取证断链的直接原因（08:57 崩溃簇五连死无 traceback/无 WER，根因悬置）。
# 快照写入 logs/crash_forensics/，取证全程 fail-open。
_CRASH_FORENSICS_DIR = PROJECT_ROOT / 'logs' / 'crash_forensics'
# 引擎子进程特征（cmdline 小写匹配）：comfy / vllm / llama 三类
_ENGINE_SIGNATURES = ('comfyui/main.py', 'vllm', 'llama-server')


def _snapshot_gpu() -> dict[str, Any] | None:
    """nvidia-smi 快照（VRAM/利用率/温度）；失败返回 None（fail-open）。"""
    try:
        out = subprocess.run(
            ['nvidia-smi',
             '--query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=5)
        parts = out.stdout.strip().splitlines()[0].split(', ')
        if out.returncode == 0 and len(parts) == 4:
            return {'vram_used_mb': int(parts[0]),
                    'vram_total_mb': int(parts[1]),
                    'gpu_util_pct': int(parts[2]),
                    'gpu_temp_c': int(parts[3])}
    except Exception:  # noqa: BLE001 - 取证失败不影响看门狗
        pass
    return None


def _snapshot_engine_procs() -> list[dict[str, Any]]:
    """当前引擎类子进程清单（pid/RSS/命令行截断），供死亡现场比对。"""
    out: list[dict[str, Any]] = []
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'memory_info']):
        try:
            # 反斜杠归一化：Windows 命令行路径分隔符不定，统一按 / 匹配
            cmd = ' '.join(
                proc.info['cmdline'] or []).lower().replace('\\', '/')
            if not any(sig in cmd for sig in _ENGINE_SIGNATURES):
                continue
            mi = proc.info.get('memory_info')
            out.append({'pid': proc.info['pid'],
                        'name': proc.info.get('name'),
                        'rss_mb': round(mi.rss / 1e6, 1) if mi else None,
                        'cmdline': cmd[:200]})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def _load_disk_start_min_gb(default: float = 20.0) -> float:
    """从 config.yaml `disk.start_min_gb` 读取启动磁盘门槛（P2 统一口径）。

    Launcher 是独立启动进程，不 import src.config（避免其目录创建 /
    回环校验副作用），直接轻量读取同一份 config.yaml；解析失败回退默认值。
    """
    try:
        cfg_path = PROJECT_ROOT / 'src' / 'config.yaml'
        with open(cfg_path, encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
        return float(cfg['disk']['start_min_gb'])
    except Exception:
        return default


@dataclass
class LauncherConfig:
    """Launcher配置"""
    backend_port: int = 5800
    frontend_port: int = 0  # 0=自动选择
    backend_host: str = '127.0.0.1'
    health_check_interval: float = 5.0
    max_restart_attempts: int = 5
    restart_cooldown: float = 30.0
    # 启动宽限期：后端冷启动（路由导入+模型加载）25s~2min 不等，
    # 宽限期内进程未健康不计入心跳三振——否则健康加载中的子进程
    # 会在 ~15s 被连杀（2026-08-31 boot E2E 实测：96s 冷启动被杀 1 次）
    startup_grace_s: float = 180.0
    # P2 统一口径：启动磁盘门槛读取 config.yaml `disk.start_min_gb`（单一起源，
    # 与 startup_check / installer 不再各自硬编码；读取失败回退 20GB）
    min_disk_space_gb: float = field(default_factory=_load_disk_start_min_gb)
    port_range: tuple[int, int] = (5800, 5835)


class PortManager:
    """端口管理器 - 三级递进策略"""

    def __init__(self, config: LauncherConfig) -> None:
        self.config = config

    def is_port_in_use(self, port: int) -> bool:
        """检查端口是否被占用"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((self.config.backend_host, port))
                return False
            except OSError:
                return True

    def get_process_using_port(self, port: int) -> psutil.Process | None:
        """获取占用端口的进程"""
        for conn in psutil.net_connections(kind='inet'):
            if conn.laddr.port == port and conn.status == 'LISTEN':
                try:
                    return psutil.Process(conn.pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    return None
        return None

    def is_omnispace_process(self, proc: psutil.Process) -> bool:
        """判断是否为OmniSpace残留进程（严格匹配，避免误杀其他服务）"""
        try:
            name = proc.name().lower()
            cmdline = ' '.join(proc.cmdline()).lower()
            cwd = ''
            try:
                cwd = proc.cwd().lower() if hasattr(proc, 'cwd') else ''
            except Exception:
                pass
            project_marker = str(PROJECT_ROOT).lower()
            # 必须满足：进程名/命令行/工作目录包含omnispace关键字
            # 且是python/uvicorn/celery进程，避免误杀系统中其他python web服务
            # （'omnispace' in name：品牌化进程名 OmniSpace-Backend.exe 等
            # 不含 python 字样，2026-09-02 品牌化后必须显式认领）
            is_python = (('python' in name) or ('omnispace' in name)
                         or ('uvicorn' in cmdline) or ('celery' in cmdline))
            is_omnispace = ('omnispace' in name) or ('omnispace' in cmdline) or (project_marker in cwd and 'src.main' in cmdline)
            return is_python and is_omnispace
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def resolve_port_conflict(self, preferred_port: int,
                              allow_kill: bool = True) -> tuple[int, str]:
        """
        三级递进端口冲突处理
        返回: (最终端口, 处理说明)
        allow_kill=False（OMNISPACE_ALLOW_MULTI=1 双实例联调）：跳过第一级
        杀残留——"残留"特征（python+omnispace）无法区分上次崩溃的尸体和
        隔壁活着的健康后端，双实例时误杀主实例（2026-09-01 异目录实测事故）
        """
        if not self.is_port_in_use(preferred_port):
            return preferred_port, '端口可用'

        if allow_kill:
            proc = self.get_process_using_port(preferred_port)

            # 第一级：检测到OmniSpace/python残留 → 自动taskkill
            if proc and self.is_omnispace_process(proc):
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                    time.sleep(1)
                    if not self.is_port_in_use(preferred_port):
                        return preferred_port, f'已自动结束残留进程 (PID: {proc.pid})'
                    proc.kill()
                    time.sleep(1)
                    if not self.is_port_in_use(preferred_port):
                        return preferred_port, f'已强制结束残留进程 (PID: {proc.pid})'
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
                    pass

        # 第二级：外部占用 → 自动扫描端口区间
        for port in range(self.config.port_range[0], self.config.port_range[1] + 1):
            if not self.is_port_in_use(port):
                return port, f'端口{preferred_port}被占用，自动切换到端口{port}'

        # 第三级：候选区间全部占用
        raise RuntimeError(
            f'端口区间{self.config.port_range[0]}-{self.config.port_range[1]}全部被占用，'
            f'请手动释放端口后重试'
        )


class EnvironmentChecker:
    """环境检查器"""

    def __init__(self, config: LauncherConfig) -> None:
        self.config = config
        self.results: dict[str, Any] = {}

    def check_all(self) -> tuple[bool, dict]:
        """执行所有环境检查"""
        checks = {
            'os': self._check_os,
            'python': self._check_python,
            'disk_space': self._check_disk_space,
            'ram_commit': self._check_ram_commit,
            'cuda': self._check_cuda,
            'dependencies': self._check_dependencies,
            'hypervisor': self._check_hypervisor,
            'path_ascii': self._check_path_ascii,
            'torch_contract': self._check_torch_contract,
        }

        all_passed = True
        results = {}

        for name, check_func in checks.items():
            try:
                passed, message = check_func()
                results[name] = {'passed': passed, 'message': message}
                if not passed:
                    all_passed = False
            except Exception as e:
                results[name] = {'passed': False, 'message': f'检查失败: {str(e)}'}
                all_passed = False

        self.results = results
        return all_passed, results

    def _check_torch_contract(self) -> tuple[bool, str]:
        """torch 版本契约（B1 2026-09-13）：三处 torch 独立安装历史上无
        机制保证一致（cu128/cu130 双代 dist-info 曾并存、文档三层失真）。
        真源 = src/torch_contract.json；主链（label 含「主运行时」）不符
        = 不通过
        （阻断，OMNISPACE_ALLOW_TORCH_DRIFT=1 豁免自担风险）；旁链
        （py313 / ComfyUI 便携包，自包含栈）不符仅黄灯提示。"""
        import json as _json
        import os as _os
        import re as _re

        contract_path = PROJECT_ROOT / 'src' / 'torch_contract.json'
        try:
            contract = _json.loads(contract_path.read_text(encoding='utf-8'))
        except Exception as e:
            return True, f'契约文件不可读（跳过，{e}）'
        expected = str(contract.get('torch') or '')
        if not expected:
            return True, '契约缺少 torch 字段（跳过）'

        def _read(ver_py: str) -> str | None:
            try:
                m = _re.search(
                    r"""__version__\s*=\s*['"]([^'"]+)['"]""",
                    (PROJECT_ROOT / ver_py).read_text(
                        encoding='utf-8', errors='ignore'))
                return m.group(1) if m else None
            except Exception:
                return None

        drift = []
        primary_bad = False
        for label, rel in (contract.get('targets') or {}).items():
            actual = _read(rel)
            if actual is None:
                drift.append(f'{label}: version.py 不可读')
                if '主运行时' in label:
                    primary_bad = True
                continue
            if actual != expected:
                drift.append(f'{label}: 实际 {actual} ≠ 契约 {expected}')
                if '主运行时' in label:
                    primary_bad = True
        if not drift:
            return True, f'三处 torch 对齐契约 {expected}'
        if primary_bad and _os.environ.get('OMNISPACE_ALLOW_TORCH_DRIFT') != '1':
            return False, (
                'torch 契约失配（' + '；'.join(drift) +
                '）——升/换 torch 请同步 src/torch_contract.json；'
                '确需带漂运行设 OMNISPACE_ALLOW_TORCH_DRIFT=1')
        return True, 'torch 契约旁链漂移（黄灯）：' + '；'.join(drift)

    def _check_os(self) -> tuple[bool, str]:
        """检查操作系统"""
        import platform
        if platform.system() != 'Windows':
            return False, '仅支持Windows 10/11 64-bit'
        ver = platform.version()
        return True, f'Windows {platform.release()} (版本{ver})'

    def _check_python(self) -> tuple[bool, str]:
        """检查Python版本"""
        version = sys.version_info
        if version.major == 3 and version.minor >= 10:
            return True, f'Python {version.major}.{version.minor}.{version.micro}'
        return False, f'需要Python 3.10+，当前版本{version.major}.{version.minor}'

    def _check_disk_space(self) -> tuple[bool, str]:
        """检查磁盘空间"""
        disk = psutil.disk_usage(str(PROJECT_ROOT))
        free_gb = disk.free / (1024**3)
        if free_gb >= self.config.min_disk_space_gb:
            return True, f'可用磁盘空间: {free_gb:.1f}GB'
        return False, f'磁盘空间不足: 可用{free_gb:.1f}GB，需要≥{self.config.min_disk_space_gb}GB'

    def _check_ram_commit(self) -> tuple[bool, str]:
        """RAM 提交余量预检（2026-09-15 审计补线，warn-only 不阻断）。

        背景：2026-09-02 实锤的后端静默死亡根因是 RAM 提交耗尽（WER
        RADAR_PRE_LEAK_64）——启动链此前对 RAM 零预检，防线只剩 ≤5 次
        崩溃补位。低余量时提示关应用再启动，但不 block（低配机也能起，
        起后 resource_guard 运行期兜底）。口径用 commit（虚拟内存承诺）
        而非物理内存——WER 按提交耗尽判死。
        """
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ('dwLength', ctypes.c_ulong),
                    ('dwMemoryLoad', ctypes.c_ulong),
                    ('ullTotalPhys', ctypes.c_ulonglong),
                    ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong),
                    ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong),
                    ('ullAvailVirtual', ctypes.c_ulonglong),
                    ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            avail_commit_gb = stat.ullAvailPageFile / (1024 ** 3)
            if avail_commit_gb >= 8.0:
                return True, f'RAM 提交余量: {avail_commit_gb:.1f}GB'
            return True, (f'⚠ RAM 提交余量仅 {avail_commit_gb:.1f}GB（建议≥8GB）——'
                          f'历史静默死亡（WER 提交耗尽）高危态，建议关闭占内存应用后重启')
        except Exception as e:  # noqa: BLE001 - 探测失败不阻断
            return True, f'RAM 预检跳过（探测失败: {e}）'

    def _check_cuda(self) -> tuple[bool, str]:
        """检查CUDA环境"""
        try:
            import torch
            if torch.cuda.is_available():
                gpu_name = torch.cuda.get_device_name(0)
                vram_gb = torch.cuda.get_device_properties(0).total_memory / (1024**3)
                return True, f'CUDA可用: {gpu_name} ({vram_gb:.1f}GB VRAM)'
            return False, 'CUDA不可用，请安装NVIDIA显卡驱动'
        except ImportError:
            return False, 'PyTorch未安装'
        except Exception as e:
            return False, f'CUDA检查失败: {str(e)}'

    def _check_hypervisor(self) -> tuple[bool, str]:
        """Hyper-V/VBS 状态（P7 首启体检红绿灯；仅提示不阻断——本机
        2026-08-29 蓝屏根因即 HYPERVISOR_ERROR+VBS，给用户一个黄灯）。"""
        try:
            r = subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 '(Get-CimInstance Win32_ComputerSystem).HypervisorPresent'],
                capture_output=True, text=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW
                if os.name == "nt" else 0)
            if 'True' in (r.stdout or ''):
                return True, 'Hyper-V 虚拟化运行中（若遇蓝屏/性能异常，可考虑关闭后对比）'
            return True, '未检测到 Hyper-V（最优配置）'
        except Exception as e:  # noqa: BLE001 - 检测失败不阻断
            return True, f'Hyper-V 状态检测跳过（{e}）'

    def _check_path_ascii(self) -> tuple[bool, str]:
        """安装路径纯 ASCII 检查（2026-09-01 测试机三层洋葱终审：
        vLLM 生态 tvm-ffi 用窄字符 LoadLibrary，路径含中文/特殊字符时
        xgrammar_bindings.dll 必加载失败 → 对话引擎起不来。黄灯提示，
        不阻断——除 AI 对话外的功能不受影响）。"""
        try:
            root = str(PROJECT_ROOT)
            root.encode('ascii')
            return True, '安装路径纯 ASCII（对话引擎可用）'
        except UnicodeEncodeError:
            bad = next(ch for ch in root if ord(ch) > 127)
            return (False, f'安装路径含非 ASCII 字符「{bad}」：'
                    'AI 对话（vLLM）将无法启动，绘画/漫剧不受影响；'
                    '请把整个文件夹移到纯英文路径（盘符任意、文件夹名用英文）')

    def _check_dependencies(self) -> tuple[bool, str]:
        """检查Python依赖：关键依赖缺失阻断，AI 依赖缺失仅警告（功能自动降级）"""
        critical = ['fastapi', 'uvicorn']
        optional = ['torch', 'transformers', 'diffusers']
        missing_critical, missing_optional = [], []
        for pkg in critical:
            try:
                __import__(pkg)
            except ImportError:
                missing_critical.append(pkg)
        for pkg in optional:
            try:
                __import__(pkg)
            except ImportError:
                missing_optional.append(pkg)
        if missing_critical:
            return False, f'缺少关键依赖: {", ".join(missing_critical)}'
        if missing_optional:
            return True, f'核心依赖已安装（{", ".join(missing_optional)} 缺失，对应能力自动降级）'
        return True, '核心依赖已安装'

    def blake3_quick_verify(self, files: list = None, sample_bytes: int = 65536,
                            manifest_path: Path = None, write_manifest: bool = False) -> tuple[bool, str]:
        """BLAKE3/Blake2b快速校验：对关键文件做完整哈希计算，支持manifest比对
        - manifest_path: 已有的manifest文件路径，存在则做比对验证
        - write_manifest: True时生成新的manifest文件
        返回(是否通过校验, 消息)
        """
        import hashlib

        def _blake3_factory() -> Any:
            return blake3.blake3()

        def _blake2b_factory() -> Any:
            return hashlib.new('blake2b', digest_size=32)

        # 优先使用blake3（需第三方包），否则降级到blake2b
        try:
            import blake3
            hash_func = _blake3_factory
            hash_name = 'blake3'
        except ImportError:
            hash_func = _blake2b_factory
            hash_name = 'blake2b-256'

        # 完整性校验清单（2026-09-16 src 扁平化勘误：旧 backend/core/security.py
        # 与 app.jsx/api.js 均为不存在的史前引用，校验恒 missing）——现行真源四件
        critical_files = files or [
            PROJECT_ROOT / 'src' / 'main.py',
            PROJECT_ROOT / 'src' / 'config.yaml',
            PROJECT_ROOT / 'launcher' / 'boot.py',
            PROJECT_ROOT / 'frontend' / 'dist' / 'index.html',
        ]
        results = {}
        all_ok = True
        for fp in critical_files:
            try:
                p = Path(fp)
                if not p.exists():
                    results[p.name] = {'status': 'missing'}
                    all_ok = False
                    continue
                # 完整文件哈希（非采样，保证完整性校验有效）
                h = hash_func()
                size = p.stat().st_size
                with open(p, 'rb') as f:
                    while True:
                        chunk = f.read(1024 * 1024)  # 1MB分块读取，避免大文件内存问题
                        if not chunk:
                            break
                        h.update(chunk)
                results[p.name] = {'status': 'ok', 'hash': h.hexdigest(), 'size': size}
            except Exception as e:
                results[p.name] = {'status': 'error', 'error': str(e)}
                all_ok = False

        # 写入manifest
        if write_manifest:
            mp = manifest_path or (PROJECT_ROOT / '.manifest.json')
            manifest = {
                'version': '1.0',
                'hash_algo': hash_name,
                'created_at': datetime.now().isoformat(),
                'files': results
            }
            mp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
            return True, f'BLAKE3 manifest已生成({hash_name}): {len(results)}个文件'

        # 比对manifest
        if manifest_path and manifest_path.exists():
            try:
                old_manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                old_files = old_manifest.get('files', {})
                mismatches = []
                for fname, info in results.items():
                    old = old_files.get(fname)
                    if not old:
                        mismatches.append(f'{fname}(新文件)')
                    elif old.get('hash') != info.get('hash'):
                        mismatches.append(f'{fname}(已修改)')
                for fname in old_files:
                    if fname not in results:
                        mismatches.append(f'{fname}(缺失)')
                if mismatches:
                    return False, f'完整性校验失败({hash_name}): ' + '; '.join(mismatches)
                return True, f'BLAKE3完整性校验通过({hash_name}): {len(results)}个文件验证OK'
            except Exception as e:
                return False, f'manifest比对失败: {e}'

        # 无manifest时，仅计算并返回结果
        ok_count = sum(1 for v in results.values() if v.get('status') == 'ok')
        return all_ok, f'BLAKE3文件哈希({hash_name}): {ok_count}/{len(results)}个文件正常'


class BackendProcess:
    """后端进程管理器"""

    def __init__(self, config: LauncherConfig) -> None:
        self.config = config
        self.process: subprocess.Popen | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._log_threads: list = []  # L-M2: 后台日志消费线程
        self._running = False
        self._restart_count = 0
        self._last_restart_time = 0.0
        # 当前子进程的拉起时刻：启动宽限期判据（见 LauncherConfig.startup_grace_s）
        self._proc_started_at = 0.0
        self.on_status_change: Callable | None = None
        # Boot 启动主程序注入的行级输出回调：置非 None 后 stdout/stderr
        # 强制走 PIPE 并逐行回调（2026-08-31，供 Splash 启动页实时日志流）
        self.on_output: Callable[[str], None] | None = None
        # R2-N3：心跳代际计数 + 崩溃处理锁——重启时旧心跳线程自动退出，
        # 防止多次崩溃后心跳线程累积并发触发重复重启（线程泄漏级缺陷）
        self._hb_gen = 0
        self._crash_lock = threading.Lock()

    # L-H1: 子进程环境变量白名单，仅传递必要变量，避免敏感信息泄露
    # 用户身份变量（USERNAME 等）必须传递：torch._dynamo 导入链经
    # getpass.getuser() 解析缓存目录，缺全部身份变量时 fallback 到
    # Unix-only 的 pwd 模块 → Windows 上 ModuleNotFoundError → dynamo
    # 半途失败 → 注册表残留 → 二次导入必炸（2026-08-22 对话故障根因）
    _ENV_WHITELIST = (
        'PATH', 'SYSTEMROOT', 'PYTHONPATH', 'PYTHONUNBUFFERED',
        'PYTHONIOENCODING', 'PYTHONUTF8',
        'CUDA_VISIBLE_DEVICES', 'HF_HOME', 'OMP_NUM_THREADS',
        'LANG', 'LC_ALL', 'TEMP', 'TMP',
        'USERNAME', 'USERPROFILE', 'USER', 'LOGONSERVER', 'LOGNAME',
        # vLLM 编译缓存开关（2026-09-01）：vllm_service 默认禁用缓存保
        # 发行稳健；开发机需要缓存时全局置 VLLM_DISABLE_COMPILE_CACHE=0
        'VLLM_DISABLE_COMPILE_CACHE',
        # 页面守卫（2026-09-03 方案A）：boot 链把自己的启动页端口传给
        # 后端，页面全关且无任务时经 /api/quit 正规退出；无此变量=非
        # boot 链（开发直启/测试实例）守卫整体不激活
        'OMNISPACE_SPLASH_PORT',
        # 批2-4：副本路径指纹（/health 回显供接管闸跨副本拒绝）
        'OMNISPACE_COPY_ID',
    )

    def _backend_exe(self) -> str:
        """后端进程身份（2026-09-02 品牌化，同日加控制台变体）：

        - 无窗链（pythonw / OmniSpace-Boot.exe 拉起）→ OmniSpace-Backend.exe
          （pythonw 底，GUI 子系统，任务管理器带 logo+说明）；
        - 控制台调试链（python.exe / OmniSpace-BootC.exe，即 bat 入口）→
          OmniSpace-BackendC.exe（python 底，控制台子系统，保留 uvicorn
          日志的 bat 黑窗回显）；
        - 副本缺失一律回退 sys.executable（不阻断启动）。
        注意不能用 'omnispace' in name 判无窗——BootC 同样含关键字但属
        控制台链，判错会让 bat 丢日志。
        """
        exe = Path(sys.executable)
        name = exe.name.lower()
        windowless = name.endswith('w.exe') or name in (
            'omnispace-boot.exe', 'omnispace-shell.exe')
        branded = exe.parent / ('OmniSpace-Backend.exe' if windowless
                                else 'OmniSpace-BackendC.exe')
        if branded.is_file():
            return str(branded)
        return sys.executable

    def _build_child_env(self, port: int) -> dict:
        """L-H1: 构建子进程环境变量（白名单方式），仅传递必要的变量"""
        env = {}
        for key in self._ENV_WHITELIST:
            val = os.environ.get(key)
            if val is not None:
                env[key] = val
        # 传递所有 OMNISPACE_* 前缀的变量
        for key, val in os.environ.items():
            if key.startswith('OMNISPACE_'):
                env[key] = val
        # 子进程 stdio 强制 UTF-8：中文 Windows 管道默认 GBK，backend 日志
        # （含 requests/triton 等三方导入期警告）经 PIPE 回流/落盘会整体乱码
        # （2026-08-31 boot E2E 实测，_drain_pipe 按 UTF-8 解码出 mojibake）
        env.setdefault('PYTHONIOENCODING', 'utf-8')
        env.setdefault('PYTHONUTF8', '1')
        env['OMNISPACE_PORT'] = str(port)
        env['OMNISPACE_LAUNCHER'] = '1'
        # 工具缓存钉死 E 盘（2026-09-17 用户令：项目产物不离开项目目录）：
        # pip/HF/triton/torchinductor/playwright 默认全落 C:\Users\...，
        # 实测累积 27G+（pip 13G/HF 13G/浏览器 0.7G）。setdefault 不覆盖
        # 外部显式设置，只补默认值。
        cache_root = PROJECT_ROOT / '.cache'
        env.setdefault('PIP_CACHE_DIR', str(cache_root / 'pip'))
        env.setdefault('HF_HOME', str(cache_root / 'huggingface'))
        env.setdefault('TRITON_CACHE_DIR', str(cache_root / 'triton'))
        env.setdefault('TORCHINDUCTOR_CACHE_DIR',
                       str(cache_root / 'torchinductor'))
        env.setdefault('PLAYWRIGHT_BROWSERS_PATH',
                       str(cache_root / 'ms-playwright'))
        return env

    # 管道日志轮转阈值（_drain_pipe 用）
    _PIPE_LOG_ROTATE_BYTES = 10 * 1024 * 1024

    def _drain_pipe(self, pipe: io.BufferedReader, log_path: Path) -> None:
        """L-M2: 后台线程持续读取子进程管道内容写入日志文件，避免管道满后子进程阻塞

        2026-09-16 批4：加轮转（原 append 无界——backend_stderr.log 已
        9.1MB 即将失守）。超阈值轮转保留一代 .1（替换式不累积），
        轮转失败继续写原文件（日志绝不因轮转而断流）。"""
        written = 0
        try:
            f = open(log_path, 'a', encoding='utf-8')
            try:
                for line in iter(pipe.readline, b''):
                    try:
                        text = line.decode('utf-8', errors='replace')
                        f.write(text)
                        f.flush()
                        written += len(text)
                        if self.on_output is not None:
                            self.on_output(text.rstrip('\r\n'))
                        if written > self._PIPE_LOG_ROTATE_BYTES:
                            f.close()
                            rotated = log_path.parent / (log_path.name + '.1')
                            try:
                                rotated.unlink(missing_ok=True)
                                log_path.replace(rotated)
                            except OSError:
                                pass
                            f = open(log_path, 'a', encoding='utf-8')
                            written = 0
                    except Exception:
                        break
            finally:
                f.close()
        except Exception:
            pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    def start(self, port: int) -> bool:
        """启动后端进程"""
        self._actual_port = port  # 保存实际运行端口，心跳检测使用
        self._proc_started_at = time.time()
        env = self._build_child_env(port)  # L-H1: 白名单环境变量

        try:
            # L-M2: 当stdout/stderr不可用时使用PIPE，并启动后台线程消费管道写入日志文件
            log_dir = PROJECT_ROOT / 'logs'
            log_dir.mkdir(exist_ok=True)
            stdout_log = log_dir / 'backend_stdout.log'
            stderr_log = log_dir / 'backend_stderr.log'

            use_pipe_stdout = (not sys.stdout) or self.on_output is not None
            use_pipe_stderr = (not sys.stderr) or self.on_output is not None

            self.process = subprocess.Popen(
                [self._backend_exe(), '-m', 'uvicorn', 'src.main:app',
                 '--host', self.config.backend_host, '--port', str(port),
                 '--log-level', 'info'],
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=subprocess.PIPE if use_pipe_stdout else None,
                stderr=subprocess.PIPE if use_pipe_stderr else None,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
            )

            # L-M2: 启动后台守护线程持续读取PIPE管道，防止管道缓冲区满导致子进程阻塞假死
            self._log_threads = []
            if use_pipe_stdout and self.process.stdout:
                t = threading.Thread(target=self._drain_pipe,
                                     args=(self.process.stdout, stdout_log), daemon=True)
                t.start()
                self._log_threads.append(t)
            if use_pipe_stderr and self.process.stderr:
                t = threading.Thread(target=self._drain_pipe,
                                     args=(self.process.stderr, stderr_log), daemon=True)
                t.start()
                self._log_threads.append(t)

            self._running = True
            # R2-N3：代际递增，旧心跳线程观察到代际失配即自行退出
            self._hb_gen += 1
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, args=(self._hb_gen,), daemon=True)
            self._heartbeat_thread.start()

            return True
        except Exception as e:
            print(f'启动后端失败: {e}')
            return False

    def stop(self, timeout: float = 10.0) -> None:
        """停止后端进程（2026-09-15 竞态根修：先递增代际号让在飞心跳
        循环立即失配退出，再与 _handle_crash 互斥串行——否则「stop 进
        行中、心跳恰好判定崩溃→锁内重启」会把后端复活成孤儿（无人
        监管、持单实例互斥体占端口）。锁内最多等一轮在飞重启收尾后
        正常终止。_running=False 双保险：即使代际递增与 start() 竞争
        丢失，循环也在下个检查点退出。）"""
        self._running = False
        self._hb_gen += 1  # 在飞心跳线程代际失配，立即退出不再触发重启
        with self._crash_lock:
            if self.process:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
                self.process = None

    def is_healthy(self, port: int) -> bool:
        """健康检查（审计 R3-ARCH2 修复三处历史残留）：
        1. 端点为根路径 /health（非 /api/v1/health——后者不存在，404 信封也返 200 会误判）
        2. timeout 传给 urlopen（Request() 不接受 timeout 参数，传了抛 TypeError
           被 except 吞掉 → is_healthy 永远 False → 启动永远超时退出）
        3. 解析信封校验 data.db，后端 DB 降质态能被感知
        """
        try:
            import json
            import urllib.request
            url = f'http://{self.config.backend_host}:{port}/health'
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status != 200:
                    return False
                payload = json.loads(resp.read().decode('utf-8'))
            data = payload.get('data') or {}
            return data.get('db', 'ok') == 'ok'
        except Exception:
            return False

    def wait_until_ready(self, port: int, timeout: float = 60.0) -> bool:
        """等待后端启动就绪"""
        start = time.time()
        while time.time() - start < timeout:
            if self.is_healthy(port):
                return True
            if self.process and self.process.poll() is not None:
                return False
            time.sleep(1)
        return False

    def _heartbeat_loop(self, gen: int) -> None:
        """心跳检测循环（使用实际运行端口；R2-N3：代际失配自动退出防线程累积）"""
        port = self._actual_port if hasattr(self, '_actual_port') else self.config.backend_port
        consecutive_failures = 0

        while self._running and gen == self._hb_gen:
            time.sleep(self.config.health_check_interval)

            if not self._running or gen != self._hb_gen:
                break

            if self.process and self.process.poll() is not None:
                # 进程已退出
                consecutive_failures += 1
                self._handle_crash(port)
                continue

            # 启动宽限期：当前子进程尚年轻（含崩溃重启后的新进程）时，
            # 未健康不计入三振——冷启动加载期不是崩溃
            proc_age = time.time() - self._proc_started_at
            if proc_age < self.config.startup_grace_s:
                consecutive_failures = 0
                continue

            if not self.is_healthy(port):
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    self._handle_crash(port)
            else:
                consecutive_failures = 0

    def _handle_crash(self, port: int) -> None:
        """处理崩溃 - 自动重启（R2-N3：锁保护，并发心跳仅一个执行重启）"""
        with self._crash_lock:
            self._handle_crash_locked(port)

    def _write_crash_forensics(self, port: int,
                               exit_code: int | None) -> Path | None:
        """后端死亡现场快照：退出码 + RAM + GPU + 引擎子进程清单。

        写入 logs/crash_forensics/crash-<时间戳>.json。取证全程
        fail-open——快照失败绝不影响看门狗重启主链。
        """
        try:
            vm = psutil.virtual_memory()
            snap: dict[str, Any] = {
                'ts': datetime.now().isoformat(timespec='seconds'),
                'backend_pid': self.process.pid if self.process else None,
                'exit_code': exit_code,
                'restart_count_so_far': self._restart_count,
                'port': port,
                'ram': {'percent': vm.percent,
                        'available_mb': round(vm.available / 1e6)},
                'gpu': _snapshot_gpu(),
                'engine_procs': _snapshot_engine_procs(),
            }
            _CRASH_FORENSICS_DIR.mkdir(parents=True, exist_ok=True)
            path = (_CRASH_FORENSICS_DIR /
                    f"crash-{datetime.now():%Y%m%d-%H%M%S}.json")
            path.write_text(
                json.dumps(snap, ensure_ascii=False, indent=2),
                encoding='utf-8')
            return path
        except Exception:  # noqa: BLE001 - 取证失败不影响看门狗
            return None

    def _cleanup_orphan_engines(self) -> list[int]:
        """热重启前清理孤儿引擎子进程（父进程已死的 comfy/vllm/llama）。

        后端猝死时引擎子进程可能存活并继续占显存——新实例在满显存上
        初始化 CUDA 是 09-16 崩溃簇 5 连死的候选放大器。冷启动链已有
        同款清理（boot.py 启动时），热重启链此前为零。只杀父进程已死
        者，绝不误伤其他存活实例的活引擎。
        """
        killed: list[int] = []
        my_pid = os.getpid()
        for proc in psutil.process_iter(['pid', 'cmdline']):
            try:
                cmd = ' '.join(
                    proc.info['cmdline'] or []).lower().replace('\\', '/')
                if not any(sig in cmd for sig in _ENGINE_SIGNATURES):
                    continue
                if proc.info['pid'] == my_pid:
                    continue
                try:
                    parent = proc.parent()
                    parent_alive = parent is not None and parent.is_running()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    parent_alive = False
                if parent_alive:
                    continue
                proc.kill()
                killed.append(proc.info['pid'])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if killed:
            print(f'[launcher] 热重启前清理孤儿引擎进程: PID {killed}',
                  flush=True)
        return killed

    def _handle_crash_locked(self, port: int) -> None:
        now = time.time()

        # 崩溃布控（2026-09-16）：入口先抓退出码与死亡现场快照
        exit_code = self.process.poll() if self.process else None
        forensics_path = self._write_crash_forensics(port, exit_code)

        # 端口归属检查（2026-08-22 僵尸循环修复）：双 launcher 共存时，
        # 对方 watchdog 拉起的 uvicorn 已占用端口，本实例重启的 uvicorn
        # 必然 bind 失败退出，而 wait_until_ready 探测到对方的健康响应
        # 还会清零重启计数 → 无限拉起短命进程（每轮完整跑 lifespan：
        # 加载 bge 嵌入模型 1.3GB + 调度引擎，实测每 17-30s 一轮，
        # RAM 周期性抖动 + 日志刷屏）。自己子进程已死但端口仍健康
        # = 响应来自别的实例 → 本实例退位退出，让幸存者继续服务。
        if self.is_healthy(port) and (not self.process or self.process.poll() is not None):
            self._running = False
            if self.on_status_change:
                self.on_status_change(
                    'crash_permanent',
                    f'端口 {port} 已被另一健康后端实例占用，本实例退位退出（避免僵尸重启循环）')
            return

        # 冷却检查
        # B0 修复（2026-09-13）：旧逻辑「距上次重启 >30s 即把计数重置为
        # 1」——09-12 实测崩溃带每 3.3min 一崩，每轮都被重置，max=5 上限
        # 从未触顶，5.5 小时 99 连崩全被「第1次」掩盖。改为：崩溃计数
        # 仅在距上次重启 ≥10 分钟（稳定窗）后才重置；短间隔崩溃持续
        # 累计直至 crash_permanent。
        if (self._last_restart_time
                and now - self._last_restart_time < 600.0):
            self._restart_count += 1
        else:
            self._restart_count = 1

        self._last_restart_time = now

        if self._restart_count > self.config.max_restart_attempts:
            self._running = False
            if self.on_status_change:
                self.on_status_change('crash_permanent', f'后端崩溃次数超过限制({self.config.max_restart_attempts}次)')
            return

        extra = f'，现场快照 {forensics_path.name}' if forensics_path else ''
        if self.on_status_change:
            self.on_status_change(
                'restarting',
                f'后端异常(exit={exit_code}{extra})，正在重启(第{self._restart_count}次)...')

        # 终止旧进程
        if self.process:
            try:
                self.process.kill()
                self.process.wait(timeout=5)
            except Exception:
                pass

        # 孤儿引擎清理（2026-09-16 布控）：清理失败不阻断重启
        try:
            self._cleanup_orphan_engines()
        except Exception:  # noqa: BLE001
            pass

        # 重启
        time.sleep(2)
        self.start(port)

        if self.wait_until_ready(port, timeout=30):
            # B0 修复（2026-09-13）：不再「重启成功即清零计数」——清零
            # 只由上面的稳定窗判定（距上次重启 ≥10min）承担；立即清零
            # 会让「崩→补位→再崩」的连环每轮都从第 1 次重新数起。
            if self.on_status_change:
                self.on_status_change('running', '后端已重启')



# B3（2026-09-13）幻影清理：本文件旧入口 TrayIcon/Launcher/main()（8765
# 守护链，约 230 行）已摘除——boot.py 链（5800）自 2026-08-31 起为唯一
# 入口，且双栈双跑有显存叠载事故案底；上方 BackendProcess/EnvironmentChecker/
# LauncherConfig/PortManager 为 boot.py 复用的库部分，原样保留。
