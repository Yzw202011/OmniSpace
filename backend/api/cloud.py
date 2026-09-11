"""云端 API 服务商接入端点（云端API接入批1：地基+文本，2026-09-06）。

方案真源=docs/云端API接入方案-2026-09-06.md。用户自带 Key 直连服务商，
本机存储；对外一律打码（api_key_masked），编辑留空=保持原值。

端点一览（路由由 main.py 按 api.<name> 自动注册，前缀 /api/v1）：
- GET    /cloud/providers            连接列表（懒触发批3 旧配置迁移）
- POST   /cloud/providers            新增连接
- PUT    /cloud/providers/{pid}      更新（api_key 留空=保持原值）
- DELETE /cloud/providers/{pid}      删除（顺带清悬空绑定）
- POST   /cloud/providers/test       连通性测试（存过的 provider_id
                                     或现场 base_url+api_key 二选一）
- GET    /cloud/providers/{pid}/models   拉取 OpenAI 兼容模型列表
- GET    /cloud/bindings             工位绑定表
- PUT    /cloud/bindings/{slot}      绑定/换绑 {provider_id, model}
- DELETE /cloud/bindings/{slot}      解绑（该工位回本地引擎）
- GET    /cloud/status               文本引擎当前承载（对话页横幅用）
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body

from ..middleware.error_handler import ApiError, ok
from ..services import cloud_provider_service as svc

logger = logging.getLogger("omnispace.api.cloud")

router = APIRouter(prefix="/cloud")


def _guard(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """业务异常 → API 信封错误（code/message 直传，不吞栈）。"""
    try:
        return fn(*args, **kwargs)
    except svc.CloudProviderError as exc:
        raise ApiError(exc.code, exc.message) from exc


@router.get("/providers")
def cloud_providers_list() -> dict[str, Any]:
    """连接列表（打码；首次访问懒迁移批3 旧远程配置）。"""
    migrated = ensure_migrated()
    return ok({"providers": svc.list_providers(mask=True),
               "slots": svc.ALL_SLOTS, "legacy_migrated": migrated})


def ensure_migrated() -> int:
    """懒迁移包装（迁移失败不阻断列表读取）。"""
    try:
        return int(svc.ensure_legacy_remote_migrated())
    except Exception as exc:  # noqa: BLE001 - 迁移失败仅记日志
        logger.warning("旧远程配置懒迁移失败: %s", exc)
        return 0


@router.post("/providers")
def cloud_provider_create(req: dict = Body(...)) -> dict[str, Any]:
    """新增连接（api_key 仅本机存储，响应打码）。"""
    item = _guard(
        svc.create_provider,
        name=str(req.get("name") or ""),
        protocol=str(req.get("protocol") or svc.PROTOCOL_OPENAI_TEXT),
        base_url=str(req.get("base_url") or ""),
        api_key=str(req.get("api_key") or ""),
        models=list(req.get("models") or []),
        enabled=bool(req.get("enabled", True)))
    return ok(svc.get_provider(str(item["id"]), mask=True),
              message="连接已添加")


@router.put("/providers/{provider_id}")
def cloud_provider_update(provider_id: str,
                          req: dict = Body(...)) -> dict[str, Any]:
    """更新连接；api_key 传空串/缺省 = 保持原值（打码回显不还原）。"""
    models = req.get("models")
    item = _guard(
        svc.update_provider, provider_id,
        name=req.get("name"),
        protocol=req.get("protocol"),
        base_url=req.get("base_url"),
        api_key=str(req.get("api_key") or "") or None,
        models=list(models) if models is not None else None,
        enabled=req.get("enabled"))
    return ok(svc.get_provider(str(item["id"]), mask=True),
              message="连接已更新")


@router.delete("/providers/{provider_id}")
def cloud_provider_delete(provider_id: str) -> dict[str, Any]:
    """删除连接并清空指向它的工位绑定（悬空绑定解析处亦有防御）。"""
    return ok(_guard(svc.delete_provider, provider_id), message="连接已删除")


@router.post("/providers/test")
def cloud_provider_test(req: dict = Body(...)) -> dict[str, Any]:
    """连通性测试（协议感知）：优先按已存连接（provider_id，支持打码
    回显场景），否则用现场 base_url+api_key（openai_text 形态）。

    文本协议走 /health+/v1/models 探测；图片协议走适配器的协议感知
    探测（openai_image=/v1/models，task_image/task_video=任务端点
    鉴权探测——DashScope 形态无 /health，误用文本探测会假报不可达）。
    """
    from ..services.inference.backends.remote_backend import probe_remote_health
    from ..services.inference.cloud_image_client import probe_image_provider
    from ..services.inference.cloud_video_client import probe_video_provider

    provider_id = str(req.get("provider_id") or "").strip()
    if provider_id:
        prov = _guard(svc.get_provider, provider_id, mask=False)
        if prov is None:
            raise ApiError("CLOUD_PROVIDER_NOT_FOUND", "服务商连接不存在或已删除")
        base_url = str(prov.get("base_url") or "")
        api_key = str(prov.get("api_key") or "")
        protocol = str(prov.get("protocol") or "")
    else:
        base_url = str(req.get("base_url") or "").strip()
        api_key = str(req.get("api_key") or "").strip()
        protocol = str(req.get("protocol") or "openai_text")
    if protocol in svc.IMAGE_PROTOCOLS:
        ep = svc.CloudEndpoint(base_url=base_url, api_key=api_key,
                               protocol=protocol)
        reachable, detail = probe_image_provider(ep)
    elif protocol in svc.VIDEO_PROTOCOLS:
        ep = svc.CloudEndpoint(base_url=base_url, api_key=api_key,
                               protocol=protocol)
        reachable, detail = probe_video_provider(ep)
    else:
        reachable, detail = probe_remote_health(base_url, api_key, timeout_s=6.0)
    return ok({"reachable": reachable, "detail": detail,
               "base_url": base_url.rstrip("/")})


@router.get("/providers/{provider_id}/models")
def cloud_provider_models(provider_id: str) -> dict[str, Any]:
    """拉取该连接的 OpenAI 兼容模型列表（/v1/models，8s 超时）。"""
    prov = _guard(svc.get_provider, provider_id, mask=False)
    if prov is None:
        raise ApiError("CLOUD_PROVIDER_NOT_FOUND", "服务商连接不存在或已删除")
    ids, err = svc.fetch_openai_models(str(prov.get("base_url") or ""),
                                       str(prov.get("api_key") or ""))
    return ok({"models": ids, "error": err})


@router.get("/bindings")
def cloud_bindings_get() -> dict[str, Any]:
    """工位绑定表（补充 provider_name 便于直显）。"""
    ensure_migrated()
    return ok({"bindings": svc.get_bindings(mask=True),
               "slots": svc.ALL_SLOTS})


@router.put("/bindings/{slot}")
def cloud_binding_set(slot: str, req: dict = Body(...)) -> dict[str, Any]:
    """绑定/换绑工位 → {provider_id, model}；model 留空=用服务默认。"""
    bind = _guard(svc.set_binding, slot,
                  str(req.get("provider_id") or ""),
                  str(req.get("model") or ""))
    return ok(bind, message="工位已绑定")


@router.delete("/bindings/{slot}")
def cloud_binding_clear(slot: str) -> dict[str, Any]:
    """解绑工位（该工位回到本地引擎）。"""
    return ok(_guard(svc.clear_binding, slot), message="工位已解绑")


@router.get("/status")
def cloud_status() -> dict[str, Any]:
    """文本引擎当前承载模式（对话页/漫剧页横幅展示用）。"""
    ensure_migrated()
    ep = svc.get_dialog_text_endpoint()
    if ep is None:
        return ok({"mode": "local", "provider_id": "", "provider_name": "",
                   "model": ""})
    return ok({"mode": "cloud", "provider_id": ep.provider_id,
               "provider_name": ep.provider_name, "model": ep.model})
