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
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from ...cloud_provider_service import (
    get_dialog_text_endpoint,
    has_dialog_binding_enabled,
    http_error_hint,
    legacy_remote_fallback_allowed,
    parse_cloud_model_id,
    resolve_provider_endpoint,
)
from .base import DialogBackend, estimate_tokens

log = logging.getLogger("omnispace.inference.backends.remote")

_SETTINGS_KEY = "system.settings"
_CFG_TTL_S = 5.0
# 流式对话字节间读超时（秒）：正常流式 token 持续到达不会触发；超过该
# 时长无任何新数据 = 服务端挂死/网络中断（2026-09-21 学习代理线程在
# iter_lines 上无声卡 30 分钟取证），宁可报错让用户重试
_STREAM_READ_TIMEOUT_S = 180.0
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
    """读 system_settings KV——B1 根修（2026-09-21）委托 settings_kv 单源。"""
    try:
        from ....data.settings_kv import kv_get
        val = kv_get(_SETTINGS_KEY)
        return val if isinstance(val, dict) else {}
    except Exception as exc:  # noqa: BLE001 - 配置读取失败按未启用
        log.debug("远程对话配置读取失败: %s", exc)
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


def _record_cloud_call(kind: str, provider: str, model: str, ok: bool,
                       ms: int, detail: str = "") -> None:
    """云端调用事件记录（fail-open：日志失败绝不影响对话主链）。"""
    try:
        from ...cloud_provider_service import record_cloud_call
        record_cloud_call(kind, provider, model, ok, ms, detail=detail)
    except Exception:  # noqa: BLE001 - 事件记录失败不影响对话
        log.debug("云端调用事件记录失败", exc_info=True)


def _image_to_data_url(img: Any) -> str:
    """图片 → PNG data URL（OpenAI image_url 形态；PIL 或已有 base64）。"""
    if isinstance(img, str):
        return img if img.startswith("data:") else \
            f"data:image/png;base64,{img}"
    import base64
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _strip_image_parts(messages: list[dict]) -> list[dict]:
    """非视觉模型：剥除消息中的图片占位（诚实丢弃，保文本）。"""
    out: list[dict] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            kept = [p for p in content
                    if not (isinstance(p, dict) and p.get("type") == "image")]
            m = {**m, "content": kept}
        out.append(m)
    return out


