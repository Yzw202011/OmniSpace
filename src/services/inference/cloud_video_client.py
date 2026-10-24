"""云端视频生成客户端（云端API接入批3，2026-09-06）。

task_video 协议适配（方案 §5.3，docs/云端API接入方案-2026-09-06.md）：
图生视频异步任务三段式=提交（首帧图 data URL + 提示词 + 时长/画幅）
→ 轮询 → 下载 mp4。批3 内置 dashscope 模板（万相视频 i2v 形态，
`/api/v1/services/aigc/video-generation/video-synthesis`——可灵/即梦
同为异步三段式，差异在请求体模板，后续批按需扩 vendor）。

sora_video 协议（2026-10-03 新增）：MagicAI Grok 视频的 Sora 任务式——
POST /v1/videos（首帧图走 multipart input_reference，无首帧=纯文生）
→ GET /v1/videos/{id} 轮询（终态词=done）→ video_url/CDN 下载。
时长档位实测 4/8/10/15 秒（_sora_seconds 钳位映射）；平台无取消接口。

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
    PROTOCOL_SORA_VIDEO,
    PROTOCOL_TASK_VIDEO,
    CloudEndpoint,
    http_error_hint,
    looks_like_dashscope_task_404,
)

log = logging.getLogger("omnispace.inference.cloud_video")

POLL_INTERVAL_S = 5.0        # 视频任务轮询周期（服务商常见 5~10s 粒度）
SORA_POLL_INTERVAL_S = 8.0   # Sora 任务式轮询周期（渲染分钟级，放宽省请求）
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


def _jpeg_bytes(img) -> bytes:
    """首帧 PIL → JPEG 字节（Sora multipart 上传用，体积小于 PNG）。"""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _sora_seconds(duration_seconds: float) -> str | None:
    """镜头时长 → 平台已实锤的时长档（2026-10-03 实测 4/8/10/15 四档）。

    ≤5s 省略参数走默认 4s 档；6~9→"8"；10~12→"10"；其余→"15"。
    更高档位未实测，钳在 15 防误触未知计费档。
    """
    d = float(duration_seconds or 5.0)
    if d <= 5:
        return None
    if d <= 9:
        return "8"
    if d <= 12:
        return "10"
    return "15"


def _sora_submit(ep: CloudEndpoint, prompt: str, first_frame,
                 duration_seconds: float) -> str:
    """Sora 任务式提交（POST /v1/videos），返回任务 id。

    有首帧图= multipart（input_reference 文件域，2026-10-03 实测路由层
    接受；上游 i2v 通道当时 502 待复验）；无首帧= JSON 纯文生视频。
    seconds 参数只带平台已实锤档位（_sora_seconds）。
    """
    import requests

    url = ep.base_url + "/v1/videos"
    fields: dict[str, str] = {"model": ep.model or "grok-imagine-video",
                              "prompt": prompt}
    seconds = _sora_seconds(duration_seconds)
    if seconds:
        fields["seconds"] = seconds
    headers = {"Authorization": f"Bearer {ep.api_key}"} if ep.api_key else {}
    # 2026-10-06 用户实报 415：grok-imagine-video 系不接受 multipart
    # input_reference（xAI 上游 415）——图生视频改走 JSON + image
    # dataurl（t2v/i2v 双形态实测 200）。官方 OpenAI Sora 域名保留
    # multipart 旧行为
    use_json_i2v = ("magicai" in (ep.base_url or "").lower()
                    and first_frame is not None)
    # 502/503 网关类重试（2026-10-06 MagicAI 实测：上游间歇 502 约 20s
    # 自愈——直连单发成功后紧跟 502 两连，带退避重试过波峰）
    _retryable = (502, 503, 504)
    try:
        resp = None
        for _attempt in range(4):
            if use_json_i2v:
                import base64 as _b64
                dataurl = ("data:image/jpeg;base64," +
                           _b64.b64encode(_jpeg_bytes(first_frame)).decode())
                resp = requests.post(
                    url, json={**fields, "image": dataurl},
                    headers={**headers,
                             "Content-Type": "application/json"},
                    timeout=(10, 120))
            elif first_frame is not None:
                resp = requests.post(
                    url, data=fields, headers=headers,
                    files={"input_reference": ("first_frame.jpg",
                                               _jpeg_bytes(first_frame),
                                               "image/jpeg")},
                    timeout=(10, 120))
            else:
                resp = requests.post(url, json=fields,
                                     headers={**headers,
                                              "Content-Type": "application/json"},
                                     timeout=(10, 120))
            if resp.status_code not in _retryable or _attempt >= 3:
                break
            time.sleep(15.0 * (_attempt + 1))
    except Exception as exc:  # noqa: BLE001 - 网络异常收敛为带出路的错误
        raise RuntimeError(
            f"无法连接云端视频服务 {ep.provider_name or ep.base_url}：{exc}。"
            f"请检查网络与地址，或到{SETUP_HINT}点「测试连接」") from exc
    if resp is not None and resp.status_code == 404:
        # 404 = 该站无 /v1/videos（非 Sora 形态）→ 自动降级 DashScope
        # 三段式（2026-10-06 用户令「三个下拉选项」：视频统一选项，
        # 双形态自适应，用户不再分辨协议）
        # P1-02 修复（2026-10-24）：返回 (task_id, degraded=True) 让
        # 调用方翻转 is_sora——此前调用方 is_sora 常量不随降级翻转，
        # 轮询 URL/间隔/解析器仍走 sora 路径必 404
        log.warning("Sora 提交 404（非 Sora 形态站），降级 DashScope 三段式")
        return _ds_submit(ep, prompt, first_frame, duration_seconds, "16:9"), True
    if resp is None or resp.status_code != 200:
        body = ((resp.text if resp is not None else "") or "")[:200]
        retry_note = "（502/503 已自动重试 4 次）" if resp is not None else ""
        raise RuntimeError(
            f"云端视频任务提交失败 HTTP {getattr(resp, 'status_code', 'N/A')}：{body}{retry_note}。"
            f"{http_error_hint(getattr(resp, 'status_code', 0))}"
            f"（当前模型={ep.model or '默认'}；时长档位也可能不受支持）")
    out = resp.json() or {}
    task_id = str(out.get("id") or out.get("request_id") or "")
    if not task_id:
        raise RuntimeError(
            f"云端视频任务提交未返回任务 id：{str(out)[:150]}")
    return task_id, False  # (task_id, degraded) — P1-02


def _sora_parse(data: dict) -> tuple[str, str, str]:
    """解析 Sora 任务式轮询响应 → (归一状态, video_url, 失败信息)。

    实测形态（2026-10-03 MagicAI）：{"id","status","video":{...},
    "video_url"}——终态词是 **done**（非 completed）；video.url 可能是
    相对路径（需拼 base_url）。
    """
    status = str(data.get("status") or "").strip().lower()
    if status in ("done", "succeeded", "completed"):
        url = str(data.get("video_url")
                  or (data.get("video") or {}).get("url") or "")
        if not url:
            return "failed", "", "任务成功但未找到视频地址：" + str(data)[:200]
        return "done", url, ""
    if status in ("failed", "error", "cancelled", "canceled", "rejected"):
        return ("failed", "",
                str(data.get("error") or data.get("message") or data)[:200])
    return "running", "", ""


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
            f"云端视频任务提交失败 HTTP {resp.status_code}：{body}。"
            f"{http_error_hint(resp.status_code)}"
            f"（当前模型={ep.model or '默认'}；时长/画幅参数也可能"
            "不受该模型支持）")
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
    # 2026-10-06 用户令「三个下拉选项」：连接类型归并后视频统一走
    # sora_video 协议值，本函数按 base_url 端点形态自动适配——
    # /v1/videos 通=sora 形态；404/非 sora=自动降级 DashScope 三段式
    is_sora = ep.protocol == PROTOCOL_SORA_VIDEO
    if ep.protocol not in (PROTOCOL_TASK_VIDEO, PROTOCOL_SORA_VIDEO):
        raise RuntimeError(
            f"连接「{ep.provider_name}」的协议 {ep.protocol} 不支持视频"
            "生成，请到设置里换绑视频类连接（task_video 或 sora_video）")
    if first_frame is None and not is_sora:
        # sora_video 平台支持纯文生视频，允许无首帧；DashScope 形态维持
        # 既有契约（漫剧工位语义=图生视频）
        raise RuntimeError(
            "云端图生视频需要首帧图：请先为该分镜生成关键帧（或到设置"
            "解绑「漫剧镜头视频」工位回本地引擎）")
    if not is_sora:
        vendor = str((ep.extra or {}).get("vendor") or "dashscope")
        if vendor != "dashscope":
            raise RuntimeError(
                f"连接「{ep.provider_name}」的供应商模板 {vendor} 尚未支持"
                "（当前支持 dashscope）")
    _check(check_cancel)
    t0 = time.time()
    try:
        vendor = str((ep.extra or {}).get("vendor") or
                     ("dashscope" if not is_sora else "sora"))
        if is_sora:
            task_id, _degraded = _sora_submit(
                ep, prompt, first_frame, duration_seconds)
            if _degraded:
                # P1-02：实际通道已切 DashScope——is_sora/vendor 翻转，
                # 轮询 URL/间隔/解析器随走 DashScope 路径
                is_sora = False
                vendor = "dashscope"
        elif vendor == "kling":
            task_id = _kling_submit(ep, prompt, first_frame, duration_seconds)
        elif vendor == "ark":
            task_id = _ark_submit(ep, prompt, first_frame, duration_seconds)
        elif vendor == "vidu":
            task_id = _vidu_submit(ep, prompt, first_frame, duration_seconds)
        else:
            task_id = _ds_submit(ep, prompt, first_frame, duration_seconds,
                                 aspect)
        log.info("云端视频任务已提交: %s model=%s task=%s 时长=%.0fs 画幅=%s 协议=%s vendor=%s",
                    ep.provider_name, ep.model or "默认", task_id,
                    duration_seconds, aspect, ep.protocol, vendor)
        if on_progress:
            on_progress(5)
        if is_sora:
            poll_url = ep.base_url + f"/v1/videos/{task_id}"
        elif vendor == "kling":
            poll_url = (ep.base_url.rstrip("/") +
                        f"/v1/videos/{task_id}")
        elif vendor == "ark":
            poll_url = (ep.base_url.rstrip("/") +
                        f"/api/v3/contents/generations/tasks/{task_id}")
        elif vendor == "vidu":
            poll_url = (ep.base_url.rstrip("/") +
                        f"/resp/v1/video/generations/{task_id}")
        else:
            poll_url = ep.base_url + f"/api/v1/tasks/{task_id}"
        poll_interval = SORA_POLL_INTERVAL_S if is_sora else POLL_INTERVAL_S
        poll_headers = (_kling_headers(ep) if vendor == "kling"
                        else _bearer(ep))
        # R3-L13（2026-10-24）：monotonic 墙钟——time.time() 受 NTP/
        # 改钟跳变影响（30 分钟窗口内跳 1 分钟即误判超时/永超时）
        deadline = time.monotonic() + TASK_TIMEOUT_S
        video_url = ""
        status = "unknown"  # 超时文案引用；kling 分支用 st，先绑定防空引用
        while True:
            _check(check_cancel)
            try:
                resp = requests.get(poll_url, headers=poll_headers,
                                    timeout=(5, 30))
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"云端视频任务轮询失败：{exc}。任务已在服务商侧排队，"
                    "可稍后到服务商后台查看结果") from exc
            if resp.status_code != 200:
                raise RuntimeError(
                    f"云端视频任务轮询 HTTP {resp.status_code}："
                    f"{(resp.text or '')[:150]}")
            if vendor in ("kling", "ark", "vidu"):
                data = resp.json() or {}
                st = str(data.get("status") or data.get("task_status")
                         or "").lower()
                status = st or status
                if st in ("succeed", "succeeded", "done", "completed",
                          "finished"):
                    video_url = (str(data.get("video_url") or "")
                                 or str(data.get("content", {})
                                        .get("video_url") or "")
                                 or str(data.get("videos", [{}])[0]
                                        .get("url") if data.get("videos")
                                        else "") or "")
                    if not video_url:
                        raise RuntimeError(
                            f"{vendor} 任务成功但未找到视频地址："
                            f"{str(data)[:200]}")
                    break
                if st in ("failed", "error"):
                    _fail = str(data.get('failed_reason')
                                or data.get('error') or data)
                    raise RuntimeError(
                        f'云端视频任务失败：{_fail[:200]}')
            elif is_sora:
                status, video_url, fail_msg = _sora_parse(resp.json() or {})
                if status == "done":
                    break
                if status == "failed":
                    raise RuntimeError(
                        f"云端视频任务失败：{fail_msg}。可在服务商后台"
                        "查看任务详情；常见原因=首帧图触发内容审核或"
                        "参数不支持（平台暂无取消接口，已提交任务会继续"
                        "计费）")
            else:
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
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"云端视频任务超时（>{TASK_TIMEOUT_S / 60:.0f} 分钟，"
                    f"状态={status}）。服务商可能排队拥塞，可到服务商后台"
                    "确认任务状态后重试")
            if on_progress:
                on_progress(min(85, 10 + int(
                    (time.monotonic() + TASK_TIMEOUT_S - deadline)
                    / TASK_TIMEOUT_S * 75)))
            time.sleep(poll_interval)
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
        # P2-12 修复（2026-10-25）：成功事件移到 return 前——此前在
        # return 之后=死代码，时间线只见失败不见成功
        record_cloud_call("video", ep.provider_name, ep.model, True,
                          int((time.time() - t0) * 1000), slot=slot)
        return resp.content
    except Exception as exc:
        record_cloud_call("video", ep.provider_name, ep.model,
                          False, int((time.time() - t0) * 1000),
                          slot=slot, detail=str(exc)[:300])
        raise


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

    if ep.protocol not in (PROTOCOL_TASK_VIDEO, PROTOCOL_SORA_VIDEO):
        return False, f"协议 {ep.protocol} 不支持视频连通性测试"
    headers = {"Authorization": f"Bearer {ep.api_key}"} if ep.api_key else {}
    vendor = str((ep.extra or {}).get("vendor") or "dashscope")
    _probe_paths = {"kling": "/v1/videos/__probe__",
                    "ark": "/api/v3/contents/generations/tasks/__probe__",
                    "vidu": "/resp/v1/video/generations/__probe__",
                    "dashscope": "/api/v1/tasks/__probe__"}
    probe_path = _probe_paths.get(vendor, "/api/v1/tasks/__probe__")
    if ep.protocol == PROTOCOL_SORA_VIDEO:
        # Sora 任务式探测（2026-10-03）：GET /v1/videos/__probe__ 查询
        # 不存在任务——绝不 POST 探测（会真创建任务计费）。gin 素 404
        # 「page not found」=路由不存在（协议不匹配）；结构化 404/200/
        # 401 前的鉴权语义与 DashScope 同思路甄别。
        try:
            resp = requests.get(ep.base_url + "/v1/videos/__probe__",
                                headers=headers, timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001 - 网络异常归为不可达
            return False, f"连接失败：{exc}"
        if resp.status_code in (401, 403):
            return False, (f"已连上服务商，但 API Key 被拒绝"
                           f"（HTTP {resp.status_code}）：请到服务商后台核对"
                           "Key 与开通的模型服务")
        if (resp.status_code == 404 and "page not found"
                in (resp.text or "").lower() and "<html" not in
                (resp.text or "").lower()):
            return False, ("已连上服务器，但 Sora 任务端点不存在（HTTP 404"
                           "）：请核对该服务商是否支持 /v1/videos 协议")
        if resp.status_code == 502:
            return False, ("服务商上游暂时不可用（HTTP 502）：稍后重试，"
                           "或到服务商后台确认视频服务状态")
        if 200 <= resp.status_code < 500:
            return True, ""
        return False, f"HTTP {resp.status_code}：{(resp.text or '')[:120]}"
    try:
        resp = requests.get(ep.base_url + probe_path,
                            headers=(headers if vendor != "kling"
                                     else _kling_headers(ep)),
                            timeout=timeout_s)
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

# ── 可灵 Kling / 火山方舟 Ark(豆包 Seedance) / Vidu 适配 ──────────
# 2026-10-06 用户令：国内外主流视频服务商对接。三家均任务式（提交→
# 轮询→取 url），差异在鉴权与端点：
#   kling: JWT 双 Key 签名（access_key/secret_key 经 extra 传入），
#          POST {base}/v1/videos/image2video，GET {base}/v1/videos/{id}
#   ark:   Bearer Key（火山方舟），POST {base}/api/v3/contents/generations
#          /tasks，GET {base}/api/v3/contents/generations/tasks/{id}
#   vidu:  Bearer Token，POST {base}/resp/v1/video/generations，
#          GET {base}/resp/v1/video/generations/{id}
# 连接配置：连接类型=视频；extra.vendor ∈ {dashscope, kling, ark, vidu}；
# kling 另需 extra.access_key / extra.secret_key。


def _kling_jwt(access_key: str, secret_key: str) -> str:
    """可灵 JWT 签名（HS256，exp 30 分钟）。依赖 PyJWT（环境已含）。"""
    import jwt as pyjwt

    now = int(time.time())
    payload = {"iss": access_key, "exp": now + 1800, "nbf": now - 5}
    return pyjwt.encode(payload, secret_key, algorithm="HS256")


def _kling_headers(ep: CloudEndpoint) -> dict:
    ak = str((ep.extra or {}).get("access_key") or "")
    sk = str((ep.extra or {}).get("secret_key") or "")
    return {"Authorization": "Bearer " + _kling_jwt(ak, sk),
            "Content-Type": "application/json"}


def _kling_submit(ep: CloudEndpoint, prompt: str, first_frame,
                  duration_seconds: float) -> str:
    """可灵图生视频提交：POST /v1/videos/image2video → data.task_id。"""
    import requests

    url = ep.base_url.rstrip("/") + "/v1/videos/image2video"
    payload: dict = {
        "model_name": ep.model or "kling-v1",
        "prompt": prompt,
        "duration": str(int(duration_seconds)) if duration_seconds < 10
        else "10",
        "mode": "std",
    }
    if first_frame is not None:

        buf = io_bytes_b64(first_frame)
        payload["image"] = "data:image/jpeg;base64," + buf
    resp = requests.post(url, json=payload, headers=_kling_headers(ep),
                         timeout=(10, 120))
    if resp.status_code != 200:
        raise RuntimeError(f"可灵提交失败 HTTP {resp.status_code}："
                           f"{(resp.text or '')[:200]}")
    data = (resp.json() or {}).get("data") or {}
    task_id = str(data.get("task_id") or "")
    if not task_id:
        raise RuntimeError("可灵提交未返回 task_id：" + str(data)[:150])
    return task_id


def io_bytes_b64(img) -> str:
    """PIL 图 → base64 JPEG 字符串（可灵 image 域要求）。"""
    import base64

    return base64.b64encode(_jpeg_bytes(img)).decode()


def _ark_submit(ep: CloudEndpoint, prompt: str, first_frame,
                duration_seconds: float) -> str:
    """火山方舟（豆包 Seedance）提交：content generations tasks。"""
    import requests

    url = (ep.base_url.rstrip("/") +
           "/api/v3/contents/generations/tasks")
    text = prompt
    if first_frame is not None:
        pass  # 首帧经 image_url 域在下方追加
    from typing import Any as _AnyT
    payload: dict[str, _AnyT] = {
        "model": ep.model or "doubao-seedance-1-0-lite-t2v",
        "content": [{"type": "text", "text": text}],
    }
    if first_frame is not None:
        payload["content"].append({
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," +
                          base64.b64encode(_jpeg_bytes(first_frame)).decode()}})
    resp = requests.post(url, json=payload,
                         headers={"Authorization": "Bearer " + ep.api_key,
                                  "Content-Type": "application/json"},
                         timeout=(10, 120))
    if resp.status_code != 200:
        raise RuntimeError(f"方舟提交失败 HTTP {resp.status_code}："
                           f"{(resp.text or '')[:200]}")
    task_id = str((resp.json() or {}).get("id") or "")
    if not task_id:
        raise RuntimeError("方舟提交未返回任务 id：" + str(resp.json())[:150])
    return task_id


def _vidu_submit(ep: CloudEndpoint, prompt: str, first_frame,
                 duration_seconds: float) -> str:
    """Vidu 提交：POST /resp/v1/video/generations → id。"""

    import requests

    url = ep.base_url.rstrip("/") + "/resp/v1/video/generations"
    payload: dict[str, Any] = {
        "model": ep.model or "viduq1",
        "prompt": prompt,
        "duration": int(duration_seconds) if duration_seconds < 10 else 8,
    }
    if first_frame is not None:
        img_entry: dict = {"type": "first_frame",
                           "content": "data:image/jpeg;base64," +
                           base64.b64encode(_jpeg_bytes(first_frame)).decode()}
        payload["images"] = [img_entry]
    resp = requests.post(url, json=payload,
                         headers={"Authorization": "Token " + ep.api_key,
                                  "Content-Type": "application/json"},
                         timeout=(10, 120))
    if resp.status_code != 200:
        raise RuntimeError(f"Vidu 提交失败 HTTP {resp.status_code}："
                           f"{(resp.text or '')[:200]}")
    task_id = str((resp.json() or {}).get("task_id") or "")
    if not task_id:
        raise RuntimeError("Vidu 提交未返回 task_id：" +
                           str(resp.json())[:150])
    return task_id
