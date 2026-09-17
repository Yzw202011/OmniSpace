"""插件子进程沙箱测试（安全模型终态=A，2026-09-17）。

真子进程实弹：正常执行/超时硬杀/崩溃隔离/内存上限/ndarray 拒收/
信任档分流/应急回退闸。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.plugin_runtime import sandbox as sb  # noqa: E402
from src.services.plugin_runtime.registry import PluginRuntime  # noqa: E402

_BENIGN = '''
from src.cutemamen.plugin import ExpertPlugin

class EchoPlugin(ExpertPlugin):
    def on_think(self, event, ctx):
        data = event.get("data") or {}
        return {"echo": data.get("msg", ""), "who": self.name}
'''

_SLEEPY = '''
import time as _t
from src.cutemamen.plugin import ExpertPlugin

class SleepyPlugin(ExpertPlugin):
    def on_think(self, event, ctx):
        _t.sleep(60)
        return {}
'''

_CRASHER = '''
import os as _os
from src.cutemamen.plugin import ExpertPlugin

class CrasherPlugin(ExpertPlugin):
    def on_think(self, event, ctx):
        _os._exit(3)  # 硬崩（非异常，父进程必须无恙）
'''

_HOG = '''
import time as _t
from src.cutemamen.plugin import ExpertPlugin

class HogPlugin(ExpertPlugin):
    def on_think(self, event, ctx):
        chunks = []
        for _ in range(64):  # 64×64MB=4GB，分块分配让 RSS 看护能追上
            chunks.append(bytearray(64 * 1024 * 1024))
            _t.sleep(0.05)
        return {}
'''


def _plugin_file(tmp_path: Path, name: str, source: str) -> Path:
    p = tmp_path / f"{name}.py"
    p.write_text(source, encoding="utf-8")
    return p


def test_sandbox_normal_invoke(tmp_path: Path) -> None:
    src = _plugin_file(tmp_path, "echo", _BENIGN)
    result = sb.invoke_sandboxed(
        "echo", src, None, {"topic": "invoke", "data": {"msg": "你好"}},
        timeout_s=60)
    assert result["echo"] == "你好"
    assert result["who"] == "echo"


def test_sandbox_timeout_hard_kill(tmp_path: Path) -> None:
    src = _plugin_file(tmp_path, "sleepy", _SLEEPY)
    t0 = time.monotonic()
    with pytest.raises(sb.SandboxError, match="超时"):
        sb.invoke_sandboxed("sleepy", src, None, {}, timeout_s=2)
    # 硬杀生效：不应等满 60s（2s 超时 + 进程开销 < 15s）
    assert time.monotonic() - t0 < 15


def test_sandbox_crash_isolation(tmp_path: Path) -> None:
    """子进程 os._exit(3) 硬崩 → 父侧收 SandboxError，测试进程自身无恙。"""
    src = _plugin_file(tmp_path, "crasher", _CRASHER)
    with pytest.raises(sb.SandboxError, match="rc=3"):
        sb.invoke_sandboxed("crasher", src, None, {}, timeout_s=30)


def test_sandbox_memory_cap(tmp_path: Path) -> None:
    src = _plugin_file(tmp_path, "hog", _HOG)
    with pytest.raises(sb.SandboxError, match="内存超限"):
        sb.invoke_sandboxed("hog", src, None, {}, timeout_s=60,
                            mem_cap_mb=128)


def test_sandbox_rejects_ndarray_event() -> None:
    import numpy as np
    with pytest.raises(sb.SandboxError, match="JSON 可序列化"):
        sb.invoke_sandboxed("x", Path("x.py"), None,
                            {"data": {"arr": np.zeros(3)}},
                            timeout_s=5)


def test_registry_routes_user_source_to_sandbox(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """信任档分流：user_source 走沙箱（不 exec 宿主），出厂档不变。"""
    monkeypatch.setattr(sb, "sandbox_enabled", lambda: True)
    import src.services.plugin_runtime.registry as pr
    monkeypatch.setattr(pr, "sandbox_enabled", lambda: True)
    rt = PluginRuntime()
    src = _plugin_file(tmp_path, "echo2", _BENIGN)
    rt.register("echo2", src, None, trust="user_source")

    called: list[str] = []

    def fake_invoke(name, source_py, pkg_path, event, **kw):
        called.append(name)
        return {"echo": "sandboxed", "who": name}

    monkeypatch.setattr(sb, "invoke_sandboxed", fake_invoke)
    import asyncio
    out = asyncio.run(rt.invoke("echo2", {"msg": "hi"}))
    assert called == ["echo2"]
    assert out["data"]["who"] == "echo2"
    assert out["sandboxed"] is True


def test_registry_fallback_gate(tmp_path: Path,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    """应急闸：sandbox=false 时 user_source 回进程内信任路径。"""
    import src.services.plugin_runtime.registry as pr
    monkeypatch.setattr(pr, "sandbox_enabled", lambda: False)
    rt = PluginRuntime()
    src = _plugin_file(tmp_path, "echo3", _BENIGN)
    rt.register("echo3", src, None, trust="user_source")

    import asyncio
    out = asyncio.run(rt.invoke("echo3", {"msg": "inline"}))
    assert "sandboxed" not in out
    assert out["data"]["echo"] == "inline"


def test_sandbox_gate_reads_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """总闸默认 true；config plugins.sandbox=false 时关闭。"""
    assert sb.sandbox_enabled() is True  # 仓库 config 默认开

    class _Cfg:
        @staticmethod
        def get(key):
            # 注意：这里返回的就是 get_config() 这个总 dict 的切片——
            # key='plugins' 应直接给 {'sandbox': False}（勿再包一层）
            return {"sandbox": False} if key == "plugins" else {}

    import src.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "get_config", lambda: _Cfg())
    assert sb.sandbox_enabled() is False
