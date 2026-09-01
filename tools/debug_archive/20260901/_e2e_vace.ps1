$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$img = [Convert]::ToBase64String([IO.File]::ReadAllBytes('e:\OmniSpace\logs\_frames\user_input.png'))
$payload = @{
  storyboard_row_id = 'e2e-vace-retest'
  description       = '在别墅泳池旁边，跳舞'
  screenshot_4in1   = $img
  resolution        = '720p'
  fps               = 16
  duration_seconds  = 5
} | ConvertTo-Json -Compress
$bytes = [Text.Encoding]::UTF8.GetBytes($payload)
$r = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/v1/video/generate' -Method Post -Body $bytes -ContentType 'application/json; charset=utf-8' -TimeoutSec 60
$taskId = $r.data.task_id
Write-Host "task: $taskId"
$start = Get-Date
while ($true) {
  Start-Sleep 15
  try { $s = Invoke-RestMethod "http://127.0.0.1:8765/api/v1/video/$taskId/status" -TimeoutSec 10 } catch { Write-Host "poll err: $_"; continue }
  $d = $s.data
  $eta = if ($d.eta_seconds) { " eta=$([int]$d.eta_seconds)s" } else { '' }
  Write-Host ("[{0:mm:ss}] {1} p={2:P0}$eta model={3}" -f (Get-Date), $d.status, $d.progress, $d.model_used)
  if ($d.status -in 'done','failed','cancelled') { break }
  if (((Get-Date) - $start).TotalMinutes -gt 40) { Write-Host 'TIMEOUT 40min'; break }
}
$final = Invoke-RestMethod "http://127.0.0.1:8765/api/v1/video/$taskId/status" -TimeoutSec 10
Write-Host ("FINAL: {0} model={1} file={2} err={3}" -f $final.data.status, $final.data.model_used, $final.data.file_path, $final.data.error)
