"""云端图片生成客户端（云端API接入批2，2026-09-06）。

两套协议适配（方案 §5.2，docs/云端API接入方案-2026-09-06.md）：
- openai_image：POST {base}/v1/images/generations 同步出图（b64_json
  或 url 二响应形态兼容），仅文生图（参考图传入时报带出路的错）；
- task_image：异步任务三段式=提交（返 task_id）→ 轮询 → 取结果 URL
  下载。批2 内置 dashscope 模板（阿里云百炼/DashScope 形态）：
  文生图走 text2image/image-synthesis（万相），带参考图走
  multimodal-generation（qwen-image-edit 图生图）。

统一契约：
- 输入：CloudEndpoint + 提示词 + 目标尺寸 + 可选参考图（PIL 列表）/
  负面词 / check_cancel（轮询间隔协作取消）/ on_progress；
- 输出：PIL.Image（精确 resize 到目标尺寸——服务商返回尺寸不保证
  严格一致，调用方下游按 (w,h) 契约继续）；
- 失败一律 RuntimeError 且信息带出路（设置→云端 API 服务→测试连接
  /服务商后台核对 Key 与模型名）；轮询默认 600s 上限。

参考图上传提示（方案 §11 隐私边界）：图生图/参考图会把本地图片
上传给服务商——由各功能在 UI 层提示，本层不重复。
"""
from __future__ import annotations

import base64
import io
import logging
import time
from collections.abc import Callable
from typing import Any

from ..cloud_provider_service import (
    PROTOCOL_OPENAI_IMAGE,
    PROTOCOL_TASK_IMAGE,
    CloudEndpoint,
    looks_like_dashscope_task_404,
)

logger = logging.getLogger("omnispace.inference.cloud_image")

POLL_INTERVAL_S = 3.0     # task_image 轮询周期
TASK_TIMEOUT_S = 600.0    # task_image 轮询总上限（云出图常见 30~120s）
MAX_REF_IMAGES = 4        # 参考图上限（服务商普遍 1~10 张，取保守值）

_SETUP_HINT = "「设置 → 云端 API 服务」"


def _check(check_cancel: Callable[[], None] | None) -> None:
    if check_cancel is not None:
        check_cancel()


def _to_dataurl(img) -> str:
    """PIL → PNG data URL（参考图上传编码）。"""
    import PIL.Image as PILImage

    if not isinstance(img, PILImage.Image):
        raise RuntimeError("参考图格式错误（需 PIL Image）")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(
        buf.getvalue()).decode("ascii")


def _download_image(url: str, api_key: str = ""):
    """下载结果图 URL → PIL（超时 60s；下载失败带出路）。"""
    import requests

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        resp = requests.get(url, headers=headers, timeout=(10, 60))
    except Exception as exc:  # noqa: BLE001 - 网络异常收敛
        raise RuntimeError(
            f"云端结果图下载失败：{exc}。可稍后重试，或到"
            f"{_SETUP_HINT}点「测试连接」排查网络") from exc
    if resp.status_code != 200:
        raise RuntimeError(
            f"云端结果图下载失败 HTTP {resp.status_code}（服务商文件"
            "链接可能已过期）")
    return _open_image_bytes(resp.content)


def _open_image_bytes(data: bytes):
    from PIL import Image

    try:
        return Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - 响应体不是图片
        raise RuntimeError(f"云端返回的内容不是有效图片：{exc}") from exc


def _normalize_size(img, width: int, height: int):
    """归一到目标尺寸（服务商不严格保证尺寸；等比差异大时如实缩放）。"""
    if img.size == (width, height):
        return img
    return img.resize((width, height), 1)  # PIL.Image.LANCZOS==1（py3.10 无枚举名）


def _wan_size(width: int, height: int) -> str:
    """万相 t2i/i2i 的 size 参数（官方格式="宽*高"，2026-09-06 文档核对）。

    wan2.2 及以下约束=宽高均在 [512,1440]；约束外先等比归档（超上限
    收缩、低于下限放大），出图后经 _normalize_size resize 回目标尺寸
    ——调用方（关键帧 1280x720 / 资产 2560x1440）尺寸契约不受影响。
    """
    w, h = int(width), int(height)
    if w > 0 and h > 0 and (w > 1440 or h > 1440):
        k = 1440 / max(w, h)
        w, h = int(w * k), int(h * k)
    if w > 0 and h > 0 and min(w, h) < 512:
        k = 512 / min(w, h)
        w, h = int(w * k), int(h * k)
    return f"{max(512, min(1440, w))}*{max(512, min(1440, h))}"


