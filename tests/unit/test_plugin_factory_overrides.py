"""出厂插件停用持久化测试（2026-09-17 B3）。

背景：出厂档停用此前只改内存态（_save_user_registry 只写用户档），
重启即复活而 UI 提供开关——审计四轮 P2。修复=独立 factory_overrides.json
只记偏离默认（停用）的条目，全部恢复默认则删文件。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from src.services.plugin_runtime import registry as pr  # noqa: E402


def _enabled_of(reg: pr.PluginRuntime, name: str) -> bool:
    return next(p["enabled"] for p in reg.plugins_info() if p["name"] == name)


@pytest.fixture()
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """登记表/覆盖文件全落 tmp，不碰真 data/。"""
    monkeypatch.setattr(pr, "USER_PLUGIN_DIR", tmp_path)
    monkeypatch.setattr(pr, "USER_REGISTRY_PATH",
                        tmp_path / "user_registry.json")
    monkeypatch.setattr(pr, "FACTORY_OVERRIDE_PATH",
                        tmp_path / "factory_overrides.json")
    # 只留一个出厂种子，缩小爆炸半径
    seeds = dict(pr._SEED_PLUGINS)
    keep = next(iter(seeds))
    monkeypatch.setattr(pr, "_SEED_PLUGINS", {keep: seeds[keep]})
    reg = pr.PluginRuntime()
    return reg, tmp_path / "factory_overrides.json"


def test_factory_disable_persists_and_reloads(isolated_registry) -> None:
    reg, overrides_path = isolated_registry
    name = next(iter(pr._SEED_PLUGINS))

    reg.set_enabled(name, False)
    assert overrides_path.exists()
    assert json.loads(overrides_path.read_text("utf-8"))[name] is False

    # 新实例（模拟重启）读覆盖：仍停用
    reg2 = pr.PluginRuntime()
    assert _enabled_of(reg2, name) is False


def test_factory_reenable_removes_override_file(isolated_registry) -> None:
    reg, overrides_path = isolated_registry
    name = next(iter(pr._SEED_PLUGINS))

    reg.set_enabled(name, False)
    reg.set_enabled(name, True)

    assert not overrides_path.exists()  # 全恢复默认=无覆盖文件
    reg2 = pr.PluginRuntime()
    assert _enabled_of(reg2, name) is True
