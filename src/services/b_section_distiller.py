"""B 段蒸馏器（2026-11-09 混合路线）。

B 段散文经本地 Rewriter（llama.cpp + Qwen3-VL-8B + H3-Prompt-Rewriter
适配器）转成画面性描述句——散文变画面后不存在被当旁白朗读的路径
（批 6 ASR 实锤），同时保住世界观入画（批 6 证明吞除丢时代锚）。

  用完即杀：临时 spawn llama-server，编译完 terminate（文本阶段
  ComfyUI 未加载权重，显存零冲突）；失败返回 None 由调用方回退吞除，
  绝不阻断出片。SHA-256 缓存防重复编译。SSRF 防线：host 恒为环回
  字面量 + 端口 int 校验 + 解析后 IP 必须环回（防 DNS rebinding）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from ..config import get_config

log = logging.getLogger("omnispace.services.b_distill")

_ROOT = Path(__file__).resolve().parents[2]
_LLAMA_SERVER = _ROOT / "runtime" / "llama-poc" / "llama-server.exe"
_BASE_GGUF = _ROOT / "models" / "llm_gguf" / "Qwen3-VL-8B-Instruct-Q4_K_M.gguf"
_ADAPTER_GGUF = _ROOT / "models" / "llm_gguf" / "h3_prompt_rewriter_Q8_0.gguf"
_LOOPBACK = "127.0.0.1"

_TEMPLATE = (
    "把下面这段世界观背景散文改写成画面性描述句：只描述看得见的画面细节"
    "（陈设、光线、物件、环境）和听得见的环境声，禁止叙述性语言、禁止"
    "总结、禁止任何会被当成旁白朗读的句子。直接输出画面描述，不要标题：\n{text}")

_cache: dict[str, str] = {}


def _cfg_int(section: str, key: str, default: int) -> int:
    try:
        return int((get_config().get(section) or {}).get(key, default))
    except Exception:  # noqa: BLE001 - 配置异常按默认
        return default


def _guard_url(url: str) -> str:
    """SSRF 防线：http 协议 + 环回字面量主机 + 解析后 IP 必须环回。"""
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname != _LOOPBACK:
        raise ValueError(f"SSRF 防线：非环回 HTTP 目标被拒绝 {url!r}")
    for _f, _t, _p, _c, sockaddr in socket.getaddrinfo(
            parts.hostname, parts.port or 80, proto=socket.IPPROTO_TCP):
        if not ipaddress.ip_address(sockaddr[0]).is_loopback:
            raise ValueError(f"SSRF 防线：解析 IP 非环回 {sockaddr[0]}")
    return url


def _wait_health(base_url: str, deadline_s: float) -> bool:
    url = _guard_url(f"{base_url}/health")
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if json.load(r).get("status") == "ok":
                    return True
        except ValueError:
            raise
        except Exception:  # noqa: BLE001 - 未就绪继续等
            time.sleep(1.0)
    return False


def _http_compile(base_url: str, b_text: str, timeout_s: int) -> str | None:
    body = {"messages": [{"role": "user", "content": _TEMPLATE.format(text=b_text)}],
            "max_tokens": 600, "temperature": 0.3, "seed": 42}
    req = urllib.request.Request(
        _guard_url(f"{base_url}/v1/chat/completions"),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            d = json.load(r)
        return str(d["choices"][0]["message"]["content"] or "").strip() or None
    except Exception as exc:  # noqa: BLE001 - 编译失败走降级
        log.warning("[b-distill] 编译失败: %s", exc)
        return None


def distill_b_section(b_text: str) -> str | None:
    """B 段散文 → 画面性描述句；失败返回 None（调用方回退吞除）。"""
    text = (b_text or "").strip()
    if not text:
        return None
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if key in _cache:
        return _cache[key]
    if not (_LLAMA_SERVER.is_file() and _BASE_GGUF.is_file()
            and _ADAPTER_GGUF.is_file()):
        log.warning("[b-distill] 权重/运行时缺失，回退吞除")
        return None

    port = _cfg_int("manga", "b_distill_port", 8131)
    ngl = _cfg_int("manga", "b_distill_ngl", 99)
    timeout_s = _cfg_int("manga", "b_distill_timeout_s", 60)
    base_url = f"http://{_LOOPBACK}:{port}"
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    proc = None
    try:
        t0 = time.time()
        proc = subprocess.Popen(
            [str(_LLAMA_SERVER), "-m", str(_BASE_GGUF), "--lora",
             str(_ADAPTER_GGUF), "--host", _LOOPBACK, "--port", str(port),
             "-ngl", str(ngl), "-c", "4096"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags)
        if not _wait_health(base_url, 90.0):
            log.warning("[b-distill] llama-server 90s 未就绪，回退吞除")
            return None
        out = _http_compile(base_url, text, timeout_s)
        if out:
            _cache[key] = out
            log.info("[b-distill] 蒸馏完成 %d 字（%.0fs）",
                     len(out), time.time() - t0)
        return out
    except Exception as exc:  # noqa: BLE001 - 全链失败走降级
        log.warning("[b-distill] 蒸馏异常（回退吞除）: %s", exc)
        return None
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                proc.kill()
