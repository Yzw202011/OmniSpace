"""llama-server 子进程服务（GGUF 对话共存档，显存调度机制批4 2026-09-10）。

背景（docs/显存调度机制方案-2026-09-10.md §3.5，D2=A 拍板）：qwen35-9b
W4A16 装载需 14.9GB，16GB 卡白天常态空闲 ~13.4GB 装不下（静默降级 4B
的根因）；GGUF Q4_K_M 实测仅 5.7GB / 114 tok/s、质量碾压 4B（2026-09-08
POC 实证，日志 runtime/llama-poc/llama_poc.log），是 12GB 基线机唯一
9B 解。llama-cpp-python 不认 qwen3_5 架构 → 唯一路=官方 llama-server
子进程（OpenAI 兼容 API，本机 POC 实录端口 8199、加载 ~3s、4 slots）。

管理对齐 vllm_service 成熟模式：单例幂等 start（端口已有健康服务直接
收养置 ready）/ taskkill /T 整树 / exe+命令行双匹配孤儿收账（防误杀
2026-08-27 事故同款防御）/ 失败诚实拒绝（last_error 必有出路）。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..services.vram_policy import DIALOG_TIERS

log = logging.getLogger("omnispace.llama")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
LLAMA_DIR = _PROJECT_ROOT / "runtime" / "llama-poc"
LLAMA_EXE = LLAMA_DIR / "llama-server.exe"
LLAMA_PORT = 8199  # POC 实录（vLLM 8101 不冲突）
HEALTH_URL = f"http://127.0.0.1:{LLAMA_PORT}/health"
CHAT_URL = f"http://127.0.0.1:{LLAMA_PORT}/v1/chat/completions"

# POC 实录参数（llama_poc.log）：n_ctx 8192 × 4 slots（--slots 路由），
# KV 统一池；qwen3.5 Q4_K_M 权重 5.5GB + KV/上下文开销 ≈5.7GB 实测。
_CTX = 8192
_SLOTS = 4
_START_TIMEOUT_S = 90.0   # 首次冷缓存磁盘读留余量（实测热载 ~3s）
_STREAM_READ_TIMEOUT_S = 600.0

# 共存档准入线（DIALOG_TIERS 的 GGUF 档实测需求）
_LLAMA_TIER = next(
    (t for t in DIALOG_TIERS if t.mode == "llama"), None)
_LLAMA_NEED_GB = float(_LLAMA_TIER.vram_gb) if _LLAMA_TIER else 5.7


def _http_get(url: str, timeout: float = 2.0) -> bool:
    """轻量健康探测（不抛异常）。"""
    try:
        import requests

        resp = requests.get(url, timeout=timeout)
        return resp.status_code < 500
    except Exception:  # noqa: BLE001 - 探测失败=不健康
        return False


class LlamaService:
    """llama-server 子进程单例（一个模型 = 一个进程，换模型=重启进程）。"""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._model_path: str = ""
        self._served_name: str = ""
        self._state: str = "unloaded"  # unloaded/booting/ready/error
        self._last_error: str = ""
        self._lock = threading.Lock()

    # ── 状态 ────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    @property
    def served_name(self) -> str:
        return self._served_name

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def is_healthy(self) -> bool:
        return self._state == "ready" and _http_get(HEALTH_URL)

    def runtime_available(self) -> bool:
        return LLAMA_EXE.is_file()

    # ── 生命周期 ────────────────────────────────────────────────

    def _reap_orphans(self) -> int:
        """清扫上次会话遗留的 llama-server 孤儿（exe+路径双匹配防误杀，
        对齐 vllm_service 2026-08-27 误杀事故防御）。"""
        try:
            import psutil
        except ImportError:
            return 0
        targets = {str(LLAMA_EXE).lower()}
        # 命令行含本仓 llama-poc 路径或当前模型路径（双保险）
        markers = [str(LLAMA_DIR).lower(), str(_PROJECT_ROOT).lower()]
        killed = 0
        for p in psutil.process_iter(["pid", "exe", "cmdline"]):
            try:
                exe = (p.info["exe"] or "").lower()
                cmdline = " ".join(p.info["cmdline"] or []).lower()
                if exe in targets and any(m in cmdline for m in markers) \
                        and p.info["pid"] != os.getpid():
                    log.warning("清扫 llama-server 孤儿进程 pid=%d",
                                p.info["pid"])
                    subprocess.run(
                        ["taskkill", "/T", "/F", "/PID", str(p.info["pid"])],
                        capture_output=True, timeout=10,
                        creationflags=subprocess.CREATE_NO_WINDOW
                        if os.name == "nt" else 0)
                    killed += 1
            except Exception:  # noqa: BLE001 - 单进程探测失败继续
                continue
        return killed

    def start(self, model_path: str | Path) -> bool:
        """启动（或收养端口上已健康的同模型服务）。幂等。"""
        model_path = str(Path(model_path).resolve())
        if not Path(model_path).is_file():
            self._last_error = f"GGUF 权重不存在: {model_path}"
            return False
        if not self.runtime_available():
            self._last_error = (
                "llama-server 运行时缺失（runtime/llama-poc），"
                "GGUF 共存档不可用")
            return False
        with self._lock:
            # 幂等：已在跑同模型且健康 → 直接就绪
            if self.is_running() and self._model_path == model_path \
                    and _http_get(HEALTH_URL):
                self._state = "ready"
                return True
            # 收养：上次会话遗留/外部启动的同模型健康服务
            if self._proc is None and _http_get(HEALTH_URL):
                try:
                    import requests

                    names = requests.get(
                        f"http://127.0.0.1:{LLAMA_PORT}/v1/models",
                        timeout=2.0).json().get("data") or []
                    served = str(
                        names[0].get("id", "")) if names else ""
                    if served and Path(model_path).name.lower() \
                            in served.lower():
                        self._model_path = model_path
                        self._served_name = Path(model_path).stem
                        self._state = "ready"
                        log.info("llama-server 孤儿收养：端口已有健康服务"
                                 "（%s），直接置 ready", served)
                        return True
                    log.info("llama-server 端口被异模型占用，先收账重启")
                except Exception:  # noqa: BLE001 - 探测失败走重启
                    pass
                self._reap_orphans()
            # 软准入：空闲显存低于共存档需求 → 诚实拒绝（llama-server
            # OOM 自退的表现是进程起后秒死，前置拒绝更快更明确）
            from ..services.inference.gpu_budget import read_physical_bytes

            ok, free_b, _total_b = read_physical_bytes(0)
            if ok:
                free_gb = free_b / 2 ** 30
                if free_gb < _LLAMA_NEED_GB:
                    self._last_error = (
                        f"空闲显存 {free_gb:.1f}GB 低于 GGUF 共存档需求 "
                        f"{_LLAMA_NEED_GB:.1f}GB；请等待在途生成结束或"
                        "选择更小档位")
                    self._state = "error"
                    log.warning("llama-server 准入拒绝: %s", self._last_error)
                    return False
            # 换模型/残留进程 → 先停
            if self.is_running():
                self.stop()
            self._model_path = model_path
            self._served_name = Path(model_path).stem
            self._state = "booting"
            cmd = [
                str(LLAMA_EXE),
                "-m", model_path,
                "--host", "127.0.0.1",
                "--port", str(LLAMA_PORT),
                "-c", str(_CTX * _SLOTS),   # 总上下文 = 槽位 × 每槽
                "-np", str(_SLOTS),
                # 思考原文保留在 content（qwen3.5 <think> 标签）——llama-server
                # 默认 reasoning-format 会把思考剥进 reasoning_content（content
                # 为空），dialog_engine 的 _ThinkingStreamParser 统一消费标签
                # 链，与 vLLM 路径同语义（批4 实弹发现，2026-09-10）
                "--reasoning-format", "none",
            ]
            log.info("启动 llama-server: %s（ctx=%d×%d slots）",
                     Path(model_path).name, _CTX, _SLOTS)
            try:
                self._proc = subprocess.Popen(
                    cmd, cwd=str(LLAMA_DIR),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW
                    if os.name == "nt" else 0)
            except Exception as exc:  # noqa: BLE001 - spawn 失败诚实拒绝
                self._last_error = f"llama-server 启动失败: {exc}"
                self._state = "error"
                log.warning("%s", self._last_error)
                return False
            deadline = time.monotonic() + _START_TIMEOUT_S
            while time.monotonic() < deadline:
                if self._proc.poll() is not None:
                    self._last_error = (
                        "llama-server 进程启动后退出（权重损坏或显存"
                        "不足），详见 runtime/llama-poc 手动复跑")
                    self._state = "error"
                    log.warning("%s", self._last_error)
                    return False
                if _http_get(HEALTH_URL):
                    self._state = "ready"
                    self._last_error = ""
                    log.info("llama-server 就绪: %s", self._served_name)
                    return True
                time.sleep(1.0)
            self._last_error = (
                f"llama-server 启动超时（{_START_TIMEOUT_S:.0f}s）")
            self._state = "error"
            self.stop()
            return False

    def stop(self) -> bool:
        """停止子进程（taskkill /T 整树；Windows 进程树终止语义）。"""
        with self._lock:
            proc, self._proc = self._proc, None
            self._state = "unloaded"
            self._served_name = ""
        if proc is None:
            return True
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW
                if os.name == "nt" else 0)
            log.info("llama-server 已停止")
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("llama-server 停止异常: %s", exc)
            return False

    # ── 推理 ────────────────────────────────────────────────────

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        extra_params: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        """流式对话（OpenAI 兼容 SSE，对齐 vllm_service.chat_stream 模式）。

        <think> 段以原文产出——qwen3.5 原生思考标签由 dialog_engine 的
        _ThinkingStreamParser 统一消费（与 vLLM 路径同语义）。
        """
        import requests

        if not self.is_healthy():
            raise RuntimeError(self._last_error or "llama-server 未就绪")
        payload: dict[str, Any] = {
            "model": self._served_name or "gguf",
            "messages": [dict(m) for m in messages],
            "max_tokens": max_tokens,
            "stream": True,
        }
        if temperature > 0:
            payload["temperature"] = temperature
        if extra_params:
            payload.update(extra_params)
        got_content = False
        try:
            with requests.post(
                CHAT_URL, json=payload, stream=True,
                timeout=(10.0, _STREAM_READ_TIMEOUT_S),
            ) as resp:
                resp.raise_for_status()
                # SSE 响应无 charset 头；pydeps 无 chardet 时 requests
                # 会 fallback ISO-8859-1 把中文解成乱码（批4 实弹发现）
                # ——llama-server 恒为 UTF-8，显式钉死
                resp.encoding = "utf-8"
                for raw in resp.iter_lines(decode_unicode=True):
                    if isinstance(raw, bytes):  # stub 未反映 decode_unicode
                        raw = raw.decode("utf-8", "replace")
                    if not raw or not raw.startswith("data: "):
                        continue
                    data = raw[6:]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        got_content = True
                        yield text
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络层异常转引擎语义
            if got_content:
                log.warning("llama-server 流式中断（已产出部分内容）: %s",
                            exc)
                return
            raise RuntimeError(f"llama-server 请求失败: {exc}") from exc


_service: LlamaService | None = None
_singleton_lock = threading.Lock()


def get_llama_service() -> LlamaService:
    """llama-server 服务单例（双重检查锁）。"""
    global _service
    if _service is None:
        with _singleton_lock:
            if _service is None:
                _service = LlamaService()
    return _service
# 本项目仅供学习使用，商业授权请+Q 3559331368
