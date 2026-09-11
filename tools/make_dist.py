#!/usr/bin/env python3
r"""OmniSpace 发行打包器（P1）：白名单出包 + 哈希清单 + 包内自检。

大白话：只往箱子里装清单上的东西，装完逐件登记造册（dist_manifest.json，
将来自动更新的地基），最后开箱验货（盘符/黑名单/必备件三查），零告警才算出厂。

用法：
  runtime/py310/python.exe tools/make_dist.py                 # 出包到 D:\ccd/
  runtime/py310/python.exe tools/make_dist.py --check-only <包目录>
                                                              # 只跑包内自检
版本单一真源：backend/config.yaml 的 app.version；构建号 = 版本+git短哈希+日期，
写入包内 backend/build_info.py（开发环境 config.BUILD_ID 回退 "+dev"）。

09-02 规则：发行产物不进开发目录——默认输出 D:\ccd（开发目录出 dist_out
会被 scripts/dir_audit.py 审计报红；用户本机以 --out 覆盖不受限）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# ── 白名单（不在清单上的一律不带）──────────────────────────────
# (源目录, 包内目录, 过滤函数或None)。过滤函数接收相对路径 parts，True=排除
DEBRIS_RE = re.compile(r"^(_|tmp_)[^_].+")  # 开发残渣文件名；[^_] 排除 __init__.py（P3 实测曾误吞致包内无 __init__，靠命名空间包蒙混）
PYCACHE = {"__pycache__", ".pytest_cache"}


def _backend_filter(parts: tuple[str, ...]) -> bool:
    if PYCACHE & set(parts[:-1]):
        return True
    if parts[0] == "tests" or "tests" in parts[:-1]:
        return True                                   # 单测不随发行
    name = parts[-1]
    if name.endswith((".db", ".db-wal", ".db-shm", ".log", ".txt")):
        return True                                   # 占位库/日志/残渣文本
    if DEBRIS_RE.match(name):
        return True
    return False


def _plain_filter(parts: tuple[str, ...]) -> bool:
    return bool(PYCACHE & set(parts[:-1]))


def _pydeps_filter(parts: tuple[str, ...]) -> bool:
    if PYCACHE & set(parts[:-1]):
        return False                                  # pydeps 的 pycache 保留（启动提速）
    # 09-02 依赖闭包裁定：chromadb 的分布式/云侧依赖单机模式永不加载
    # （拦截导入逐项实测通过），modelscope 仅开发脚本用——发行剔除 ~110M，
    # 开发目录保留不动；opentelemetry 为 chromadb 硬依赖（实测），不可剔除
    top = parts[0].split("-")[0].replace("_", "-")
    if top in {"kubernetes", "oauthlib", "requests-oauthlib", "durationpy",
               "modelscope", "modelscope-hub", "grpcio-status"}:
        return True
    return parts[-1].endswith(".whl")                 # 剔除夹带的安装轮子


def _comfy_filter(parts: tuple[str, ...]) -> bool:
    """ComfyUI 便携包：只带引擎代码，剔权重/产物/用户区。

    - models：权重剔除（用户侧由挂接器按 comfy_model_map 重建硬链），
      但保留引擎自带小件 vae_approx/configs/embeddings（预览/花样等，
      ~30M，随包走免得用户机缺件）
    - output/input/temp/user：产物与用户区（user 含 V8 明文工作流=核心
      IP，后端链路用的是资产金库加密模板，不依赖此目录）
    - custom_nodes：保留（MiniMaxH3 上下文循环等后端链路依赖）
    """
    _DROP = {"output", "input", "temp", "user", "__pycache__",
             "update", ".git"}
    _KEEP_MODELS = {"vae_approx", "configs", "embeddings"}
    if "models" in parts[:-1]:
        i = parts.index("models")
        nxt = parts[i + 1] if i + 1 < len(parts) - 1 else ""
        if nxt not in _KEEP_MODELS:
            return True
    if _DROP & set(parts[:-1]):
        return True
    return parts[-1].endswith((".log", ".lock", ".db", ".whl", ".zip"))


COPY_DIRS: list[tuple[str, str, object]] = [
    ("backend", "backend", _backend_filter),
    ("launcher", "launcher", _plain_filter),
    # 升级小医生（升级机制批3 2026-09-11）：独立进程换文件，随包出厂
    ("updater", "updater", _plain_filter),
    ("skills", "skills", _plain_filter),
    # ComfyUI 模型挂接工具：仓库真源在 scripts/comfy_link/（目录规范
    # §2.3），包内仍落 modelxiazai/——交付物与说明书路径不变
    ("scripts/comfy_link", "modelxiazai", _plain_filter),
    ("pydeps", "pydeps", _pydeps_filter),
    ("frontend/dist", "frontend/dist", None),
    ("runtime/py310", "runtime/py310", _plain_filter),
    ("runtime/py313", "runtime/py313", _plain_filter),  # vLLM 对话引擎运行时
    ("tools/ComfyUI_windows_portable", "tools/ComfyUI_windows_portable",
     _comfy_filter),  # 绘画/漫剧/视频管线引擎（代码+引擎小件）
    ("runtime/ffmpeg", "runtime/ffmpeg", None),
    # 内置辅助件（2026-09-02 包政策：≤50G、大模型外置、其余内置；
    # 法律考量经用户裁定豁免）——四个目录合计 ~4.3G
    ("models/sam-vit-h", "models/sam-vit-h", _plain_filter),
    ("models/embed", "models/embed", _plain_filter),
    ("models/face", "models/face", _plain_filter),
    ("models/whisper-tiny", "models/whisper-tiny", _plain_filter),
]
COPY_FILES = [
    ("models/models_manifest.json", "models/models_manifest.json"),
    # 09-02 改名：仓库根的开发版 exe 带「开发版」后缀防点错（用户曾把开发
    # 环境当发行包测）；包内仍叫 启动OmniSpace.exe，交付物名不变
    ("启动OmniSpace-开发版.exe", "启动OmniSpace.exe"),
    ("启动OmniSpace.bat", "启动OmniSpace.bat"),
    ("停止OmniSpace.bat", "停止OmniSpace.bat"),
    ("requirements-lock.txt", "requirements-lock.txt"),
    ("README.md", "README.md"),
]

# ── 包内自检规则 ────────────────────────────────────────────────
DRIVE_RE = re.compile(r"(?i)\b[a-z]:[\\/]")
SYSTEM_PREFIXES = ("c:\\windows", "c:\\program files", "c:\\ffmpeg")
MARKER = "portable-ok"
SCAN_EXT = {".py", ".bat", ".ps1", ".cmd", ".yaml", ".yml", ".toml",
            ".cfg", ".ini", ".pth", ".ts", ".tsx", ".js", ".css", ".html", ".json"}
# 只扫自有代码区：第三方树（runtime/pydeps）的文档示例、压缩 JS 的
# 三元/正则语法（`a:/b/`）都会造成盘符误伤；它们的真源在出包前已由
# portability_check.py 把过关
SCAN_ZONES = ["backend", "launcher", "skills", "scripts/comfy_link",
              "frontend/dist/index.html", "models/models_manifest.json"]

# 黑名单：包内出现任何一条即 FAIL（路径小写匹配，支持 * 通配）
BLACKLIST_RES = [
    re.compile(p) for p in (
        r"^keys[/\\].*\.pem$", r"[/\\]dbkey\.bin$", r"[/\\]omnispace\.db",
        r"[/\\]omnispace\.db-(wal|shm)$", r"art_styles_backup.*\.json$",
        r"art_styles_removed.*\.json$", r"^(_|tmp_)[^/\\]+\.(py|txt)$",
        r"[/\\](_tmp_|tmp_)[^/\\]*\.py$", r"\.log$", r"\.whl$",
        r"upgrade_signing\.key$",
        r"e2e_state\.json$", r"^\.mimosa", r"[/\\]\.mimosa", r"debug_archive",
        r"[/\\]backups?[/\\]", r"^\.git",
        r"\.(png|jpg|jpeg)\.jpg$",                                  # 截图双后缀残渣
        # 注：__pycache__ 不进黑名单——pydeps 的预编译缓存刻意随包（启动提速），
        # 自有代码区的 pycache 由拷贝过滤器排除
    )
]

ESSENTIALS = [
    "启动OmniSpace.exe", "启动OmniSpace.bat", "launcher/boot.py", "launcher/splash.html",
    "updater/updater.py",
    "launcher/omnispace.ico", "backend/main.py", "backend/config.yaml",
    "backend/build_info.py", "frontend/dist/index.html",
    "pydeps/fastapi/__init__.py", "pydeps/uvicorn/__init__.py",
    "runtime/py310/python.exe", "runtime/py310/python310._pth",
    # 进程品牌化副本（2026-09-02，tools/brand_exe.py 生成；boot 拉起链
    # 优先取用，缺失会静默回退裸 python——哨兵防包内悄悄退化。
    # C 后缀=控制台变体，供 bat 入口）
    "runtime/py310/OmniSpace-Boot.exe",
    "runtime/py310/OmniSpace-Backend.exe",
    "runtime/py310/OmniSpace-Shell.exe",
    "runtime/py310/OmniSpace-BootC.exe",
    "runtime/py310/OmniSpace-BackendC.exe",
    "runtime/py313/OmniSpace-LLM.exe",
    "tools/ComfyUI_windows_portable/python_embeded/OmniSpace-Engine.exe",
    # MinGW 运行库哨兵（2026-09-01 测试机事故）：Cython .pyd 动态链接
    # winpthread；缺它则异机启动即崩（本机有 WinLibs PATH 掩盖过问题）
    "runtime/py310/libwinpthread-1.dll",
    "runtime/py310/libgcc_s_seh-1.dll",
    # VC++ 运行库哨兵（2026-09-01 测试机事故二）：torch/c10 等依赖
    # msvcp140/vcomp140；干净机器无 VC Redist 时后端 import torch 即崩
    # （本机 System32 有掩盖过问题）。py310/py313 两个运行时都要带
    "runtime/py310/msvcp140.dll",
    "runtime/py310/vcomp140.dll",
    "runtime/py313/msvcp140.dll",
    # 引擎件哨兵（P8 全功能包）：vLLM 对话运行时 + ComfyUI 管线引擎
    "runtime/py313/python.exe",
    "tools/ComfyUI_windows_portable/python_embeded/python.exe",
    "tools/ComfyUI_windows_portable/ComfyUI/main.py",
    "models/models_manifest.json", "dist_manifest.json",
    # 即插即用挂接两件套（2026-09-02 挂接器产品化）：包内目录名是
    # modelxiazai/（COPY_DIRS 由 scripts/comfy_link 映射而来）——
    # 缺映射表则挂接器空转，缺 comfy_mount 则 boot/comfy_proc 接线
    # 全部静默跳过（绘画引擎找不到模型），哨兵防悄悄丢件
    "modelxiazai/comfy_mount.py", "modelxiazai/comfy_model_map.json",
]


def read_version() -> str:
    import yaml
    cfg = yaml.safe_load((REPO / "backend" / "config.yaml").read_text("utf-8"))
    return str(cfg["app"]["version"])


def git_hash() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=REPO, capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "nogit"
    except Exception:
        return "nogit"


def _lp(path: Path) -> str:
    """Windows 长路径前缀（ComfyUI 的 torch 许可目录 >260 字符，
    WinError 206；\\?\\ 前缀自包含解决，不动系统注册表）。"""
    s = str(path.resolve())
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + s
    return s


def copy_all(dest: Path) -> tuple[int, int]:
    n, total = 0, 0
    for src_rel, dst_rel, filt in COPY_DIRS:
        src = REPO / src_rel
        for p in src.rglob("*"):
            if not p.is_file():
                continue
            rel_parts = p.relative_to(src).parts
            if filt and filt(rel_parts):
                continue
            target = dest / dst_rel / Path(*rel_parts)
            os.makedirs(os.path.dirname(_lp(target)), exist_ok=True)
            shutil.copy2(str(p), _lp(target))
            n += 1
            total += p.stat().st_size
    for src_rel, dst_rel in COPY_FILES:
        src = REPO / src_rel
        if not src.exists():
            print(f"⚠️ 白名单文件缺失：{src_rel}")
            continue
        target = dest / dst_rel
        os.makedirs(os.path.dirname(_lp(target)), exist_ok=True)
        shutil.copy2(str(src), _lp(target))
        n += 1
        total += src.stat().st_size
    return n, total


def write_build_info(dest: Path, build_id: str) -> None:
    (dest / "backend" / "build_info.py").write_text(
        '"""由 make_dist.py 生成的构建号文件（勿手改、勿提交）。"""\n'
        f'BUILD_ID = "{build_id}"\n', encoding="utf-8")


def write_manifest(dest: Path, build_id: str) -> int:
    files = []
    root_lp = _lp(dest)
    for dirpath, _dirs, names in os.walk(root_lp):
        for name in names:
            full = os.path.join(dirpath, name)
            if name == "dist_manifest.json":
                continue
            h = hashlib.sha256()
            with open(full, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            files.append({
                "path": os.path.relpath(full, root_lp).replace("\\", "/"),
                "size": os.stat(full).st_size, "sha256": h.hexdigest()})
    manifest = {"build_id": build_id, "edition": "release",
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "generator": "tools/make_dist.py", "files": files}
    (dest / "dist_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(files)


def self_check(dest: Path) -> list[str]:
    alerts: list[str] = []
    scanned = 0
    zone_files: set[Path] = set()

    def _blacklist(rel: str, alerts: list[str]) -> None:
        low = rel.lower()
        for rx in BLACKLIST_RES:
            if rx.search(low):
                alerts.append(f"[黑名单] {rel}  ← {rx.pattern}")

    for zone in SCAN_ZONES:
        zpath = dest / zone
        entries = zpath.rglob("*") if zpath.is_dir() else (
            [zpath] if zpath.exists() else [])
        for p in entries:
            if not p.is_file():
                continue
            zone_files.add(p)
            rel = p.relative_to(dest).as_posix()
            _blacklist(rel, alerts)
            if p.suffix.lower() in SCAN_EXT:
                scanned += 1
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if DRIVE_RE.search(line) and MARKER not in line:
                        ll = line.lower()
                        if not any(pf in ll for pf in SYSTEM_PREFIXES):
                            alerts.append(
                                f"[盘符] {rel}:{i}: {line.strip()[:120]}")
    # 黑名单仍按全包检查（第三方树也不许夹带敏感物），已查过的跳过；
    # 长路径树（ComfyUI）用 os.walk+前缀遍历
    root_lp = _lp(dest)
    for dirpath, _dirs, names in os.walk(root_lp):
        for name in names:
            rel = os.path.relpath(os.path.join(dirpath, name),
                                  root_lp).replace("\\", "/")
            _blacklist(rel, alerts)
    for ess in ESSENTIALS:
        if not (dest / ess).exists():
            alerts.append(f"[必备件缺失] {ess}")
    print(f"自检扫描：{scanned} 个文本文件（自有区）+ 全包黑名单/必备件")
    return alerts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="D:/ccd", help="输出根目录（默认 D:/ccd，"
                    "发行产物不进开发目录；dist_out 会被目录审计报红）")
    ap.add_argument("--pubkey", default="",
                    help="发行公钥 hex（来自授权管理台；注入后激活门禁生效）")
    ap.add_argument("--check-only", metavar="包目录",
                    help="只对已有包目录跑自检，不打包")
    args = ap.parse_args()

    if args.check_only:
        alerts = self_check(Path(args.check_only))
        _report(alerts)
        return 1 if alerts else 0

    version = read_version()
    gh = git_hash()
    build_id = f"{version}+g{gh}+{time.strftime('%Y%m%d')}"
    dest = Path(args.out) / f"OmniSpace-{version}-g{gh}"
    if dest.exists():
        shutil.rmtree(_lp(dest))  # 长路径安全清场（上次半成品可能含超长目录）
    dest.mkdir(parents=True)
    print(f"构建号：{build_id}\n输出目录：{dest}")

    # 进程品牌化刷新（2026-09-02）：出包前强制重建五个品牌副本（logo+版本
    # 说明随 config.yaml 版本号走）；运行中的副本无法覆盖时跳过——此时包内
    # 会带上旧副本，属可容忍降级但必须吆喝出来
    sys.path.insert(0, str(REPO / "tools"))
    import brand_exe
    brand_results = brand_exe.ensure_all(force=True)
    stale = {p: s for p, s in brand_results.items() if s not in ("ok", "fresh")}
    for p, s in stale.items():
        print(f"⚠️ 品牌副本未刷新（{s}）：{p}")
    if stale:
        print("⚠️ 包内将带旧品牌副本，建议停机后重新出包")

    t0 = time.time()
    n, total = copy_all(dest)
    write_build_info(dest, build_id)
    if args.pubkey:
        gate = dest / "backend" / "license_gate.py"
        src_txt = gate.read_text(encoding="utf-8")
        patched = src_txt.replace('PUBKEY_HEX = ""',
                                  f'PUBKEY_HEX = "{args.pubkey}"')
        if patched == src_txt:
            raise SystemExit("公钥注入失败：license_gate.py 中找不到占位符")
        gate.write_text(patched, encoding="utf-8")
        print(f"已注入发行公钥（激活门禁生效）：{args.pubkey[:16]}…")
        # 资产加密（P6 锁4）：工作流明文出包即灭；风格种子入金库
        from backend.asset_vault import encrypt_bytes
        enc_dir = dest / "backend" / "assets_enc"
        enc_dir.mkdir(parents=True, exist_ok=True)
        wf = dest / "backend" / "services" / "inference" / "h3_chain_workflow_api.json"
        if wf.is_file():
            (enc_dir / "h3_chain_workflow_api.json.enc").write_bytes(
                encrypt_bytes(wf.read_bytes(), args.pubkey))
            wf.unlink()
            print("已加密：h3_chain 工作流模板（包内明文已移除）")
        seed = REPO / "data" / "art_styles_seed_final.json"
        if seed.is_file():
            (enc_dir / "art_styles_seed.json.enc").write_bytes(
                encrypt_bytes(seed.read_bytes(), args.pubkey))
            print("已加密：风格库种子（1117 条）")
    print(f"复制 {n} 个文件 / {total / (1 << 30):.2f} GiB / {time.time() - t0:.0f}s")

    t1 = time.time()
    m = write_manifest(dest, build_id)
    print(f"哈希清单 {m} 条 / {time.time() - t1:.0f}s → dist_manifest.json")

    alerts = self_check(dest)
    _report(alerts)
    return 1 if alerts else 0


def _report(alerts: list[str]) -> None:
    if alerts:
        print(f"\n❌ 自检发现 {len(alerts)} 个问题（前 30 条）：")
        for a in alerts[:30]:
            print(f"  {a}")
        return
    print("\n✅ 自检零告警，包可出厂（点火验收：ALLOW_MULTI=1 + --port 580x --no-browser）")


if __name__ == "__main__":
    sys.exit(main())
