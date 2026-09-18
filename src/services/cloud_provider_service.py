"""云端 API 服务商连接管理（云端API接入批1：地基+文本，2026-09-06）。

方案真源=docs/云端API接入方案-2026-09-06.md。用户自带 Key 直连服务商
官方端点，软件不经手中转；Provider（连接）与 Slot 绑定（工位→连接）
分开存储（system_settings KV 两个独立键），各功能工位未绑定时行为与
今天逐比特一致（纯增量安全网铁律）。

存储：
- cloud.providers  → JSON 数组（连接列表；api_key 以 AES-256-GCM 密文
  落库〔B0 2026-09-13，与 dialog_messages 同一套 crypto，DPAPI 绑机〕，
  历史明文首次读取时自动迁移回写；对外 API 一律打码，见 mask_key）；
- cloud.bindings   → JSON 对象（slot → {provider_id, model}）；
- cloud.legacy_migrated → 批3 旧 remote_dialog_* 单服务器配置一次性
  迁移标记（旧键保留不删，回退安全）。

批1 槽位：仅 dialog.text（AI 对话与写作全局文本工位）——对话页、
漫剧剧本/切分、小说共用同一 dialog_engine，绑定后三处自动受益；
manga.text / novel.text 独立绑定随批2/3 引擎按调用槽位路由时开放。

依赖方向（无环）：remote_backend → 本模块 → data.database；
本模块运行期对 remote_backend 仅有函数内延迟 import（旧配置兼容层，
保证其单测 monkeypatch _read_settings_kv 继续生效）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("omnispace.services.cloud_provider")

KV_PROVIDERS = "cloud.providers"
KV_BINDINGS = "cloud.bindings"
KV_LEGACY_MIGRATED = "cloud.legacy_migrated"
KV_SETTINGS = "cloud.settings"

# 对话与写作的全局文本工位（批1）
SLOT_DIALOG_TEXT = "dialog.text"
# 文本工位拆分（2026-09-06 用户组合场景：对话/漫剧文字/写作台各自
# 独立绑云或留本地）。拆分后语义：
# - dialog.text      对话页（含旧 remote_dialog 兼容层兜底）
# - manga.text       漫剧文字（AI 切分/描述词生成；VLM 一致性评分
#                     走图片输入，恒本地——remote 后端暂不支持图片）
# - novel.text       写作台/小说创作
# 互不影响的组合示例：对话=云端、绘画=本地、漫剧文字=云端、
# 漫剧生图=云端、漫剧视频=本地、写作台=云端。
SLOT_MANGA_TEXT = "manga.text"
SLOT_NOVEL_TEXT = "novel.text"
# 图片三工位（批2 云端API 2026-09-06）：绘画页/漫剧关键帧/漫剧资产图。
# 四视图与资产重生管线含本地 VLM 校验/SAM/修复步骤，暂不穿云端（本地道）。
SLOT_PAINT_IMAGE = "paint.image"
SLOT_KEYFRAME_IMAGE = "keyframe.image"
SLOT_ASSET_IMAGE = "asset.image"
# 视频工位（批3 云端API 2026-09-06）：漫剧镜头视频（图生视频）
SLOT_MANGA_VIDEO = "manga.video"
# 全部可绑定槽位注册表
ALL_SLOTS: dict[str, str] = {
    SLOT_DIALOG_TEXT: "AI 对话（对话页）",
    SLOT_MANGA_TEXT: "漫剧文字（AI 切分 / 描述词生成）",
    SLOT_NOVEL_TEXT: "写作台（小说创作）",
    SLOT_PAINT_IMAGE: "绘画页出图（文生图/图生图）",
    SLOT_KEYFRAME_IMAGE: "漫剧关键帧生成",
    SLOT_ASSET_IMAGE: "漫剧资产图（角色/场景/道具；四视图与重生暂走本地）",
    SLOT_MANGA_VIDEO: "漫剧镜头视频（图生视频，需该行已有关键帧）",
}
# 文本类槽位（openai_text 协议）
TEXT_SLOTS = (SLOT_DIALOG_TEXT, SLOT_MANGA_TEXT, SLOT_NOVEL_TEXT)
# 图片类槽位（get_image_endpoint 消费）
IMAGE_SLOTS = (SLOT_PAINT_IMAGE, SLOT_KEYFRAME_IMAGE, SLOT_ASSET_IMAGE)
# 图片类协议（绑定图片槽位时校验连接协议）
IMAGE_PROTOCOLS = ("openai_image", "task_image")
# 视频类槽位/协议
VIDEO_SLOTS = (SLOT_MANGA_VIDEO,)
VIDEO_PROTOCOLS = ("task_video",)

# 协议类型（批3 起 task_video 开放）
PROTOCOL_OPENAI_TEXT = "openai_text"
PROTOCOL_OPENAI_IMAGE = "openai_image"
PROTOCOL_TASK_IMAGE = "task_image"
PROTOCOL_TASK_VIDEO = "task_video"
VALID_PROTOCOLS = (PROTOCOL_OPENAI_TEXT, PROTOCOL_OPENAI_IMAGE,
                   PROTOCOL_TASK_IMAGE, PROTOCOL_TASK_VIDEO)


def looks_like_dashscope_task_404(resp: Any) -> bool:
    """404 返回体是否为 DashScope 任务查询形态（甄别协议不匹配的网关 404）。

    实测（2026-09-10 双厂商对照）：DashScope 有效 Key 查不存在任务返回
    结构化 JSON（{"request_id": ..., "output": {...}}）；MiniMax 等无此
    路径的服务商返回 nginx HTML——后者只证明服务器存在，鉴权未被验证，
    连通性测试不得假报「连接成功」。判据=JSON 对象且含 request_id。
    """
    try:
        body = (str(getattr(resp, "text", "") or ""))[:500].lstrip()
        if not body.startswith("{"):
            return False
        parsed = json.loads(body)
    except Exception:  # noqa: BLE001 - 非 JSON 按非 DashScope 形态
        return False
    return isinstance(parsed, dict) and "request_id" in parsed

# 绑定读取 TTL 缓存（写路径主动失效；读路径 5s 自愈，与 remote 配置同口径）
_TTL_S = 5.0
_cache_lock = threading.Lock()
_endpoint_cache: tuple[float, CloudEndpoint | None] | None = None
_kv_lock = threading.Lock()


@dataclass
class CloudEndpoint:
    """一次云端调用的连接参数（路由解析产物）。"""
    base_url: str
    api_key: str = ""
    model: str = ""
    provider_id: str = ""
    provider_name: str = ""
    protocol: str = PROTOCOL_OPENAI_TEXT
    models: list[str] = field(default_factory=list)
    # 供应商模板参数（task_image 协议的 vendor 模板等）
    extra: dict = field(default_factory=dict)

    def endpoint_key(self) -> str:
        """端点身份（切换检测用）：地址+模型 变了才算换目标。"""
        return f"{self.base_url}|{self.model}"


def mask_key(key: str) -> str:
    """API Key 打码（对外 API 一律走此函数，绝不回显完整 Key）。"""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "***"
    return f"{key[:3]}***{key[-4:]}"


# ── KV 读写（模块级函数，单测 monkeypatch 注入点）─────────────────

def _read_kv(key: str, default: object = None) -> object:
    """读 system_settings KV（异常降级 default，不抛）。"""
    try:
        from ..data.database import get_db_safe
        db = get_db_safe()
        if db is None:
            return default
        row = db.query_one(
            "SELECT value FROM system_settings WHERE key=?", (key,))
        if row and row.get("value"):
            return json.loads(row["value"])
    except Exception as exc:  # noqa: BLE001 - 配置读取失败按默认值
        log.debug("cloud KV 读取失败 %s: %s", key, exc)
    return default


def _write_kv(key: str, value: object) -> bool:
    """写 system_settings KV（UPSERT）。失败返回 False（调用方兜底）。"""
    try:
        from ..data.database import get_db_safe
        db = get_db_safe()
        if db is None:
            return False
        db.sql(
            "INSERT INTO system_settings (key, value, updated_at)"
            " VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET"
            " value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), time.time()))
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("cloud KV 写入失败 %s: %s", key, exc)
        return False


# ── Provider CRUD ────────────────────────────────────────────────

def _load_providers() -> list[dict]:
    """读连接列表；api_key 解密（enc:v1: 前缀，历史明文原样透传）。

    B0（2026-09-13）：api_key 落库改 AES-256-GCM（与 dialog_messages
    同一套 crypto，DPAPI 绑机）。首次读到历史明文时立即回写迁移，
    此后库内不再有明文密钥——整库快照/导出 tar 不再随库泄钥。
    """
    from ..data.crypto import decrypt_text, is_encrypted

    raw = _read_kv(KV_PROVIDERS, [])
    items = list(raw) if isinstance(raw, list) else []
    migrated = False
    for item in items:
        if not isinstance(item, dict):
            continue
        stored = item.get("api_key")
        if isinstance(stored, str) and stored and not is_encrypted(stored):
            migrated = True
        if isinstance(stored, str):
            item["api_key"] = decrypt_text(stored)
    if migrated:
        try:
            _save_providers(items)  # _save_providers 统一加密——一次性迁移
            log.info("云端 API Key 明文已迁移为密文落库（%d 条）", len(items))
        except Exception:  # noqa: BLE001 - 迁移失败不影响读取（下次重试）
            log.warning("云端 api_key 明文迁移回写失败（下次读取重试）")
    return items


def _save_providers(items: list[dict]) -> bool:
    from ..data.crypto import encrypt_text, is_encrypted

    persisted: list[object] = []
    for item in items:
        if isinstance(item, dict):
            item = dict(item)
            key = item.get("api_key")
            if isinstance(key, str) and key and not is_encrypted(key):
                item["api_key"] = encrypt_text(key)
        persisted.append(item)
    return _write_kv(KV_PROVIDERS, persisted)


def list_providers(mask: bool = True) -> list[dict]:
    """连接列表（mask=True 时 api_key 打码；内部路由用 mask=False）。"""
    out: list[dict] = []
    for p in _load_providers():
        item = dict(p)
        if mask:
            item["api_key_masked"] = mask_key(str(item.get("api_key") or ""))
            item.pop("api_key", None)
        out.append(item)
    return out


def get_provider(provider_id: str, mask: bool = True) -> dict | None:
    for p in _load_providers():
        if p.get("id") == provider_id:
            item = dict(p)
            if mask:
                item["api_key_masked"] = mask_key(
                    str(item.get("api_key") or ""))
                item.pop("api_key", None)
            return item
    return None


class CloudProviderError(Exception):
    """Provider 配置业务错误（message 面向用户，code 面向信封）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _validate_provider_fields(name: str, protocol: str,
                              base_url: str) -> None:
    if not name.strip():
        raise CloudProviderError(
            "CLOUD_PARAM_INVALID", "服务商名称不能为空")
    if protocol not in VALID_PROTOCOLS:
        raise CloudProviderError(
            "CLOUD_PARAM_INVALID", f"不支持的协议类型: {protocol}")
    base = base_url.strip()
    if not base:
        raise CloudProviderError(
            "CLOUD_PARAM_INVALID", "服务地址不能为空")
    if not (base.startswith("http://") or base.startswith("https://")):
        raise CloudProviderError(
            "CLOUD_PARAM_INVALID", "服务地址必须以 http:// 或 https:// 开头")


