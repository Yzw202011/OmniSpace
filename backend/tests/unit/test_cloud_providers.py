"""云端API接入批1 单测（2026-09-06，方案=docs/云端API接入方案-2026-09-06.md）。

覆盖：
- Key 打码：长 Key 保留首3末4、短 Key 全遮、空串；
- Provider CRUD：持久化、明文 Key 不出 API 层（打码回显）、
  api_key 留空=保持原值、协议校验（未开放协议拒绝）；
- 槽位绑定/解绑：未知槽位拒绝、停用连接拒绝绑定；
- 路由解析：绑定→端点、无绑定无旧配置→None（本地旧行为）、
  绑定指向停用连接→回落本地；
- 批3 旧配置兼容：绑定无+旧配置启用→is_remote_dialog_enabled True、
  旧配置一次性迁移（幂等、已有连接时只标记）；
- dialog_engine 接线哨兵：cloud:: 虚拟模型分支/remote 短路
  matches_target 源码必须存在（防回归误删）；
- matches_target：未就绪 False、目标一致 True、绑定切换后 False、
  无效 cloud:: 连接 False；
- 资源域：绑定生效 dialog→remote 伪域，解绑回本地卡号。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import pytest

import backend.services.cloud_provider_service as cs
import backend.services.inference.backends.remote_backend as rb
from backend.engines import gpu_domains
from backend.services.cloud_provider_service import (
    CloudProviderError,
    clear_binding,
    create_provider,
    delete_provider,
    ensure_legacy_remote_migrated,
    get_dialog_text_endpoint,
    has_dialog_binding_enabled,
    list_providers,
    mask_key,
    parse_cloud_model_id,
    set_binding,
    update_provider,
)
from backend.services.inference.backends.remote_backend import (
    RemoteDialogBackend,
    is_remote_dialog_enabled,
)


@pytest.fixture()
def mem_kv(monkeypatch: pytest.MonkeyPatch):
    """内存 KV（替换 cloud_provider_service 的读写），并清路由缓存。"""
    store: dict = {}

    def _read(key, default=None):
        return store.get(key, default)

    def _write(key, value):
        store[key] = value
        return True

    monkeypatch.setattr(cs, "_read_kv", _read)
    monkeypatch.setattr(cs, "_write_kv", _write)
    cs._invalidate_route_cache()
    yield store
    cs._invalidate_route_cache()


def _disable_legacy_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧 remote_dialog 配置按「未启用」处理（隔离开发机真实配置）。"""
    monkeypatch.setattr(rb, "_read_settings_kv", lambda: {})
    monkeypatch.setattr(rb, "_cfg_cache", None)


# ── 打码 ─────────────────────────────────────────────────────────

def test_mask_key() -> None:
    assert mask_key("sk-abcdef1234567890") == "sk-***7890"
    assert mask_key("short") == "***"
    assert mask_key("") == ""
    assert mask_key("   ") == ""


# ── Provider CRUD ────────────────────────────────────────────────

def test_provider_create_list_masks_key(mem_kv) -> None:
    item = create_provider("DeepSeek", "openai_text",
                           "https://api.deepseek.com", "sk-secret-123456",
                           ["deepseek-chat"])
    assert item["id"].startswith("prov_")
    listed = list_providers(mask=True)
    assert len(listed) == 1
    assert listed[0]["api_key_masked"] == "sk-***3456"
    assert "api_key" not in listed[0]  # 明文绝不出 API 层
    # 内部路由读取可见明文（发请求用）
    assert list_providers(mask=False)[0]["api_key"] == "sk-secret-123456"


def test_provider_update_keep_key_when_blank(mem_kv) -> None:
    item = create_provider("A", "openai_text", "http://10.0.0.1:8000",
                           "sk-keep-me-9876543")
    update_provider(item["id"], name="B", api_key="")
    got = list_providers(mask=False)[0]
    assert got["name"] == "B"
    assert got["api_key"] == "sk-keep-me-9876543"  # 留空=保持原值


def test_provider_protocol_gate(mem_kv) -> None:
    # 批3 起 task_video 已开放（语义迁移至 test_cloud_video 协议闸）
    item = create_provider("X", "task_video", "https://k.klingai.com", "k")
    assert item["protocol"] == "task_video"
    with pytest.raises(CloudProviderError):
        create_provider("X", "openai_text", "ftp://bad", "")
    with pytest.raises(CloudProviderError):
        create_provider("", "openai_text", "http://x", "")


