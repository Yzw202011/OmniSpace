#!/usr/bin/env python3
"""生成 OmniSpace 桌面快捷方式（免黑窗启动入口，2026-08-31）。

快捷方式以 pythonw.exe 无控制台方式拉起 launcher\boot.py：
- stdout 为 None 的场景 boot.py 已兼容（后端日志经 PIPE 落 logs/，
  启动过程可视化由浏览器启动页承担）
- 退出通道：根目录 停止OmniSpace.bat（或启动页失败态的「退出」按钮）
- 图标：首次运行时用 PIL 现画 launcher/omnispace.ico（多尺寸），
  与 launcher 托盘图标的「圆环+卫星点」母题一致

用法：
  runtime\\py310\\python.exe launcher\\make_shortcut.py
重复运行 = 覆盖重建快捷方式（幂等，图标已存在则不重画）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

LAUNCHER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = LAUNCHER_DIR.parent


def _boot_exe() -> Path:
    """快捷方式目标（2026-09-02 品牌化）：OmniSpace-Boot.exe（logo+进程名
    规范化）优先，副本缺失回退 pythonw.exe——两者同为 pythonw 底，行为一致。"""
    branded = PROJECT_ROOT / 'runtime' / 'py310' / 'OmniSpace-Boot.exe'
    if branded.is_file():
        return branded
    return PROJECT_ROOT / 'runtime' / 'py310' / 'pythonw.exe'


PYTHONW = _boot_exe()
ICON_PATH = LAUNCHER_DIR / 'omnispace.ico'
SHORTCUT_NAME = 'OmniSpace.lnk'


def build_icon(path: Path = ICON_PATH) -> bool:
    """生成应用图标（竖向渐变圆角方 + 白色圆环 + 卫星点）。"""
    if path.exists():
        return True
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print('PIL 未安装，快捷方式将使用 pythonw 默认图标。')
        return False

    size = 256
    top, bottom = (88, 116, 255), (55, 63, 199)
    grad = Image.new('RGBA', (size, size))
    draw = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / (size - 1)
        color = tuple(int(a + (b - a) * t) for a, b in zip(top, bottom, strict=False)) + (255,)
        draw.line([(0, y), (size, y)], fill=color)
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [6, 6, size - 7, size - 7], radius=58, fill=255)
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)

    draw = ImageDraw.Draw(img)
    cx, cy, radius, width = 118, 138, 62, 20
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                 outline=(255, 255, 255, 255), width=width)
    draw.ellipse([cx + radius - 4, cy - radius - 20, cx + radius + 22, cy - radius + 6],
                 fill=(255, 255, 255, 240))
    img.save(path, sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    print(f'图标已生成: {path}')
    return True


def create_shortcut() -> Path:
    """经 PowerShell WScript.Shell COM 创建桌面快捷方式。

    桌面路径用 [Environment]::GetFolderPath 解析（OneDrive/自定义重定向
    场景下 %USERPROFILE%\\Desktop 不可靠）。路径均不含单引号，可直接
    内插进 PS 单引号字符串。
    """
    if not PYTHONW.is_file():
        raise FileNotFoundError(f'未找到启动解释器: {PYTHONW}')
    ps_script = '\n'.join([
        '$ws = New-Object -ComObject WScript.Shell',
        "$desktop = [Environment]::GetFolderPath('Desktop')",
        f"$lnk = $ws.CreateShortcut((Join-Path $desktop '{SHORTCUT_NAME}'))",
        f"$lnk.TargetPath = '{PYTHONW}'",
        r"$lnk.Arguments = 'launcher\boot.py'",
        f"$lnk.WorkingDirectory = '{PROJECT_ROOT}'",
        f"$lnk.IconLocation = '{ICON_PATH},0'",
        "$lnk.Description = 'OmniSpace AI 无窗启动（停止请运行根目录 停止OmniSpace.bat）'",
        '$lnk.Save()',
        'Write-Output $lnk.FullName',
    ])
    result = subprocess.run(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps_script],
        capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode != 0:
        raise RuntimeError(f'快捷方式创建失败: {result.stderr.strip()}')
    return Path(result.stdout.strip())


def main() -> int:
    print('=' * 60)
    print('OmniSpace AI · 桌面快捷方式生成')
    print('=' * 60)
    build_icon()
    lnk = create_shortcut()
    print(f'✓ 快捷方式已就绪: {lnk}')
    print('  双击即无窗启动（无控制台黑窗）；停止请运行根目录 停止OmniSpace.bat')
    return 0


if __name__ == '__main__':
    sys.exit(main())