def create_provider(name: str, protocol: str, base_url: str,
                    api_key: str = "", models: list[str] | None = None,
                    enabled: bool = True,
                    extra: dict | None = None) -> dict:
    """新增连接（返回含 id 的完整对象；api_key 仅本机存储）。"""
    _validate_provider_fields(name, protocol, base_url)
    with _kv_lock:
        items = _load_providers()
        now = time.time()
        item: dict = {
            "id": f"prov_{uuid.uuid4().hex[:10]}",
            "name": name.strip(),
            "protocol": protocol,
            "base_url": base_url.strip().rstrip("/"),
            "api_key": (api_key or "").strip(),
            "models": [str(m).strip() for m in (models or []) if str(m).strip()],
            "enabled": bool(enabled),
            "extra": dict(extra) if isinstance(extra, dict) else {},
            "created_at": now,
            "updated_at": now,
        }
        items.append(item)
        if not _save_providers(items):
            raise CloudProviderError(
                "CLOUD_STORAGE_FAILED", "保存失败（本地数据库不可用）")
    _invalidate_route_cache()
    log.info("云端服务商已添加: %s (%s) %s", item["name"], item["protocol"],
                item["base_url"])
    return item


def update_provider(provider_id: str, name: str | None = None,
                    protocol: str | None = None,
                    base_url: str | None = None,
                    api_key: str | None = None,
                    models: list[str] | None = None,
                    enabled: bool | None = None,
                    extra: dict | None = None) -> dict:
    """更新连接；api_key 传 None 或空串 = 保持原值（前端打码回显不还原）。"""
    with _kv_lock:
        items = _load_providers()
        target: dict | None = None
        for p in items:
            if p.get("id") == provider_id:
                target = p
                break
        if target is None:
            raise CloudProviderError(
                "CLOUD_PROVIDER_NOT_FOUND", "服务商连接不存在或已删除")
        new_name = target["name"] if name is None else name
        new_protocol = target["protocol"] if protocol is None else protocol
        new_base = target["base_url"] if base_url is None else base_url
        _validate_provider_fields(new_name, new_protocol, new_base)
        target["name"] = new_name.strip()
        target["protocol"] = new_protocol
        target["base_url"] = new_base.strip().rstrip("/")
        if api_key:  # 空串/None=保持原值
            target["api_key"] = api_key.strip()
        if models is not None:
            target["models"] = [str(m).strip() for m in models
                                if str(m).strip()]
        if extra is not None and isinstance(extra, dict):
            target["extra"] = dict(extra)
        if enabled is not None:
            target["enabled"] = bool(enabled)
        target["updated_at"] = time.time()
        if not _save_providers(items):
            raise CloudProviderError(
                "CLOUD_STORAGE_FAILED", "保存失败（本地数据库不可用）")
    _invalidate_route_cache()
    return dict(target)


