#!/usr/bin/env python3
r"""OmniSpace 进程品牌化工具（进程命名规范化 + logo 嵌入，2026-09-02）。

大白话：任务管理器认进程只看 exe 文件名、内嵌图标和版本说明。本项目所有
常驻进程原本都是 python.exe/pythonw.exe 换皮（名字和图标全是 Python 官方的）。
本工具把解释器复制成带名字的副本，再往副本里嵌项目 logo（launcher/
omnispace.ico）与版本信息——从此任务管理器里看到的是「OmniSpace-后端.exe /
OmniSpace 绘画引擎」这一家人，而不是一排蓝色 Python。

七个副本（进程名 = 任务管理器显示名；说明列 = FileDescription）：
无 C 后缀 = pythonw 底（无窗主链）；带 C 后缀 = python 底（bat 控制台入口专用，
保控制台日志回显——GUI 子系统副本会让 bat 黑窗失去后端日志）：
  runtime/py310/OmniSpace-Boot.exe     启动守护进程（boot.py，无窗链）
  runtime/py310/OmniSpace-Backend.exe  后端服务（uvicorn，无窗链）
  runtime/py310/OmniSpace-Shell.exe    桌面窗口（pywebview，S1 桌面壳）
  runtime/py310/OmniSpace-BootC.exe    启动守护进程（bat 控制台入口）
  runtime/py310/OmniSpace-BackendC.exe 后端服务（bat 控制台入口）
  runtime/py313/OmniSpace-LLM.exe      对话引擎（vLLM，python.exe 底）
  tools/ComfyUI_windows_portable/python_embeded/OmniSpace-Engine.exe
                                      绘画引擎（ComfyUI，python_embeded 底）

技术要点（2026-09-02 三运行时实测）：
  - 副本必须与原版同目录：._pth 按 DLL 版本定位（python310.dll 等），
    与 exe 文件名无关——py310 / py313 / python_embeded 三个运行时的
    改名副本均正确解析 sys.path（含 pydeps / site-packages）。
  - 图标写入 RT_GROUP_ICON(14)/RT_ICON(3)，版本信息写入 RT_VERSION(16)
    （同时删除 CPython 自带的 0x0409 英文版本块，防双版本资源歧义）。
  - 运行中的 exe 无法被覆盖（Windows 锁写）：先在临时名上做完资源注入，
    再 os.replace 到位；替换失败（进程在跑）= 跳过该副本不阻断其余。
  - 免编译、免 windres、纯 stdlib（ctypes 调 kernel32/version.dll），
    供 make_dist 出包前调用（ensure_all），也可手动随时重跑（幂等）。

用法：
  runtime\py310\python.exe tools\brand_exe.py             # 全家生成/刷新
  runtime\py310\python.exe tools\brand_exe.py --verify    # 只读校验资源就位
"""
from __future__ import annotations

import argparse
import ctypes
import os
import re
import shutil
import struct
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ICON_PATH = REPO / 'launcher' / 'omnispace.ico'
DEFAULT_VERSION = '1.0.0.0'

# RT_* 资源类型常量（winuser.h）
RT_ICON = 3
RT_GROUP_ICON = 14
RT_VERSION = 16
LANG_ENGLISH_US = 0x0409   # CPython 自带版本块的语言（PC/python_ver_rc.h）
LANG_ZH_CN = 0x0804        # 我们写入的语言（与 Translation 值一致）
CODEPAGE_UNICODE = 0x04B0

# 副本必须落在原解释器同目录（._pth 定位），此处只登记相对路径
# 带 C 后缀 = python.exe 底（控制台子系统，供 bat 调试入口保控制台回显）；
# 无后缀 = pythonw.exe 底（GUI 子系统，无窗主链）
BRAND_MANIFEST: list[dict] = [
    # B1（2026-09-13 拍板项 0=A）：主链五件套源切 py312；py310 旧品牌件
    # 原地保留（打包基线/回退锚点仍走 py310，不重生成）
    {'src': 'runtime/py312/pythonw.exe',
     'dst': 'runtime/py312/OmniSpace-Boot.exe',
     'desc': 'OmniSpace 启动守护进程'},
    {'src': 'runtime/py312/pythonw.exe',
     'dst': 'runtime/py312/OmniSpace-Backend.exe',
     'desc': 'OmniSpace 后端服务'},
    {'src': 'runtime/py312/pythonw.exe',
     'dst': 'runtime/py312/OmniSpace-Shell.exe',
     'desc': 'OmniSpace 桌面窗口'},
    {'src': 'runtime/py312/python.exe',
     'dst': 'runtime/py312/OmniSpace-BootC.exe',
     'desc': 'OmniSpace 启动守护进程（控制台）'},
    {'src': 'runtime/py312/python.exe',
     'dst': 'runtime/py312/OmniSpace-BackendC.exe',
     'desc': 'OmniSpace 后端服务（控制台）'},
    {'src': 'runtime/py313/python.exe',
     'dst': 'runtime/py313/OmniSpace-LLM.exe',
     'desc': 'OmniSpace 对话引擎 (vLLM)'},
    {'src': 'tools/ComfyUI_windows_portable/python_embeded/python.exe',
     'dst': 'tools/ComfyUI_windows_portable/python_embeded/OmniSpace-Engine.exe',
     'desc': 'OmniSpace 绘画引擎 (ComfyUI)'},
]


