# Latency verification for browser API throttle-queue fix (ASCII only)
$base = 'http://127.0.0.1:8765/api/v1'

function Measure-Api($name, $method, $path, $body) {
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  try {
    if ($method -eq 'GET') {
      Invoke-RestMethod -Uri "$base$path" -Method Get -TimeoutSec 60 | Out-Null
    } else {
      Invoke-RestMethod -Uri "$base$path" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 60 | Out-Null
    }
    $status = 'OK'
  } catch {
    $status = 'ERR'
  }
  $sw.Stop()
  "{0,-38} {1,8:F1} ms  {2}" -f $name, $sw.Elapsed.TotalMilliseconds, $status
}

Write-Output '=== 1. setup: navigate to create a page ==='
Measure-Api 'navigate baidu.com' 'POST' '/browser/navigate' '{"url":"https://www.baidu.com"}'

Write-Output ''
Write-Output '=== 2. screenshot cache behavior ==='
Measure-Api 'screenshot #1 (real capture)' 'GET' '/browser/screenshot'
Measure-Api 'screenshot #2 (expect cache hit)' 'GET' '/browser/screenshot'
Start-Sleep -Seconds 3
Measure-Api 'screenshot #3 (cache expired)' 'GET' '/browser/screenshot'
Measure-Api 'screenshot #4 (expect cache hit)' 'GET' '/browser/screenshot'

Write-Output ''
Write-Output '=== 3. other endpoints ==='
Measure-Api 'status' 'GET' '/browser/status'
Measure-Api 'takeover (button)' 'POST' '/browser/takeover' '{}'
Measure-Api 'handback (button)' 'POST' '/browser/handback' '{}'

Write-Output ''
Write-Output '=== 4. button latency under 2s polling (simulates frontend) ==='
$job = Start-Job -ScriptBlock {
  $b = 'http://127.0.0.1:8765/api/v1'
  foreach ($i in 1..6) {
    try { Invoke-RestMethod -Uri "$b/browser/screenshot" -Method Get -TimeoutSec 30 | Out-Null } catch {}
    Start-Sleep -Seconds 2
  }
}
Start-Sleep -Seconds 2
Measure-Api 'takeover under polling' 'POST' '/browser/takeover' '{}'
Measure-Api 'navigate under polling' 'POST' '/browser/navigate' '{"url":"https://example.com"}'
Measure-Api 'handback under polling' 'POST' '/browser/handback' '{}'
Measure-Api 'screenshot under polling' 'GET' '/browser/screenshot'
Wait-Job $job -Timeout 30 | Out-Null
Stop-Job $job -ErrorAction SilentlyContinue
Remove-Job $job -Force
Write-Output '=== done ==='
