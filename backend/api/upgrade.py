"""应用内升级 API（升级机制批2，docs/升级机制方案-2026-09-08.md §3.1）。

端点（router 无 prefix，经 main._API_MODULES 以 /api/v1 挂载）：
- GET    /upgrade/status           当前版本/发行形态 + 升级状态机 + updates 目录
- GET    /upgrade/packages         扫描 updates/*.upg（轻量验签+兼容判定）
- POST   /upgrade/import           导入升级包（multipart 流式落盘，上限 2GB）
- POST   /upgrade/start            开始升级（三道预检→深验→spawn updater）
- DELETE /upgrade/packages/{name}  删除 updates/ 下指定包
- POST   /upgrade/open-folder      打开 updates/ 文件夹（资源管理器）

安全纪律：
- 导入/删除的文件名只取 basename、强制 .upg 后缀，updates/ 目录固定——
  杜绝任意路径读写；导入落盘流式限长 2GB
- start 需 body.confirm == "UPGRADE"（前端确认弹窗回传）防误触
- start 是唯一危险动作：三道预检（签名/兼容/磁盘）+ 深验 + 任务忙拒绝，
  任一不过零改动
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, UploadFile

from ..config import ROOT_DIR
from ..middleware.error_handler import ApiError, ok
from ..services import upgrade_service as usvc
from ..services.upgrade_service import UpgradeError

router = APIRouter()

#: 导入包大小硬上限（方案 §3.1：2GB）
_MAX_PACKAGE_BYTES = 2 * 1024 ** 3
_PACKAGE_EXT = ".upg"


def _err_from(exc: UpgradeError) -> ApiError:
    """服务层 UpgradeError → 统一信封 ApiError（码/话/出路原样透传）。

    UpgradeError 未存 message 属性（人话在 ValueError.args[0]），
    用 str(exc) 取。"""
    return ApiError(exc.code, str(exc), detail=exc.detail,
                    suggestion=exc.suggestion)


def _safe_package_name(name: str) -> str:
    """包名白名单：纯文件名 + .upg 后缀（防路径穿越）。"""
    base = os.path.basename(str(name or "").strip())
    if not base.endswith(_PACKAGE_EXT) or "/" in base or "\\" in base:
        raise ApiError("SYSTEM_PARAM_INVALID", "非法包名（仅支持 updates/ 下的 .upg 文件）",
                       detail={"name": base})
    return base


def _busy_reason() -> str:
    """任务占用检查：有在跑的生成/训练任务时拒绝升级（人话原因）。"""
    try:
        from ..middleware.feature_lock import FeatureLockManager
        active = FeatureLockManager.instance().active_feature
        if active:
            return f"有功能正在运行（{active}）"
    except Exception:  # noqa: BLE001 - 占用检查失败不阻断预检链
        pass
    try:
        from ..services.image_queue import ImageTaskQueue
        from ..services.video_queue import VideoTaskQueue
        img = ImageTaskQueue.instance().snapshot()
        vid = VideoTaskQueue.instance().snapshot()
        busy = (int(img.get("pending", 0) or 0) + int(img.get("running", 0) or 0)
                + int(vid.get("pending", 0) or 0) + int(vid.get("running", 0) or 0))
        if busy:
            return f"生成队列还有 {busy} 个任务"
    except Exception:  # noqa: BLE001
        pass
    return ""


@router.get("/upgrade/status")
def upgrade_status() -> dict[str, Any]:
    """升级总览：当前身份 + updates 目录 + 状态机（断电自愈判据）。"""
    try:
        state = usvc.read_state()
    except Exception:  # noqa: BLE001
        state = {}
    return ok({"current": usvc.current_info(), "state": state,
               "updater_present": (ROOT_DIR / "updater" / "updater.py").is_file()})


@router.get("/upgrade/packages")
def upgrade_packages() -> dict[str, Any]:
    """扫描 updates/ 全部升级包（轻量验签+兼容判定，单包异常不拖垮整体）。"""
    try:
        packages = usvc.scan_updates()
    except UpgradeError as exc:
        raise _err_from(exc) from exc
    return ok({"packages": packages,
               "current": usvc.current_info()})


@router.post("/upgrade/import")
async def upgrade_import(file: UploadFile) -> dict[str, Any]:
    """导入升级包：流式落盘到 updates/（限 2GB），落盘后轻量验签给结论。"""
    name = _safe_package_name(os.path.basename(file.filename or ""))
    dest = usvc.updates_dir() / name
    written = 0
    try:
        usvc.updates_dir().mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_PACKAGE_BYTES:
                    raise ApiError("SYSTEM_PARAM_INVALID",
                                   "升级包超过 2GB 上限，疑似不是升级包",
                                   suggestion="请从正规渠道重新下载 .upg 升级包")
                f.write(chunk)
    except ApiError:
        dest.unlink(missing_ok=True)
        raise
    except OSError as exc:
        raise ApiError("SYSTEM_IO_ERROR", f"包落盘失败: {exc}") from exc
    finally:
        await file.close()
    if written == 0:
        dest.unlink(missing_ok=True)
        raise ApiError("SYSTEM_PARAM_INVALID", "导入内容为空")
    # 落盘即轻量验签：让用户立刻知道包是否可信（深验放在 start）
    entry: dict[str, Any] = {"file": name, "bytes": written,
                             "signature_ok": False, "compatible": False,
                             "reason": "", "manifest": None}
    try:
        manifest, sig = usvc.read_package(dest)
        usvc.verify_signature(manifest, sig)
        entry["signature_ok"] = True
        entry["manifest"] = usvc.summarize(manifest, dest)
        comp, reason = usvc.check_compatibility(manifest)
        entry["compatible"] = comp
        entry["reason"] = reason
    except UpgradeError as exc:
        entry["reason"] = str(exc)
    return ok(entry)


@router.post("/upgrade/start")
def upgrade_start(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """开始升级：三道预检 + 深验 + 任务忙检查 → spawn 独立 updater 进程。

    confirm 必须 == "UPGRADE"（前端确认弹窗回传）。预检任一不过零改动。
    updater 拉起后本请求即返回（后续时序由 updater 全权接管）。
    """
    if str(body.get("confirm") or "") != "UPGRADE":
        raise ApiError("SYSTEM_PARAM_INVALID",
                       "缺少升级确认（confirm 必须为 UPGRADE）",
                       suggestion="请在确认弹窗中点「确认升级」")
    name = _safe_package_name(str(body.get("package") or ""))
    pkg = usvc.updates_dir() / name
    if not pkg.is_file():
        raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", f"升级包不存在: {name}",
                       suggestion="请先在设置页导入升级包")
    updater_py = ROOT_DIR / "updater" / "updater.py"
    if not updater_py.is_file():
        raise ApiError("UPGRADE_UPDATER_MISSING", "updater 组件缺失，无法执行升级",
                       detail={"expect": str(updater_py)})
    try:
        # 预检①：清单+签名（快）
        manifest, sig = usvc.read_package(pkg)
        usvc.verify_signature(manifest, sig)
        comp, reason = usvc.check_compatibility(manifest)
        if not comp:
            raise UpgradeError("UPGRADE_INCOMPATIBLE", reason)
        # 预检②：磁盘余量 ≥ payload×2.2（落位+备份双份余量）
        need = int(manifest.get("payload_bytes", 0)) * 2.2 + 64 * 1024 * 1024
        free = shutil.disk_usage(str(ROOT_DIR)).free
        if free < need:
            raise UpgradeError(
                "UPGRADE_DISK_FULL",
                f"磁盘余量不足（需约 {need / 1024 / 1024:.0f}MB，"
                f"当前剩余 {free / 1024 / 1024:.0f}MB）",
                suggestion="请清理磁盘后重试")
        # 预检③：任务占用（生成/训练进行中拒绝）
        busy = _busy_reason()
        if busy:
            raise UpgradeError(
                "UPGRADE_BUSY", f"当前{busy}，请等任务完成或取消后再升级")
        # 深验：解包+逐文件 SHA256（含 zip-slip/符号链接/保护区三闸）
        work_dir = usvc.updates_dir() / "_work"
        shutil.rmtree(work_dir, ignore_errors=True)
        usvc.extract_verified(pkg, work_dir)
    except UpgradeError as exc:
        raise _err_from(exc) from exc

    # spawn 独立 updater（DETACHED：脱离本进程树，后端死了它活着）
    state = {"phase": "handed_over", "package": name,
             "to_version": manifest.get("to_version"),
             "to_build_id": manifest.get("to_build_id"),
             "updated_at": __import__("time").time()}
    usvc.write_state(state)
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
    python_exe = Path(sys.executable)
    subprocess.Popen(
        [str(python_exe), str(updater_py), "--install-root", str(ROOT_DIR)],
        cwd=str(ROOT_DIR), creationflags=flags,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True)
    from ..services.event_log import log_event
    log_event("system", "upgrade_started",
              f"开始升级到 v{manifest.get('to_version')}，软件将自动关闭并更新，"
              "完成后自动重启（请勿断电）", level="warning")
    return ok({"started": True, "package": name,
               "to_version": manifest.get("to_version")})


@router.delete("/upgrade/packages/{name}")
def upgrade_delete_package(name: str) -> dict[str, Any]:
    """删除 updates/ 下指定升级包（纯文件名白名单）。"""
    base = _safe_package_name(name)
    pkg = usvc.updates_dir() / base
    if not pkg.is_file():
        raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", f"升级包不存在: {base}")
    pkg.unlink()
    return ok({"deleted": base})


@router.post("/upgrade/open-folder")
def upgrade_open_folder() -> dict[str, Any]:
    """打开 updates/ 文件夹（用户拖包的指定目录）。"""
    udir = usvc.updates_dir()
    udir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(udir))  # noqa: S606 - 固定目录，无用户可控参数
    else:
        raise ApiError("UNSUPPORTED_FORMAT", "仅支持 Windows 桌面环境")
    return ok({"opened": str(udir)})