def delete_provider(provider_id: str) -> dict:
    """删除连接并顺带清空指向它的绑定（悬空绑定在解析处亦有防御）。"""
    with _kv_lock:
        items = _load_providers()
        remain = [p for p in items if p.get("id") != provider_id]
        if len(remain) == len(items):
            raise CloudProviderError(
                "CLOUD_PROVIDER_NOT_FOUND", "服务商连接不存在或已删除")
        if not _save_providers(remain):
            raise CloudProviderError(
                "CLOUD_STORAGE_FAILED", "保存失败（本地数据库不可用）")
        bindings = _read_kv(KV_BINDINGS, {})
        changed = False
        if isinstance(bindings, dict):
            for slot, bind in list(bindings.items()):
                if isinstance(bind, dict) \
                        and bind.get("provider_id") == provider_id:
                    bindings.pop(slot, None)
                    changed = True
            if changed:
                _write_kv(KV_BINDINGS, bindings)
    _invalidate_route_cache()
    log.info("云端服务商已删除: %s", provider_id)
    return {"deleted": provider_id, "bindings_cleared": changed}


# ── 槽位绑定 ─────────────────────────────────────────────────────

def get_bindings(mask: bool = True) -> dict[str, dict]:
    """槽位绑定表（provider 侧信息补充名称便于前端直显）。"""
    raw = _read_kv(KV_BINDINGS, {})
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    names = {p.get("id"): p.get("name") for p in _load_providers()}
    for slot, bind in raw.items():
        if not isinstance(bind, dict):
            continue
        item = dict(bind)
        pid = str(item.get("provider_id") or "")
        item["provider_name"] = names.get(pid, "（已删除的连接）")
        if mask and pid:
            prov = get_provider(pid, mask=True)
            if prov is not None:
                item["api_key_masked"] = prov.get("api_key_masked", "")
        out[str(slot)] = item
    return out


