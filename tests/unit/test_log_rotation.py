"""日志轮转回归测试（2026-09-16 批4）。

背景（审计三洞之二）：boot.log 原「超 20MB 砍前半」=无痕丢一半历史；
backend_stdout/stderr.log（launcher._drain_pipe）append 无界（stderr 已
9.1MB）。修复=两处统一 10MB 标准轮转，保留一代 .1（替换式不累积）。
"""
from __future__ import annotations

import importlib.util
import io
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_launcher_mod():
    """launcher/ 非包：按文件直载私有名（不占 sys.modules['launcher']，
    理由见 test_backend_crash_forensics.py 同款注释）。"""
    spec = importlib.util.spec_from_file_location(
        "_launcher_rotation_mod", ROOT / "launcher" / "launcher.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_boot_mod(monkeypatch):
    """boot.py 依赖 launcher 包名：沿用 test_updater_apply 的 stub 手法。"""
    repo = ROOT
    stub = types.ModuleType("launcher")
    stub.PROJECT_ROOT = repo  # type: ignore[attr-defined]
    for nm in ("BackendProcess", "EnvironmentChecker", "LauncherConfig",
               "PortManager"):
        setattr(stub, nm, type(nm, (), {}))
    monkeypatch.setitem(sys.modules, "launcher", stub, )
    spec = importlib.util.spec_from_file_location(
        "_boot_rotation_mod", repo / "launcher" / "boot.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_drain_pipe_rotates_at_threshold(tmp_path: Path,
                                         monkeypatch) -> None:
    L = _load_launcher_mod()
    log = tmp_path / "backend_stderr.log"
    monkeypatch.setattr(
        L.BackendProcess, "_PIPE_LOG_ROTATE_BYTES", 200)
    bp = L.BackendProcess.__new__(L.BackendProcess)
    bp.on_output = None

    payload = b"line-of-log-data-" * 10 + b"\n"   # ~171B/行
    stream = io.BytesIO(payload * 4)               # ~684B → 3 次轮转
    bp._drain_pipe(stream, log)  # type: ignore[arg-type]

    assert log.exists()
    assert (tmp_path / "backend_stderr.log.1").exists()
    assert log.stat().st_size <= 200 + len(payload)


def test_boot_log_rotates_not_truncates(tmp_path: Path,
                                        monkeypatch) -> None:
    boot = _load_boot_mod(monkeypatch)
    log = tmp_path / "boot.log"
    monkeypatch.setattr(boot, "BOOT_LOG", log)
    monkeypatch.setattr(boot, "_BOOT_LOG_ROTATE_BYTES", 120)

    for i in range(20):
        boot._boot_log_write(f"line-{i:02d}-" + "x" * 20)

    assert log.exists()
    rotated = tmp_path / "boot.log.1"
    assert rotated.exists()
    # 轮转档必须是一次完整历史（含时间戳前缀的行），不是砍半残卷
    text = rotated.read_text(encoding="utf-8")
    assert "line-" in text and "2026" in text
