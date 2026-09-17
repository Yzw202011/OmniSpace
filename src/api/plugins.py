"""插件系统 API（OSP v1，插件系统 P1 2026-09-16）。

端点清单（/api/v1 前缀由 main.py 挂载）：
- GET   /plugins                 已登记插件清单（状态/信任级/能力/统计）
- POST  /plugins/{name}/load     加载（幂等；invoke 也会自动加载）
- POST  /plugins/{name}/unload   卸载（faulty 复位通道）
- POST  /plugins/{name}/invoke   执行插件任务（通用 data 形态：spec
                                  原样交插件 → 结果键透传；帧本体等
                                  大数组在 registry 层压形状摘要——
                                  POC2-A 实测 1080p float64 帧列 1.2GB）

2026-09-17 用户令：video-making 插件（运镜预览）整链移除，invoke 的
keyframes/shots video 形态随之退役，仅保留通用 data 形态。
"""
from __future__ import annotations

import ast
import inspect
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Query, UploadFile
from pydantic import BaseModel, Field

from ..cutemamen.pkg import native_registry
from ..middleware.error_handler import ApiError, ok
from ..services.offload import run_blocking
from ..services.plugin_runtime import get_plugin_runtime
from ..services.plugin_runtime import registry as pr_registry
from ..services.plugin_runtime.kernel_gateway import (
    get_plugin_kernel,
    register_user_pkg,
    unregister_plugin,
)
from ..services.plugin_runtime.loader import (
    PluginLoadError,
    find_plugin_classes,
    load_plugin_module,
    read_cutemamen_pkg,
)
from ..services.plugin_runtime.registry import (
    MIN_FREE_RAM_GB,
    PluginRuntimeError,
    _json_summary,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["plugins"])


class PluginInvokeRequest(BaseModel):
    """invoke 请求：通用形态，data 原样作为插件 spec。

    如 rust-coding 的 {"code": ...}；结果键由插件决定、原样透传。
    """
    timeout_s: float = Field(
        default=180.0, ge=1.0, le=600.0)
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="插件 spec（如 {\"code\": ...}）")


def _translate(exc: PluginRuntimeError) -> ApiError:
    return ApiError(exc.code, exc.message, suggestion=exc.suggestion)


@router.get("/plugins")
async def list_plugins() -> dict[str, Any]:
    """已登记插件清单（轻量，无加载副作用）。"""
    return ok({"plugins": get_plugin_runtime().plugins_info()})


@router.get("/plugins/skills")
async def list_plugin_skills(
        feature: str | None = Query(
            default=None,
            description="功能页过滤：chat/novel/comic/manga；缺省=全部")
        ) -> dict[str, Any]:
    """技能索引（技能插座批0）：各创作功能页「插件技能」入口的数据源。"""
    rt = get_plugin_runtime()
    try:
        skills = rt.skills_info(feature)
    except PluginRuntimeError as exc:
        raise _translate(exc) from exc
    return ok({"skills": skills,
               "features": list(pr_registry.SKILL_FEATURES)})


@router.post("/plugins/{name}/load")
async def load_plugin(name: str) -> dict[str, Any]:
    """加载插件（幂等；产品铁律=未加载自动加载，不要求用户点两次）。"""
    rt = get_plugin_runtime()
    try:
        instance = await run_blocking(rt.ensure_loaded, name)
    except PluginRuntimeError as exc:
        raise _translate(exc) from exc
    return ok({"name": name, "state": "loaded",
               "capability": instance.CAPABILITY})


@router.post("/plugins/{name}/unload")
async def unload_plugin(name: str) -> dict[str, Any]:
    """卸载插件（故障态复位通道：unload → load 即重建）。"""
    rt = get_plugin_runtime()
    try:
        await run_blocking(rt.unload, name)
    except PluginRuntimeError as exc:
        raise _translate(exc) from exc
    return ok({"name": name, "state": "unloaded"})


@router.post("/plugins/{name}/invoke")
async def invoke_plugin(name: str, req: PluginInvokeRequest
                        ) -> dict[str, Any]:
    """执行插件任务：data 原样作 spec → 自动加载 → 结果键透传。"""
    if not req.data:
        raise ApiError("PLUGIN_SPEC_MISMATCH", "data 不能为空",
                       suggestion='传插件 spec（如 {"code": ...}）')
    rt = get_plugin_runtime()
    try:
        result = await rt.invoke(name, dict(req.data),
                                 timeout_s=req.timeout_s)
    except PluginRuntimeError as exc:
        raise _translate(exc) from exc
    return ok(result)


