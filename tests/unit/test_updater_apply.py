"""升级 updater 本体单测（升级机制批3，docs/升级机制方案-2026-09-08.md）。

假安装目录直测（不碰真实安装根）：
  - 备份：被替换/删除旧文件按原路径进备份（含 DB 快照条件）
  - 落位：replace 原子换名、delete 清单执行、受保护区（data/）绝不触碰
  - 回滚：restore.restore_backup 把旧文件原样救回
  - boot 恢复钩子：中断态 state.json → 自动还原 + 状态机落 rolled_back
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from updater import core, restore
from updater.updater import apply_payload, backup_files

_REPO_ROOT = Path(__file__).resolve().parents[2]

# boot.py 以脚本态运行（依赖顶层 launcher 模块）——测试里预置桩模块后
# 经文件路径加载，只测 _recover_interrupted_upgrade 这一个方法
_stub_launcher = types.ModuleType("launcher")
_stub_launcher.PROJECT_ROOT = _REPO_ROOT  # type: ignore[attr-defined]
for _name in ("BackendProcess", "EnvironmentChecker", "LauncherConfig",
              "PortManager"):
    setattr(_stub_launcher, _name, type(_name, (), {}))
sys.modules.setdefault("launcher", _stub_launcher)
_spec = importlib.util.spec_from_file_location(
    "_omnispace_boot_test", _REPO_ROOT / "launcher" / "boot.py")
assert _spec is not None and _spec.loader is not None
boot_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = boot_mod
_spec.loader.exec_module(boot_mod)

OLD_MAIN = "OLD MAIN v2.3.1\n"
NEW_MAIN = "NEW MAIN v2.9.9\n"
OLD_JS = "console.log('old')\n"
NEW_JS = "console.log('new')\n"


def _sha(data: str) -> str:
    import hashlib
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


@pytest.fixture()
def fake_install(tmp_path: Path) -> Path:
    """假安装根：两个将被替换的文件 + 一个被删除清单条目 + 用户数据。"""
    root = tmp_path / "install"
    (root / "backend").mkdir(parents=True)
    (root / "frontend" / "dist").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    (root / "launcher").mkdir(parents=True)  # boot 钩子的 BOOT_DIR 锚点
    (root / "src" / "main.py").write_text(OLD_MAIN, encoding="utf-8")
    (root / "frontend" / "dist" / "index.js").write_text(OLD_JS, encoding="utf-8")
    (root / "old_extra.txt").write_text("DELETE_ME", encoding="utf-8")
    (root / "data" / "omnispace.db").write_text("USER_DATA", encoding="utf-8")
    return root


def _manifest() -> dict:
    return {
        "format": 1, "from_min": "2.3.1", "from_max": "3.0.0",
        "to_version": "2.9.9", "to_build_id": "b999",
        "created": "2026-09-11T00:00:00", "contains_migration": False,
        "payload_bytes": len(NEW_MAIN) + len(NEW_JS),
        "files": [
            {"path": "src/main.py", "action": "replace",
             "sha256": _sha(NEW_MAIN), "size": len(NEW_MAIN.encode())},
            {"path": "frontend/dist/index.js", "action": "replace",
             "sha256": _sha(NEW_JS), "size": len(NEW_JS.encode())},
            {"path": "old_extra.txt", "action": "delete"},
        ],
    }


@pytest.fixture()
def work_dir(tmp_path: Path) -> Path:
    w = tmp_path / "work" / core.PAYLOAD_DIR
    (w / "backend").mkdir(parents=True)
    (w / "frontend" / "dist").mkdir(parents=True)
    (w / "src" / "main.py").write_text(NEW_MAIN, encoding="utf-8")
    (w / "frontend" / "dist" / "index.js").write_text(NEW_JS, encoding="utf-8")
    return w.parent


def test_apply_replaces_deletes_and_backs_up(fake_install: Path,
                                             work_dir: Path,
                                             tmp_path: Path) -> None:
    manifest = _manifest()
    backup_dir = tmp_path / "backup" / "20260911_000000"
    backup_dir.mkdir(parents=True)
    backup_files(fake_install, manifest, backup_dir)
    # 备份里是旧内容
    assert (backup_dir / "src" / "main.py").read_text("utf-8") == OLD_MAIN
    assert (backup_dir / "old_extra.txt").read_text("utf-8") == "DELETE_ME"
    # 落位
    apply_payload(fake_install, work_dir, manifest)
    assert (fake_install / "src" / "main.py").read_text("utf-8") == NEW_MAIN
    assert (fake_install / "frontend" / "dist" / "index.js").read_text(
        "utf-8") == NEW_JS
    assert not (fake_install / "old_extra.txt").exists()
    # 受保护区零触碰
    assert (fake_install / "data" / "omnispace.db").read_text(
        "utf-8") == "USER_DATA"


def test_rollback_restores_old_files(fake_install: Path, work_dir: Path,
                                     tmp_path: Path) -> None:
    manifest = _manifest()
    backup_dir = tmp_path / "backup" / "20260911_000000"
    backup_dir.mkdir(parents=True)
    backup_files(fake_install, manifest, backup_dir)
    apply_payload(fake_install, work_dir, manifest)
    restored, errs = restore.restore_backup(fake_install, backup_dir)
    assert errs == []
    assert restored >= 3
    assert (fake_install / "src" / "main.py").read_text("utf-8") == OLD_MAIN
    assert (fake_install / "old_extra.txt").read_text("utf-8") == "DELETE_ME"


def test_boot_hook_recovers_interrupted_upgrade(
        fake_install: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """中断态 state.json → 钩子从备份还原 + 状态落 rolled_back。"""
    # 造中断现场：升级已把 main.py 换成新版但被中断（verifying_start）
    (fake_install / "src" / "main.py").write_text(NEW_MAIN, encoding="utf-8")
    backup_dir = fake_install / "updates" / "backup" / "20260911_120000"
    (backup_dir / "backend").mkdir(parents=True)
    (backup_dir / "src" / "main.py").write_text(OLD_MAIN, encoding="utf-8")
    state = {"phase": "verifying_start", "package": "x.upg",
             "to_version": "2.9.9"}
    (fake_install / "updates" / "state.json").write_text(
        json.dumps(state), encoding="utf-8")
    # 生产里 restore.py 随包出厂——假安装根里放一份真身副本
    (fake_install / "updater").mkdir(parents=True, exist_ok=True)
    (fake_install / "updater" / "restore.py").write_text(
        (Path(__file__).resolve().parents[2] / "updater" / "restore.py")
        .read_text("utf-8"), encoding="utf-8")
    # BOOT_DIR 指向假安装根的 launcher/
    monkeypatch.setattr(boot_mod, "BOOT_DIR", fake_install / "launcher")

    class _StubState:
        logs: list = []

        def log(self, msg: str, level: str = "info") -> None:
            _StubState.logs.append((level, msg))

    stub = types.SimpleNamespace(state=_StubState())
    boot_mod.BootOrchestrator._recover_interrupted_upgrade(stub)

    assert (fake_install / "src" / "main.py").read_text(
        "utf-8") == OLD_MAIN
    new_state = json.loads(
        (fake_install / "updates" / "state.json").read_text("utf-8"))
    assert new_state["phase"] == "rolled_back"
    assert any("自动还原" in msg for _lvl, msg in _StubState.logs)


def test_boot_hook_noop_on_normal_state(
        fake_install: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """正常态（无 state.json / phase=success）→ 钩子零动作。"""
    (fake_install / "updates").mkdir(exist_ok=True)
    (fake_install / "updates" / "state.json").write_text(
        json.dumps({"phase": "success"}), encoding="utf-8")
    (fake_install / "src" / "main.py").write_text("LIVE", encoding="utf-8")
    monkeypatch.setattr(boot_mod, "BOOT_DIR", fake_install / "launcher")

    class _StubState:
        def log(self, msg: str, level: str = "info") -> None:
            raise AssertionError("正常态不应打日志")

    stub = types.SimpleNamespace(state=_StubState())
    boot_mod.BootOrchestrator._recover_interrupted_upgrade(stub)
    assert (fake_install / "src" / "main.py").read_text(
        "utf-8") == "LIVE"