def _bearer(ep: CloudEndpoint) -> dict:
    headers = {"Content-Type": "application/json"}
    if ep.api_key:
        headers["Authorization"] = f"Bearer {ep.api_key}"
    return headers


def _post_json(ep: CloudEndpoint, url: str, payload: dict,
               timeout: tuple = (10, 180)) -> dict:
    import requests

    try:
        resp = requests.post(url, json=payload, headers=_bearer(ep),
                             timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 网络异常收敛为带出路的错误
        raise RuntimeError(
            f"无法连接云端图片服务 {ep.provider_name or ep.base_url}："
            f"{exc}。请检查网络与地址，或到{_SETUP_HINT}点「测试连接」"
        ) from exc
    if resp.status_code != 200:
        body = (resp.text or "")[:200]
        raise RuntimeError(
            f"云端图片服务返回 HTTP {resp.status_code}：{body}。常见原因="
            "API Key 无效或欠费（去服务商后台核对）、模型名与该服务商"
            f"不符（检查设置里的模型，当前={ep.model or '默认'}）")
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"云端图片服务响应解析失败：{exc}") from exc


# ── openai_image：同步 /v1/images/generations ────────────────────

def _generate_openai_image(
        ep: CloudEndpoint, prompt: str, width: int, height: int,
        negative: str, ref_images: list,
        check_cancel: Callable[[], None] | None) -> Any:

    if ref_images:
        raise RuntimeError(
            f"连接「{ep.provider_name}」的协议（openai_image）暂不支持"
            "参考图/图生图：请在设置里把该工位换绑为任务型图片服务商"
            "（task_image，如通义万相）或恢复本地引擎")
    payload: dict = {
        "model": ep.model or "default",
        "prompt": prompt,
        "size": f"{width}x{height}",
        "n": 1,
    }
    if negative.strip():
        # OpenAI images API 无标准负面词参数；兼容支持 negative_prompt
        # 的服务商（硅基流动等），不支持者忽略未知字段或如实报错
        payload["negative_prompt"] = negative.strip()
    data = _post_json(ep, ep.base_url + "/v1/images/generations", payload,
                      timeout=(10, 300))
    items = data.get("data") or []
    if not items:
        raise RuntimeError(
            f"云端图片服务未返回图片数据：{str(data)[:200]}")
    item = items[0]
    _check(check_cancel)
    if item.get("b64_json"):
        return _normalize_size(
            _open_image_bytes(base64.b64decode(item["b64_json"])),
            width, height)
    if item.get("url"):
        return _normalize_size(_download_image(str(item["url"]), ep.api_key),
                               width, height)
    raise RuntimeError(
        f"云端图片服务返回形态不支持（无 b64_json/url）：{str(item)[:150]}")


# ── task_image：异步三段式（dashscope 模板）──────────────────────