def set_binding(slot: str, provider_id: str, model: str = "") -> dict:
    """绑定工位 → 连接+模型（未绑定的槽位恒走本地引擎）。

    协议匹配校验：文本槽位须 openai_text 连接，图片槽位须图片协议
    （openai_image/task_image）连接——跨类绑定直接拒绝（防「绑了但跑
    不通」的隐性坏配置）。
    """
    if slot not in ALL_SLOTS:
        raise CloudProviderError("CLOUD_SLOT_UNKNOWN", f"未知工位: {slot}")
    with _kv_lock:
        items = _load_providers()
        prov = next((p for p in items if p.get("id") == provider_id), None)
        if prov is None:
            raise CloudProviderError(
                "CLOUD_PROVIDER_NOT_FOUND", "服务商连接不存在或已删除")
        if not prov.get("enabled"):
            raise CloudProviderError(
                "CLOUD_PROVIDER_DISABLED", "该连接已停用，请先启用再绑定")
        proto = str(prov.get("protocol") or "")
        if slot in IMAGE_SLOTS and proto not in IMAGE_PROTOCOLS:
            raise CloudProviderError(
                "CLOUD_PROTOCOL_MISMATCH",
                f"该工位需要图片类连接（openai_image/task_image），"
                f"当前连接协议为 {proto}")
        if slot in VIDEO_SLOTS and proto not in VIDEO_PROTOCOLS:
            raise CloudProviderError(
                "CLOUD_PROTOCOL_MISMATCH",
                f"该工位需要视频类连接（task_video），"
                f"当前连接协议为 {proto}")
        if slot in TEXT_SLOTS and proto != PROTOCOL_OPENAI_TEXT:
            raise CloudProviderError(
                "CLOUD_PROTOCOL_MISMATCH",
                f"该工位需要 OpenAI 兼容文本连接，当前连接协议为 {proto}")
        bindings = _read_kv(KV_BINDINGS, {})
        if not isinstance(bindings, dict):
            bindings = {}
        bindings[slot] = {
            "provider_id": provider_id,
            "model": (model or "").strip(),
            "updated_at": time.time(),
        }
        if not _write_kv(KV_BINDINGS, bindings):
            raise CloudProviderError(
                "CLOUD_STORAGE_FAILED", "保存失败（本地数据库不可用）")
    _invalidate_route_cache()
    log.info("工位绑定: %s → %s · %s", slot, prov["name"], model)
    return bindings[slot]


