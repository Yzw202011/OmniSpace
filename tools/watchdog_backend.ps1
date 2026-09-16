# OmniSpace 后端看门狗（2026-08-30）
# 用途：针对"后端静默死亡"悬案（RAM 高压场景偶发,无 traceback）的兜底——
# 每 30 秒探测 5800 端口,失联即自动重启后端并记录时间线。
# 启动方式（可选,不影响正常使用）:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\watchdog_backend.ps1
# 停止:关闭该 PowerShell 窗口即可(不会影响正在运行的后端)。

$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root 'runtime\py310\python.exe'
$log = Join-Path $root 'logs\watchdog.log'

function Log($msg) {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg" | Add-Content -Path $log -Encoding UTF8
}

Log '看门狗启动(每 30s 探测 127.0.0.1:5800)'
while ($true) {
    $resp = try {
        Invoke-WebRequest -Uri 'http://127.0.0.1:5800/api/v1/manga/media/x' `
            -TimeoutSec 6 -UseBasicParsing
    } catch { $null }
    $alive = ($resp -ne $null) -or (
        (Test-NetConnection -ComputerName 127.0.0.1 -Port 5800 -InformationLevel Quiet -WarningAction SilentlyContinue))

    if (-not $alive) {
        Log '探测失败:后端失联,执行自动重启'
        Start-Process -FilePath $py -ArgumentList '-m', 'src.main' `
            -WorkingDirectory $root -WindowStyle Hidden
        Start-Sleep -Seconds 12
        $check = try { Invoke-WebRequest -Uri 'http://127.0.0.1:5800/' -TimeoutSec 6 -UseBasicParsing } catch { $null }
        if ($check -ne $null) { Log '重启成功,后端已就绪' }
        else { Log '重启后仍未就绪(可能仍在启动,下轮继续探测)' }
    }
    Start-Sleep -Seconds 30
}
