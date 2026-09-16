"""崩溃布控单测（2026-09-16 批2）：看门狗死亡快照 + 孤儿引擎清理。

launcher.BackendProcess 的取证与清理逻辑；psutil 全部 monkeypatch 假件，
不触真实进程/GPU。对应 09-16 崩溃簇审计：看门狗此前「只记重启次数、
不记退出码、不留现场」= 无声死取证断链根因。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

# launcher/ 非包（无 __init__.py）：运行时由 boot.py 以顶层模块方式加载
# launcher.py。此处按文件直载并注册私有名，绝不占用 sys.modules["launcher"]
# ——test_updater_apply 对该名字有 setdefault 打桩，先占会破坏其 boot 加载。
_spec = importlib.util.spec_from_file_location(
    "_launcher_forensics_mod", ROOT / "launcher" / "launcher.py")
assert _spec is not None and _spec.loader is not None
L = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = L
_spec.loader.exec_module(L)


class _LiveParent:
    @staticmethod
    def is_running() -> bool:
        return True


class _FakeProc:
    """psutil.Process 假件：只实现取证/清理路径用到的方法。"""

    def __init__(self, pid: int, cmdline: str,
                 parent: Any = None, parent_raises: bool = False) -> None:
        self.info: dict[str, Any] = {
            'pid': pid, 'name': 'fake.exe',
            'cmdline': cmdline.split(), 'memory_info': None,
        }
        self._parent = parent
        self._parent_raises = parent_raises
        self.killed = False

    def parent(self) -> Any:
        if self._parent_raises:
            raise L.psutil.NoSuchProcess(1)
        return self._parent

    def kill(self) -> None:
        self.killed = True


class _FakeVM:
    percent = 91.0
    available = 2_000_000_000


class _DeadBackendProc:
    pid = 4321


def _bare_backend_process() -> L.BackendProcess:
    """跳过 __init__（其依赖 LauncherConfig），只挂取证所需属性。"""
    bp = L.BackendProcess.__new__(L.BackendProcess)
    bp.process = _DeadBackendProc()  # type: ignore[assignment]
    bp._restart_count = 2
    return bp


def test_crash_forensics_writes_snapshot(tmp_path: Path,
                                         monkeypatch: Any) -> None:
    monkeypatch.setattr(L, '_CRASH_FORENSICS_DIR', tmp_path)
    monkeypatch.setattr(L, '_snapshot_gpu',
                        lambda: {'vram_used_mb': 15000})
    monkeypatch.setattr(L.psutil, 'virtual_memory', lambda: _FakeVM())
    monkeypatch.setattr(L, '_snapshot_engine_procs', lambda: [
        {'pid': 8189, 'name': 'python.exe', 'rss_mb': 2600.0,
         'cmdline': 'e:/x/comfyui/main.py'}])

    path = _bare_backend_process()._write_crash_forensics(5800, -1073741819)

    assert path is not None and path.exists()
    data = json.loads(path.read_text(encoding='utf-8'))
    assert data['exit_code'] == -1073741819
    assert data['backend_pid'] == 4321
    assert data['restart_count_so_far'] == 2
    assert data['gpu']['vram_used_mb'] == 15000
    assert data['ram']['percent'] == 91.0
    assert data['engine_procs'][0]['pid'] == 8189


def test_cleanup_kills_only_orphans(monkeypatch: Any) -> None:
    orphan_comfy = _FakeProc(101, r'e:\x\ComfyUI\main.py --cpu',
                             parent_raises=True)
    alive_vllm = _FakeProc(102, 'python -m vllm.entrypoints.openai.api_server',
                           parent=_LiveParent())
    unrelated = _FakeProc(103, 'python something_else.py')

    monkeypatch.setattr(
        L.psutil, 'process_iter',
        lambda attrs=None: [orphan_comfy, alive_vllm, unrelated])

    killed = _bare_backend_process()._cleanup_orphan_engines()

    assert killed == [101]
    assert orphan_comfy.killed
    assert not alive_vllm.killed
    assert not unrelated.killed


def test_forensics_failopen(tmp_path: Path, monkeypatch: Any) -> None:
    """取证内部炸了必须返回 None，绝不抛出（看门狗主链保护）。"""
    monkeypatch.setattr(L, '_CRASH_FORENSICS_DIR', tmp_path)

    def boom() -> Any:
        raise RuntimeError('vm 不可读')

    monkeypatch.setattr(L.psutil, 'virtual_memory', boom)
    bp = _bare_backend_process()
    bp.process = None
    assert bp._write_crash_forensics(5800, None) is None
