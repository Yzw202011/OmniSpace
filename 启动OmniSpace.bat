@echo off
chcp 65001 >nul
title OmniSpace AI - 启动主程序
cd /d "%~dp0"
runtime\py310\python.exe launcher\boot.py %*
if errorlevel 1 (
  echo.
  echo 启动异常退出，详情见上方输出或 logs\ 目录。
  pause
)