def _read_app_version() -> str:
    """从 backend/config.yaml 的 app.version 读版本号（避免引 yaml，纯正则）。

    版本是单一真源（make_dist 同源）；解析失败回退 1.0.0.0。
    """
    try:
        text = (REPO / 'backend' / 'config.yaml').read_text(encoding='utf-8')
        m = re.search(r'^\s*version:\s*["\']?(\d+(?:\.\d+)+)', text, re.M)
        if m:
            parts = m.group(1).split('.')
            return '.'.join((parts + ['0'] * 4)[:4])  # 补齐到 4 段（[:4] 在列表上）
    except OSError:
        pass
    return DEFAULT_VERSION


# ── VS_VERSION_INFO 二进制组包（结构对齐是唯一难点） ──────────────

def _u16z(s: str) -> bytes:
    return s.encode('utf-16-le') + b'\x00\x00'


def _res_block(key: str, w_type: int, value: bytes | None,
               children: bytes = b'', value_is_chars: bool = False) -> bytes:
    """一个资源子块：wLength/wValueLength/wType + szKey + 对齐 + 值 + 子块。"""
    vlen = 0 if value is None else (
        len(value) // 2 if value_is_chars else len(value))
    head = bytearray(struct.pack('<HHH', 0, vlen, w_type) + _u16z(key))
    while len(head) % 4:
        head += b'\x00'
    total = bytearray(head + (value or b'') + children)
    while len(total) % 4:
        total += b'\x00'
    struct.pack_into('<H', total, 0, len(total))
    return bytes(total)


def _string_block(key: str, text: str) -> bytes:
    return _res_block(key, 1, _u16z(text), value_is_chars=True)


def build_version_resource(file_version: str, descriptions: dict[str, str]) -> bytes:
    """组 VS_VERSION_INFO 整块（根 wType=0，字符串子块 wType=1）。

    descriptions 至少含 FileDescription；Translation=0804/04B0 与资源写入
    语言一致，PowerShell (Get-Item).VersionInfo 与任务管理器均可读。
    """
    ms = [int(x) for x in file_version.split('.')]
    while len(ms) < 4:
        ms.append(0)
    fixed = struct.pack(
        '<13I',
        0xFEEF04BD,                # dwSignature
        0x00010000,                # dwStrucVersion 1.0
        (ms[0] << 16) | ms[1],     # dwFileVersionMS
        (ms[2] << 16) | ms[3],     # dwFileVersionLS
        (ms[0] << 16) | ms[1],     # dwProductVersionMS
        (ms[2] << 16) | ms[3],     # dwProductVersionLS
        0x3F, 0x0,                 # dwFileFlagsMask / dwFileFlags
        0x40004,                   # dwFileOS = VOS_NT_WINDOWS32
        0x1, 0x0,                  # dwFileType = VFT_APP / subtype
        0x0, 0x0,                  # dwFileDate
    )
    strings = dict(descriptions)
    strings.setdefault('ProductName', 'OmniSpace AI')
    strings.setdefault('CompanyName', 'OmniSpace')
    strings.setdefault('LegalCopyright', '© 2026 OmniSpace')
    strings.setdefault('FileVersion', file_version)
    strings.setdefault('ProductVersion', file_version)
    table = _res_block(f'{LANG_ZH_CN:04x}{CODEPAGE_UNICODE:04x}', 1, None,
                       b''.join(_string_block(k, v) for k, v in strings.items()))
    sfi = _res_block('StringFileInfo', 1, None, table)
    # Translation 值 = 一个 DWORD：低字语言 + 高字代码页（rc 语义
    # "0x0804, 1200"；曾错打包成两个 DWORD，系统拼出 08040000 路径致读回 None）
    var = _res_block('Translation', 0,
                     struct.pack('<I', LANG_ZH_CN | (CODEPAGE_UNICODE << 16)))
    vfi = _res_block('VarFileInfo', 1, None, var)
    return _res_block('VS_VERSION_INFO', 0, fixed, sfi + vfi)


# ── ICO 解析 → RT_ICON/RT_GROUP_ICON 资源 ────────────────────────

def parse_ico(ico_path: Path) -> tuple[bytes, list[bytes]]:
    """ico 文件 → (GRPICONDIR 字节, 各尺寸图像字节列表)。

    组目录把原 entry 的 4 字节 imageOffset 换成 2 字节资源 ID，
    其余字段（宽高/位深/字节数）原样保留。
    """
    data = ico_path.read_bytes()
    _res, _type, count = struct.unpack_from('<HHH', data, 0)
    images: list[bytes] = []
    entries = []
    for i in range(count):
        entry = data[6 + i * 16:6 + (i + 1) * 16]
        img_off, = struct.unpack_from('<I', entry, 12)
        size, = struct.unpack_from('<I', entry, 8)
        images.append(data[img_off:img_off + size])
        entries.append(entry[:12] + struct.pack('<H', i + 1))
    group = struct.pack('<HHH', 0, 1, count) + b''.join(entries)
    return group, images


# ── kernel32 资源写入（BeginUpdate/Update/EndUpdate 三段式） ──────

def _update_resources(path: Path,
                      puts: list[tuple[int, int, int, bytes]],
                      deletes: list[tuple[int, int, int]]) -> None:
    """写入/删除 PE 资源（不动其余资源：CPython 的 manifest 等保留）。

    puts/deletes 元组 = (RT 类型, 资源 ID, 语言, 数据)。
    """
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    k32.BeginUpdateResourceW.argtypes = [ctypes.c_wchar_p, ctypes.c_bool]
    k32.BeginUpdateResourceW.restype = ctypes.c_void_p
    k32.UpdateResourceW.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ushort,
        ctypes.c_void_p, ctypes.c_uint]
    k32.UpdateResourceW.restype = ctypes.c_bool
    k32.EndUpdateResourceW.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    k32.EndUpdateResourceW.restype = ctypes.c_bool

    handle = k32.BeginUpdateResourceW(str(path), False)
    if not handle:
        raise OSError(f'BeginUpdateResource 失败（文件可能在运行中）: {path.name}')
    try:
        # 先删后写：同名(类型,名字,语言)写入本就是覆盖，但清单里可能
        # 含「清旧语言块」——顺序反了会把刚写入的块再删掉（实测翻车点）
        for rt, rid, lang in deletes:
            if not k32.UpdateResourceW(handle, rt, rid, lang, None, 0):
                raise OSError(f'删除资源(RT={rt}, ID={rid}) 失败')
        for rt, rid, lang, blob in puts:
            buf = ctypes.create_string_buffer(blob, len(blob))
            if not k32.UpdateResourceW(handle, rt, rid, lang, buf, len(blob)):
                raise OSError(f'UpdateResource(RT={rt}, ID={rid}) 失败')
    except Exception:
        k32.EndUpdateResourceW(handle, True)  # 丢弃全部改动
        raise
    if not k32.EndUpdateResourceW(handle, False):
        raise OSError(f'EndUpdateResource 失败: {path.name}')