def clear_binding(slot: str) -> dict:
    """解绑工位（该工位回到本地引擎）。

    槽位名不做注册表校验（宽松清理）：历史占位槽位/已下线槽位的残留
    绑定也应可清。
    """
    with _kv_lock:
        bindings = _read_kv(KV_BINDINGS, {})
        if isinstance(bindings, dict) and slot in bindings:
            bindings.pop(slot, None)
            if not _write_kv(KV_BINDINGS, bindings):
                raise CloudProviderError(
                    "CLOUD_STORAGE_FAILED", "保存失败（本地数据库不可用）")
    _invalidate_route_cache()
    return {"cleared": slot}


# ── 路由解析（remote_backend / dialog_engine / gpu_domains 消费）──

def legacy_remote_fallback_allowed() -> bool:
    """批3 旧配置兼容层是否允许兜底。

    未迁移（KV 无标记）→ 允许（老版本升级用户首次打开设置页前，旧
    remote_dialog_* 配置继续生效，行为不变）；已迁移 → 永久退位
    （单一真源=Provider：用户在新 UI 停用/删除全部连接即回本地引擎，
    旧 remote_dialog_* 残留键不得把云端悄悄拉起来）。
    """
    return not bool(_read_kv(KV_LEGACY_MIGRATED, False))


def get_dialog_text_endpoint(force: bool = False) -> CloudEndpoint | None:
    """解析「AI 对话与写作」当前云端端点；未绑定时回落批3 旧配置。

    优先级：dialog.text 槽位绑定 → 旧 remote_dialog_* 配置（批3 兼容
    层，仅未迁移机器生效，函数内延迟 import 保证其单测 monkeypatch
    继续生效）。两者皆无返回 None = 本地引擎（旧行为）。
    """
    global _endpoint_cache
    with _cache_lock:
        if not force and _endpoint_cache is not None and (
                time.time() - _endpoint_cache[0] < _TTL_S):
            return _endpoint_cache[1]

    endpoint: CloudEndpoint | None = None
    bindings = _read_kv(KV_BINDINGS, {})
    bind = bindings.get(SLOT_DIALOG_TEXT) if isinstance(bindings, dict) \
        else None
    if isinstance(bind, dict) and bind.get("provider_id"):
        prov = next((p for p in _load_providers()
                     if p.get("id") == bind.get("provider_id")), None)
        if prov is not None and prov.get("enabled") \
                and prov.get("protocol") == PROTOCOL_OPENAI_TEXT:
            endpoint = CloudEndpoint(
                base_url=str(prov.get("base_url") or "").rstrip("/"),
                api_key=str(prov.get("api_key") or ""),
                model=str(bind.get("model") or ""),
                provider_id=str(prov.get("id") or ""),
                provider_name=str(prov.get("name") or ""),
                protocol=PROTOCOL_OPENAI_TEXT,
                models=[str(m) for m in prov.get("models") or []],
            )
        elif prov is not None:
            # 绑定指向的连接被停用/协议不匹配：视为未绑定（走本地），
            # 并提示原因供状态端点展示
            endpoint = None
            log.info(
                "dialog.text 绑定的连接 %s 已停用或协议不匹配，回落本地引擎",
                bind.get("provider_id"))

    if endpoint is None and legacy_remote_fallback_allowed():
        try:  # 批3 兼容层：未迁移机器的旧 remote_dialog_* 配置仍生效
            from ..services.inference.backends.remote_backend import get_remote_config
            cfg = get_remote_config(force=force)
            if cfg.enabled and cfg.base_url:
                endpoint = CloudEndpoint(
                    base_url=cfg.base_url, api_key=cfg.api_key,
                    model=cfg.model, provider_id="",
                    provider_name="远程推理服务器",
                    protocol=PROTOCOL_OPENAI_TEXT)
        except Exception:  # noqa: BLE001 - 兼容层失败按未配置
            endpoint = None

    with _cache_lock:
        _endpoint_cache = (time.time(), endpoint)
    return endpoint


