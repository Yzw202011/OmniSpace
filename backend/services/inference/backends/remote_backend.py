"""远程推理后端（批3 D3 直连专业卡服务器 → 批1 云端API泛化，2026-09-06）。

对话由 OpenAI 兼容端点承载（云端服务商 API Key 或自建 vLLM 服务器），
桌面端零本地显存占用。实现 DialogBackend 协议（base.py），经
create_backend("remote") 接入统一路由——绑定生效后对话引擎自动短路
到本后端，本地模型装载/显存闸门全部绕行。

连接来源（云端API接入批1，docs/云端API接入方案-2026-09-06.md）：
- 优先：cloud_provider_service 的 dialog.text 槽位绑定（设置页
  「云端 API 服务」卡片配置，支持多服务商）；
- 兼容：批3 旧 remote_dialog_* 单服务器配置（system.settings KV）——
  get_dialog_text_endpoint 内部兜底，旧配置未迁移也照常工作。

协议约定：
- 健康探测：GET {base}/health（vLLM 标准），404 时回退 GET {base}/v1/models，
  任一 2xx 即视为可达；
- 对话：POST {base}/v1/chat/completions（SSE 流式透传给对话 UI）；
- 未就绪铁律（用户产品铁律：不许「再点一次」）：远端冷启动装载
  需要数分钟 → load() 内做有界健康轮询（默认 5 分钟，与对话页同口径），
  超时才报错且报错带出路；
- 鉴权：api_key 可选（Bearer）；出网目标=用户配置的服务商/服务器
  （规格 §14 回环约束指 API 服务端，本模块为客户端出站连接）。

配置存 system_settings KV。绑定读取带 5s TTL 缓存。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass

from ...cloud_provider_service import (
    get_dialog_text_endpoint,
    has_dialog_binding_enabled,
    legacy_remote_fallback_allowed,
    parse_cloud_model_id,
    resolve_provider_endpoint,
)
from .base import DialogBackend, estimate_tokens

logger = logging.getLogger("omnispace.inference.backends.remote")

_SETTINGS_KEY = "system.settings"
_CFG_TTL_S = 5.0
_cfg_cache: tuple[float, RemoteDialogConfig] | None = None
_cfg_lock = threading.Lock()


@dataclass
class RemoteDialogConfig:
    """远程对话配置（设置页可编辑，KV 持久化）。"""
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_s: float = 300.0


def _read_settings_kv() -> dict:
    """读 system_settings KV（异常降级空 dict，不抛）。"""
    try:
        from ....data.database import get_db_safe
        db = get_db_safe()
        if db is None:
            return {}
        row = db.query_one(
            "SELECT value FROM system_settings WHERE key=?", (_SETTINGS_KEY,))
        if row and row.get("value"):
            return json.loads(row["value"]) or {}
    except Exception as exc:  # noqa: BLE001 - 配置读取失败按未启用
        logger.debug("远程对话配置读取失败: %s", exc)
    return {}


def get_remote_config(force: bool = False) -> RemoteDialogConfig:
    """读远程对话配置（5s TTL 缓存；force=True 跳缓存）。"""
    global _cfg_cache
    with _cfg_lock:
        if not force and _cfg_cache is not None and (
                time.time() - _cfg_cache[0] < _CFG_TTL_S):
            return _cfg_cache[1]
    raw = _read_settings_kv()
    cfg = RemoteDialogConfig(
        enabled=bool(raw.get("remote_dialog_enabled", False)),
        base_url=str(raw.get("remote_dialog_base_url") or "").strip().rstrip("/"),
        api_key=str(raw.get("remote_dialog_api_key") or "").strip(),
        model=str(raw.get("remote_dialog_model") or "").strip(),
        timeout_s=max(30.0, float(raw.get("remote_dialog_timeout_s") or 300.0)),
    )
    with _cfg_lock:
        _cfg_cache = (time.time(), cfg)
    return cfg


def invalidate_remote_config_cache() -> None:
    """配置保存后清缓存（设置页 PUT 后调用）。"""
    global _cfg_cache
    with _cfg_lock:
        _cfg_cache = None


def is_remote_dialog_enabled() -> bool:
    """文本引擎是否由云端承载（gpu_domains/引擎短路裁决用）。

    批1 云端API语义：dialog.text 槽位绑定（无缓存直读）或批3 旧
    remote_dialog_* 配置启用（仅未迁移机器，已迁移机器旧配置永久退位，
    防止残留键把用户停用/删除的云端悄悄拉起来）——二者其一即视为云端。
    """
    if has_dialog_binding_enabled():
        return True
    if not legacy_remote_fallback_allowed():
        return False
    return get_remote_config().enabled


def probe_remote_health(base_url: str, api_key: str = "",
                        timeout_s: float = 5.0) -> tuple[bool, str]:
    """健康探测（纯函数式）：/health 优先，404 回退 /v1/models。

    Returns:
        (可达, 不可达原因)；可达时原因为空串。
    """
    import requests

    base = (base_url or "").strip().rstrip("/")
    if not base:
        return False, "未填写服务器地址"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    last = ""
    for path in ("/health", "/v1/models"):
        try:
            resp = requests.get(base + path, headers=headers,
                                timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001 - 网络异常归为不可达
            last = f"{path} 连接失败：{exc}"
            continue
        if 200 <= resp.status_code < 300:
            return True, ""
        last = f"{path} 返回 HTTP {resp.status_code}"
    return False, last


class RemoteDialogBackend(DialogBackend):
    """OpenAI 兼容云端端点后端（云厂商 API / 自建 vLLM 均为此形态）。

    load() = 有界健康轮询（远端冷启动装载可达 5 分钟级）；chat_stream
    = SSE 透传；本地零显存/零子进程——unload 只重置标志位。

    批1 多连接扩展：连接参数来自 dialog.text 槽位绑定（旧配置兜底），
    load 时缓存生效端点身份 _loaded_key；绑定切换后 ensure_loaded 经
    matches_target 检测到目标变化即轻量重载（remote 重载仅健康探测，
    零显存成本）。
    """

    name = "remote"
    supports_images = False  # MVP：远端多模态（Qwen-VL 服务端）后续批

    def __init__(self, health_poll_s: float = 5.0,
                 health_wait_s: float = 300.0) -> None:
        super().__init__()
        self._health_poll_s = health_poll_s
        self._health_wait_s = health_wait_s
        self._base_url = ""
        self._api_key = ""
        self._timeout_s = 300.0
        self._loaded_key = ""  # 生效端点身份（base|model，切换检测用）
        self._provider_name = ""  # 生效服务商名（事件日志用）

    def load(self, model_id: str, model_dir=None,
             required_gb: float = 0.0) -> bool:
        """有界健康轮询直到云端端点可达（未就绪铁律：排队等，不甩给用户重试）。

        端点解析（2026-09-06 文本槽位拆分）：请求为 cloud::prov::model
        虚拟模型时按 provider_id 直取该连接（漫剧文字/写作台各自绑定
        的连接，与对话槽位无关）；否则回落 dialog.text 槽位/旧配置。
        """
        parsed = parse_cloud_model_id(model_id or "")
        ep = None
        if parsed is not None:
            ep = resolve_provider_endpoint(parsed[0])
            if ep is None:
                self._last_error = (
                    f"云端服务商连接无效或已停用（{parsed[0]}）："
                    "请到「设置 → 云端 API 服务」检查连接状态")
                return False
        else:
            ep = get_dialog_text_endpoint(force=True)
        if ep is None or not ep.base_url:
            self._last_error = (
                "云端 API 未配置：请到「设置 → 云端 API 服务」添加服务商，"
                "并绑定对应工位（或沿用旧「远程推理服务器」配置）")
            return False
        deadline = time.time() + self._health_wait_s
        last_err = ""
        while True:
            ok, last_err = probe_remote_health(
                ep.base_url, ep.api_key, timeout_s=5.0)
            if ok:
                self._base_url = ep.base_url
                self._api_key = ep.api_key
                self._provider_name = ep.provider_name or "自定义"
                self.model_id = ((parsed[1] if parsed else "")
                                 or ep.model or model_id or "remote-model")
                self._loaded_key = f"{ep.base_url}|{self.model_id}"
                self._ready = True
                self._last_error = ""
                logger.info("云端端点就绪: %s（服务商=%s 模型=%s）",
                            ep.base_url, ep.provider_name or "自定义",
                            self.model_id)
                return True
            if time.time() >= deadline:
                self._last_error = (
                    f"云端 API 持续不可达（已等待 {self._health_wait_s:.0f}s）："
                    f"{last_err}。请确认服务商在线/网络可达，"
                    f"或到「设置 → 云端 API 服务」点「测试连接」排查")
                logger.warning(self._last_error)
                return False
            time.sleep(self._health_poll_s)

    def matches_target(self, requested_model_id: str | None) -> bool:
        """请求目标与当前已载入端点是否一致（ensure_loaded 短路判定）。

        批1 多连接扩展：请求可为槽位默认绑定（传 None/普通模型名）或
        请求级云端虚拟模型 cloud::prov::model——目标端点身份（base|model）
        与 _loaded_key 一致才允许短路，绑定切换后强制走轻量重载。
        """
        if not self._ready:
            return False
        parsed = parse_cloud_model_id(requested_model_id or "")
        if parsed is not None:
            ep = resolve_provider_endpoint(parsed[0])
            if ep is None:
                return False
            want = f"{ep.base_url}|{parsed[1] or 'remote-model'}"
        else:
            ep = get_dialog_text_endpoint()
            if ep is None:
                return False
            want = (f"{ep.base_url}|"
                    f"{ep.model or requested_model_id or 'remote-model'}")
        return want == self._loaded_key

    def unload(self) -> bool:
        """本地无资源可放；只重置就绪标志（下次对话重新健康探测）。"""
        had = self._ready
        self._ready = False
        return had

    def chat_stream(
        self,
        messages: list[dict],
        images: list | None = None,
        temperature: float = 0.7,
        max_new_tokens: int = 1024,
        extra_params: dict | None = None,
    ):
        """SSE 流式对话（OpenAI 兼容 /v1/chat/completions）。

        连接参数用 load() 时缓存的生效端点（与就绪态自洽；绑定切换由
        ensure_loaded 的 matches_target 检测并触发重载）。失败一律抛
        RuntimeError 且信息带出路（检查服务商/地址/测试连接）。
        每次调用落云端事件日志（系统日志升级 2026-09-06）。
        """
        import requests

        if not self._ready:
            if not self.load("", None, 0.0):
                raise RuntimeError(self._last_error or "云端 API 未就绪")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict = {
            "model": self.model_id or "default",
            "messages": messages,
            "temperature": temperature if temperature > 0 else 0.0,
            "max_tokens": max_new_tokens,
            "stream": True,
        }
        if extra_params:
            payload.update(extra_params)
        t0 = time.time()

        def _log_call(ok: bool, detail: str = "") -> None:
            from ...cloud_provider_service import record_cloud_call
            record_cloud_call(
                "text", self._provider_name or "云端服务商",
                self.model_id or "", ok,
                int((time.time() - t0) * 1000), detail=detail)

        try:
            resp = requests.post(
                self._base_url + "/v1/chat/completions", json=payload,
                headers=headers, stream=True, timeout=(10, self._timeout_s))
        except Exception as exc:  # noqa: BLE001 - 网络异常收敛为带出路的错误
            _log_call(False, str(exc)[:200])
            raise RuntimeError(
                f"无法连接云端 API {self._base_url}：{exc}。请检查"
                "网络是否可用、地址是否正确，或到「设置 → 云端 API "
                "服务」点「测试连接」") from exc
        if resp.status_code != 200:
            body = (resp.text or "")[:200]
            resp.close()
            _log_call(False, f"HTTP {resp.status_code}: {body[:150]}")
            raise RuntimeError(
                f"云端 API 返回 HTTP {resp.status_code}：{body}。"
                "常见原因=API Key 无效或欠费（去服务商后台核对）、"
                "模型名与该服务商不符（检查设置里的模型）；"
                "也可到「设置 → 云端 API 服务」点「测试连接」排查")
        try:
            for raw in resp.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace") \
                    if isinstance(raw, bytes) else raw
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                delta = (choices[0].get("delta") or {}) if choices else {}
                text = delta.get("content")
                if text:
                    yield text
            _log_call(True)
        finally:
            resp.close()

    def count_tokens(self, text: str) -> int:
        # 远端 tokenizer 计数代价不值；粗估满足上下文预算截断
        return estimate_tokens(text)


_remote_backend: RemoteDialogBackend | None = None


def get_remote_backend() -> RemoteDialogBackend:
    """远程后端单例（进程级；健康状态随 load/unload 流转）。"""
    global _remote_backend
    if _remote_backend is None:
        _remote_backend = RemoteDialogBackend()
    return _remote_backend
