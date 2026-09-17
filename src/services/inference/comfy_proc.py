"""ComfyUI 子进程统一生命周期管理器（2026-08-31 用户问题治理）。

背景问题（两轮实测复现）：
  1. 后端停止后 ComfyUI 残留在进程列表——launcher 用 terminate()
     停后端，Windows 下等价强杀，atexit 不执行；且 comfy_paint_engine
     此前未注册 atexit，h3_engine 的 shutdown 也只认自己 spawn 的
     _proc（两引擎共用 8189 同一实例，交叉 spawn 时杀不到）。
  2. 退出漫剧模块使用其他功能时 ComfyUI 抢占资源——驱逐策略只
     POST /free（权重下卡保进程），ComfyUI 进程的 CUDA context
     （数百 MB 显存）+ torch 常驻（GB 级 RAM）仍然压着 16GB 卡。

治理方案（本模块 + 接线方）：
  - 单一所有者：spawn/shutdown 全走本管理器，谁起都能杀；
  - Windows Job Object（KILL_ON_JOB_CLOSE）：ComfyUI 绑入后端
    进程的 job——后端无论怎么死（强杀/崩溃/OOM），OS 关闭 job
    句柄时自动终结 ComfyUI，不依赖 atexit；
  - 空闲自动关闭：任务结束（mark_idle）后 idle_shutdown_seconds
    无新任务 → 主动杀进程（默认 300s，0=禁用）。ComfyUI 冷启动
    ~40s，空闲期驻留 GB 级内存不值；
  - atexit 注册兜底（正常解释器退出路径）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import atexit
import ctypes
import http.client
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from ..vram_policy import COMFY_IDLE_SHUTDOWN_S

logger = logging.getLogger("omnispace.inference.comfy_proc")

ROOT_DIR = Path(__file__).resolve().parents[3]
COMFY_DIR = ROOT_DIR / "tools" / "ComfyUI_windows_portable"
COMFY_PORT = int(os.environ.get("OMNISPACE_COMFYUI_PORT", "8189"))

# ComfyUI 可写状态统一收编进项目 data（2026-09-02 存储统一）：
# 产物/输入/临时/用户模板全部经启动参数重定向到 data/comfyui/，
# 引擎目录（tools/ComfyUI_windows_portable）保持只读——模型经硬链接
# 挂接本就同源，至此 ComfyUI 树内不再产生任何独立数据。
COMFY_DATA_DIR = ROOT_DIR / "data" / "comfyui"
COMFY_OUTPUT_DIR = COMFY_DATA_DIR / "output"
COMFY_INPUT_DIR = COMFY_DATA_DIR / "input"
COMFY_TEMP_DIR = COMFY_DATA_DIR / "temp"
COMFY_USER_DIR = COMFY_DATA_DIR / "user"

# 进程品牌化（2026-09-02）：任务管理器显示 OmniSpace-Engine.exe + logo，
# 不再是裸 python.exe（tools/brand_exe.py 生成；缺失回退原版解释器）
_COMFY_BRAND_EXE = COMFY_DIR / "python_embeded" / "OmniSpace-Engine.exe"

# 空闲关闭默认值（秒）；config.yaml comfyui.idle_shutdown_seconds 可覆盖
# （2026-09-10 批1 搬家 vram_policy 单源，本地名保留为别名）
DEFAULT_IDLE_SHUTDOWN_S = COMFY_IDLE_SHUTDOWN_S
# 空闲巡检间隔
_IDLE_CHECK_INTERVAL_S = 15.0


def _use_sage_attention() -> bool:
    """SageAttention V7 真内核开关（config comfyui.use_sage_attention，
    默认 true）。2026-09-12 A/B 实测多角色格提速 ~18%，PuLID 一致性
    目验无损；false 回退 SDPA。依赖 triton-windows + woct0rdho 真轮
    （缺失时 ComfyUI 侧会自行回落 SDPA，不会启动失败）。"""
    try:
        from src.config import get_config
        return bool((get_config().get("comfyui") or {}).get(
            "use_sage_attention", True))
    except Exception:  # noqa: BLE001 - 配置异常按开启处理
        # （真轮缺失时 ComfyUI 侧自行回落 SDPA，不会因此失败）
        return True


def _idle_shutdown_seconds() -> float:
    """读 config（缺失/异常回落默认值）。"""
    try:
        from src.config import get_config
        cfg = (get_config().get("comfyui") or {}).get(
            "idle_shutdown_seconds", DEFAULT_IDLE_SHUTDOWN_S)
        return max(0.0, float(cfg))
    except Exception:  # noqa: BLE001 - 配置异常不阻断生命周期
        return DEFAULT_IDLE_SHUTDOWN_S


# ── Windows Job Object（KILL_ON_JOB_CLOSE） ─────────────────────
# 后端死 → 内核回收本进程句柄 → job 关闭 → OS 终结 job 内 ComfyUI。
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),  # ULONG_PTR
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _bind_kill_on_close(proc: subprocess.Popen) -> bool:
    """把子进程绑入 KILL_ON_JOB_CLOSE job（仅 Windows）。"""
    if os.name != "nt":
        return False
    try:
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        if not k32.SetInformationJobObject(
                job, _JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info)):
            return False
        # _handle: CPython Popen 的原生进程句柄。绑定成功后 job 句柄
        # 由本进程持有且刻意不关闭——本进程退出（含被强杀）时 OS 自动
        # 关闭全部句柄，job 内 ComfyUI 随之终结。
        if not k32.AssignProcessToJobObject(job, int(proc._handle)):  # noqa: SLF001
            return False
        return True
    except Exception:  # noqa: BLE001 - job 绑定失败不影响功能（退回 atexit 兜底）
        logger.debug("ComfyUI Job Object 绑定失败，退回 atexit 兜底",
                     exc_info=True)
        return False


# ── 管理器本体 ───────────────────────────────────────────────────

def _mount_models_before_spawn() -> None:
    """拉起前模型挂接（2026-09-02 体验流定稿：拖入→启动→首启激活→即用）。

    挂接器与映射表随包在 scripts/comfy_link（包内落 modelxiazai），
    纯 stdlib，按文件路径加载不进 backend 包体系。时机铁律＝ComfyUI
    拉起前（进程内有模型列表缓存，运行中挂接不可见）；引擎每次冷启
    都重挂，天然覆盖「拖入/替换 models 后」的一切场景。失败只记日志
    绝不阻断启动——挂接缺位由各引擎既有的「模型不存在」报错兜底。
    """
    try:
        import importlib.util
        link_dir = next(
            (d for d in (ROOT_DIR / "scripts" / "comfy_link",
                         ROOT_DIR / "modelxiazai")
             if (d / "comfy_model_map.json").is_file()), None)
        if link_dir is None or not (link_dir / "comfy_mount.py").is_file():
            return
        spec = importlib.util.spec_from_file_location(
            "_omnispace_comfy_mount", link_dir / "comfy_mount.py")
        mod = importlib.util.module_from_spec(spec)
        # 先注册再 exec：comfy_mount 的 dataclass 在字符串注解下按模块名
        # 查 sys.modules，未注册会在导入期崩（被 except 吞成"挂接跳过"）
        import sys
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        report = mod.ensure_mounted(
            comfy_models=COMFY_DIR / "ComfyUI" / "models")
        yaml_path = COMFY_DATA_DIR / "extra_model_paths.yaml"
        if report.total:
            logger.info("模型挂接：%s", report.summary_line())
            for line in report.details[:8]:
                logger.info("模型挂接明细：%s", line)
            if report.cross_volume:
                # 2b 跨卷降级（引擎只读红线不破：yaml 落 data，经
                # --extra-model-paths-config 指入，cli_args.py:71 原生支持）
                fr = mod.cross_volume_fallback(
                    comfy_models=COMFY_DIR / "ComfyUI" / "models",
                    yaml_path=yaml_path)
                logger.warning(
                    "模型与引擎不在同一磁盘，已启用跨盘降级：%s "
                    "（最佳体验仍是把 models 移到软件所在盘，同盘移动秒完成）",
                    fr.summary_line())
                for rel in fr.unresolvable[:5]:
                    logger.warning("跨盘无解件（请移到同盘）：%s", rel)
            elif yaml_path.is_file():
                # 已回到同卷（用户移盘后）：清降级 yaml 回硬链正轨
                try:
                    yaml_path.unlink()
                    logger.info("检测到模型与引擎同盘，已撤销跨盘降级 yaml")
                except OSError:
                    pass
    except Exception:  # noqa: BLE001 - 挂接失败不阻断 ComfyUI 启动
        logger.warning("模型挂接跳过（不影响启动）", exc_info=True)


class ComfyProcManager:
    """ComfyUI 子进程唯一所有者（进程/空闲计时；探活归各引擎 HTTP 层）。"""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._log_fp = None
        self._lock = threading.Lock()
        self._busy_count = 0
        self._last_activity = time.monotonic()
        self._idle_thread: threading.Thread | None = None
        self._exited = False

    # ── 生成期活动标记（空闲计时的忙碌保护） ──────────────────────

    def mark_busy(self) -> None:
        """生成任务开始：空闲关闭暂停（可重入，多任务计数）。"""
        with self._lock:
            self._busy_count += 1
            self._last_activity = time.monotonic()

    def mark_idle(self) -> None:
        """生成任务结束：重置空闲计时起点。"""
        with self._lock:
            self._busy_count = max(0, self._busy_count - 1)
            self._last_activity = time.monotonic()

    # ── 进程生命周期 ──────────────────────────────────────────────

    def _reap_orphans(self) -> int:
        """清扫上次会话遗留的 ComfyUI 孤儿（F-5，2026-09-10 严格测试
        实测：后端崩溃后 ComfyUI 残留占 8189 端口+数百 MB 显存，冷启动
        收养探测失败后 spawn 端口冲突——对齐 vllm_service 双匹配防御：
        exe ∈ 本仓 embeded python/品牌 exe 且命令行含 ComfyUI/main.py）。
        返回清扫数；失败不阻断 spawn。
        """
        try:
            import psutil
        except ImportError:
            return 0
        raw_py = str(COMFY_DIR / "python_embeded" / "python.exe").lower()
        targets = {raw_py, str(_COMFY_BRAND_EXE).lower()}
        marker = "comfyui/main.py"
        killed = 0
        for p in psutil.process_iter(["pid", "exe", "cmdline"]):
            try:
                exe = (p.info["exe"] or "").lower()
                cmdline = " ".join(p.info["cmdline"] or []).lower()
                if (exe in targets and marker in cmdline
                        and p.info["pid"] != os.getpid()):
                    logger.warning("清扫 ComfyUI 孤儿进程 pid=%d", p.info["pid"])
                    subprocess.run(
                        ["taskkill", "/T", "/F", "/PID", str(p.info["pid"])],
                        capture_output=True, check=False, timeout=10,
                        creationflags=subprocess.CREATE_NO_WINDOW
                        if os.name == "nt" else 0)
                    killed += 1
            except Exception:  # noqa: BLE001 - 单进程探测失败继续
                continue
        return killed

    def spawn(self, log_name: str) -> subprocess.Popen:
        """冷启动 ComfyUI 子进程（幂等：已在运行直接返回现有 Popen）。

        log_name 由调用引擎指定（comfyui_paint.log / comfyui_h3.log
        ——两引擎共用同一进程实例，日志文件以首起方为准）。
        """
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._last_activity = time.monotonic()
                return self._proc
            # 孤儿收账（F-5）：本管理器无在管进程但端口可能被上次会话
            # 遗留占用——spawn 前清扫（在管进程存活时不进来，无误杀面）
            try:
                if self._reap_orphans():
                    time.sleep(1.0)  # 端口释放窗口
            except Exception as exc:  # noqa: BLE001 - 收账失败不阻断
                logger.warning("ComfyUI 孤儿清扫异常（继续 spawn）: %s", exc)
            logs_dir = ROOT_DIR / "logs"
            logs_dir.mkdir(exist_ok=True)
            # 审计 P2-3（2026-09-12）：重开前先关旧日志句柄——进程崩溃
            # 路径未经 stop() 换手时旧 fp 会泄漏（句柄累积）
            _old_fp, self._log_fp = self._log_fp, None
            if _old_fp is not None and not _old_fp.closed:
                _old_fp.close()
            # 追加式日志轮转（2026-09-15 审计修复）：mtime 恒新使 30 天
            # 清理够不着——超 10MB 落 .1（保一份旧）
            _log_path = logs_dir / log_name
            try:
                if (_log_path.is_file()
                        and _log_path.stat().st_size > 10 * 1024 * 1024):
                    _log_path.replace(_log_path.with_name(_log_path.name + ".1"))
            except OSError:
                pass
            self._log_fp = open(_log_path, "ab")
            for d in (COMFY_OUTPUT_DIR, COMFY_INPUT_DIR,
                      COMFY_TEMP_DIR, COMFY_USER_DIR):
                d.mkdir(parents=True, exist_ok=True)
            # 拉起前模型挂接（即插即用主时机，见 _mount_models_before_spawn）
            _mount_models_before_spawn()
            comfy_py = (_COMFY_BRAND_EXE if _COMFY_BRAND_EXE.is_file()
                        else COMFY_DIR / "python_embeded" / "python.exe")
            cmd = [str(comfy_py), "-s", "ComfyUI/main.py",
                   "--windows-standalone-build",
                   # SageAttention V7 真内核（config comfyui.use_sage_attention
                   # 可关；进程级：H3/绘画共实例同时生效）
                   * (["--use-sage-attention"] if _use_sage_attention() else []),
                   "--deterministic",  # 绘画 PuLID 身份锁链路要求可复现
                                      # （对 H3 共实例仅微弱代价，产物无影响）
                   "--listen", "127.0.0.1", "--port", str(COMFY_PORT),
                   # 可写目录重定向（存储统一，见 COMFY_DATA_DIR 注释）
                   "--output-directory", str(COMFY_OUTPUT_DIR),
                   "--input-directory", str(COMFY_INPUT_DIR),
                   "--temp-directory", str(COMFY_TEMP_DIR),
                   "--user-directory", str(COMFY_USER_DIR)]
            # 跨盘降级（2b）：存在降级 yaml 则指入（引擎原生支持多份）
            extra_yaml = COMFY_DATA_DIR / "extra_model_paths.yaml"
            if extra_yaml.is_file():
                cmd += ["--extra-model-paths-config", str(extra_yaml)]
            env = dict(os.environ)
            env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            env.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
            # runtime/ffmpeg 前置（2026-08-30 P0：防宿主 Electron 阉割版
            # ffmpeg 探测能过、执行报 Option not found）
            ff_bin = ROOT_DIR / "runtime" / "ffmpeg" / "bin"
            if ff_bin.is_dir():
                env["PATH"] = str(ff_bin) + os.pathsep + env.get("PATH", "")
            # 多卡绑卡（批1 多卡地基 2026-09-05）：ComfyUI 按资源域分配
            # 固定卡（paint/video_gen 同卡由 gpu_domains 归一化保证——
            # 二者共用本实例）。显式设 CUDA_VISIBLE_DEVICES 即接管
            # ComfyUI main.py 在 Windows 的「未指定强制 0」默认；子进程
            # 内该卡就是 cuda:0，不再传 --cuda-device（避免双重映射错位）。
            try:
                from src.engines.gpu_domains import resolve_feature_device
                env["CUDA_VISIBLE_DEVICES"] = str(
                    resolve_feature_device("paint"))
                logger.info("ComfyUI 绑卡: CUDA_VISIBLE_DEVICES=%s",
                            env["CUDA_VISIBLE_DEVICES"])
            except Exception:  # noqa: BLE001 - 分配失败回落 ComfyUI 默认
                pass
            proc = subprocess.Popen(
                cmd, cwd=str(COMFY_DIR), stdout=self._log_fp,
                stderr=subprocess.STDOUT,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
                env=env)
            self._proc = proc
            self._last_activity = time.monotonic()
            # 共生死绑定（强杀/崩溃后端也不残留）
            bound = _bind_kill_on_close(proc)
            logger.info("ComfyUI 子进程已启动 (pid=%s, port=%s, job=%s)",
                        proc.pid, COMFY_PORT, "bound" if bound else "atexit-only")
            self._ensure_idle_thread()
            return proc

    def poll(self) -> int | None:
        """当前子进程退出码（None=在运行/从未启动）。"""
        proc = self._proc
        return None if proc is None else proc.poll()

    def pid(self) -> int | None:
        proc = self._proc
        return None if proc is None else proc.pid

    def shutdown(self) -> None:
        """终止 ComfyUI 子进程树（幂等；外部手动起的实例不在此列）。"""
        with self._lock:
            proc, self._proc = self._proc, None
            log_fp, self._log_fp = self._log_fp, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW
                    if os.name == "nt" else 0)
                try:
                    proc.wait(timeout=10.0)
                except subprocess.TimeoutExpired:  # noqa: PERF203 - 兜底
                    pass
            logger.info("ComfyUI 子进程已终止 (pid=%s)", proc.pid)
        finally:
            if log_fp is not None:
                log_fp.close()
            self._sweep_engine_placeholders()

    @staticmethod
    def _sweep_engine_placeholders() -> None:
        """清掉 ComfyUI 在引擎树重建的空壳占位目录（存储统一自愈）。

        ComfyUI 核心在 folder_paths.py 导入期（先于 main.py 应用
        --input-directory）无条件 makedirs 引擎树默认 input/，故每次
        启动必现 0 字节空壳；所有真实读写均走重定向后的 data/comfyui，
        空壳永不收文件。此处仅 os.rmdir（非空自动失败跳过），若未来
        ComfyUI 升级后空壳内出现文件，说明重定向被绕过——留给审计拦截。
        """
        base = COMFY_DIR / "ComfyUI"
        for name in ("output", "input", "temp", "user"):
            try:
                os.rmdir(base / name)  # 仅空目录可删，非空抛 OSError 跳过
            except OSError:
                pass

    # ── 空闲自动关闭 ──────────────────────────────────────────────

    def _ensure_idle_thread(self) -> None:
        if self._idle_thread is not None and self._idle_thread.is_alive():
            return
        self._idle_thread = threading.Thread(
            target=self._idle_loop, name="comfy-idle-watch", daemon=True)
        self._idle_thread.start()

    def _queue_has_work(self) -> bool:
        """ComfyUI 实际队列探查（running+pending 非空 = 有活在跑）。

        空闲关闭的事实判据（2026-08-31 事故）：H3 链式单个 prompt 运行
        >300s，引擎侧漏打忙碌标记时纯计时判定把在跑任务当空闲杀掉
        （链式中断报错）。HTTP 仅指向字面量回环主机；探测失败按无任务
        处理，不改变既有计时行为。
        """
        try:
            conn = http.client.HTTPConnection("127.0.0.1", COMFY_PORT,
                                              timeout=1.5)
            try:
                conn.request("GET", "/queue")
                resp = conn.getresponse()
                body = json.loads(resp.read().decode("utf-8", "replace"))
            finally:
                conn.close()
            return bool(body.get("queue_running") or body.get("queue_pending"))
        except Exception:  # noqa: BLE001 - 探测失败视为无任务
            return False

    def _generation_active(self) -> bool:
        """本地重型生成是否活跃（批3 空闲豁免，2026-09-10）。

        判据：功能锁持有 paint/video_gen，或忙碌登记簿有本地
        paint/video_gen 任务（两条队列批2 起在准入后登记）。活跃时
        空闲计时冻结——任务间隙 >idle 上限时不再把热引擎杀掉吃
        ~40s 冷启动（keep_loaded 接力的保护网）。
        """
        try:
            from ...middleware.feature_lock import get_feature_lock

            if get_feature_lock().active_feature in ("paint", "video_gen"):
                return True
        except Exception:  # noqa: BLE001 - 锁查询失败交由登记簿判定
            pass
        try:
            from .gpu_budget import get_busy_registry

            registry = get_busy_registry()
            return registry.is_busy("paint") or registry.is_busy("video_gen")
        except Exception:  # noqa: BLE001 - 登记簿不可用按不活跃（原行为）
            return False

    def _idle_loop(self) -> None:
        """空闲巡检：无任务且超时 → 杀进程（释放 CUDA context + RAM）。"""
        while not self._exited:
            time.sleep(_IDLE_CHECK_INTERVAL_S)
            if self._exited or self._proc is None:
                continue
            limit = _idle_shutdown_seconds()
            if limit <= 0:
                continue  # 配置禁用（常驻热启动）
            if self._generation_active():
                # 批3 空闲豁免：重型生成活跃（锁/登记簿）→ 冻结计时，
                # 下一轮再看（不影响原 busy_count 判定语义）
                with self._lock:
                    self._last_activity = time.monotonic()
                continue
            with self._lock:
                idle_for = time.monotonic() - self._last_activity
                busy = self._busy_count > 0
            if busy or self._proc is None or idle_for < limit:
                continue
            if self.poll() is not None:
                continue
            # 队列事实判据：计时到线但 ComfyUI 队列仍有活在跑（引擎漏打
            # 忙碌标记 / 超长单 prompt）→ 重置计时下轮再看，绝不误杀
            if self._queue_has_work():
                with self._lock:
                    self._last_activity = time.monotonic()
                continue
            logger.info("ComfyUI 空闲 %.0fs（>=%ds 上限）自动关闭",
                        idle_for, int(limit))
            self.shutdown()
            return


_manager: ComfyProcManager | None = None
_manager_lock = threading.Lock()


def get_comfy_proc() -> ComfyProcManager:
    """进程管理器单例（atexit 只注册一次）。"""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = ComfyProcManager()
            atexit.register(_manager.shutdown)
        return _manager