def _invalidate_route_cache() -> None:
    """绑定/连接变更后清路由缓存（写路径调用）。"""
    global _endpoint_cache
    with _cache_lock:
        _endpoint_cache = None


def has_dialog_cloud_endpoint() -> bool:
    """文本引擎当前是否由云端承载（gpu_domains 资源域裁决用）。"""
    return get_dialog_text_endpoint() is not None


def has_dialog_binding_enabled() -> bool:
    """dialog.text 是否绑定了启用的云端连接（无缓存直读版）。

    与 has_dialog_cloud_endpoint 的区别：不走 5s TTL 缓存、不回落旧
    remote_dialog 配置——供 remote_backend.is_remote_dialog_enabled
    组合裁决（绑定优先 + 旧配置兜底），保证其单测 monkeypatch 语义。
    """
    bindings = _read_kv(KV_BINDINGS, {})
    bind = bindings.get(SLOT_DIALOG_TEXT) if isinstance(bindings, dict) \
        else None
    if not (isinstance(bind, dict) and bind.get("provider_id")):
        return False
    prov = next((p for p in _load_providers()
                 if p.get("id") == bind.get("provider_id")), None)
    return bool(prov and prov.get("enabled")
                and prov.get("protocol") == PROTOCOL_OPENAI_TEXT)


# ── 图片工位路由（批2：draw/keyframe/asset 提交点消费）────────────

def get_image_endpoint(slot: str) -> CloudEndpoint | None:
    """解析图片工位的云端端点；未绑定/连接停用/协议不符返回 None（本地）。

    每次生成提交时直读（不走 TTL 缓存）：提交频率低（秒级一张），
    直读保证绑定切换即时生效。
    """
    if slot not in IMAGE_SLOTS:
        return None
    bindings = _read_kv(KV_BINDINGS, {})
    bind = bindings.get(slot) if isinstance(bindings, dict) else None
    if not (isinstance(bind, dict) and bind.get("provider_id")):
        return None
    prov = next((p for p in _load_providers()
                 if p.get("id") == bind.get("provider_id")), None)
    if prov is None or not prov.get("enabled"):
        return None
    proto = str(prov.get("protocol") or "")
    if proto not in IMAGE_PROTOCOLS:
        return None
    return CloudEndpoint(
        base_url=str(prov.get("base_url") or "").rstrip("/"),
        api_key=str(prov.get("api_key") or ""),
        model=str(bind.get("model") or ""),
        provider_id=str(prov.get("id") or ""),
        provider_name=str(prov.get("name") or ""),
        protocol=proto,
        models=[str(m) for m in prov.get("models") or []],
        extra=dict(prov.get("extra") or {}),
    )


def get_image_concurrency() -> int:
    """云端图片并发上限（cloud.settings.image_concurrency，默认 2，钳 1~8）。

    云端道独立于本地单 worker；上限防触发服务商限流。
    """
    raw = _read_kv(KV_SETTINGS, {})
    if not isinstance(raw, dict):
        return 2
    try:
        return max(1, min(8, int(raw.get("image_concurrency", 2))))
    except (TypeError, ValueError):
        return 2


# ── 视频工位路由（批3：manga/video.py 提交点消费）────────────────

def get_video_endpoint(slot: str = SLOT_MANGA_VIDEO) -> CloudEndpoint | None:
    """解析视频工位的云端端点；未绑定/连接停用/协议不符返回 None（本地）。

    每次生成提交时直读（同 get_image_endpoint 语义）。
    """
    if slot not in VIDEO_SLOTS:
        return None
    bindings = _read_kv(KV_BINDINGS, {})
    bind = bindings.get(slot) if isinstance(bindings, dict) else None
    if not (isinstance(bind, dict) and bind.get("provider_id")):
        return None
    prov = next((p for p in _load_providers()
                 if p.get("id") == bind.get("provider_id")), None)
    if prov is None or not prov.get("enabled"):
        return None
    proto = str(prov.get("protocol") or "")
    if proto not in VIDEO_PROTOCOLS:
        return None
    return CloudEndpoint(
        base_url=str(prov.get("base_url") or "").rstrip("/"),
        api_key=str(prov.get("api_key") or ""),
        model=str(bind.get("model") or ""),
        provider_id=str(prov.get("id") or ""),
        provider_name=str(prov.get("name") or ""),
        protocol=proto,
        models=[str(m) for m in prov.get("models") or []],
        extra=dict(prov.get("extra") or {}),
    )


