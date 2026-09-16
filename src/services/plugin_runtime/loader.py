"""插件加载器：源码模块加载 + CuteMamen 包内存直读。

安全要点（方案 v1.2 修正案 2/3/12）：
- CuteMamen 包**内存直读**（tarfile → extractfile → BytesIO），
  永不 extractall 落盘——tar 路径穿越面归零；npz 走
  ``np.load(..., allow_pickle=False)``（npy/npz 无 pickle 面）；
- 源码加载 = 在宿主进程内 exec 插件代码（进程内插件的信任前提，
  见 base.py 信任模型声明）；
- py310/py312 双兼容（禁 3.11+ 语法）。
"""
from __future__ import annotations

import io
import json
import sys
import tarfile
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .base import ExpertPlugin

# 宿主标准模块名（插件写 from omnispace.plugin import ...）
HOST_MODULE = "omnispace.plugin"
_MODULE_PREFIX = "omnispace_plugin_"

# CuteMamen 包条目白名单与上限（防御性：内存读天然免疫穿越，仍卡规模）
_PKG_MAX_ENTRIES = 64
_PKG_MAX_BYTES = 64 * 1024 * 1024
_PKG_ALLOWED = ("manifest.json", "weights/weights.npz",
                "memory/working.json", "memory/episodic.json",
                "memory/semantic.json")


class PluginLoadError(RuntimeError):
    """插件加载失败（源码执行错/包结构不合规）。"""


@dataclass
class CuteMamenPkg:
    """内存态插件包（manifest + 权重 + 三级记忆）。"""
    manifest: dict[str, Any] = field(default_factory=dict)
    weights: dict[str, np.ndarray] = field(default_factory=dict)
    memory: dict[str, dict[str, Any]] = field(default_factory=dict)


def _ensure_host_module() -> None:
    """把 base.py 的基类注入宿主标准模块名（幂等）。

    插件源码 ``from omnispace.plugin import ExpertPlugin`` 由此生效；
    重复调用不重复注入（热重载拿同一基类，isinstance 判定稳定）。
    """
    if HOST_MODULE in sys.modules:
        return
    from . import base
    pkg = sys.modules.get("omnispace")
    if pkg is None:
        pkg = types.ModuleType("omnispace")
        pkg.__path__ = []  # type: ignore[attr-defined]  # 标记为包供子模块解析
        sys.modules["omnispace"] = pkg
    sys.modules[HOST_MODULE] = base


