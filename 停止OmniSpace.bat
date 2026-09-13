@echo off
chcp 65001 >nul
title OmniSpace AI - 停止
cd /d "%~dp0"
rem B1：主链 py312（stop.py 逻辑与解释器版本无关，但统一新链）
runtime\py312\python.exe launcher\stop.py %*
echo.
pause