def get_video_concurrency() -> int:
    """云端视频并发上限（cloud.settings.video_concurrency，默认 1，钳 1~4）。

    视频任务分钟级且计费重，默认串行（并发 1）防限流与误烧钱。
    """
    raw = _read_kv(KV_SETTINGS, {})
    if not isinstance(raw, dict):
        return 1
    try:
        return max(1, min(4, int(raw.get("video_concurrency", 1))))
    except (TypeError, ValueError):
        return 1


# ── 文本槽位解析（漫剧文字/写作台调用点消费）────────────────────

def _slot_text_endpoint(slot: str) -> CloudEndpoint | None:
    """文本槽位绑定解析（binding-only，不含 dialog 旧配置兜底）。"""
    if slot not in TEXT_SLOTS:
        return None
    bindings = _read_kv(KV_BINDINGS, {})
    bind = bindings.get(slot) if isinstance(bindings, dict) else None
    if not (isinstance(bind, dict) and bind.get("provider_id")):
        return None
    prov = next((p for p in _load_providers()
                 if p.get("id") == bind.get("provider_id")), None)
    if prov is None or not prov.get("enabled") \
            or str(prov.get("protocol") or "") != PROTOCOL_OPENAI_TEXT:
        return None
    return CloudEndpoint(
        base_url=str(prov.get("base_url") or "").rstrip("/"),
        api_key=str(prov.get("api_key") or ""),
        model=str(bind.get("model") or ""),
        provider_id=str(prov.get("id") or ""),
        provider_name=str(prov.get("name") or ""),
        protocol=PROTOCOL_OPENAI_TEXT,
        models=[str(m) for m in prov.get("models") or []],
    )


def resolve_slot_cloud_model(slot: str) -> str | None:
    """文本槽位 → 云端虚拟模型 id（调用点 ensure_loaded 直接可用）。

    返回 ``cloud::prov_xxx::model`` 或 None（未绑定/连接停用=本地引擎，
    调用点保持旧行为）。漫剧文字/写作台接线用。
    """
    ep = _slot_text_endpoint(slot)
    if ep is None:
        return None
    return f"cloud::{ep.provider_id}::{ep.model or 'remote-model'}"


# ── 云端调用事件日志（系统日志升级，2026-09-06 用户要求）────────

def record_cloud_call(kind: str, provider_name: str, model: str,
                      ok: bool, latency_ms: int,
                      *, slot: str = "", detail: str = "") -> None:
    """云端调用落事件日志（logs/events JSONL，前端日志面板可查）。

    kind: text/image/video；friendly 用大白话（用户裁定的事件日志
    契约），detail 带技术字段。失败事件 level=error 带出路提示。
    """
    try:
        from .event_log import log_event
        kind_label = {"text": "文本", "image": "出图",
                      "video": "出片"}.get(kind, kind)
        slot_label = ALL_SLOTS.get(slot, "") if slot else ""
        prefix = f"云端{kind_label}（{provider_name}"
        if model:
            prefix += f" · {model}"
        prefix += "）"
        if ok:
            friendly = f"{prefix}成功，用时 {latency_ms / 1000:.1f} 秒"
            level = "success"
        else:
            friendly = (f"{prefix}失败：{detail[:80]}。可到"
                        "「设置 → 云端 API 服务」点「测试连接」排查")
            level = "error"
        log_event("cloud", f"call_{kind}_{'ok' if ok else 'fail'}",
                  friendly, level=level,
                  detail=(f"slot={slot_label or slot or '-'} "
                          f"provider={provider_name} model={model or '-'} "
                          f"latency_ms={latency_ms}"
                          + (f" error={detail[:200]}" if detail else "")),
                  duration_ms=latency_ms)
    except Exception as exc:  # noqa: BLE001 - 日志失败绝不影响业务
        log.debug("云端调用事件日志写入失败: %s", exc)


