"""云端视频生成客户端（云端API接入批3，2026-09-06）。

task_video 协议适配（方案 §5.3，docs/云端API接入方案-2026-09-06.md）：
图生视频异步任务三段式=提交（首帧图 data URL + 提示词 + 时长/画幅）
→ 轮询 → 下载 mp4。批3 内置 dashscope 模板（万相视频 i2v 形态，
`/api/v1/services/aigc/video-generation/video-synthesis`——可灵/即梦
同为异步三段式，差异在请求体模板，后续批按需扩 vendor）。

统一契约：
- 输入：CloudEndpoint + 提示词 + 首帧图（PIL）+ 时长秒 + 画幅标签 +
  check_cancel（轮询间隔协作取消）+ on_progress；
- 输出：mp4 字节（调用方落盘与登记，走 video_tasks 既有下游）；
- 轮询上限 30 分钟（方案 §5.3：视频任务分钟级，上限从宽）；期间可
  取消（轮询间隔检查旗标）；
- 失败一律 RuntimeError 且信息带出路（设置→云端 API 服务→测试连接
  /服务商后台核对 Key 与模型名/余额）。

隐私提示（方案 §11）：首帧图会上传给服务商，由 UI 层提示，本层不重复。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import base64
import io
import logging
import time
from collections.abc import Callable
from typing import Any

from ..cloud_provider_service import (
    PROTOCOL_TASK_VIDEO,
    CloudEndpoint,
    looks_like_dashscope_task_404,
)

log = logging.getLogger("omnispace.inference.cloud_video")

POLL_INTERVAL_S = 5.0        # 视频任务轮询周期（服务商常见 5~10s 粒度）
TASK_TIMEOUT_S = 1800.0      # 轮询总上限 30 分钟（方案 §5.3）
SETUP_HINT = "「设置 → 云端 API 服务」"


def _check(check_cancel: Callable[[], None] | None) -> None:
    if check_cancel is not None:
        check_cancel()


def _to_dataurl(img) -> str:
    """首帧 PIL → JPEG data URL（视频首帧无需透明通道，JPEG 体积小）。"""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=92)
    return "data:image/jpeg;base64," + base64.b64encode(
        buf.getvalue()).decode("ascii")


def _bearer(ep: CloudEndpoint) -> dict:
    headers = {"Content-Type": "application/json"}
    if ep.api_key:
        headers["Authorization"] = f"Bearer {ep.api_key}"
    return headers


def _result_headers(api_key: str, base_url: str, url: str) -> dict:
    """结果文件 URL 的请求头：Bearer 仅在结果主机与 API 端点同源时附加。

    审计 P1-7（2026-09-19）：结果 mp4 落在服务商 OSS/CDN（另一台主机），
    无差别附带 API Key 会把凭据写进第三方主机的访问日志；且任务结果
    链接一般自带签名，鉴权头本就多余。
    """
    if not api_key:
        return {}
    try:
        from urllib.parse import urlparse

        if urlparse(url).netloc != urlparse(base_url).netloc:
            return {}
    except Exception:  # noqa: BLE001 - 解析不了按异源处理（宁可不带）
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def _ds_submit(ep: CloudEndpoint, prompt: str, first_frame,
               duration_seconds: float, aspect: str) -> str:
    """DashScope 形态提交图生视频任务，返回 task_id。"""
    import requests

    url = ep.base_url + \
        "/api/v1/services/aigc/video-generation/video-synthesis"
    payload: dict[str, Any] = {
        "model": ep.model or "wanx-i2v-turbo",
        "input": {
            "prompt": prompt,
            "img_url": _to_dataurl(first_frame),
        },
        "parameters": {
            "duration": max(3, min(15, int(round(duration_seconds)))),
            "aspect_ratio": aspect if aspect in ("16:9", "9:16") else "16:9",
        },
    }
    headers = _bearer(ep)
    headers["X-DashScope-Async"] = "enable"
    try:
        resp = requests.post(url, json=payload, headers=headers,
                             timeout=(10, 60))
    except Exception as exc:  # noqa: BLE001 - 网络异常收敛为带出路的错误
        raise RuntimeError(
            f"无法连接云端视频服务 {ep.provider_name or ep.base_url}：{exc}。"
            f"请检查网络与地址，或到{SETUP_HINT}点「测试连接」") from exc
    if resp.status_code != 200:
        body = (resp.text or "")[:200]
        raise RuntimeError(
            f"云端视频任务提交失败 HTTP {resp.status_code}：{body}。常见"
            "原因=API Key 无效或欠费（去服务商后台核对）、模型名与该"
            f"服务商不符（当前={ep.model or '默认'}）、时长/画幅参数"
            "不受该模型支持")
    out = (resp.json() or {}).get("output") or {}
    task_id = str(out.get("task_id") or "")
    if not task_id:
        raise RuntimeError(
            f"云端视频任务提交未返回 task_id：{str(out)[:150]}")
    return task_id


def generate_video(
        ep: CloudEndpoint, prompt: str, first_frame,
        *, duration_seconds: float = 5.0, aspect: str = "16:9",
        check_cancel: Callable[[], None] | None = None,
        on_progress: Callable[[int], None] | None = None,
        slot: str = "") -> bytes:
    """云端图生视频（协议适配统一入口）。

    slot：来源工位（manga.video），仅用于事件日志（系统日志升级）。

    Returns:
        mp4 字节（调用方落盘到 VIDEO_OUT_DIR 并登记 video_tasks）。
    Raises:
        RuntimeError：信息带出路。
    """
    import requests

    from ..cloud_provider_service import record_cloud_call
    if ep.protocol != PROTOCOL_TASK_VIDEO:
        raise RuntimeError(
            f"连接「{ep.provider_name}」的协议 {ep.protocol} 不支持视频"
            "生成，请到设置里换绑视频类连接（task_video）")
    if first_frame is None:
        raise RuntimeError(
            "云端图生视频需要首帧图：请先为该分镜生成关键帧（或到设置"
            "解绑「漫剧镜头视频」工位回本地引擎）")
    vendor = str((ep.extra or {}).get("vendor") or "dashscope")
    if vendor != "dashscope":
        raise RuntimeError(
            f"连接「{ep.provider_name}」的供应商模板 {vendor} 尚未支持"
            "（当前支持 dashscope）")
    _check(check_cancel)
    t0 = time.time()
    try:
        task_id = _ds_submit(ep, prompt, first_frame, duration_seconds, aspect)
        log.info("云端视频任务已提交: %s model=%s task=%s 时长=%.0fs 画幅=%s",
                    ep.provider_name, ep.model or "默认", task_id,
                    duration_seconds, aspect)
        if on_progress:
            on_progress(5)
        poll_url = ep.base_url + f"/api/v1/tasks/{task_id}"
        deadline = time.time() + TASK_TIMEOUT_S
        video_url = ""
        while True:
            _check(check_cancel)
            try:
                resp = requests.get(poll_url, headers=_bearer(ep),
                                    timeout=(5, 30))
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"云端视频任务轮询失败：{exc}。任务已在服务商侧排队，"
                    "可稍后到服务商后台查看结果") from exc
            if resp.status_code != 200:
                raise RuntimeError(
                    f"云端视频任务轮询 HTTP {resp.status_code}："
                    f"{(resp.text or '')[:150]}")
            out = (resp.json() or {}).get("output") or {}
            status = str(out.get("task_status") or "").upper()
            if status == "SUCCEEDED":
                video_url = str(out.get("video_url") or "")
                if not video_url:
                    raise RuntimeError(
                        f"云端视频任务成功但未找到 video_url：{str(out)[:200]}")
                break
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                raise RuntimeError(
                    f"云端视频任务失败（{status}）："
                    f"{str(out.get('message') or out)[:200]}。可在服务商后台"
                    "查看任务详情；常见原因=首帧图触发内容审核或参数不支持")
            if time.time() >= deadline:
                raise RuntimeError(
                    f"云端视频任务超时（>{TASK_TIMEOUT_S / 60:.0f} 分钟，"
                    f"状态={status}）。服务商可能排队拥塞，可到服务商后台"
                    "确认任务状态后重试")
            if on_progress:
                on_progress(min(85, 10 + int(
                    (time.time() + TASK_TIMEOUT_S - deadline)
                    / TASK_TIMEOUT_S * 75)))
            time.sleep(POLL_INTERVAL_S)
        # 下载 mp4（视频文件大，超时从宽）；Bearer 仅同源附加（P1-7）
        headers = _result_headers(ep.api_key, ep.base_url, video_url)
        try:
            resp = requests.get(video_url, headers=headers, timeout=(10, 300))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"云端视频结果下载失败：{exc}（任务已完成，链接与 task_id "
                f"{task_id} 可到服务商后台重取）") from exc
        if resp.status_code != 200 or not resp.content:
            raise RuntimeError(
                f"云端视频结果下载失败 HTTP {resp.status_code}")
        log.info("云端视频完成: %s task=%s %.1fMB 耗时=%.0fs",
                    ep.provider_name, task_id, len(resp.content) / 1e6,
                    time.time() - t0)
        return resp.content
    except Exception as exc:
        record_cloud_call("video", ep.provider_name, ep.model,
                          False, int((time.time() - t0) * 1000),
                          slot=slot, detail=str(exc)[:300])
        raise
    record_cloud_call("video", ep.provider_name, ep.model, True,
                      int((time.time() - t0) * 1000), slot=slot)


def probe_video_provider(ep: CloudEndpoint,
                         timeout_s: float = 6.0) -> tuple[bool, str]:
    """视频连接的连通性测试（协议感知，设置页「测试连接」用）。

    task_video（dashscope）：GET /api/v1/tasks/__probe__——不存在任务
    404=可达且鉴权通过；401/403=Key 被拒；网络异常=不可达。
    404 双义甄别（2026-09-10 MiniMax 实测根修）：DashScope 对有效 Key
    查不存在任务返回含 request_id 的结构化 JSON；协议不匹配的服务商
    （任务端点路径根本不存在）返回网关 HTML——后者鉴权未被验证，
    不得假报「连接成功」。
    """
    import requests

    if ep.protocol != PROTOCOL_TASK_VIDEO:
        return False, f"协议 {ep.protocol} 不支持视频连通性测试"
    headers = {"Authorization": f"Bearer {ep.api_key}"} if ep.api_key else {}
    try:
        resp = requests.get(ep.base_url + "/api/v1/tasks/__probe__",
                            headers=headers, timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - 网络异常归为不可达
        return False, f"连接失败：{exc}"
    if resp.status_code in (401, 403):
        return False, (f"已连上服务商，但 API Key 被拒绝"
                       f"（HTTP {resp.status_code}）：请到服务商后台核对"
                       "Key 与开通的模型服务")
    if resp.status_code == 404 and not looks_like_dashscope_task_404(resp):
        return False, ("已连上服务器，但任务端点不存在（HTTP 404，非"
                       "DashScope 任务协议形态）：请核对服务地址与连接"
                       "类型是否匹配该服务商")
    if 200 <= resp.status_code < 500:
        return True, ""
    return False, f"HTTP {resp.status_code}：{(resp.text or '')[:120]}"
