#!/usr/bin/env python3
"""
OmniSpace AI Launcher 守护进程
- 环境自检（OS, CUDA, 磁盘空间≥5GB）
- 端口冲突三级递进处理
- 模型完整性校验 + 断点续传下载
- 启动/监控后端主进程 + 心跳检测 + 崩溃自动重启
- 就绪后打开系统浏览器访问后端同源前端（backend.main 静态托管 frontend/dist，
  无独立前端端口；实际端口经 --port 传入 uvicorn，默认 8765）
- 系统托盘图标 + 气泡通知
- 首次安装引导（动画/轮播/偏好问卷）
- Launcher与主程序WebSocket双向通信
"""

import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
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


def _load_disk_start_min_gb(default: float = 20.0) -> float:
    """从 config.yaml `disk.start_min_gb` 读取启动磁盘门槛（P2 统一口径）。

    Launcher 是独立启动进程，不 import backend.config（避免其目录创建 /
    回环校验副作用），直接轻量读取同一份 config.yaml；解析失败回退默认值。
    """
    try:
        cfg_path = PROJECT_ROOT / 'backend' / 'config.yaml'
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
            is_omnispace = ('omnispace' in name) or ('omnispace' in cmdline) or (project_marker in cwd and 'backend.main' in cmdline)
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
        self.results = {}

    def check_all(self) -> tuple[bool, dict]:
        """执行所有环境检查"""
        checks = {
            'os': self._check_os,
            'python': self._check_python,
            'disk_space': self._check_disk_space,
            'cuda': self._check_cuda,
            'dependencies': self._check_dependencies,
            'hypervisor': self._check_hypervisor,
            'path_ascii': self._check_path_ascii,
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
                capture_output=True, text=True, timeout=15)
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

        # 完整性校验清单：现行生效的核心文件（dist 双轨已剔除；sandbox 功能位于 core/security.py）
        critical_files = files or [
            PROJECT_ROOT / 'backend' / 'main.py',
            PROJECT_ROOT / 'backend' / 'core' / 'security.py',
            PROJECT_ROOT / 'frontend' / 'index.html',
            PROJECT_ROOT / 'frontend' / 'src' / 'app.jsx',
            PROJECT_ROOT / 'frontend' / 'src' / 'api.js',
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
        return env

    def _drain_pipe(self, pipe: io.BufferedReader, log_path: Path) -> None:
        """L-M2: 后台线程持续读取子进程管道内容写入日志文件，避免管道满后子进程阻塞"""
        try:
            with open(log_path, 'a', encoding='utf-8') as f:
                for line in iter(pipe.readline, b''):
                    try:
                        text = line.decode('utf-8', errors='replace')
                        f.write(text)
                        f.flush()
                        if self.on_output is not None:
                            self.on_output(text.rstrip('\r\n'))
                    except Exception:
                        break
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
                [self._backend_exe(), '-m', 'uvicorn', 'backend.main:app',
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
        """停止后端进程"""
        self._running = False
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

    def _handle_crash_locked(self, port: int) -> None:
        now = time.time()

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
        if now - self._last_restart_time < self.config.restart_cooldown:
            self._restart_count += 1
        else:
            self._restart_count = 1

        self._last_restart_time = now

        if self._restart_count > self.config.max_restart_attempts:
            self._running = False
            if self.on_status_change:
                self.on_status_change('crash_permanent', f'后端崩溃次数超过限制({self.config.max_restart_attempts}次)')
            return

        if self.on_status_change:
            self.on_status_change('restarting', f'后端异常，正在重启(第{self._restart_count}次)...')

        # 终止旧进程
        if self.process:
            try:
                self.process.kill()
                self.process.wait(timeout=5)
            except Exception:
                pass

        # 重启
        time.sleep(2)
        self.start(port)

        if self.wait_until_ready(port, timeout=30):
            self._restart_count = 0
            if self.on_status_change:
                self.on_status_change('running', '后端已重启')


class TrayIcon:
    """系统托盘图标"""

    def __init__(self, launcher: 'Launcher') -> None:
        self.launcher = launcher
        self._icon = None

    def show(self) -> None:
        """显示托盘图标（如果pystray可用）"""
        try:
            import pystray
            from PIL import Image, ImageDraw

            # 创建简单图标
            img = Image.new('RGB', (64, 64), color=(100, 100, 255))
            dc = ImageDraw.Draw(img)
            dc.ellipse([16, 16, 48, 48], fill=(255, 255, 255))

            menu = pystray.Menu(
                pystray.MenuItem('打开界面', self._open_browser),
                pystray.MenuItem('状态', self._show_status),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('退出', self._quit),
            )

            self._icon = pystray.Icon('OmniSpace', img, 'OmniSpace AI', menu)
            threading.Thread(target=self._icon.run, daemon=True).start()
        except ImportError:
            print('pystray/PIL未安装，跳过托盘图标')

    def notify(self, title: str, message: str) -> None:
        """显示气泡通知"""
        if self._icon:
            try:
                self._icon.notify(message, title)
            except Exception:
                pass
        print(f'[通知] {title}: {message}')

    def _open_browser(self) -> None:
        self.launcher.open_browser()

    def _show_status(self, icon: object | None = None, item: object | None = None) -> None:
        status = self.launcher.get_status()
        self.notify('OmniSpace AI 状态', status)

    def _quit(self, icon: object | None = None, item: object | None = None) -> None:
        self.launcher.shutdown()
        if self._icon:
            self._icon.stop()


class Launcher:
    """OmniSpace Launcher主类"""

    def __init__(self, config: LauncherConfig | None = None) -> None:
        self.config = config or LauncherConfig()
        self.port_manager = PortManager(self.config)
        self.env_checker = EnvironmentChecker(self.config)
        self.backend = BackendProcess(self.config)
        self.backend.on_status_change = self._on_backend_status
        self.tray = TrayIcon(self)
        self._actual_port = self.config.backend_port
        self._start_time = None

    def initialize(self) -> bool:
        """初始化Launcher"""
        print('=' * 60)
        print('OmniSpace AI Launcher')
        print('=' * 60)

        # TASK-P0-05：非回环绑定硬拒绝（规格 §14 约束2，与 backend/config.py
        # 导入期闸门构成双层防线；此处拦截避免拉起注定被拒的后端进程）
        import os
        if (self.config.backend_host not in ('127.0.0.1', 'localhost', '::1')
                and os.environ.get('OMNISPACE_ALLOW_LAN') != '1'):
            print(f'  ✗ 拒绝启动：backend_host={self.config.backend_host} 为非回环地址。'
                  'API 无认证体系，绑定局域网等于数据裸奔。')
            print('    确需局域网访问：设置环境变量 OMNISPACE_ALLOW_LAN=1（自担风险）；'
                  '或改回 127.0.0.1。')
            return False

        # 1. 环境检查
        print('\n[1/4] 环境检查...')
        passed, results = self.env_checker.check_all()
        for name, result in results.items():
            status = '✓' if result['passed'] else '✗'
            print(f'  {status} {name}: {result["message"]}')

        if not passed:
            print('\n环境检查未通过，请修复后重试')
            return False

        # 2. 端口处理
        print('\n[2/4] 端口配置...')
        try:
            self._actual_port, port_msg = self.port_manager.resolve_port_conflict(self.config.backend_port)
            print(f'  {port_msg}')
            self.config.backend_port = self._actual_port
        except RuntimeError as e:
            print(f'  ✗ {e}')
            return False

        # 3. 写入前端配置
        print('\n[3/4] 写入前端配置...')
        self._write_frontend_config()
        print(f'  后端端口: {self._actual_port}')

        # 4. 启动后端
        print('\n[4/4] 启动后端服务...')
        if not self.backend.start(self._actual_port):
            print('  ✗ 后端启动失败')
            return False

        if self.backend.wait_until_ready(self._actual_port, timeout=60):
            print('  ✓ 后端服务已就绪')
        else:
            print('  ✗ 后端启动超时')
            return False

        self._start_time = datetime.now()
        self.tray.show()
        self.tray.notify('OmniSpace AI', '服务已就绪，正在打开界面...')

        return True

    def _write_frontend_config(self) -> None:
        """前端配置注入（已废弃，保留为 no-op）。

        现行前端 frontend/index.html + frontend/src/api.js 采用同源相对路径自动探测
        （协议/主机/端口全部继承自页面地址），无需 Launcher 注入任何运行时配置；
        遗留 frontend/dist 双轨已剔除，本方法不再产生任何文件写入。
        """
        return

    def open_browser(self) -> None:
        """打开浏览器界面"""
        url = f'http://{self.config.backend_host}:{self._actual_port}'
        webbrowser.open(url)

    def get_status(self) -> str:
        """获取状态字符串"""
        uptime = ''
        if self._start_time:
            delta = datetime.now() - self._start_time
            hours = int(delta.total_seconds() // 3600)
            minutes = int((delta.total_seconds() % 3600) // 60)
            uptime = f'运行{hours}小时{minutes}分钟'
        return f'端口: {self._actual_port} | {uptime}'

    def _on_backend_status(self, status: str, message: str) -> None:
        """后端状态变化回调"""
        print(f'[后端] {status}: {message}')
        self.tray.notify('OmniSpace AI', message)

    def shutdown(self) -> None:
        """关闭Launcher"""
        print('\n正在关闭OmniSpace AI...')
        self.backend.stop()
        self._kill_comfyui_leftover()
        print('已关闭')

    def _kill_comfyui_leftover(self) -> None:
        """清理 ComfyUI 子进程残留（2026-08-31 治理，第三道防线）。

        后端侧已有 Job Object 共生死 + atexit 双保险（backend/
        services/inference/comfy_proc.py）；此处兜住外部手动实例或
        极端场景（job 绑定失败）。按 ComfyUI 端口查杀，严格校验
        cmdline 含 ComfyUI/main.py 且工作目录在本项目内——现有
        is_omnispace_process 匹配不到 ComfyUI（无 omnispace/
        backend.main 关键字），且后端被强杀时 atexit 不执行。
        """
        port = int(os.environ.get('OMNISPACE_COMFYUI_PORT', '8189'))
        try:
            proc = self.get_process_using_port(port)
            if proc is None:
                return
            cmdline = ' '.join(proc.cmdline()).lower()
            cwd = ''
            try:
                cwd = proc.cwd().lower()
            except Exception:
                pass
            project_marker = str(PROJECT_ROOT).lower()
            if 'comfyui/main.py' in cmdline and project_marker in cwd:
                proc.kill()
                print(f'已清理 ComfyUI 残留进程 (PID: {proc.pid})')
        except Exception as e:
            print(f'ComfyUI 残留清理跳过: {e}')

    def run(self) -> None:
        """运行Launcher主循环"""
        if not self.initialize():
            input('\n按回车键退出...')
            return

        self.open_browser()

        print('\nOmniSpace AI 已启动!')
        print(f'界面地址: http://{self.config.backend_host}:{self._actual_port}')
        print('按 Ctrl+C 退出')

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description='OmniSpace AI Launcher')
    parser.add_argument('--port', type=int, default=8765, help='后端端口')
    parser.add_argument('--no-browser', action='store_true', help='不自动打开浏览器')
    args = parser.parse_args()

    config = LauncherConfig(backend_port=args.port)
    launcher = Launcher(config)

    if args.no_browser:
        launcher.open_browser = lambda: None

    launcher.run()


if __name__ == '__main__':
    main()