def _ds_submit(ep: CloudEndpoint, prompt: str, width: int, height: int,
               ref_images: list) -> str:
    """DashScope 形态提交：带参考图走 multimodal-generation，否则
    text2image/image-synthesis。返回 task_id。"""
    headers = _bearer(ep)
    headers["X-DashScope-Async"] = "enable"
    import requests

    if ref_images:
        url = ep.base_url + \
            "/api/v1/services/aigc/multimodal-generation/generation"
        content: list[dict[str, Any]] = [
            {"image": _to_dataurl(img)} for img in ref_images]
        content.append({"text": prompt})
        payload: dict[str, Any] = {
            "model": ep.model or "qwen-image-edit",
            "input": {"messages": [{"role": "user", "content": content}]},
            # qwen-image-edit：size 为"宽*高"（星号分隔，官方格式）
            "parameters": {"size": _wan_size(width, height)},
        }
    else:
        url = ep.base_url + \
            "/api/v1/services/aigc/text2image/image-synthesis"
        payload = {
            "model": ep.model or "wanx2.1-t2i-turbo",
            "input": {"prompt": prompt},
            # 官方 size 格式="宽*高"（2026-09-06 官方文档核对：此前
            # 误写 *宽x高 必被拒）；约束外尺寸先归档（见 _wan_size），
            # 出图后 _normalize_size 统一 resize 回目标尺寸
            "parameters": {"size": _wan_size(width, height), "n": 1},
        }
    try:
        resp = requests.post(url, json=payload, headers=headers,
                             timeout=(10, 60))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"无法连接云端图片服务 {ep.provider_name or ep.base_url}："
            f"{exc}。请检查网络与地址，或到{_SETUP_HINT}点「测试连接」"
        ) from exc
    if resp.status_code != 200:
        body = (resp.text or "")[:200]
        raise RuntimeError(
            f"云端图片任务提交失败 HTTP {resp.status_code}：{body}。"
            "常见原因=API Key 无效或欠费（去服务商后台核对）、模型名"
            f"与该服务商不符（当前={ep.model or '默认'}）")
    out = (resp.json() or {}).get("output") or {}
    task_id = str(out.get("task_id") or "")
    if not task_id:
        raise RuntimeError(
            f"云端图片任务提交未返回 task_id：{str(out)[:150]}")
    return task_id


def _extract_task_url(out: dict) -> str:
    """从 DashScope 任务结果提取首个图片 URL（t2i 与 multimodal 两种形态）。"""
    results = out.get("results") or []          # text2image 形态
    for r in results:
        if isinstance(r, dict) and r.get("url"):
            return str(r["url"])
    for choice in out.get("choices") or []:      # multimodal 形态
        content = ((choice.get("message") or {}).get("content")) or []
        for c in content:
            if isinstance(c, dict) and (c.get("image") or c.get("url")):
                return str(c.get("image") or c.get("url"))
    return ""


def _generate_task_image(
        ep: CloudEndpoint, prompt: str, width: int, height: int,
        negative: str, ref_images: list,
        check_cancel: Callable[[], None] | None,
        on_progress: Callable[[int], None] | None) -> Any:
    import requests

    vendor = str((ep.extra or {}).get("vendor") or "dashscope")
    if vendor != "dashscope":
        raise RuntimeError(
            f"连接「{ep.provider_name}」的供应商模板 {vendor} 尚未支持"
            "（当前支持 dashscope）")
    task_id = _ds_submit(ep, prompt, width, height, ref_images)
    logger.info("云端图片任务已提交: %s model=%s task=%s refs=%d",
                ep.provider_name, ep.model or "默认", task_id,
                len(ref_images))
    if on_progress:
        on_progress(20)
    poll_url = ep.base_url + f"/api/v1/tasks/{task_id}"
    deadline = time.time() + TASK_TIMEOUT_S
    out: dict = {}
    while True:
        _check(check_cancel)
        try:
            resp = requests.get(poll_url, headers=_bearer(ep),
                                timeout=(5, 30))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"云端图片任务轮询失败：{exc}。可稍后重试（任务已在"
                "服务商侧排队，重试会提交新任务）") from exc
        if resp.status_code != 200:
            raise RuntimeError(
                f"云端图片任务轮询 HTTP {resp.status_code}："
                f"{(resp.text or '')[:150]}")
        out = (resp.json() or {}).get("output") or {}
        status = str(out.get("task_status") or "").upper()
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "CANCELED", "UNKNOWN"):
            raise RuntimeError(
                f"云端图片任务失败（{status}）："
                f"{str(out.get('message') or out)[:200]}。可在服务商"
                "后台查看任务详情")
        if time.time() >= deadline:
            raise RuntimeError(
                f"云端图片任务超时（>{TASK_TIMEOUT_S:.0f}s，状态={status}）。"
                "服务商可能排队拥塞，可稍后重试")
        if on_progress:
            on_progress(min(75, 30 + int(
                (time.time() + TASK_TIMEOUT_S - deadline)
                / TASK_TIMEOUT_S * 45)))
        time.sleep(POLL_INTERVAL_S)
    url = _extract_task_url(out)
    if not url:
        raise RuntimeError(
            f"云端图片任务成功但未找到结果图 URL：{str(out)[:200]}")
    if on_progress:
        on_progress(85)
    _check(check_cancel)
    return _normalize_size(_download_image(url, ep.api_key), width, height)