def _embed_images_as_data_urls(messages: list[dict], urls: list[str]) -> None:
    """把引擎的 {"type": "image"} 占位（按序）替换为 OpenAI image_url。"""
    idx = 0
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list) or idx >= len(urls):
            continue
        new_parts: list[dict] = []
        for part in content:
            if (isinstance(part, dict) and part.get("type") == "image"
                    and idx < len(urls)):
                new_parts.append({"type": "image_url",
                                  "image_url": {"url": urls[idx]}})
                idx += 1
            else:
                new_parts.append(part)
        m["content"] = new_parts


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
    # 2026-09-22 云端图片透传：引擎按此放行图片占位，是否真发由
    # chat_stream 按模型视觉能力二次裁决（非视觉型号剥除占位）
    supports_images = True

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
        # 最近一次 chat_stream 的 finish_reason（length=截断，上层标注用）
        self.last_finish_reason = ""

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
                log.info("云端端点就绪: %s（服务商=%s 模型=%s）",
                            ep.base_url, ep.provider_name or "自定义",
                            self.model_id)
                return True
            if time.time() >= deadline:
                self._last_error = (
                    f"云端 API 持续不可达（已等待 {self._health_wait_s:.0f}s）："
                    f"{last_err}。请确认服务商在线/网络可达，"
                    f"或到「设置 → 云端 API 服务」点「测试连接」排查")
                log.warning(self._last_error)
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
        elif requested_model_id:
            # 显式本地模型 id（非 cloud::）：语义=载本地——remote 后端
            # 必然不匹配（返回 False 触发切回本地），不得拿对话槽绑定
            # 短路（2026-10-07 用户实报：漫剧模型设置选本地，生词被
            # 对话槽云绑定劫持成云端 glm-5.3）
            return False
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

    def chat_nostream(
        self,
        messages: list[dict],
        images: list | None = None,
        temperature: float = 0.7,
        max_new_tokens: int = 1024,
        extra_params: dict | None = None,
    ) -> str:
        """非流式对话：一次取回 message.content（2026-10-10 翻译专用——
        网关 SSE 层每块双发致口吃，非流式路径实测干净）。"""
        texts = list(self.chat_stream(
            messages, images=images, temperature=temperature,
            max_new_tokens=max_new_tokens, extra_params=extra_params,
            stream=False))
        return "".join(texts)

    def chat_stream(
        self,
        messages: list[dict],
        images: list | None = None,
        temperature: float = 0.7,
        max_new_tokens: int = 1024,
        extra_params: dict | None = None,
        stream: bool = True,
    ):
        """SSE 流式对话（OpenAI 兼容 /v1/chat/completions）。

        stream=False：非流式一次取回（2026-10-10 翻译专用——MagicAI
        网关的 SSE 层每块双发，非流式路径实测干净；生成器语义不变，
        非流式时只 yield 一个完整块）。

        连接参数用 load() 时缓存的生效端点（与就绪态自洽；绑定切换由
        ensure_loaded 的 matches_target 检测并触发重载）。失败一律抛
        RuntimeError 且信息带出路（检查服务商/地址/测试连接）。
        每次调用落云端事件日志（系统日志升级 2026-09-06）。
        """
        import requests

        if not self._ready:
            if not self.load("", None, 0.0):
                raise RuntimeError(self._last_error or "云端 API 未就绪")
        self.last_finish_reason = ""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict = {
            "model": self.model_id or "default",
            "messages": messages,
            "temperature": temperature if temperature > 0 else 0.0,
            "max_tokens": max_new_tokens,
            "stream": stream,
        }
        if extra_params:
            payload.update(extra_params)
        # 云端图片透传（2026-09-22）：视觉型号把引擎占位换成 OpenAI
        # image_url（PNG data URL）；非视觉型号剥除占位诚实丢弃——
        # DeepSeek 文本系收图会 400，qwen3-vl/qvq/omni 系真看图
        if images:
            from ...cloud_provider_service import is_vision_model
            if is_vision_model(self.model_id or ""):
                _embed_images_as_data_urls(
                    payload["messages"],
                    [_image_to_data_url(i) for i in images])
            else:
                payload["messages"] = _strip_image_parts(payload["messages"])
                log.info("云端模型 %s 非视觉型号，已忽略 %d 张图片",
                         self.model_id, len(images))
        t0 = time.time()

        def _log_call(ok: bool, detail: str = "") -> None:
            _record_cloud_call(
                "text", self._provider_name or "云端服务商",
                self.model_id or "", ok,
                int((time.time() - t0) * 1000), detail=detail)

        # 断线自动重试（2026-10-03 MagicAI 实测连接抖动）：仅对未收到
        # 响应的连接类异常重试 2 次（2s/4s 退避，stream=True 的 post 在
        # 拿到响应头前失败=零字节已交付，重试无重复风险）；流中途断线
        # 不重试（部分内容已交付）
        resp = None
        _fatal: Exception | None = None
        for _attempt in range(3):
            try:
                resp = requests.post(
                    self._base_url + "/v1/chat/completions", json=payload,
                    headers=headers, stream=stream,
                    timeout=(10, _STREAM_READ_TIMEOUT_S))
                break
            except requests.exceptions.ReadTimeout as exc:
                # P1-03 修复（2026-10-24）：读超时=服务端已受理并在
                # 处理（首字节>180s 的深度推理/网关缓冲），重试=同
                # prompt 二发/三发→双/三倍计费。绝不重试。
                _fatal = exc
                break
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as exc:
                # 连接级失败/连接超时=服务端未收到请求，重试无风险
                _fatal = exc
                if _attempt < 2:
                    time.sleep(2.0 * (_attempt + 1))
            except Exception as exc:  # noqa: BLE001 - 网络异常收敛为带出路的错误
                _fatal = exc
                break
        if resp is None:
            if isinstance(_fatal, requests.exceptions.Timeout):
                _log_call(False, f"connect timeout: {_fatal}")
                raise RuntimeError(
                    "无法连接云端 API（连接超时，已自动重试 3 次）：请检查"
                    "网络是否可用、服务商是否在线，或到「设置 → 云端 API "
                    "服务」点「测试连接」") from _fatal
            if isinstance(_fatal, requests.exceptions.ConnectionError):
                _log_call(False, str(_fatal)[:200])
                raise RuntimeError(
                    f"无法连接云端 API {self._base_url}（已自动重试 3 次）："
                    f"{_fatal}。请检查网络是否可用、地址是否正确，或到"
                    "「设置 → 云端 API 服务」点「测试连接」") from _fatal
            _log_call(False, str(_fatal)[:200])
            raise RuntimeError(
                f"无法连接云端 API {self._base_url}：{_fatal}。请检查"
                "网络是否可用、地址是否正确，或到「设置 → 云端 API "
                "服务」点「测试连接」") from _fatal
        if resp.status_code != 200:
            body = (resp.text or "")[:200]
            resp.close()
            _log_call(False, f"HTTP {resp.status_code}: {body[:150]}")
            raise RuntimeError(
                f"云端 API 返回 HTTP {resp.status_code}：{body}。"
                + http_error_hint(resp.status_code))
        # 非流式分支（2026-10-10 翻译专用）：MagicAI 网关的 SSE 层每块
        # 双发（翻译口吃实锤），非流式路径实测干净——一次取回 message。
        if not stream:
            obj = resp.json()
            choices = obj.get("choices") or []
            msg = (choices[0].get("message") or {}) if choices else {}
            content = msg.get("content") or ""
            # P2-13 修复（2026-10-25）：非流式分支补 reasoning_content
            # 回退——流式分支 :519-528 有完整兜底，此分支缺失=思考型
            # 模型（R1 系绑 manga.text）翻译/机械预处理必报空回复
            if not content:
                reasoning = str(msg.get("reasoning_content") or "")
                if reasoning.strip():
                    content = reasoning.strip()
            self.last_finish_reason = str(
                (choices[0].get("finish_reason") if choices else "") or "")
            _log_call(bool(content))
            if content:
                yield content
            else:
                raise RuntimeError("云端模型未返回正文（非流式空回复）")
            return
        content_yielded = False
        reasoning_parts: list[str] = []
        # SSE 相邻重复块丢弃（2026-10-10 用户实报翻译口吃）：MagicAI
        # 网关实测把每个 content 块原样双发（14 块=7 组逐对重复，
        # [i]==[i+1] 完全周期）——拼接后全文翻倍（翻译口吃/对话双份
        # 实锤）。一次延迟语义：块先缓冲，与后块相同即丢弃；正常流仅
        # 多一块延迟，代价=相邻完全同文块会少一块（多为标点重复，可
        # 接受）。OMNISPACE_SSE_CHUNK_DEDUP=0 可关。
        _sse_dedup = os.environ.get("OMNISPACE_SSE_CHUNK_DEDUP", "1") != "0"
        _pending: str | None = None
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
                # 思考型云端模型（DeepSeek-R1 系等）把输出放进
                # reasoning_content 通道：累积作正文兜底，不直接透传
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    reasoning_parts.append(reasoning)
                text = delta.get("content")
                if text:
                    content_yielded = True
                    if _sse_dedup:
                        if text == _pending:
                            _pending = text  # 重复块：丢弃（保留基线）
                            continue
                        if _pending is not None:
                            yield _pending
                        _pending = text
                        continue
                    yield text
                fr = choices[0].get("finish_reason") if choices else None
                if fr:
                    self.last_finish_reason = str(fr)
            if _sse_dedup and _pending is not None:
                yield _pending
        except requests.exceptions.Timeout as exc:
            _log_call(False, f"read timeout: {exc}")
            raise RuntimeError(
                "云端 API 流式响应超时（180 秒无新数据）：服务商可能"
                "过载或网络中断，请重发重试") from exc
        finally:
            resp.close()
        # 空回复兜底（2026-09-21 用户报「写 3000 字剧本一片空白」实测
        # 根因：正文 0 片段也记成功 → 前端空气泡）。对齐本地 vLLM 通道
        # 语义（vllm_service reasoning 正文回退）：宁可重复不给空白。
        # 2026-10-07 收紧（用户实报分镜生词混英文思考）：finish=length
        # 时思考被硬切在半截——回退出去的是残缺推演不是答案（分镜描述
        # 词 B 段截断+「Let me analyze」泄漏实证），只认 finish=stop 的
        # 思考回退；length 一律报错走重试
        if content_yielded:
            _log_call(True)
            return
        if reasoning_parts and self.last_finish_reason == "stop":
            _log_call(True,
                      "reasoning_content 回退为正文（服务商未返回 content）")
            yield "".join(reasoning_parts)
            return
        if reasoning_parts and self.last_finish_reason == "length":
            _log_call(False, "reasoning-only truncated, finish_reason=length")
            raise RuntimeError(
                "云端思考型模型把 token 预算全部耗在思考上（finish=length），"
                "正文 0 字。请重试；若持续出现，请在模型设置换非思考模型")
        _log_call(False,
                  f"empty stream, finish_reason="
                  f"{self.last_finish_reason or 'none'}")
        raise RuntimeError(
            "云端模型未返回正文（回复可能全部是思考内容且被长度上限"
            "截断，或服务商异常）。可尝试关闭深度思考后重发；若持续"
            "出现，请到「设置 → 云端 API 服务」核对模型名并点"
            "「测试连接」排查")

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
# 本项目仅供学习使用，商业授权请+Q 3559331368