# ── 版本资源读取（自校验用，version.dll 三段式） ──────────────────

def read_file_description(path: Path) -> str | None:
    """读 exe 的 FileDescription（写完自己读回来，不依赖 PowerShell）。"""
    ver = ctypes.windll.version  # type: ignore[attr-defined]
    ver.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
    ver.GetFileVersionInfoSizeW.restype = ctypes.c_uint
    ver.GetFileVersionInfoW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
    ver.VerQueryValueW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint)]

    size = ver.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(str(path), 0, size, buf):
        return None
    node = ctypes.c_void_p()
    plen = ctypes.c_uint()
    # Translation 表 → 拼 StringFileInfo 路径（语言自适应，硬编码 0804 会脆）
    if not ver.VerQueryValueW(buf, '\\VarFileInfo\\Translation',
                              ctypes.byref(node), ctypes.byref(plen)):
        return None
    lang, cp = struct.unpack_from('<HH', ctypes.string_at(node.value, 4))
    key = f'\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription'
    if not ver.VerQueryValueW(buf, key, ctypes.byref(node), ctypes.byref(plen)):
        return None
    return ctypes.wstring_at(node.value)  # 自动读到 null（plen 含结尾符）


def has_group_icon(path: Path) -> bool:
    """exe 是否嵌有图标组资源（自校验用）。"""
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    k32.LoadLibraryExW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint]
    k32.LoadLibraryExW.restype = ctypes.c_void_p
    k32.FindResourceW.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    k32.FindResourceW.restype = ctypes.c_void_p
    k32.FreeLibrary.argtypes = [ctypes.c_void_p]
    k32.FreeLibrary.restype = ctypes.c_bool
    # LOAD_LIBRARY_AS_DATAFILE | LOAD_LIBRARY_AS_IMAGE_RESOURCE = 0x22
    hmod = k32.LoadLibraryExW(str(path), None, 0x22)
    if not hmod:
        return False
    try:
        return bool(k32.FindResourceW(hmod, 1, RT_GROUP_ICON))
    finally:
        k32.FreeLibrary(hmod)


