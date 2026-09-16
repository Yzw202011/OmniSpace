"""ComfyUI 模型挂接器（产品化运行时，2026-09-02 体验流定稿配套）。

与 link_comfy_models.py 的分工：那边是卖家机工具（--export 扫本机硬链
生成映射表、--check 体检）；本模块是随包运行时——在 ComfyUI 拉起前按
comfy_model_map.json 把中央 models 的权重硬链接进 ComfyUI 分类目录。

设计要点（docs/模型即插即用-多轮理论验证-2026-09-02.md 三章背书）：
  - 幂等判据＝两端同 inode（NTFS file index，os.stat().st_ino），
    防「删后重放」版本漂移（同路径不同 inode 视为冲突，保守跳过）；
  - 孤儿清理＝映射表登记的门牌、源模型已删且目标无其他硬链伙伴时，
    删除该死链（表是权威：登记过的位置就是受管槽位）；
  - 冲突＝目标被外来文件占用（不同 inode）：绝不删用户数据，跳过+报告；
  - 跨卷＝CreateHardLinkW winerror 17（ERROR_NOT_SAME_DEVICE）逐条
    计入 cross_volume，由调用方提示「移动到同盘」。

调用方（都按文件路径 importlib 加载本模块，不进包体系）：
  - src/services/inference/comfy_proc.py：spawn() 拉起前（主时机，
    引擎每次冷启都重挂，天然覆盖拖入后的一切场景）；
  - launcher/boot.py：拖入识别（通道 A）与启动期接线反馈。

纯 stdlib；本目录仓库真源 scripts/comfy_link/，包内落 modelxiazai/
（make_dist.py COPY_DIRS 映射），两处布局下 REPO 推导一致。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import ctypes
import errno
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[2]
DEFAULT_COMFY_MODELS = (REPO / "tools" / "ComfyUI_windows_portable"
                        / "ComfyUI" / "models")
DEFAULT_CENTRAL = REPO / "models"
DEFAULT_MAP = Path(__file__).resolve().parent / "comfy_model_map.json"

# Windows CreateHardLinkW 的跨卷失败码（ERROR_NOT_SAME_DEVICE）
ERROR_NOT_SAME_DEVICE = 17

# 必须 use_last_error=True 否则 ctypes.get_last_error() 永远是陈旧值
# （link_comfy_models.py 旧写法同病：失败码从未被真实读到过）
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_CreateHardLinkW = _kernel32.CreateHardLinkW
_CreateHardLinkW.restype = ctypes.c_int
_CreateHardLinkW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p,
                             ctypes.c_void_p]

# 拖入根识别：models_manifest.json 或任一已知分类目录
_KNOWN_CATEGORIES = (
    "image_gen", "video_gen", "paint", "dialog", "lora", "embed", "face",
    "segment", "whisper-tiny", "sam-vit-h", "sd15", "style_lora", "_build",
)


@dataclass
class MountReport:
    """一次 ensure_mounted 的对账结果（caller 据此打日志/提示）。"""

    total: int = 0
    linked: int = 0           # 本次新建硬链
    ok: int = 0               # 已就位（两端同 inode，幂等跳过）
    missing_src: int = 0      # 中央缺文件（大模型包没拖齐）
    orphan_cleaned: int = 0   # 清掉的死链（源已删、目标无伙伴）
    conflict: int = 0         # 目标被外来文件占用（保守跳过不删）
    cross_volume: int = 0     # 跨卷硬链失败（提示移动到同盘）
    errors: int = 0           # 其他 IO 错误
    details: list = field(default_factory=list)

    def summary_line(self) -> str:
        return (f"共{self.total} 已就位{self.ok} 新链{self.linked} "
                f"缺源{self.missing_src} 清死链{self.orphan_cleaned} "
                f"冲突{self.conflict} 跨卷{self.cross_volume} "
                f"错误{self.errors}")


def find_map_file(root: Path | None = None) -> Path | None:
    """定位随包映射表（dev=scripts/comfy_link，包=modelxiazai）。"""
    base = root or REPO
    for d in (base / "scripts" / "comfy_link", base / "modelxiazai"):
        f = d / "comfy_model_map.json"
        if f.is_file():
            return f
    return None


def validate_models_root(path: Path) -> tuple[bool, str]:
    """判断拖入的文件夹像不像大模型包根（有清单或已知分类目录）。"""
    try:
        if not path.is_dir():
            return False, "不是文件夹"
        if (path / "models_manifest.json").is_file():
            return True, "含 models_manifest.json"
        hit = [c for c in _KNOWN_CATEGORIES if (path / c).is_dir()]
        if hit:
            return True, f"含分类目录 {hit[0]}"
        return False, "缺 models_manifest.json 与已知分类目录"
    except OSError as exc:
        return False, f"读取失败: {exc}"


def _same_inode(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_ino == os.stat(b).st_ino
    except OSError:
        return False


def _hardlink(src: Path, dst: Path) -> None:
    """CreateHardLinkW；跨卷抛 winerror 17（4 参 OSError 携带 winerror，
    errno 由 Windows 自动映射，供 EXDEV 兜底判断）。"""
    if not _CreateHardLinkW(str(dst), str(src), None):
        err = ctypes.get_last_error()
        raise OSError(0, f"CreateHardLinkW 失败 (winerror={err})",
                      str(dst), err)


def ensure_mounted(models_root: Path | None = None,
                   comfy_models: Path | None = None,
                   map_file: Path | None = None,
                   clean_orphans: bool = True) -> MountReport:
    """按映射表把中央 models 挂进 ComfyUI 分类目录（幂等，可反复调用）。

    models_root 为 None 时用包根 models/（通道 B：用户拖进安装目录）；
    boot 拖入识别（通道 A）把外部路径作为 models_root 传入——同卷即可
    直接挂接，跨卷逐条计入 cross_volume 由调用方提示移动。
    """
    report = MountReport()
    central = Path(models_root) if models_root else DEFAULT_CENTRAL
    comfy = Path(comfy_models) if comfy_models else DEFAULT_COMFY_MODELS
    map_path = Path(map_file) if map_file else DEFAULT_MAP
    if not map_path.is_file():
        return report  # 无映射表＝无可管槽位，静默空报告
    try:
        mapping = json.loads(map_path.read_text("utf-8"))["map"]
    except (OSError, ValueError, KeyError):
        report.errors = 1
        report.details.append(f"映射表不可读: {map_path}")
        return report

    report.total = len(mapping)
    for comfy_rel, central_rel in mapping.items():
        src = central / central_rel
        dst = comfy / comfy_rel
        try:
            if src.is_file():
                if dst.exists():
                    if _same_inode(src, dst):
                        report.ok += 1
                    else:
                        # 表说这里是受管链接槽位，现实是别的文件——绝不删
                        report.conflict += 1
                        report.details.append(
                            f"冲突 {comfy_rel}（目标已存在且非同源，"
                            f"确认后删除目标文件可自动重接）")
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        _hardlink(src, dst)
                        report.linked += 1
                    except OSError as exc:
                        if (getattr(exc, "winerror", None)
                                == ERROR_NOT_SAME_DEVICE
                                or exc.errno == errno.EXDEV):
                            report.cross_volume += 1
                        else:
                            report.errors += 1
                            report.details.append(f"链接失败 {comfy_rel}: {exc}")
            else:
                if dst.exists():
                    st = os.stat(dst)
                    if st.st_nlink <= 1 and clean_orphans:
                        # 登记过的死链：源模型已删、门牌还占着引擎列表
                        dst.unlink()
                        report.orphan_cleaned += 1
                        report.details.append(f"清死链 {comfy_rel}（源已删）")
                    else:
                        report.conflict += 1
                        report.details.append(
                            f"冲突 {comfy_rel}（源缺且目标有其他硬链伙伴）")
                else:
                    report.missing_src += 1
        except OSError as exc:
            report.errors += 1
            report.details.append(f"{comfy_rel}: {exc}")
    return report


# ── 跨卷降级（2b，2026-09-03）─────────────────────────────────────
# 硬链同卷才成立；模型在别的盘时按三级策略降级：
#   ① 目标已存在（同卷挂接已覆盖/混合场景）→ 跳过；
#   ② 「平条目」（comfy 侧就是 类型/文件名 两级）且文件名与中央侧同名
#     → extra_model_paths.yaml 追加目录（ComfyUI 引擎 v0.34 原生支持，
#     cli_args.py:71 --extra-model-paths-config；utils/extra_config.py
#     按类型吃「换行分隔的路径串」，零拷贝）；
#   ③ 需改名/带子目录形状（yaml 复现不了，如 vae/flux2-vae ←
#     diffusion_pytorch_model、insightface/models/antelopev2 形状）→
#     小文件（≤copy_limit）拷贝进引擎 models 树保留门牌；超限进
#     unresolvable（指路：移到同盘）。
# yaml 落 data/comfyui/（引擎树只读红线不破），comfy_proc 启动时以
# --extra-model-paths-config 指入。

YAML_SECTION = "omnispace-external"
DEFAULT_COPY_LIMIT_BYTES = 1024 * 1024 * 1024  # 1GB：改名件里最大的
# vae/flux2-vae 仅 161MB，1GB 上限覆盖全部已知改名件；再大就该移盘而非拷贝


@dataclass
class FallbackReport:
    """一次 cross_volume_fallback 的降级结果。"""

    yaml_path: str = ""
    yaml_types: list = field(default_factory=list)   # 覆盖的 comfy 类型
    yaml_entries: int = 0                            # 经 yaml 覆盖的条目数
    already: int = 0                                 # 目标已在位（硬链已覆盖）
    copied: list = field(default_factory=list)       # 拷贝件（改名/子目录形状）
    copy_bytes: int = 0
    unresolvable: list = field(default_factory=list) # 超限改名件（移盘指路）
    missing: int = 0

    def summary_line(self) -> str:
        return (f"yaml覆盖{self.yaml_entries}({len(self.yaml_types)}类) "
                f"已挂接跳过{self.already} 拷贝{len(self.copied)} "
                f"({self.copy_bytes // (1024 * 1024)}MB) "
                f"无解{len(self.unresolvable)} 缺源{self.missing}")


def cross_volume_fallback(models_root: Path | None = None,
                          comfy_models: Path | None = None,
                          map_file: Path | None = None,
                          yaml_path: Path | None = None,
                          copy_limit_bytes: int = DEFAULT_COPY_LIMIT_BYTES,
                          ) -> FallbackReport:
    """跨卷三级降级：yaml 追加目录 + 小件拷贝 + 无解清单，并写 yaml。"""
    central = Path(models_root) if models_root else DEFAULT_CENTRAL
    comfy = Path(comfy_models) if comfy_models else DEFAULT_COMFY_MODELS
    map_path = Path(map_file) if map_file else DEFAULT_MAP
    ypath = (Path(yaml_path) if yaml_path
             else central.parent / "data" / "comfyui"
             / "extra_model_paths.yaml")
    rep = FallbackReport(yaml_path=str(ypath))
    if not map_path.is_file():
        return rep
    try:
        mapping = json.loads(map_path.read_text("utf-8"))["map"]
    except (OSError, ValueError, KeyError):
        return rep

    type_dirs: dict[str, set[str]] = {}
    for comfy_rel, central_rel in mapping.items():
        src = central / central_rel
        dst = comfy / comfy_rel
        if not src.is_file():
            rep.missing += 1
            continue
        if dst.exists():
            rep.already += 1          # 同卷挂接已覆盖（混合场景）
            continue
        parts = PurePosixPath(comfy_rel).parts
        same_name = (Path(comfy_rel).name == Path(central_rel).name)
        if len(parts) == 2 and same_name:
            # 平条目且同名：类型 ← 文件所在目录，零拷贝
            type_dirs.setdefault(parts[0], set()).add(str(src.parent))
            rep.yaml_entries += 1
            continue
        try:
            size = src.stat().st_size
        except OSError:
            rep.unresolvable.append(comfy_rel)
            continue
        if size <= copy_limit_bytes:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() and dst.stat().st_size == size:
                continue              # 上次已拷过（幂等）
            shutil.copy2(src, dst)
            rep.copied.append(comfy_rel)
            rep.copy_bytes += size
        else:
            rep.unresolvable.append(comfy_rel)

    if type_dirs:
        ypath.parent.mkdir(parents=True, exist_ok=True)
        # extra_config.py 的类型取值是「换行分隔的路径串」。一律用块标量
        # （|-）逐行写：块标量是完全字面量——双引号字符串会把 Windows
        # 路径里的 \U \t 等当 YAML 转义吃掉（实测坑），单双引号都不安全
        body = ""
        for t in sorted(type_dirs):
            body += (f"  {t}: |-\n"
                     + "\n".join(f"    {d}" for d in sorted(type_dirs[t]))
                     + "\n")
        ypath.write_text(f"{YAML_SECTION}:\n{body}", encoding="utf-8")
        rep.yaml_types = sorted(type_dirs)
    elif ypath.is_file():
        ypath.unlink()                # 无可降级条目：清掉陈旧 yaml 防僵尸路径
    return rep
