[Console]::OutputEncoding = [Text.Encoding]::UTF8
$mp4 = 'E:\OmniSpace\data\videos\2b6b81a1-604f-45be-9299-64f66c44200f.mp4'
$ff = 'E:\OmniSpace\runtime\ffmpeg\bin\ffmpeg.exe'
$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Force E:\OmniSpace\logs\_frames5 | Out-Null
Remove-Item E:\OmniSpace\logs\_frames5\f*.jpg -ErrorAction SilentlyContinue
& $ff -y -i $mp4 -vf "select='eq(n\,0)+eq(n\,40)+eq(n\,80)'" -vsync vfr E:\OmniSpace\logs\_frames5\f%d.jpg 2>&1 | Out-Null
$ErrorActionPreference = 'Stop'
$frames = Get-ChildItem E:\OmniSpace\logs\_frames5\f*.jpg | Sort-Object Name
Write-Host "frames: $($frames.Count)"
foreach ($f in $frames) {
  $b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($f.FullName))
  $payload = @{ message = 'Describe this video frame objectively in Chinese: what is the person doing (action), where is the scene (pool/villa/other), and what does the person look like (clothing/appearance)?'; images = @($b64); stream = $false } | ConvertTo-Json -Compress
  $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
  $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/v1/dialog/send' -Method Post -Body $bytes -ContentType 'application/json; charset=utf-8' -TimeoutSec 120
  $txt = $r.data.message
  if ($txt -is [string] -and $txt) { } elseif ($txt.content) { $txt = $txt.content } else { $txt = ($r.data | ConvertTo-Json -Depth 3 -Compress).Substring(0, 200) }
  Write-Host "[$($f.Name)] $txt"
}
