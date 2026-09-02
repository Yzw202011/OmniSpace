@echo off
chcp 65001 >nul
title OmniSpace AI - 启动主程序
cd /d "%~dp0"
rem 品牌化控制台入口（2026-09-02）：BootC=python 底，任务管理器带 logo，
rem 且保住本黑窗的后端日志回显；副本缺失回退裸 python
if exist "runtime\py310\OmniSpace-BootC.exe" (
  runtime\py310\OmniSpace-BootC.exe launcher\boot.py %*
) else (
  runtime\py310\python.exe launcher\boot.py %*
)
if errorlevel 1 (
  echo.
  echo 启动异常退出，详情见上方输出或 logs\ 目录。
  pause
)
