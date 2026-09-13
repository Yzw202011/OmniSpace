@echo off
chcp 65001 >nul
title OmniSpace AI - 启动主程序
cd /d "%~dp0"
rem B1（2026-09-13 拍板项 0=A）：主链升 py312；品牌化 BootC 已在 py312 重打
if exist "runtime\py312\OmniSpace-BootC.exe" (
  runtime\py312\OmniSpace-BootC.exe launcher\boot.py %*
) else (
  runtime\py312\python.exe launcher\boot.py %*
)
if errorlevel 1 (
  echo.
  echo 启动异常退出，详情见上方输出或 logs\ 目录。
  pause
)