# ── 内核接线（2026-09-16 拍板：CuteMamen 内核产品入口） ──────────

class KernelThinkRequest(BaseModel):
    """内核 think 请求：按主题路由到插件（未加载自动热加载）。"""
    topic: str = Field(min_length=1, max_length=64,
                       description="路由主题（如 rust）")
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="事件数据（如 rust 传 {\"code\": ...}）")


@router.get("/plugins/kernel")
async def kernel_status() -> dict[str, Any]:
    """内核状态：统计 + 已挂载/已登记插件（轻量，无加载副作用）。"""
    kernel = get_plugin_kernel()
    return ok({"kernel": kernel.stats(),
               "plugins": kernel.list_plugins()})


@router.post("/plugins/kernel/think")
async def kernel_think(req: KernelThinkRequest) -> dict[str, Any]:
    """内核路由一次事件 → 目标插件 on_think → 结果（JSON 安全化出线）。

    纯 CPU 轻量内核（numpy），帧本体等大数组压成形状摘要；
    RAM 闸口径与 registry.invoke 一致。
    """
    import psutil
    avail_gb = psutil.virtual_memory().available / (1 << 30)
    if avail_gb < MIN_FREE_RAM_GB:
        raise ApiError("PLUGIN_RAM_LOW",
                       f"系统可用内存不足（{avail_gb:.1f}GB < {MIN_FREE_RAM_GB}GB）",
                       suggestion="关闭其他大内存任务后重试")
    kernel = get_plugin_kernel()
    results = await run_blocking(
        kernel.think, {"topic": req.topic, "data": req.data})
    safe = [_json_summary(r) for r in results if r is not None]
    routed = len(safe) > 0
    return ok({"topic": req.topic, "routed": routed, "results": safe})


# ── 用户导入（2026-09-16 拍板；规范=docs/插件开发规范.md） ────────

# 源码静态安检（机器闸，规范 §2）：AST 级查禁，命中即拒
_FORBIDDEN_MODULES = {
    "subprocess", "socket", "urllib", "urllib3", "requests", "http",
    "httpx", "ctypes", "pickle", "cpickle", "multiprocessing",
    "importlib", "webbrowser", "ftplib", "smtplib", "telnetlib",
    # 2026-09-17 终态=A 补口（审计实证的绕过向量；黑名单仍属防呆层，
    # 真正的隔离墙=含源码档子进程沙箱 sandbox.py）：
    "builtins", "shutil", "runpy", "codeop", "code",
}
_BARE_FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "open"}
# 属性调用黑名单：名字本身几乎不可能是良性方法（防 os.system/Path.unlink 等）
_STRICT_ATTR_FORBIDDEN = {
    "system", "popen", "unlink", "rmdir", "removedirs", "makedirs",
    "mkdir", "startfile", "execv", "execve", "execvp", "execvpe",
    "spawnl", "spawnle", "spawnv", "spawnve", "fork", "forkpty",
    "kill", "killpg", "write_text", "write_bytes", "read_text",
    "read_bytes",
    # 2026-09-17 补口：io.open / os.remove / os.posix_spawn 系
    "remove", "posix_spawn", "posix_spawnp", "spawn", "terminate",
}