# ── 单副本品牌化 + 全家批处理 ────────────────────────────────────

def brand_one(item: dict, version: str, icon_path: Path) -> str:
    """生成一个品牌副本。返回 'ok' / 'skip-running' / 错误描述。"""
    src = REPO / item['src']
    dst = REPO / item['dst']
    if not src.is_file():
        return f'missing-src({item["src"]})'
    group, images = parse_ico(icon_path)
    ver_res = build_version_resource(version, {
        'FileDescription': item['desc'],
        'InternalName': Path(item['dst']).name,
        'OriginalFilename': Path(item['dst']).name,
    })
    # 先在临时文件上做完资源注入，再原子替换（半成品绝不落到正式名）
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(dst.parent), prefix='~omnibrand_', suffix='.tmp')
    os.close(tmp_fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(src, tmp)
        puts = [(RT_GROUP_ICON, 1, 0, group)]
        puts += [(RT_ICON, i + 1, 0, blob) for i, blob in enumerate(images)]
        puts.append((RT_VERSION, 1, LANG_ZH_CN, ver_res))
        # 只删 CPython 自带英文版本块（副本永远从原始 exe 新拷，必存在）；
        # 0x0804 槽位同名写入即覆盖，无需自删——且删除不存在的资源
        # UpdateResourceW 返回 FALSE，会被当失败误报
        _update_resources(tmp, puts, deletes=[(RT_VERSION, 1, LANG_ENGLISH_US)])
        os.replace(tmp, dst)
        return 'ok'
    except PermissionError:
        tmp.unlink(missing_ok=True)
        return 'skip-running'
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return f'error({exc})'


def ensure_all(force: bool = False) -> dict[str, str]:
    """生成/刷新全家品牌副本（幂等）。force=False 时未变更源不重做。"""
    version = _read_app_version()
    results: dict[str, str] = {}
    for item in BRAND_MANIFEST:
        src = REPO / item['src']
        dst = REPO / item['dst']
        if (not force and dst.is_file()
                and dst.stat().st_mtime >= src.stat().st_mtime
                and read_file_description(dst) == item['desc']
                and has_group_icon(dst)):
            results[item['dst']] = 'fresh'
            continue
        results[item['dst']] = brand_one(item, version, ICON_PATH)
    return results


def verify_all() -> int:
    """只读校验：每个副本存在 + 说明列 + 图标组。退出码 = 问题数。"""
    problems = 0
    for item in BRAND_MANIFEST:
        dst = REPO / item['dst']
        if not dst.is_file():
            print(f'  ✗ {item["dst"]}  缺失')
            problems += 1
            continue
        desc = read_file_description(dst)
        icon_ok = has_group_icon(dst)
        if desc == item['desc'] and icon_ok:
            print(f'  ✓ {item["dst"]}  「{desc}」图标✓')
        else:
            print(f'  ✗ {item["dst"]}  说明={desc!r} 图标={"✓" if icon_ok else "✗"}')
            problems += 1
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description='OmniSpace 进程品牌化工具')
    ap.add_argument('--verify', action='store_true', help='只校验不生成')
    ap.add_argument('--force', action='store_true', help='无视新鲜度强制重建')
    args = ap.parse_args()

    if args.verify:
        print('=' * 60)
        print('OmniSpace 品牌副本校验')
        print('=' * 60)
        n = verify_all()
        print('✅ 全部就位' if n == 0 else f'❌ {n} 个问题（先跑一次生成）')
        return 0 if n == 0 else 1

    print('=' * 60)
    print(f'OmniSpace 进程品牌化（版本 {_read_app_version()}，图标 {ICON_PATH.name}）')
    print('=' * 60)
    results = ensure_all(force=args.force)
    bad = 0
    for path, status in results.items():
        mark = '✓' if status in ('ok', 'fresh') else '⚠'
        if status not in ('ok', 'fresh'):
            bad += 1
        label = {'ok': '已生成', 'fresh': '已是最新（跳过）',
                 'skip-running': '进程运行中，本次跳过'}.get(status, status)
        print(f'  {mark} {path}  {label}')
    if bad:
        print(f'⚠ {bad} 个副本未就位（运行中可稍后重跑；其余不受影响）')
    return verify_all()


if __name__ == '__main__':
    sys.exit(main())
