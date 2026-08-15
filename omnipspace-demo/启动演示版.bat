@echo off
chcp 65001 >nul
title OmniSpace AI - 纯前端演示版

echo ========================================
echo   OmniSpace AI 纯前端演示版
echo ========================================
echo.

cd /d "%~dp0"

REM 检查 Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 3.x
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

REM 检查端口是否被占用
netstat -ano | findstr ":8899" >nul
if %errorlevel% equ 0 (
    echo [提示] 端口 8899 已被占用，可能已有服务在运行
)

echo [启动] 正在启动本地 HTTP 服务器 (端口 8899)...
echo [提示] 请勿关闭此窗口，关闭后服务将停止
echo.

REM 启动服务器（后台运行）
start "OmniSpace HTTP Server" /min python -m http.server 8899

REM 等待服务器启动
timeout /t 2 /nobreak >nul

REM 打开浏览器
echo [打开] 正在启动浏览器...
start http://localhost:8899/

echo.
echo ========================================
echo   服务已启动！
echo   访问地址: http://localhost:8899/
echo   如浏览器未自动打开，请手动访问上述地址
echo ========================================
echo.
echo 按任意键关闭此窗口（服务将继续在后台运行）
pause >nul
