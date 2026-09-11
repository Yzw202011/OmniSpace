"""升级独立进程「小医生」（升级机制批3，docs/升级机制方案-2026-09-08.md §2.3）。

为什么独立：软件运行中自己的 .pyd 被锁——换文件必须由软件完全关机后
仍在运行的进程执行。本文件 **只准标准库** 且绝不 import backend/pydeps。

时序（方案 §2.3 步骤 5-9，由 backend/api/upgrade.py 三道预检+深验之后拉起）：
  ① 等 2 秒让后端把 HTTP 响应发完
  ② POST splash /api/quit → 等整链退净（后端 5800-5835+启动页 5850-5869
     +ComfyUI 8189，宽限 90s）→ 退不净则放弃升级、零改动
  ③ backing_up：被替换/被删除旧文件按原路径备份进 updates/backup/<ts>/；
     包声明含 DB 迁移则额外快照 data/omnispace.db(+wal/shm)
  ④ applying：payload 逐文件原子落位（清只读+失败重试×3）→ 执行删除
     清单 → updater 自身文件最后落位
  ⑤ verifying_start：拉起 Boot exe（桌面快捷方式同款命令）→ 探测 /health
     （5800-5835，冷启动宽限 300s）→ 核对 build == 目标 build_id
  ⑥ 成功：清工作目录、旧备份只留最近 1 份；失败：自动回滚（备份还原
     + DB 快照还原）→ 重新拉起旧版 → 状态记 rolled_back

全程写 updates/state.json 状态机 + updates/logs/upgrade_*.log。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402 - updater 同目录共享核心（stdlib-only）
import restore  # noqa: E402

# ── 常量 ─────────────────────────────────────────────────────
BACKEND_PORTS = range(5800, 5836)
SPLASH_PORTS = range(5850, 5870)
COMFY_PORT = 8189
QUIT_GRACE_S = 90.0
HEALTH_TIMEOUT_S = 300.0
SETTLE_S = 2.0
_BACKUP_KEEP = 1


def _log(install_root: Path, line: str) -> None:
    logs_dir = install_root / core.STATE_DIRNAME / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(logs_dir / f"upgrade_{time.strftime('%Y%m%d')}.log",
              "a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {line}\n")


def _set_state(install_root: Path, **kv) -> None:
    state = core.read_state(install_root)
    state.update(kv)
    state["updated_at"] = time.time()
    core.write_state(install_root, state)
    _log(install_root, f"state -> {kv.get('phase', kv)}")


def _http_status(port: int, path: str = "/health") -> int | None:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{path}", timeout=2) as resp:
            return int(resp.status)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _port_open(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_chain_quiet(install_root: Path) -> tuple[bool, str]:
    """等整链退净（后端+启动页+ComfyUI）。True=退净；False=超时零改动。"""
    # ② 先礼后兵：通知启动页正规退出（5850-5869 逐个试）
    for port in SPLASH_PORTS:
        if _port_open(port):
            try:
                urllib.request.urlopen(
                    urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/quit", method="POST"),
                    timeout=3)
            except (urllib.error.URLError, OSError):
                pass
    deadline = time.time() + QUIT_GRACE_S
    check_ports = [*BACKEND_PORTS, *SPLASH_PORTS, COMFY_PORT]
    while time.time() < deadline:
        if not any(_port_open(p) for p in check_ports):
            return True, ""
        time.sleep(1.0)
    alive = [p for p in check_ports if _port_open(p)]
    return False, f"端口仍未退净: {alive}"


def backup_files(install_root: Path, manifest: dict,
                 backup_dir: Path) -> None:
    """③ 备份将被替换/删除的旧文件（含 DB 快照，若声明迁移）。"""
    for ent in manifest["files"]:
        rel = ent["path"]
        target = core.payload_target(install_root, rel)
        if not target.is_file():
            continue  # 新增文件无旧版可备份
        dst = backup_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, dst)
    if manifest.get("contains_migration"):
        for name in ("omnispace.db", "omnispace.db-wal", "omnispace.db-shm"):
            db = install_root / "data" / name
            if db.is_file():
                shutil.copy2(db, backup_dir / name)


def apply_payload(install_root: Path, work_dir: Path, manifest: dict) -> None:
    """④ payload 落位（清只读 + 重试×3 + os.replace 原子换名）→ 删除清单。"""
    payload_root = work_dir / core.PAYLOAD_DIR
    for ent in manifest["files"]:
        rel = ent["path"]
        target = core.payload_target(install_root, rel)
        if ent["action"] == "delete":
            if target.exists():
                target.chmod(target.stat().st_mode | 0o200)
                target.unlink()
            continue
        src = payload_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        last: OSError | None = None
        for _ in range(3):
            try:
                if target.exists():
                    target.chmod(target.stat().st_mode | 0o200)
                tmp = target.with_suffix(target.suffix + ".upgrading")
                shutil.copy2(src, tmp)
                os.replace(tmp, target)
                last = None
                break
            except OSError as exc:
                last = exc
                time.sleep(0.5)
        if last is not None:
            raise RuntimeError(f"落位失败 {rel}: {last}")


def _spawn_boot(install_root: Path) -> bool:
    """拉起 Boot（桌面快捷方式同款命令；exe 缺失回退裸 python）。"""
    boot_exe = install_root / "runtime" / "py310" / "OmniSpace-Boot.exe"
    boot_py = install_root / "launcher" / "boot.py"
    exe = str(boot_exe) if boot_exe.is_file() else str(
        install_root / "runtime" / "py310" / "python.exe")
    creation = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    try:
        subprocess.Popen(
            [exe, str(boot_py)], cwd=str(install_root),
            creationflags=creation if sys.platform == "win32" else 0,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True)
        return True
    except OSError as exc:
        _log(install_root, f"Boot 拉起失败: {exc}")
        return False


def verify_start(install_root: Path, to_build_id: str) -> tuple[bool, str]:
    """⑤ 验证启动：等 /health 200 且 build==目标（冷启动宽限 300s）。"""
    _spawn_boot(install_root)
    deadline = time.time() + HEALTH_TIMEOUT_S
    while time.time() < deadline:
        for port in BACKEND_PORTS:
            if _http_status(port) == 200:
                try:
                    with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/health", timeout=3) as r:
                        data = json.loads(r.read().decode("utf-8"))
                    build = str((data.get("data") or {}).get("build") or "")
                    if build == to_build_id:
                        return True, f":{port}"
                    return False, (f"启动后 build={build or '?'} 与目标 "
                                   f"{to_build_id} 不符")
                except (urllib.error.URLError, OSError, ValueError):
                    pass
        time.sleep(3.0)
    return False, f"等待后端健康超时（{HEALTH_TIMEOUT_S:.0f}s）"


def _prune_backups(install_root: Path) -> None:
    """成功后旧备份只留最近 1 份。"""
    backup_root = install_root / core.STATE_DIRNAME / "backup"
    if not backup_root.is_dir():
        return
    stamps = sorted((d for d in backup_root.iterdir() if d.is_dir()),
                    key=lambda d: d.name, reverse=True)
    for old in stamps[_BACKUP_KEEP:]:
        shutil.rmtree(old, ignore_errors=True)


def main(argv: list[str]) -> int:
    install_root = Path(argv[argv.index("--install-root") + 1]).resolve()
    state = core.read_state(install_root)
    package = str(state.get("package") or "")
    to_build = str(state.get("to_build_id") or "")
    to_version = str(state.get("to_version") or "")
    if not package:
        _log(install_root, "state.json 无 package，无事可做")
        return 0
    pkg = install_root / core.STATE_DIRNAME / package
    work_dir = install_root / core.STATE_DIRNAME / "_work"

    _log(install_root, f"updater 启动：{package} -> v{to_version}")
    _set_state(install_root, phase="handover_wait")
    time.sleep(SETTLE_S)  # ① 让后端把响应发完

    quiet, why = wait_chain_quiet(install_root)  # ② 整链退净
    if not quiet:
        _log(install_root, f"整链未退净，放弃升级（零改动）: {why}")
        _set_state(install_root, phase="failed", reason=f"整链未退净: {why}")
        return 1

    # 重新读包+验清单（幂等防线：即使预检后包被换，这里再拦一次）。
    # 签名校验在 backend 侧已做（Ed25519 需 cryptography）；本地做
    # 结构校验+安全解包（zip-slip）+逐文件哈希深验（core 纯 stdlib）
    try:
        with zipfile.ZipFile(pkg) as zf:
            manifest = json.loads(
                zf.read(core.MANIFEST_NAME).decode("utf-8"))
            core.validate_manifest(manifest)
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                if not name.startswith(core.PAYLOAD_DIR + "/"):
                    continue  # 清单/签名/README 不落位
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError(f"包内含符号链接条目: {name}")
                target = core.safe_join(work_dir, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as fsrc, open(target, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst, 1 << 20)
        problems = core.verify_extracted(work_dir, manifest)
        if problems:
            raise RuntimeError(f"深验失败: {problems[:3]}")
    except Exception as exc:  # noqa: BLE001
        _log(install_root, f"包复核失败，零改动: {exc}")
        _set_state(install_root, phase="failed", reason=f"包复核失败: {exc}")
        shutil.rmtree(work_dir, ignore_errors=True)
        return 1

    # ③ 备份
    _set_state(install_root, phase="backing_up")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = install_root / core.STATE_DIRNAME / "backup" / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_files(install_root, manifest, backup_dir)

    # ④ 落位
    _set_state(install_root, phase="applying")
    try:
        apply_payload(install_root, work_dir, manifest)
    except Exception as exc:  # noqa: BLE001
        _log(install_root, f"落位失败，自动回滚: {exc}")
        restored, errs = restore.restore_backup(install_root, backup_dir)
        _set_state(install_root, phase="rolled_back",
                   reason=f"落位失败已回滚({restored} 文件, 错误{len(errs)})")
        _spawn_boot(install_root)
        return 1

    # ⑤ 验证启动
    _set_state(install_root, phase="verifying_start")
    ok, detail = verify_start(install_root, to_build)
    if not ok:
        _log(install_root, f"验证启动失败，自动回滚: {detail}")
        restored, errs = restore.restore_backup(install_root, backup_dir)
        _set_state(install_root, phase="rolled_back",
                   reason=f"验证失败已回滚({restored} 文件, {detail[:80]}, "
                          f"错误{len(errs)})")
        _spawn_boot(install_root)
        return 1

    # ⑥ 成功收尾
    shutil.rmtree(work_dir, ignore_errors=True)
    _prune_backups(install_root)
    _set_state(install_root, phase="success",
               reason=f"已升级到 v{to_version}（:{detail}）")
    _log(install_root, f"升级成功: v{to_version} on {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