# ── 请求级云端虚拟模型（cloud::prov_xxx::model-name）─────────────

CLOUD_MODEL_PREFIX = "cloud::"


def parse_cloud_model_id(model_id: str) -> tuple[str, str] | None:
    """解析云端虚拟模型 id。

    ``cloud::prov_xxx::model-name`` → (provider_id, model)；
    非云端格式返回 None。provider_id 为空视为非法返回 None。
    """
    if not model_id or not model_id.startswith(CLOUD_MODEL_PREFIX):
        return None
    rest = model_id[len(CLOUD_MODEL_PREFIX):]
    prov_id, _, model = rest.partition("::")
    if not prov_id.strip():
        return None
    return prov_id.strip(), model.strip()


def resolve_provider_endpoint(provider_id: str) -> CloudEndpoint | None:
    """按 provider_id 解析请求级端点（模型名留空由调用方补）。

    连接不存在/停用/协议不符返回 None（调用方给带出路的报错）。
    """
    prov = next((p for p in _load_providers()
                 if p.get("id") == provider_id), None)
    if prov is None or not prov.get("enabled") \
            or prov.get("protocol") != PROTOCOL_OPENAI_TEXT:
        return None
    return CloudEndpoint(
        base_url=str(prov.get("base_url") or "").rstrip("/"),
        api_key=str(prov.get("api_key") or ""),
        model="",
        provider_id=str(prov.get("id") or ""),
        provider_name=str(prov.get("name") or ""),
        protocol=PROTOCOL_OPENAI_TEXT,
        models=[str(m) for m in prov.get("models") or []],
    )


# ── 批3 旧配置一次性迁移 ─────────────────────────────────────────

def ensure_legacy_remote_migrated() -> int:
    """把批3「远程推理服务器」单份配置迁移为一条 Provider（幂等）。

    仅当 providers 为空且旧配置存在时生成（name=远程推理服务器，
    Key/地址/模型带过去，enabled 继承）；旧键保留不删（回退安全）。
    返回本次迁移条数（0=无需迁移或已迁移过）。
    """
    if bool(_read_kv(KV_LEGACY_MIGRATED, False)):
        return 0
    with _kv_lock:
        if _load_providers():  # 已有连接：不迁移只标记
            _write_kv(KV_LEGACY_MIGRATED, True)
            return 0
        migrated = 0
        try:
            from ..services.inference.backends.remote_backend import get_remote_config
            cfg = get_remote_config(force=True)
            if cfg.base_url:
                item = {
                    "id": f"prov_{uuid.uuid4().hex[:10]}",
                    "name": "远程推理服务器",
                    "protocol": PROTOCOL_OPENAI_TEXT,
                    "base_url": cfg.base_url,
                    "api_key": cfg.api_key,
                    "models": [cfg.model] if cfg.model else [],
                    "enabled": bool(cfg.enabled),
                    "created_at": time.time(),
                    "updated_at": time.time(),
                }
                if _save_providers([item]):
                    migrated = 1
                    log.info("批3 远程推理服务器配置已迁移为云端连接 %s",
                                item["id"])
        except Exception as exc:  # noqa: BLE001 - 迁移失败不阻断启动
            log.warning("旧远程配置迁移失败（下次重试）: %s", exc)
            return 0
        _write_kv(KV_LEGACY_MIGRATED, True)
        return migrated


# ── 模型列表拉取（OpenAI 兼容 /v1/models）────────────────────────

def fetch_openai_models(base_url: str, api_key: str,
                        timeout_s: float = 8.0) -> tuple[list[str], str]:
    """拉取模型列表。Returns: (模型 id 列表, 错误说明；成功时为空串)。"""
    import requests

    base = (base_url or "").strip().rstrip("/")
    if not base:
        return [], "未填写服务地址"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        resp = requests.get(base + "/v1/models", headers=headers,
                            timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - 网络异常归为拉取失败
        return [], f"连接失败：{exc}"
    if resp.status_code != 200:
        return [], f"HTTP {resp.status_code}：{(resp.text or '')[:150]}"
    try:
        data = resp.json().get("data") or []
        ids = [str(m.get("id")) for m in data
               if isinstance(m, dict) and m.get("id")]
        return sorted(set(ids)), ""
    except Exception as exc:  # noqa: BLE001 - 响应体非 OpenAI 格式
        return [], f"响应解析失败：{exc}"
# 本项目仅供学习使用，商业授权请+Q 3559331368
