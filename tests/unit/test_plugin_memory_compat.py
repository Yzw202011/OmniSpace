"""插件记忆格式跨体系兼容测试（2026-09-16 批5）。

背景：内核 save_pkg 写 memory/episodic.json 为 {"events": [...]}，
OSP loader 只认 "entries" → 跨体系加载时整段 episodic 记忆静默丢失。
修复=loader 加 events 回退（原样收进 events 键零丢失）。
"""
from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.plugin_runtime import loader  # noqa: E402


def _build_pkg(path: Path, episodic: dict, working: dict) -> None:
    """造一个最小 .CuteMamen 包（manifest+空权重+两级记忆）。"""
    with tarfile.open(path, "w:gz") as tar:
        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        add("manifest.json", json.dumps(
            {"name": "compat-test", "version": "1.0.0"}).encode())
        buf = io.BytesIO()
        import numpy as np
        np.savez(buf, w0=np.zeros(2, dtype=np.float32))
        add("weights/weights.npz", buf.getvalue())
        add("memory/episodic.json",
            json.dumps(episodic, ensure_ascii=False).encode())
        add("memory/working.json",
            json.dumps(working, ensure_ascii=False).encode())


def test_kernel_events_format_survives_osp_load(tmp_path: Path) -> None:
    pkg_path = tmp_path / "compat.CuteMamen"
    events = [{"kind": "task", "ts": 1.0}, {"kind": "reward", "ts": 2.0}]
    _build_pkg(pkg_path,
               episodic={"events": events, "capacity": 64},
               working={"entries": {"mode": "idle"}})

    pkg = loader.read_cutemamen_pkg(pkg_path)

    # 修复前：episodic 整段静默丢失（memory 无该层）
    assert pkg.memory.get("episodic") == {"events": events}
    assert pkg.memory.get("working") == {"mode": "idle"}
    assert pkg.manifest["name"] == "compat-test"
    assert "w0" in pkg.weights


def test_pickle_payload_rejected(tmp_path: Path) -> None:
    """恶意/意外 pickle 化 npz 必须被 allow_pickle=False 拒收。"""
    import numpy as np
    pkg_path = tmp_path / "evil.CuteMamen"
    with tarfile.open(pkg_path, "w:gz") as tar:
        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        add("manifest.json", b"{}")
        buf = io.BytesIO()
        np.savez(buf, obj=np.array([{"dangerous": object()}], dtype=object))
        add("weights/weights.npz", buf.getvalue())

    try:
        loader.read_cutemamen_pkg(pkg_path)
    except Exception:
        return  # 拒收=通过（ValueError: Object arrays cannot be loaded…）
    raise AssertionError("pickle 化载荷未被 allow_pickle=False 拦截")