# ── 统一入口 ─────────────────────────────────────────────────────

def probe_image_provider(ep: CloudEndpoint,
                         timeout_s: float = 6.0) -> tuple[bool, str]:
    """图片连接的连通性测试（协议感知，设置页「测试连接」用）。

    openai_image：GET /v1/models（200=通；401/403=Key 问题）；
    task_image（dashscope）：GET /api/v1/tasks/__probe__（不存在任务
    404=可达且鉴权通过——未授权会是 401；401/403=Key 问题）。
    task_image 的 404 双义甄别（2026-09-10 MiniMax 实测根修）：仅
    DashScope 任务查询形态的 JSON（含 request_id）算通过；协议不匹配
    服务商的网关 HTML 404 只证明服务器存在，不得假报「连接成功」。

    Returns:
        (可达, 说明)；可达时说明为空串。
    """
    import requests

    if ep.protocol == PROTOCOL_OPENAI_IMAGE:
        path = "/v1/models"
    elif ep.protocol == PROTOCOL_TASK_IMAGE:
        path = "/api/v1/tasks/__probe__"
    else:
        return False, f"协议 {ep.protocol} 不支持图片连通性测试"
    headers = {"Authorization": f"Bearer {ep.api_key}"} if ep.api_key else {}
    try:
        resp = requests.get(ep.base_url + path, headers=headers,
                            timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - 网络异常归为不可达
        return False, f"连接失败：{exc}"
    if resp.status_code in (401, 403):
        return False, (f"已连上服务商，但 API Key 被拒绝"
                       f"（HTTP {resp.status_code}）：请到服务商后台核对"
                       "Key 与开通的模型服务")
    if (resp.status_code == 404 and ep.protocol == PROTOCOL_TASK_IMAGE
            and not looks_like_dashscope_task_404(resp)):
        return False, ("已连上服务器，但任务端点不存在（HTTP 404，非"
                       "DashScope 任务协议形态）：请核对服务地址与连接"
                       "类型是否匹配该服务商")
    if 200 <= resp.status_code < 500:
        # 200（models 列表）或 404（DashScope 探测任务不存在）都证明：
        # 地址可达且 Key 通过鉴权
        return True, ""
    return False, f"HTTP {resp.status_code}：{(resp.text or '')[:120]}"


def generate_image(
        ep: CloudEndpoint, prompt: str, *, width: int, height: int,
        negative: str = "", ref_images: list | None = None,
        check_cancel: Callable[[], None] | None = None,
        on_progress: Callable[[int], None] | None = None,
        slot: str = ""):
    """云端生成一张图片（协议适配统一入口）。

    slot：来源工位（paint.image/keyframe.image/asset.image），仅用于
    事件日志（系统日志升级 2026-09-06）。

    Returns:
        PIL.Image（RGB，精确 (width, height)）。
    Raises:
        RuntimeError：信息带出路（设置页排查指引）。
    """
    from ..cloud_provider_service import record_cloud_call
    refs = [r for r in (ref_images or []) if r is not None][:MAX_REF_IMAGES]
    _check(check_cancel)
    t0 = time.time()
    try:
        if ep.protocol == PROTOCOL_OPENAI_IMAGE:
            img = _generate_openai_image(ep, prompt, width, height,
                                         negative, refs, check_cancel)
        elif ep.protocol == PROTOCOL_TASK_IMAGE:
            img = _generate_task_image(ep, prompt, width, height, negative,
                                       refs, check_cancel, on_progress)
        else:
            raise RuntimeError(
                f"连接「{ep.provider_name}」的协议 {ep.protocol} 不支持"
                "图片生成，请到设置里换绑图片类连接")
    except Exception as exc:
        record_cloud_call("image", ep.provider_name, ep.model, False,
                          int((time.time() - t0) * 1000), slot=slot,
                          detail=str(exc)[:300])
        raise
    record_cloud_call("image", ep.provider_name, ep.model, True,
                      int((time.time() - t0) * 1000), slot=slot)
    logger.info("云端图片完成: %s %dx%d refs=%d 耗时=%.1fs slot=%s",
                ep.provider_name, width, height, len(refs),
                time.time() - t0, slot or "-")
    return img