def test_provider_delete_clears_binding(
        mem_kv, monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_legacy_remote(monkeypatch)  # 隔离旧配置兜底（兼容层语义）
    item = create_provider("A", "openai_text", "http://10.0.0.1:8000", "")
    set_binding("dialog.text", item["id"], "m1")
    delete_provider(item["id"])
    assert has_dialog_binding_enabled() is False
    assert get_dialog_text_endpoint(force=True) is None


# ── 槽位绑定 ─────────────────────────────────────────────────────

def test_binding_slot_and_disabled_provider(mem_kv) -> None:
    item = create_provider("A", "openai_text", "http://10.0.0.1:8000", "")
    with pytest.raises(CloudProviderError):
        set_binding("unknown.slot", item["id"])
    set_binding("dialog.text", item["id"], "deepseek-chat")
    update_provider(item["id"], enabled=False)
    with pytest.raises(CloudProviderError) as ei:
        set_binding("dialog.text", item["id"], "x")
    assert ei.value.code == "CLOUD_PROVIDER_DISABLED"
    clear_binding("dialog.text")
    assert "dialog.text" not in cs.get_bindings()


# ── 路由解析 ─────────────────────────────────────────────────────

def test_endpoint_resolution_binding_first_then_none(
        mem_kv, monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_legacy_remote(monkeypatch)
    # 未绑定：None → 本地引擎（旧行为铁律）
    assert get_dialog_text_endpoint(force=True) is None
    assert is_remote_dialog_enabled() is False

    item = create_provider("DeepSeek", "openai_text",
                           "https://api.deepseek.com/", "sk-x-0000001111",
                           ["deepseek-chat"])
    set_binding("dialog.text", item["id"], "deepseek-chat")
    ep = get_dialog_text_endpoint(force=True)
    assert ep is not None
    assert ep.base_url == "https://api.deepseek.com"  # 尾斜杠归一
    assert ep.model == "deepseek-chat"
    assert ep.provider_name == "DeepSeek"

    clear_binding("dialog.text")
    assert get_dialog_text_endpoint(force=True) is None


def test_endpoint_binding_to_disabled_falls_local(
        mem_kv, monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_legacy_remote(monkeypatch)
    item = create_provider("A", "openai_text", "http://10.0.0.1:8000", "")
    set_binding("dialog.text", item["id"], "m")
    update_provider(item["id"], enabled=False)
    assert get_dialog_text_endpoint(force=True) is None


def test_legacy_remote_config_still_works_without_binding(
        mem_kv, monkeypatch: pytest.MonkeyPatch) -> None:
    """批3 兼容层：未迁移机器的旧 remote_dialog_* 配置继续生效。"""
    monkeypatch.setattr(rb, "_read_settings_kv",
                        lambda: {"remote_dialog_enabled": True,
                                 "remote_dialog_base_url": "http://10.1.1.1:8000",
                                 "remote_dialog_model": "qwen3-32b"})
    monkeypatch.setattr(rb, "_cfg_cache", None)
    ep = get_dialog_text_endpoint(force=True)
    assert ep is not None and ep.base_url == "http://10.1.1.1:8000"
    assert is_remote_dialog_enabled() is True
    monkeypatch.setattr(rb, "_read_settings_kv", lambda: {})
    monkeypatch.setattr(rb, "_cfg_cache", None)


def test_after_migration_legacy_config_never_fires_back(
        mem_kv, monkeypatch: pytest.MonkeyPatch) -> None:
    """批1 语义闸（2026-09-06 实弹暴露）：已迁移机器旧配置永久退位。

    用户停用/删除全部连接后，DB 里残留的 remote_dialog_enabled=True
    不得把云端悄悄拉起来——单一真源=Provider。
    """
    # 模拟已迁移态：迁移标记在位，且残留旧配置 enabled=True
    mem_kv[cs.KV_LEGACY_MIGRATED] = True
    monkeypatch.setattr(rb, "_read_settings_kv",
                        lambda: {"remote_dialog_enabled": True,
                                 "remote_dialog_base_url": "http://10.3.3.3:8000"})
    monkeypatch.setattr(rb, "_cfg_cache", None)
    assert cs.legacy_remote_fallback_allowed() is False
    assert get_dialog_text_endpoint(force=True) is None
    assert is_remote_dialog_enabled() is False
    # 未迁移机器不受影响（兼容层照常兜底）
    mem_kv[cs.KV_LEGACY_MIGRATED] = False
    assert get_dialog_text_endpoint(force=True) is not None
    monkeypatch.setattr(rb, "_read_settings_kv", lambda: {})
    monkeypatch.setattr(rb, "_cfg_cache", None)
    mem_kv.pop(cs.KV_LEGACY_MIGRATED, None)


# ── 批3 旧配置一次性迁移 ─────────────────────────────────────────

def test_legacy_migration_and_idempotency(
        mem_kv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rb, "_read_settings_kv",
                        lambda: {"remote_dialog_enabled": True,
                                 "remote_dialog_base_url": "http://10.2.2.2:8000",
                                 "remote_dialog_api_key": "sk-old-555",
                                 "remote_dialog_model": "m-32b"})
    monkeypatch.setattr(rb, "_cfg_cache", None)
    assert ensure_legacy_remote_migrated() == 1
    provs = list_providers(mask=False)
    assert len(provs) == 1 and provs[0]["name"] == "远程推理服务器"
    assert provs[0]["api_key"] == "sk-old-555"
    assert provs[0]["enabled"] is True
    # 幂等：第二次不再迁移
    assert ensure_legacy_remote_migrated() == 0
    # 已有连接时：只标记不再生成
    monkeypatch.setattr(rb, "_read_settings_kv", lambda: {})
    monkeypatch.setattr(rb, "_cfg_cache", None)
    mem_kv.pop(cs.KV_LEGACY_MIGRATED, None)
    create_provider("已有", "openai_text", "http://10.0.0.9", "")
    assert ensure_legacy_remote_migrated() == 0
    assert len(list_providers()) == 2


# ── cloud:: 虚拟模型解析 ─────────────────────────────────────────

def test_parse_cloud_model_id() -> None:
    assert parse_cloud_model_id("cloud::prov_a::deepseek-chat") == \
        ("prov_a", "deepseek-chat")
    assert parse_cloud_model_id("cloud::prov_a::") == ("prov_a", "")
    assert parse_cloud_model_id("cloud::") is None
    assert parse_cloud_model_id("qwen3-vl-4b") is None
    assert parse_cloud_model_id(None) is None  # type: ignore[arg-type]


# ── dialog_engine 接线哨兵（防回归误删）──────────────────────────

def test_dialog_engine_wiring_sentinel() -> None:
    import inspect

    from backend.services.inference.dialog_engine import DialogEngine
    pick_src = inspect.getsource(DialogEngine._pick_model)
    assert "parse_cloud_model_id" in pick_src
    assert "resolve_provider_endpoint" in pick_src
    loaded_src = inspect.getsource(DialogEngine.ensure_loaded)
    assert "matches_target" in loaded_src
    load_model_src = inspect.getsource(DialogEngine.load_model)
    assert "matches_target" in load_model_src


# ── matches_target（ensure_loaded 短路判定）──────────────────────

def _ready_backend(base_url: str, model: str) -> RemoteDialogBackend:
    b = RemoteDialogBackend(health_poll_s=0.01, health_wait_s=1.0)
    b._ready = True
    b._base_url = base_url
    b.model_id = model
    b._loaded_key = f"{base_url}|{model}"
    return b


def test_matches_target_scenarios(mem_kv) -> None:
    item = create_provider("DeepSeek", "openai_text",
                           "https://api.deepseek.com", "sk-k",
                           ["deepseek-chat"])
    set_binding("dialog.text", item["id"], "deepseek-chat")
    b = _ready_backend("https://api.deepseek.com", "deepseek-chat")
    assert b.matches_target(None) is True  # 槽位绑定同目标 → 短路
    assert b.matches_target("deepseek-chat") is True
    # 请求级 cloud:: 指向同连接同模型 → 短路
    assert b.matches_target(f"cloud::{item['id']}::deepseek-chat") is True
    # 请求级指定了不同模型 → 不短路（轻量重载换目标）
    assert b.matches_target(f"cloud::{item['id']}::deepseek-reasoner") \
        is False
    # 无效连接 → 不短路
    assert b.matches_target("cloud::prov_ghost::m") is False
    b._ready = False
    assert b.matches_target(None) is False


def test_matches_target_binding_switch_forces_reload(mem_kv) -> None:
    a = create_provider("A", "openai_text", "http://10.0.0.1:8000", "",
                        ["ma"])
    bprov = create_provider("B", "openai_text", "http://10.0.0.2:8000", "",
                            ["mb"])
    set_binding("dialog.text", a["id"], "ma")
    b = _ready_backend("http://10.0.0.1:8000", "ma")
    assert b.matches_target(None) is True
    set_binding("dialog.text", bprov["id"], "mb")  # 用户切了服务商
    assert b.matches_target(None) is False  # 旧端点不得短路


# ── 资源域：绑定生效 dialog→remote 伪域 ──────────────────────────

def test_gpu_domain_remote_when_bound(
        mem_kv, monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_legacy_remote(monkeypatch)
    item = create_provider("A", "openai_text", "http://10.0.0.1:8000", "")
    set_binding("dialog.text", item["id"], "ma")
    assert gpu_domains.resolve_feature_domain("dialog") == \
        gpu_domains.REMOTE_DOMAIN
    clear_binding("dialog.text")
    assert gpu_domains.resolve_feature_domain("dialog") == \
        str(gpu_domains.resolve_feature_device("dialog"))
