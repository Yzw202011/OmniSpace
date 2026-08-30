"""vLLM 推理子进程服务（Windows 社区构建，方案A：独立进程 + OpenAI 兼容 API）。

架构（2026-08-21 用户裁定采用 vLLM 集成）：
  - vLLM 运行于 runtime/py313 独立嵌入式 Python（vllm-windows-build
    v0.26.0+cu128，官方 vLLM 不支持 Windows 原生）
  - 以 OpenAI 兼容 HTTP 服务（127.0.0.1:8101）提供推理，后端通过
    requests 调用；不与主进程（py310 + transformers）共享 CUDA 上下文
  - 显存由 vLLM gpu_memory_utilization 预算控制（默认 0.85，16GB 卡
    留 ~2.4GB 给系统/bge 常驻小模型；对话模块激活时其他模块已释放）
  - 模块切换 3 秒资源释放（release_for_module）：杀子进程即秒级回收
    全部显存，无引擎引用清理问题

安全约束：
  - start() 幂等：已在运行直接返回 True
  - stop() 强杀语义：terminate → 超时 kill，确保显存回收（用户操作
    最高权限，与训练取消按钮同一裁定）
  - 子进程 stdout/stderr 落 logs/vllm-server.log 供诊断

生命周期（P3 §3.2 常驻热备 + 按需冷启双策略）：
  - 常驻热备：非重显存模块切换时保留 vLLM 进程（复用已加载 worker），
    切回对话免二次 ~157s 冷启动；is_booting() 供前端"模型加载中"
  - 按需冷启：重模块（绘画/视频/训练）切换按需终止供其显存；
    首个对话/视觉请求经 start_async()/warmup 后台无阻塞预冷
  - 健康轮询 5s、启动超时 240s（本项目收敛值）
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

log = logging.getLogger("omnispace.vllm")

# ── 路径常量（项目根 = backend/ 上级） ───────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
PY313_EXE = _PROJECT_ROOT / "runtime" / "py313" / "python.exe"
VLLM_LOG = _PROJECT_ROOT / "logs" / "vllm-server.log"
MODELS_DIR = _PROJECT_ROOT / "models"

# 默认服务模型：Qwen3-VL-8B AWQ int4（权重 ~6GB，16GB 卡可承载）
DEFAULT_MODEL_REL = "qwen3-vl-8b-awq"
# served-model-name 默认值（实际随所载模型目录动态变化，多模型热切换）
SERVED_NAME = "qwen3-vl-8b-awq"

VLLM_HOST = "127.0.0.1"
VLLM_PORT = 8101
HEALTH_URL = f"http://{VLLM_HOST}:{VLLM_PORT}/health"
CHAT_URL = f"http://{VLLM_HOST}:{VLLM_PORT}/v1/chat/completions"
# 生成期显存协商 dev 路由（VLLM_SERVER_DEV_MODE=1 时挂载）
SLEEP_URL = f"http://{VLLM_HOST}:{VLLM_PORT}/sleep"
WAKE_URL = f"http://{VLLM_HOST}:{VLLM_PORT}/wake_up"
SLEEP_STATUS_URL = f"http://{VLLM_HOST}:{VLLM_PORT}/is_sleeping"

# 启动健康轮询：模型加载 + CUDA 图编译可能耗时，预算放宽
# P3 §3.2 收敛：超时 300s → 240s（低于 WARMUP_TIMEOUT 的预算值，
# 去掉冗余余量），健康轮询 2s → 5s（冷启动期无意义高频探测省 I/O；
# 取消自查最坏延迟仅增 3s，远低于 3s 模块切换预算的外部感知）。
START_TIMEOUT_S = 240.0
HEALTH_POLL_INTERVAL_S = 5.0
# 单次 socket 读超时。2026-08-21 实测教训：首条带图请求触发视觉内核
# Triton JIT 编译（_bilinear_pos_embed_kernel/rotary_kernel），编译期间
# 零 chunk 产出，120s 会误杀 → 提至 180s 兜底（start() 预热消除常态命中）
STREAM_READ_TIMEOUT_S = 180.0
# 启动预热预算：视觉内核 JIT 编译在预热请求内完成（start() 阻塞窗口）
WARMUP_TIMEOUT_S = 240.0


class VLLMService:
    """vLLM OpenAI 兼容服务子进程生命周期管理（进程内单例）。

    线程模型：start/stop 持锁串行；chat_stream 无锁（HTTP 调用，
    vLLM 服务端自身做连续批处理，多请求并发安全）。
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._started_at: float = 0.0
        self._model_dir: str = ""
        # 当前服务的 served-model-name（= 模型目录名，随热切换更新）
        self._served_name: str = ""
        self._last_error: str = ""
        self._log_fh: io.TextIOWrapper | None = None
        # Windows fallback 停止旗标（2026-08-27）：sleep_for_paint 404
        # 停子进程让渡显存后置位，wake_from_paint 后台重启并复位
        self._stopped_for_paint: bool = False
        # 取消标志（2026-08-22 锁竞争修复）：stop() 无权排队等待
        # start() 持有的锁（健康轮询+预热全程持锁，最长 ~300s），
        # 先无锁立此标志，start() 轮询循环自查自杀
        self._cancel_requested: bool = False
        # P3 §3.2 常驻热备：无阻塞 fire-and-forget 后台启动状态
        # （is_booting 供前端"模型加载中"；去重由 _booting_lock 守卫）
        self._booting: bool = False
        self._booting_lock = threading.Lock()

    # ── 状态查询 ──────────────────────────────────────────────

    def runtime_ready(self) -> bool:
        """py313 运行时 + vllm 包是否就绪（未装好时前端可提示）。"""
        return PY313_EXE.is_file()

    def is_running(self) -> bool:
        """子进程是否存活（不保证 health 就绪）。"""
        return self._proc is not None and self._proc.poll() is None

    def is_booting(self) -> bool:
        """是否正在后台无阻塞启动（P3 常驻热备：前端"模型加载中"）。"""
        return self._booting

    def is_healthy(self) -> bool:
        """服务是否可推理（进程存活 + /health 200）。"""
        if not self.is_running():
            return False
        try:
            import requests
            resp = requests.get(HEALTH_URL, timeout=3.0)
            return resp.status_code == 200
        except Exception:  # noqa: BLE001 - 健康探测失败按未就绪
            return False

    def status(self) -> dict[str, Any]:
        """供 /models/vllm/status 全景状态。"""
        return {
            "runtime_installed": self.runtime_ready(),
            "running": self.is_running(),
            "healthy": self.is_healthy(),
            "booting": self.is_booting(),
            # ADR-003 P3：睡眠/让渡态纳入可观测面（sleeping 探测 best-effort，
            # 服务不可达为 None；stopped_for_paint 为 Windows fallback 让渡旗标）
            "stopped_for_paint": self._stopped_for_paint,
            "sleeping": self._is_sleeping() if self.is_healthy() else None,
            "pid": self._proc.pid if self._proc else None,
            "model_dir": self._model_dir,
            "served_name": self._served_name,
            "port": VLLM_PORT,
            "uptime_s": round(time.time() - self._started_at, 1)
            if self._started_at else 0.0,
            "last_error": self._last_error,
        }

    @property
    def served_name(self) -> str:
        """当前服务的模型名（= 模型目录名；未运行/已终止为空串）。"""
        return self._served_name

    @property
    def stopped_for_paint(self) -> bool:
        """是否处于「停进程让渡显存」睡眠态（Windows fallback，wake 后自愈）。"""
        return self._stopped_for_paint

    def _transition(self, to_state: str, reason: str) -> None:
        """确定性状态迁移日志（ADR-003 P3 验收②）。

        统一 grep 前缀「vLLM 状态:」——启动/取消/睡眠/唤醒/终止全链
        可从 backend.log 单一关键词回放，消除「启动中→取消」不可观测态。
        to_state 取值 = base_engine.EngineState 的 value 子集。
        """
        log.info("vLLM 状态: -> %s (%s)", to_state, reason)

    # ── 生命周期 ──────────────────────────────────────────────

    def _is_sleeping(self) -> bool | None:
        """查询睡眠态；None = 查询失败（服务不可达）。"""
        try:
            import requests
            r = requests.get(SLEEP_STATUS_URL, timeout=5.0)
            if r.status_code == 200:
                return bool(r.json().get("is_sleeping"))
        except Exception:  # noqa: BLE001
            pass
        return None

    def sleep_for_paint(self, settle_timeout_s: float = 90.0) -> bool:
        """生成期显存协商：vLLM 权重卸到 CPU RAM（sleep level 1）。

        互斥矩阵（OmniChat ↔ OmniDraw）的运行时落地：关键帧/绘画
        生成入口在持 paint 锁后调用，把 vLLM 占用的 ~7GB 显存让给
        FLUX 采样；生成结束调 wake_from_paint 恢复。

        Windows fallback（2026-08-27）：vLLM 0.26.0 的 sleep mode
        在 Windows 不可用（--enable-sleep-mode 启动校验读
        /proc/self/maps 崩溃），/sleep 路由 404 → 停子进程让渡
        全部显存（权重+KV），wake 时后台重启（冷启动 ~157s，
        生成期 5min 场景可接受）。

        协商是 best-effort：未运行/未就绪/已在睡眠均返回 True；
        失败只记日志不抛异常——显存不足由 paint 引擎降级链兜底，
        不让协商故障阻断生成链路。sleep 命令发出后轮询 is_sleeping
        等卸载真正完成（权重 GPU→CPU 拷贝需数秒）。
        """
        if not self.is_healthy():
            if self.is_running() or self.is_booting():
                # booting 盲区修补（2026-08-28 V77 事故）：进程已
                # Popen 但 health 未通（权重装载中，最长 240s）——
                # 继续装载与在途生成争抢 GPU 算力/显存/RAM（V77 实
                # 测并发 240s 触发质量总督深度降步），停掉让渡；
                # 生成结束后 wake_from_paint 后台重启恢复对话能力
                log.info("vLLM 启动中（未就绪），停止让渡显存（booting 分支）")
                self._stopped_for_paint = True
                self.stop()
            return True
        if self._is_sleeping() is True:
            return True  # 幂等：已在睡眠
        try:
            import requests
            resp = requests.post(f"{SLEEP_URL}?level=1", timeout=30.0)
        except Exception as e:  # noqa: BLE001
            log.warning("vLLM sleep 请求失败（不阻断生成）: %s", e)
            return False
        if resp.status_code == 404:
            # Windows fallback：无 sleep mode 路由 → 停子进程
            # （taskkill /T 进程树，显存秒级回收）
            self._stopped_for_paint = True
            if self.stop():
                self._transition("sleeping", "windows fallback stopped_for_paint")
                log.info("vLLM 已停止让渡显存（Windows fallback，"
                         "生成结束后后台重启）")
                return True
            log.warning("vLLM 停止失败（不阻断生成，交由降级链兜底）")
            return False
        if resp.status_code != 200:
            log.warning("vLLM sleep 响应 %s（不阻断生成）",
                        resp.status_code)
            return False
        deadline = time.time() + settle_timeout_s
        while time.time() < deadline:
            st = self._is_sleeping()
            if st is True:
                self._transition("sleeping", "sleep level1 settled (weights -> RAM)")
                log.info("vLLM 已睡眠（权重卸至 RAM，显存已让渡）")
                return True
            if st is None:
                return False  # 服务在卸载中途失联，交由上层兜底
            time.sleep(1.0)
        log.warning("vLLM sleep 结算超时 %.0fs（不阻断生成）",
                    settle_timeout_s)
        return False

    def _restart_after_paint_bg(self) -> None:
        """Windows fallback 收尾：后台重启 vLLM（同模型目录）。

        独立线程执行——冷启动健康轮询 ~157s 不能阻塞生成 API 的
        finally 收尾；期间 chat 请求由 vllm dialog 端点的失败自愈
        /下次 start() 幂等拉起兜底。
        """
        try:
            ok = self.start(model_dir=self._model_dir or None)
            log.info("vLLM 生成后重启%s", "成功" if ok else "失败")
        except Exception as e:  # noqa: BLE001
            log.warning("vLLM 生成后重启异常（看门狗自愈兜底）: %s", e)

    def wake_from_paint(self, settle_timeout_s: float = 120.0) -> bool:
        """生成期显存协商收尾：唤醒 vLLM（权重 RAM→GPU）。

        best-effort 同 sleep_for_paint；唤醒需重建 KV cache，预算
        放宽到 120s。sleep 中途引擎崩溃等极端场景返回 False，由
        vLLM 看门狗/下次 start() 自愈。Windows fallback 停掉的
        实例在此后台重启。
        """
        if self._stopped_for_paint:
            self._stopped_for_paint = False
            self._transition("booting", "wake: background restart after paint")
            threading.Thread(
                target=self._restart_after_paint_bg,
                name="vllm-restart-after-paint", daemon=True,
            ).start()
            return True
        if not self.is_healthy():
            return True  # 未运行无谓唤醒
        if self._is_sleeping() is False:
            return True  # 幂等：已醒
        try:
            import requests
            resp = requests.post(WAKE_URL, timeout=60.0)
            if resp.status_code != 200:
                log.warning("vLLM wake_up 响应 %s", resp.status_code)
                return False
        except Exception as e:  # noqa: BLE001
            log.warning("vLLM wake_up 请求失败: %s", e)
            return False
        deadline = time.time() + settle_timeout_s
        while time.time() < deadline:
            st = self._is_sleeping()
            if st is False:
                self._transition("ready", "wake_up settled (weights -> GPU)")
                log.info("vLLM 已唤醒（权重回 GPU，对话能力恢复）")
                return True
            if st is None:
                return False
            time.sleep(1.0)
        log.warning("vLLM wake_up 结算超时 %.0fs", settle_timeout_s)
        return False

    def start(
        self,
        model_dir: str | Path | None = None,
        gpu_memory_utilization: float = 0.85,
        max_model_len: int = 8192,
        startup_timeout_s: float = START_TIMEOUT_S,
    ) -> bool:
        """启动 vLLM OpenAI 服务子进程并等待健康就绪。

        Args:
            model_dir: 模型目录绝对路径（AWQ int4 布局）；None 用默认
            gpu_memory_utilization: vLLM 显存预算比例（整卡占比上限，
                2026-08-21 实测 0.55 时 8GB 权重装载后 KV 仅剩 0.99GB
                < 8K 上下文所需 1.12GB 启动失败；对话模块激活时其他
                模块已按 release_for_module 释放，0.85 = 16GB 卡留
                ~2.4GB 给系统/bge 常驻，KV 可用 ~5GB）
            max_model_len: 最大上下文（性能指标：8K 满上下文）
            startup_timeout_s: 健康轮询预算（权重加载+图编译）

        Returns:
            True 服务就绪；False 失败（原因见 last_error，进程已回收）
        """
        # 功能锁门禁（2026-08-28 V77 事故根修）：paint/video_gen/
        # training 持锁期间禁止启动 vLLM——冷启动全程（最长 240s）
        # 与在途生成争抢 GPU 算力/显存/RAM，触发质量总督深度降步
        #（36→14 步，V77 shot3 棕发/shot4 3D 化画质崩坏）。一致性
        # 守卫在锁释放后才点火（keyframe 端点 finally 先释锁再
        # create_task），对话请求自身持 dialog 锁，均不受影响。
        try:
            from ..middleware.feature_lock import get_feature_lock
            _active = get_feature_lock().active_feature
        except Exception:  # noqa: BLE001 - 锁探测失败不阻断启动
            _active = None
        if _active in ("paint", "video_gen", "training"):
            self._last_error = f"功能锁占用中（{_active}），vLLM 延迟启动"
            log.info("vLLM 启动暂缓：%s 功能锁持有中，生成结束后再启动",
                     _active)
            return False
        # 显存准入闸门（2026-08-29 E2E 压测修复）：两次后端进程静默
        # 死亡（无 Python traceback，原生层崩溃特征）均发生于显存
        # ≥97% 时启动 vLLM——0.85 util 对整卡硬预分配，空闲不足即推
        # 过 100% 触发原生崩溃。设备级空闲 < 预分配量 → 诚实拒绝
        #（dialog_engine 有 transformers 4B 诚实降级链兜底）。
        try:
            import torch as _torch
            if _torch.cuda.is_available():
                _free_b, _total_b = _torch.cuda.mem_get_info(0)
                _need_b = int(_total_b * gpu_memory_utilization * 0.98)
                if _free_b < _need_b:
                    self._last_error = (
                        f"设备空闲显存 {_free_b / 2 ** 30:.1f}GB 不足以"
                        f"安全启动 vLLM（需预分配 {_need_b / 2 ** 30:.1f}GB，"
                        f"util={gpu_memory_utilization}）；请等待在途生成"
                        "结束或卸载驻留模型后重试")
                    log.warning("vLLM 启动被显存准入闸门拒绝: %s",
                                self._last_error)
                    return False
        except Exception:  # noqa: BLE001 - 探测失败保持旧行为
            pass
        with self._lock:
            self._last_error = ""
            self._cancel_requested = False  # 新一轮启动，清除历史取消
            mdir = Path(model_dir) if model_dir else MODELS_DIR / DEFAULT_MODEL_REL
            mdir_str = str(mdir)

            if self.is_healthy():
                if self._model_dir == mdir_str:
                    return True  # 幂等：同模型已在服务
                # 多模型热切换（2026-08-21）：healthy 但请求的是不同模型
                # → 杀进程回收显存 → 换目录重启（16GB 卡按需换载核心路径）
                log.info("vLLM 热切换: %s -> %s（杀进程重启）",
                         self._model_dir, mdir_str)
                try:  # 大白话事件：vLLM 热切换
                    from ..services.event_log import log_event
                    log_event(
                        "vllm", "hot_switch",
                        f"AI 推理引擎正在换模型：从「{Path(self._model_dir).name}」"
                        f"换成「{mdir.name}」。要先完全关掉旧引擎再启动新的，"
                        "大约需要 1 分钟，请稍等",
                        level="info",
                        detail=f"from={self._model_dir}, to={mdir_str}")
                except Exception:  # noqa: BLE001
                    pass
                if not self._kill_locked():
                    self._last_error = "热切换失败：旧 vLLM 子进程无法终止"
                    return False
            elif self.is_running():
                self._last_error = (
                    "vLLM 子进程存活但健康检查未就绪（可能仍在加载模型），"
                    f"请稍后重试或查看 {VLLM_LOG}")
                return False

            # 冷启动前清扫上次会话孤儿（后端崩溃重启场景：py313
            # EngineCore 孤儿占满显存会导致新服务无法加载权重）
            self._reap_orphans()

            if not self.runtime_ready():
                self._last_error = (
                    f"vLLM 运行时未安装: {PY313_EXE}（需 runtime/py313 + "
                    "vllm-windows wheel，见 backend/engines/vllm_service.py 头注）")
                log.warning(self._last_error)
                return False

            mdir = Path(mdir_str)
            if not (mdir / "config.json").is_file():
                self._last_error = f"模型目录未就绪: {mdir}"
                log.warning(self._last_error)
                return False

            # served-model-name = 模型目录名（热切换后请求方按此路由）
            served_name = mdir.name
            cmd = [
                str(PY313_EXE), "-m", "vllm.entrypoints.openai.api_server",
                "--model", str(mdir),
                "--served-model-name", served_name,
                "--host", VLLM_HOST,
                "--port", str(VLLM_PORT),
                "--dtype", "float16",
                "--max-model-len", str(max_model_len),
                "--gpu-memory-utilization", str(gpu_memory_utilization),
                "--enable-prefix-caching",
                # sleep mode 已回退（2026-08-27）：vLLM 0.26.0 Windows
                # 兼容 bug——--enable-sleep-mode 启动校验走 cumem
                # allocator 探测（find_loaded_library 读 /proc/self/maps，
                # Linux 专属路径）→ FileNotFoundError 子进程直接崩溃。
                # 9B 关键帧改 GGUF Q4_K_M（5.5GB GPU 常驻）后激活空间
                # 充足，1280×720 原生采样不再依赖 vLLM 让渡；协商端点
                # 保留为 best-effort（路由 404 → warning 不阻断）。
                "--no-enable-log-requests",
                "--seed", "42",
            ]
            # DeepSeek R1 系（2026-08-25 接入 W4A16）：reasoning parser
            # 把 <think>...</think> 推理段分离到 delta.reasoning_content
            # ——chat_stream 只读 delta.content，思考过程天然剥离不进
            # 前端正文（parser 与模型架构无关，仅文本层解析 <think> 标签）
            if "deepseek-r1" in mdir.name.lower():
                cmd += ["--reasoning-parser", "deepseek_r1"]
            env = os.environ.copy()
            # Windows 控制台默认 GBK，vLLM banner 含 unicode 块字符会
            # UnicodeEncodeError 丢日志（日志文件亦按此编码写）
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            # vllm-windows-build wiki 要求：单机回环初始化
            env["VLLM_HOST_IP"] = VLLM_HOST
            # 本地权重离线加载（防止联网探测 HF）
            env["HF_HUB_OFFLINE"] = "1"
            env["TRANSFORMERS_OFFLINE"] = "1"
            # wiki 明示：Windows 构建不支持 expandable_segments，勿设置
            # Windows MAX_PATH=260：默认用户目录下 inductor 编译缓存
            # （.cache/vllm/torch_compile_cache/<64位hash>/inductor_cache/...）
            # 超长导致 FileNotFoundError(WinError 3)。vLLM 自管缓存根
            # （会覆盖 TORCHINDUCTOR_CACHE_DIR），须设 VLLM_CACHE_ROOT 短路径
            cache_root = _PROJECT_ROOT / ".cache" / "vllm"
            cache_root.mkdir(parents=True, exist_ok=True)
            env["VLLM_CACHE_ROOT"] = str(cache_root)
            env["TRITON_CACHE_DIR"] = str(_PROJECT_ROOT / ".cache" / "triton")

            VLLM_LOG.parent.mkdir(parents=True, exist_ok=True)
            self._log_fh = open(  # noqa: SIM115 - 生命周期随进程关闭
                VLLM_LOG, "a", encoding="utf-8", buffering=1)

            self._transition("booting", f"cold start {mdir.name}")
            log.info("启动 vLLM 子进程: %s", " ".join(cmd))
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=self._log_fh,
                    stderr=subprocess.STDOUT,
                    cwd=str(_PROJECT_ROOT),
                    env=env,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"vLLM 子进程启动失败: {exc}"
                log.exception(self._last_error)
                self._transition("error", "popen_failed")
                self._close_log()
                return False

            self._started_at = time.time()
            self._model_dir = str(mdir)
            self._served_name = served_name

            # 健康轮询（持锁阻塞：调用方 expect start 返回即就绪）
            deadline = time.time() + startup_timeout_s
            while time.time() < deadline:
                # 取消自查（2026-08-22 锁竞争修复）：stop() 无锁立
                # 标志后此处自杀——release_for_module 无须等本方法
                # 释放锁（T2 实测旧实现排队 157s 击穿 3s 预算）
                if self._cancel_requested:
                    self._last_error = "vLLM 启动被取消（模块切换资源释放）"
                    self._transition("stopping", "cancel_requested (模块切换)")
                    log.info(self._last_error)
                    try:  # 大白话事件：启动被取消
                        from ..services.event_log import log_event
                        log_event(
                            "vllm", "start_cancelled",
                            "AI 推理引擎还没启动完就被叫停了"
                            "（切换到了其他功能），显存已回收",
                            level="info", detail=self._last_error)
                    except Exception:  # noqa: BLE001
                        pass
                    self._kill_locked()
                    self._transition("unloaded", "start cancelled")
                    return False
                if self._proc.poll() is not None:
                    code = self._proc.returncode
                    self._last_error = (
                        f"vLLM 子进程异常退出(code={code})，"
                        f"详见 {VLLM_LOG}")
                    log.error(self._last_error)
                    self._transition("error", f"proc_exit code={code}")
                    self._proc = None
                    self._close_log()
                    return False
                try:
                    import requests
                    resp = requests.get(HEALTH_URL, timeout=2.0)
                    if resp.status_code == 200:
                        _ready_s = time.time() - self._started_at
                        self._transition("ready", f"boot took {_ready_s:.0f}s")
                        log.info(
                            "vLLM 服务就绪: %s (%.0fs, pid=%d, 模型 %s)",
                            CHAT_URL.rsplit("/", 1)[0],
                            _ready_s,
                            self._proc.pid, mdir.name)
                        try:  # 大白话事件：vLLM 引擎就绪
                            from ..services.event_log import log_event
                            log_event(
                                "vllm", "service_ready",
                                f"AI 推理引擎（vLLM）已就绪，正在运行模型"
                                f"「{mdir.name}」，启动用了 {_ready_s:.0f} 秒",
                                level="success", duration_ms=_ready_s * 1000,
                                detail=(f"pid={self._proc.pid}, "
                                        f"port={VLLM_PORT}, "
                                        f"served_name={served_name}"))
                        except Exception:  # noqa: BLE001
                            pass
                        # ── 启动预热（2026-08-21 超时事故修复）──────────
                        # 首条带图请求会触发视觉内核 Triton JIT 编译
                        # （编译期零 chunk 产出，实测可超 120s → 前端
                        # 「生成失败 Read timed out」）。健康检查只覆盖
                        # 文本路径，此处主动发一条小图请求把编译挪进
                        # start() 阻塞窗口（用户预期"模型加载中"）。
                        # 失败不阻断启动：编译缓存落盘后自然加速。
                        self._warmup(mdir.name)
                        # 预热窗口（最长 240s）同样可能收到取消请求：
                        # 预热完即终止，不能带着取消标志返回就绪
                        if self._cancel_requested:
                            self._last_error = (
                                "vLLM 启动被取消（模块切换资源释放）")
                            self._transition(
                                "stopping", "cancel_requested (预热窗口)")
                            log.info("预热完成时发现取消请求，终止子进程")
                            self._kill_locked()
                            self._transition("unloaded", "warmup cancelled")
                            return False
                        return True
                except Exception:  # noqa: BLE001 - 未就绪继续轮询
                    pass
                time.sleep(HEALTH_POLL_INTERVAL_S)

            # 超时：回收半启动进程，避免僵尸占显存
            self._last_error = (
                f"vLLM 启动超时({startup_timeout_s:.0f}s)，已终止子进程，"
                f"详见 {VLLM_LOG}")
            log.warning(self._last_error)
            self._transition("stopping", f"startup_timeout {startup_timeout_s:.0f}s")
            try:  # 大白话事件：启动失败（子进程退出/超时共用）
                from ..services.event_log import log_event
                log_event(
                    "vllm", "service_failed",
                    f"AI 推理引擎启动失败了（模型「{mdir.name}」）。"
                    "引擎已被自动关闭，显存已回收。可以稍后重试，"
                    "若反复失败请查看技术日志",
                    level="error", detail=self._last_error)
            except Exception:  # noqa: BLE001
                pass
            self._kill_locked()
            self._transition("unloaded", "startup timeout")
            return False

    def start_async(
        self,
        model_dir: str | Path | None = None,
        gpu_memory_utilization: float = 0.85,
        max_model_len: int = 8192,
    ) -> bool:
        """无阻塞 fire-and-forget 后台启动 vLLM（P3 常驻热备双策略）。

        立即返回 True；实际启动（含健康轮询+预热，最长 240s）在后台
        线程执行，就绪后互斥复原。安全：启动中/进程已在运行则复用现有
        实例（不重复起线程、不二次冷启动）；start() 幂等，与 stop() 的
        取消协议兼容（stop 立 _cancel_requested 后由启动循环自查自杀）。

        Returns:
            True 已接手启动（无论是否已就绪）；模型目录无效/运行时缺失
            在后台 start() 内如实失败（读 _last_error / is_healthy()）。
        """
        with self._booting_lock:
            if self._booting or self.is_running():
                return True  # 已有后台启动或进程常驻：复用，不叠加线程
            self._booting = True
        mdir = Path(model_dir) if model_dir else MODELS_DIR / DEFAULT_MODEL_REL

        def _bg_run() -> None:
            try:
                self.start(
                    model_dir=str(mdir),
                    gpu_memory_utilization=gpu_memory_utilization,
                    max_model_len=max_model_len,
                )
            except Exception as exc:  # noqa: BLE001 - 后台启动失败如实记
                log.exception("后台预热 vLLM 异常: %s", exc)
                self._last_error = f"后台预热 vLLM 异常: {exc}"
            finally:
                with self._booting_lock:
                    self._booting = False

        threading.Thread(target=_bg_run, daemon=True,
                         name="vllm-warmup").start()
        try:  # 大白话事件：后台预冷已点火
            from ..services.event_log import log_event
            log_event(
                "vllm", "warmup_ignited",
                f"AI 推理引擎正在后台预冷（模型「{mdir.name}」）。"
                "你切到对话/发图时它通常已就绪，首字会快很多；"
                "如果还没好，页面会提示「模型加载中」，稍等片刻即可",
                level="info", detail=f"model={mdir.name}")
        except Exception:  # noqa: BLE001
            pass
        return True

    def stop(self, timeout_s: float = 5.0) -> bool:
        """停止 vLLM 子进程（terminate → 超时 kill），回收全部显存。

        子进程退出后 CUDA 上下文随进程销毁立即释放（无需 gc/empty_cache，
        这是方案A独立进程架构的核心收益：3 秒模块切换预算内完成）。

        锁竞争修复（2026-08-22 T2 事故）：start() 持锁全程（健康轮询
        ~157s + 预热最长 240s），旧实现 `with self._lock:` 排队等锁
        → release_for_module 3s 预算被击穿（客户端超时、vLLM 未被
        终止继续占显存）。新协议：无锁先立取消标志（start 轮询循环
        每 2s 自查自杀）→ 短超时拿锁兜底直接杀；拿不到锁说明 start
        在跑，返回 True 表示"终止已确保"（进程最迟预热完成后回收）。
        """
        self._cancel_requested = True
        acquired = self._lock.acquire(timeout=max(timeout_s, 2.0))
        if acquired:
            try:
                self._cancel_requested = False  # 已直接处理，无需 start 自杀
                return self._kill_locked(timeout_s)
            finally:
                self._lock.release()
        # start() 持锁中（健康轮询/预热）：取消标志已立，其轮询循环
        # 每 2s 自查一次，发现即 _kill_locked 自杀并返回 False
        log.info("vLLM 启动进行中，取消已请求（由启动循环终止子进程）")
        return True

    def _kill_locked(self, timeout_s: float = 5.0) -> bool:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            self._proc = None
            self._close_log()
            return True
        self._transition("stopping", f"kill process tree pid={proc.pid}")
        log.info("停止 vLLM 子进程树 pid=%d", proc.pid)
        # Windows: vLLM APIServer 会 spawn EngineCore 等孙进程，
        # terminate() 仅杀主进程，孙进程成孤儿继续持有整卡显存
        # （2026-08-21 冒烟实测：stop 返回 True 但显存仍占 15.8GB）。
        # taskkill /T 递归终止整棵进程树，/F 强杀（无状态需善后）
        if sys.platform == "win32":
            r = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True)
            if r.returncode != 0:
                log.error("taskkill 失败(code=%d): %s",
                          r.returncode, r.stderr.decode(errors="replace"))
                return False
        else:
            proc.terminate()
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                log.warning("vLLM terminate 超时，强杀 pid=%d", proc.pid)
                proc.kill()
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    log.error("vLLM 子进程无法终止 pid=%d", proc.pid)
                    return False
        self._proc = None
        self._started_at = 0.0
        _stopped_name = self._served_name
        self._served_name = ""
        self._close_log()
        self._transition("unloaded", f"process tree terminated (was {_stopped_name or 'unknown'})")
        log.info("vLLM 子进程已退出，显存已随进程回收")
        try:  # 大白话事件：vLLM 引擎停止
            from ..services.event_log import log_event
            log_event(
                "vllm", "service_stopped",
                "AI 推理引擎已停止"
                + (f"（原运行模型「{_stopped_name}」）" if _stopped_name else "")
                + "，显存已全部回收",
                level="info")
        except Exception:  # noqa: BLE001
            pass
        return True

    def _reap_orphans(self) -> None:
        """清扫上次会话遗留的 py313 vLLM 孤儿进程（尽力而为，不抛错）。

        判据 = exe == runtime/py313/python.exe **且命令行含
        vllm.entrypoints**（2026-08-27 误杀事故修复：原版只按 exe
        路径匹配，把同运行时的任意 py313 进程——沙箱验证脚本、
        用户自启工具——一律 taskkill /T 误杀；两次 int4 压测「无
        traceback 静默死亡」均为此因）。仍须排除当前进程及其全部
        祖先（2026-08-26 自杀事故，后端/launcher 同样运行在 py313
        运行时，launcher 父进程被当孤儿 taskkill /T 整树（含后端
        自己），表现为预载阶段日志戛然、全进程消失）。
        """
        try:
            import psutil
        except ImportError:  # py310 无 psutil 时跳过（依赖交付清单含 psutil）
            return
        target = str(PY313_EXE).lower()
        try:
            me_proc = psutil.Process(os.getpid())
            protected = {me_proc.pid} | {pp.pid for pp in me_proc.parents()}
        except Exception:  # noqa: BLE001 - 保底仅排除自身
            protected = {os.getpid()}
        for p in psutil.process_iter(["pid", "exe", "cmdline"]):
            try:
                exe = (p.info["exe"] or "").lower()
                cmdline = " ".join(p.info["cmdline"] or []).lower()
                if (exe == target and "vllm.entrypoints" in cmdline
                        and p.info["pid"] not in protected):
                    log.warning("清扫 vLLM 孤儿进程 pid=%d", p.info["pid"])
                    subprocess.run(
                        ["taskkill", "/T", "/F", "/PID", str(p.info["pid"])],
                        capture_output=True)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    def _close_log(self) -> None:
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_fh = None

    # ── 推理（OpenAI 兼容） ───────────────────────────────────

    def _warmup(self, model_name: str) -> None:
        """启动预热：触发视觉内核 Triton JIT 编译（在 start() 窗口完成）。

        2026-08-21 超时事故根因：健康检查仅覆盖文本路径，首条带图请求
        触发 _bilinear_pos_embed_kernel 等视觉内核 JIT 编译，编译期间
        SSE 零 chunk 产出超过读超时 → 前端「生成失败 Read timed out」。
        预热失败不阻断启动（Triton 编译缓存落盘后自然加速）。
        """
        t0 = time.perf_counter()
        try:
            from PIL import Image
            img = Image.new("RGB", (448, 448), color=(255, 255, 255))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            img_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as exc:  # noqa: BLE001 - PIL 缺失只影响图预热
            log.warning("vLLM 预热跳过（构造图片失败）: %s", exc)
            return

        import requests
        payload = {
            "model": self._served_name or SERVED_NAME,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "图"},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                ],
            }],
            "max_tokens": 1,
            "temperature": 0,
        }
        try:
            requests.post(CHAT_URL, json=payload,
                          timeout=(10.0, WARMUP_TIMEOUT_S))
            dur = time.perf_counter() - t0
            log.info("vLLM 预热完成（视觉内核 JIT 编译就绪，%.0fs）", dur)
            try:  # 大白话事件：预热完成
                from ..services.event_log import log_event
                log_event(
                    "vllm", "warmup_done",
                    f"AI 引擎预热完成（{dur:.0f} 秒）：图片处理的加速内核"
                    "已编译就绪，发图对话不会再卡顿",
                    level="success", duration_ms=dur * 1000,
                    detail=f"image=448x448 png, model={model_name}")
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001 - 预热失败不阻断启动
            log.warning("vLLM 预热未完成（不阻断启动，编译缓存会自然落盘）: %s",
                        exc)

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        images_b64: list[str] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        stop_check: Any = None,
    ) -> Iterator[str]:
        """流式对话：POST /v1/chat/completions(SSE) 逐 token 产出。

        Args:
            messages: OpenAI 消息格式（role/content）
            images_b64: 多模态图片（PNG base64，无 data: 前缀）
            temperature / max_tokens: 采样参数
            stop_check: 中断回调（True 时提前断开，vLLM 服务端
                abort 请求并释放该请求 KV）

        Yields:
            文本增量片段
        """
        import requests

        if not self.is_healthy():
            raise RuntimeError(self._last_error or "vLLM 服务未就绪")

        payload_msgs: list[dict[str, Any]] = [dict(m) for m in messages]
        if images_b64:
            # 多模态：最后一条 user 消息注入 image_url 列表。
            # build_context 产出的 content 可能是列表格式
            # （[{"type":"image"},...,{"type":"text","text":...}]，图片
            # 实体经 images_b64 通道传递），此处仅提取文本段防丢失
            for msg in reversed(payload_msgs):
                if msg.get("role") == "user":
                    content = msg.get("content")
                    if isinstance(content, str):
                        text = content
                    else:
                        text = "".join(
                            p.get("text", "") for p in content or []
                            if isinstance(p, dict) and p.get("type") == "text")
                    parts: list[dict[str, Any]] = [
                        {"type": "text", "text": text}] if text else []
                    parts.extend({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    } for b64 in images_b64)
                    msg["content"] = parts
                    break

        payload = {
            "model": self._served_name or SERVED_NAME,
            "messages": payload_msgs,
            "temperature": temperature if temperature > 0 else None,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if payload["temperature"] is None:
            payload.pop("temperature")

        got_chunk = False  # 是否收到过任何 SSE 数据（区分首字超时/中途断开）
        # R1 系 reasoning parser：思考进 delta.reasoning_content、正文进
        # delta.content。max_tokens 不足时思考耗尽预算、正文零产出——
        # 收集思考段，流结束时若正文为空则回退产出（内部功能调用
        # 如 AI 画面描述/实体提取依赖完整输出；正常对话正文非空不受影响）
        reasoning_parts: list[str] = []
        got_content = False
        try:
            with requests.post(
                CHAT_URL, json=payload,
                stream=True, timeout=(10.0, STREAM_READ_TIMEOUT_S),
            ) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"vLLM 推理请求失败(HTTP {resp.status_code}): "
                        f"{resp.text[:300]}")
                for raw in resp.iter_lines(decode_unicode=True):
                    if stop_check is not None and stop_check():
                        break
                    if not raw or not raw.startswith("data:"):
                        continue
                    got_chunk = True
                    data = raw[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        got_content = True
                        yield text
                    else:
                        rc = delta.get("reasoning_content")
                        if rc:
                            reasoning_parts.append(rc)
            # 正文零产出回退：思考耗尽 max_tokens 的场景（R1 短预算
            # 内部调用），产出思考段保证调用方拿到模型实际输出
            if not got_content and reasoning_parts:
                fallback = "".join(reasoning_parts).strip()
                if fallback:
                    log.info("vLLM 正文为空，回退产出思考段（%d 字）",
                             len(fallback))
                    yield fallback
        except requests.exceptions.ReadTimeout as exc:
            raise self._timeout_error(exc, got_chunk) from exc
        except requests.exceptions.ConnectionError as exc:
            # requests 陷阱（models.py iter_content）：**流式读取阶段**的
            # 读超时被包装成 ConnectionError（内层 ReadTimeoutError 才是
            # 真身）——2026-08-21 事故：180s 读超时误报"连接中断"，
            # ReadTimeout 分支永远捕不到。按异常链内容重新归类。
            if "Read timed out" in str(exc):
                raise self._timeout_error(exc, got_chunk) from exc
            try:  # 大白话事件：引擎连接中断（日志面板可见）
                from ..services.event_log import log_event
                log_event(
                    "vllm", "engine_disconnected",
                    "和 AI 推理引擎的连接断开了（可能正在切换模型或释放"
                    "资源）。稍等一下重试，或到「模型管理」确认对话模型"
                    "已加载",
                    level="warning", detail=str(exc)[:200])
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(
                "AI 推理引擎连接中断（可能正在切换模型或已释放资源）。"
                "请稍候重试，或到「模型管理」确认对话模型已加载") from exc

    @staticmethod
    def _timeout_error(exc: Exception, got_chunk: bool) -> RuntimeError:
        """构造读超时错误（大白话 + 事件日志）。"""
        try:  # 大白话事件：推理超时（日志面板可见）
            from ..services.event_log import log_event
            log_event(
                "vllm", "inference_timeout",
                "一次对话请求超时了：AI 还没回完这轮内容。图片越大"
                "理解越慢（超大图已自动压缩），稍等片刻重新发送通常"
                "就好",
                level="warning",
                detail=(f"read_timeout={STREAM_READ_TIMEOUT_S}s, "
                        f"got_chunk={got_chunk}"))
        except Exception:  # noqa: BLE001
            pass
        if got_chunk:
            return RuntimeError(
                "这次回复生成到一半超时了（内容可能较长）。"
                "请重新发送，或把问题拆短一点")
        return RuntimeError(
            "这次请求等太久了：AI 还在理解你的内容（图片越大理解越慢，"
            "超大图下次会自动压缩）。请稍等几秒重新发送，"
            "通常马上就会恢复正常")


# 进程内单例（与 dialog_engine 等共享同一子进程句柄）
_vllm_service: VLLMService | None = None
_vllm_lock = threading.Lock()


def get_vllm_service() -> VLLMService:
    """获取 VLLMService 单例。"""
    global _vllm_service
    with _vllm_lock:
        if _vllm_service is None:
            _vllm_service = VLLMService()
        return _vllm_service


def pil_images_to_b64(images: list[Any]) -> list[str]:
    """PIL 图片列表 → PNG base64 列表（无 data: 前缀）。"""
    out: list[str] = []
    for img in images or []:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        out.append(base64.b64encode(buf.getvalue()).decode("ascii"))
    return out