def _scan_source_violations(code_text: str) -> list[str]:
    """AST 静态安检：返回违规清单（行号+原因），空列表=通过。"""
    violations: list[str] = []
    try:
        tree = ast.parse(code_text)
    except SyntaxError as exc:
        # 2026-09-17 收口：此前 SyntaxError 直穿 → main.py 无兜底 → 500；
        # 坏语法属用户输入错误，必须走语义错误码
        raise ApiError("PLUGIN_SOURCE_INVALID",
                       f"源码语法错误（第 {exc.lineno or '?'} 行）: {exc.msg}"
                       ) from None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _FORBIDDEN_MODULES:
                    violations.append(f"第 {node.lineno} 行：禁用导入 {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".")[0]
            if root in _FORBIDDEN_MODULES:
                violations.append(f"第 {node.lineno} 行：禁用导入 from {module}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _BARE_FORBIDDEN_CALLS:
                violations.append(f"第 {node.lineno} 行：禁用调用 {func.id}()")
            elif isinstance(func, ast.Attribute) and \
                    func.attr in _STRICT_ATTR_FORBIDDEN:
                violations.append(f"第 {node.lineno} 行：禁用调用 .{func.attr}()")
    return violations


def _import_plugin_core(package_bytes: bytes, package_filename: str,
                        source_bytes: bytes | None,
                        source_filename: str | None,
                        confirm_source: bool) -> dict[str, Any]:
    """导入管线（线程体）：校验链 → 落盘 → 登记（失败清场）。"""
    if not package_filename.lower().endswith(".cutemamen"):
        raise ApiError("PLUGIN_PACKAGE_INVALID",
                       f"插件包必须是 .CuteMamen 文件: {package_filename}",
                       suggestion="选择 .CuteMamen 插件包（见 docs/插件开发规范.md §4）")
    if len(package_bytes) > 64 * 1024 * 1024:
        raise ApiError("PLUGIN_PACKAGE_INVALID", "插件包超过 64MB 上限")
    if package_bytes[:2] != b"\x1f\x8b":
        raise ApiError("PLUGIN_PACKAGE_INVALID",
                       "插件包不是合法的 gzip 归档（魔数校验失败）")

    # 内存直读校验（条目白名单/上限/manifest 必备全在 loader 闸内）
    tmp = tempfile.NamedTemporaryFile(suffix=".CuteMamen", delete=False)
    try:
        tmp.write(package_bytes)
        tmp.close()
        pkg = read_cutemamen_pkg(Path(tmp.name))
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    manifest = pkg.manifest
    name = str(manifest.get("name") or "")
    if not re.match(pr_registry.PLUGIN_NAME_RE, name):
        raise ApiError("PLUGIN_PACKAGE_INVALID",
                       f"manifest.name 不合规: {name!r}",
                       suggestion="规则 ^[a-z0-9][a-z0-9_-]{0,63}$（docs/插件开发规范.md §4）")
    base_model = str(manifest.get("base_model") or "")
    # 技能插座批0：manifest.skills 校验（非法即拒，规范 §3.1）
    try:
        skills = pr_registry.validate_skills(manifest.get("skills"))
    except PluginRuntimeError as exc:
        raise _translate(exc) from exc
    # v2.1 内嵌源码：包里带 source/plugin.py 即视为含源码档（用户只选
    # 一个文件）；外部分开上传的 .py 仅为兼容旧流程保留，内嵌优先
    if source_bytes is None and pkg.source:
        source_bytes = pkg.source.encode("utf-8")
        source_filename = f"{name}.py"

    rt = get_plugin_runtime()
    if rt.is_registered(name):
        raise ApiError("PLUGIN_ALREADY_REGISTERED",
                       f"插件名已存在: {name}",
                       suggestion="换一个名字，或先删除同名旧插件")

    native = native_registry()
    source_path: Path
    written: list[Path] = []
    if source_bytes is None:
        # 纯数据档：base_model 必须是已知类型，源码复用原生实现
        if base_model not in native:
            raise ApiError(
                "PLUGIN_SOURCE_REQUIRED",
                f"新类型插件（base_model={base_model!r}）必须附 .py 源码",
                suggestion="上传源码文件（见 docs/插件开发规范.md §1/§2）")
        trust = "user_data"
        source_path = Path(inspect.getfile(native[base_model])).resolve()
    else:
        # 含源码档：安检 → 确认门 → 试装载
        if not (source_filename or "").lower().endswith(".py"):
            raise ApiError("PLUGIN_SOURCE_INVALID", "源码必须是 .py 文件")
        if len(source_bytes) > pr_registry.MAX_SOURCE_BYTES:
            raise ApiError("PLUGIN_SOURCE_INVALID",
                           f"源码超过 {pr_registry.MAX_SOURCE_BYTES // 1024}KB 上限")
        try:
            source_text = source_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise ApiError("PLUGIN_SOURCE_INVALID",
                           "源码不是 UTF-8 文本") from None
        violations = _scan_source_violations(source_text)
        if violations:
            raise ApiError(
                "PLUGIN_SOURCE_FORBIDDEN",
                f"源码静态安检未通过（{len(violations)} 处违规）",
                suggestion="；".join(violations[:8])
                           + "（禁用清单见 docs/插件开发规范.md §2）")
        if not confirm_source:
            raise ApiError(
                "PLUGIN_SOURCE_CONFIRM_REQUIRED",
                "含源码插件需用户显式确认后才能导入",
                suggestion="此插件将在软件内部直接运行，请只安装信任来源；"
                           "确认信任请带 confirm_source=true 重试")
        tmp_src = tempfile.NamedTemporaryFile(suffix=".py", delete=False)
        try:
            tmp_src.write(source_bytes)
            tmp_src.close()
            module = load_plugin_module(Path(tmp_src.name))
            if not find_plugin_classes(module):
                raise ApiError("PLUGIN_SOURCE_INVALID",
                               "源码内没有 ExpertPlugin 子类",
                               suggestion="参照 src/cutemamen/rust_coding.py 的写法")
        except PluginLoadError as exc:
            raise ApiError("PLUGIN_SOURCE_INVALID",
                           f"源码试装载失败: {exc}") from exc
        finally:
            Path(tmp_src.name).unlink(missing_ok=True)
        trust = "user_source"

    # 落盘 + 登记（登记失败清场）
    pr_registry.USER_PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    pkg_path = pr_registry.USER_PLUGIN_DIR / f"{name}.CuteMamen"
    pkg_path.write_bytes(package_bytes)
    written.append(pkg_path)
    if source_bytes is not None:
        source_path = pr_registry.USER_PLUGIN_DIR / f"{name}.py"
        source_path.write_bytes(source_bytes)
        written.append(source_path)
    imported_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        rt.register(name, source_path, pkg_path, trust,
                    imported_at=imported_at, skills=skills)
        rt.save_user_registry()
    except PluginRuntimeError as exc:
        for f in written:
            f.unlink(missing_ok=True)
        raise _translate(exc) from exc
    if trust == "user_data":
        register_user_pkg(pkg_path)
    return {
        "name": name, "trust": trust,
        "trust_label": pr_registry._TRUST_LABELS.get(trust, trust),
        "base_model": base_model,
        "route": str(manifest.get("route") or ""),
        "capability": str(manifest.get("capability") or ""),
        "skills": skills,
        "origin": "user", "imported_at": imported_at,
    }


@router.post("/plugins/import")
async def import_plugin(
        package: UploadFile = File(..., description=".CuteMamen 插件包"),
        source: UploadFile | None = File(
            default=None, description="可选 .py 源码（新类型插件必附）"),
        confirm_source: bool = Form(
            default=False,
            description="含源码插件的显式确认（用户勾选后前端才置 true）"),
) -> dict[str, Any]:
    """导入用户插件：校验链（规范=docs/插件开发规范.md）→ 登记 → 持久化。"""
    package_bytes = await package.read()
    source_bytes = await source.read() if source is not None else None
    result = await run_blocking(
        _import_plugin_core, package_bytes,
        package.filename or "package.CuteMamen",
        source_bytes, source.filename if source else None,
        confirm_source)
    return ok(result)


@router.post("/plugins/{name}/enable")
async def enable_plugin(name: str) -> dict[str, Any]:
    """启用插件（停用件恢复可用）。"""
    rt = get_plugin_runtime()
    try:
        await run_blocking(rt.set_enabled, name, True)
    except PluginRuntimeError as exc:
        raise _translate(exc) from exc
    return ok({"name": name, "enabled": True})


@router.post("/plugins/{name}/disable")
async def disable_plugin(name: str) -> dict[str, Any]:
    """停用插件（即卸载释放内存；重启后保持停用状态）。"""
    rt = get_plugin_runtime()
    try:
        await run_blocking(rt.set_enabled, name, False)
    except PluginRuntimeError as exc:
        raise _translate(exc) from exc
    return ok({"name": name, "enabled": False})


@router.delete("/plugins/{name}")
async def delete_plugin(name: str) -> dict[str, Any]:
    """删除用户插件（出厂插件拒删；文件与登记一并清理）。"""
    rt = get_plugin_runtime()
    try:
        await run_blocking(rt.remove, name)
    except PluginRuntimeError as exc:
        raise _translate(exc) from exc
    unregister_plugin(name)
    return ok({"name": name, "deleted": True})