def load_plugin_module(py_path: Path) -> types.ModuleType:
    """按独立命名空间加载插件源码模块（不走 sys.path，避免名字劫持）。

    每次加载生成新模块名（omnispace_plugin_<stem>_<n>）：unload 后
    重载拿新实例，旧模块残留由 GC 回收（Python 模块缓存不可驱逐，
    这是进程内热重载的固有限制，文档化而非掩盖）。
    """
    _ensure_host_module()
    if not py_path.is_file():
        raise PluginLoadError(f"插件源码不存在: {py_path}")
    try:
        source = py_path.read_bytes()
    except OSError as exc:
        raise PluginLoadError(f"插件源码读取失败: {exc}") from exc
    mod_name = f"{_MODULE_PREFIX}{py_path.stem}_{id(source) & 0xFFFFFF:x}"
    mod = types.ModuleType(mod_name)
    mod.__file__ = str(py_path)
    try:
        code = compile(source, str(py_path), "exec")
        sys.modules[mod_name] = mod  # 先注册再 exec（供相对自引用解析）
        exec(code, mod.__dict__)  # noqa: S102 - 进程内插件=信任代码（base.py 声明）
    except SyntaxError as exc:
        sys.modules.pop(mod_name, None)
        raise PluginLoadError(f"插件语法错误: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - 加载期任何异常统一收口
        sys.modules.pop(mod_name, None)
        raise PluginLoadError(f"插件执行失败: {exc}") from exc
    return mod


def find_plugin_classes(module: types.ModuleType) -> list[type[ExpertPlugin]]:
    """发现模块内的 ExpertPlugin 具体子类（排除宿主基类自身）。"""
    found: list[type[ExpertPlugin]] = []
    seen = {id(ExpertPlugin)}
    for attr in vars(module).values():
        if (isinstance(attr, type) and issubclass(attr, ExpertPlugin)
                and id(attr) not in seen):
            found.append(attr)
            seen.add(id(attr))
    return found


def read_cutemamen_pkg(pkg_path: Path) -> CuteMamenPkg:
    """内存直读 CuteMamen 包（tar.gz：manifest + weights + memory）。

    DistributedFormer 专属字段（min_core_version/core_version/
    memory_budget）只登记不参与逻辑——我们用自己的标准版本
    （方案 v1.2 修正案 3）。
    """
    if not pkg_path.is_file():
        raise PluginLoadError(f"插件包不存在: {pkg_path}")
    pkg = CuteMamenPkg()
    try:
        with tarfile.open(pkg_path, "r:gz") as tar:
            members = [m for m in tar.getmembers() if m.isfile()]
            if len(members) > _PKG_MAX_ENTRIES:
                raise PluginLoadError(f"包条目超限: {len(members)}")
            for m in members:
                if m.name not in _PKG_ALLOWED or m.size > _PKG_MAX_BYTES:
                    raise PluginLoadError(f"包条目不合规: {m.name}")
                f = tar.extractfile(m)
                if f is None:
                    continue
                _parse_pkg_entry(pkg, m.name, f.read())
    except tarfile.TarError as exc:
        raise PluginLoadError(f"插件包损坏: {exc}") from exc
    if not pkg.manifest:
        raise PluginLoadError("插件包缺 manifest.json")
    return pkg


def _parse_pkg_entry(pkg: CuteMamenPkg, name: str, raw: bytes) -> None:
    """单条目解析：manifest / 权重 / 三级记忆。"""
    if name == "manifest.json":
        pkg.manifest = json.loads(raw.decode("utf-8"))
    elif name == "weights/weights.npz":
        # allow_pickle=False：npy/npz 安全体（无任意代码执行面）
        with np.load(io.BytesIO(raw), allow_pickle=False) as arrs:
            pkg.weights = {k: arrs[k] for k in arrs.files}
    elif name.startswith("memory/"):
        level = name.split("/", 1)[1].rsplit(".", 1)[0]
        data = json.loads(raw.decode("utf-8"))
        entries = data.get("entries") if isinstance(data, dict) else None
        if isinstance(entries, dict):
            pkg.memory[level] = dict(entries)
        elif isinstance(entries, list):  # [{key, value}] 形态兼容
            pkg.memory[level] = {
                str(e.get("key")): e.get("value")
                for e in entries if isinstance(e, dict) and "key" in e}


def read_image_as_frame(img_path: Path) -> np.ndarray:
    """读服务端图片为 HxWx3 uint8（值域归一化交给插件 _as_frame）。"""
    img: Image.Image = Image.open(img_path)
    img = img.convert("RGB")
    frame: np.ndarray = np.asarray(img, dtype=np.uint8)
    if frame.ndim != 3 or frame.shape[2] != 3:
        raise PluginLoadError(f"图片维度不支持: {img_path} -> {frame.shape}")
    return frame


def frames_to_png_files(frames: list[np.ndarray], out_dir: Path,
                        prefix: str = "frame") -> list[str]:
    """帧序列逐帧 uint8 化落盘 PNG（内存红线：float64 即转即弃）。

    POC2-A 实测：1080p float64 全帧驻留 1.2GB（RAM 静默死亡历史雷），
    uint8 化省 8 倍且逐帧处理峰值恒为单帧级。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for i, frame in enumerate(frames):
        arr = np.clip(np.asarray(frame), 0.0, 1.0)
        if arr.dtype != np.uint8:
            arr = (arr * 255.0).astype(np.uint8)
        if arr.ndim == 2:  # 灰度帧补通道（PNG 统一 RGB 三通道）
            arr = np.repeat(arr[:, :, None], 3, axis=2)
        name = f"{prefix}_{i:05d}.png"
        Image.fromarray(arr, mode="RGB").save(out_dir / name, format="PNG")
        names.append(name)
    return names


def optional_manifest_str(manifest: dict[str, Any], key: str,
                          default: str = "") -> str:
    """manifest 字段安全读取（缺省/类型不符给默认值）。"""
    val = manifest.get(key, default)
    return val if isinstance(val, str) else default
