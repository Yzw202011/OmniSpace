@echo off
chcp 65001 >nul
title OmniSpace AI - 停止
cd /d "%~dp0"
runtime\py310\python.exe launcher\stop.py %*
echo.
pause
