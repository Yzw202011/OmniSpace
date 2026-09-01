$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
function Ask-VL($imgPath, $question) {
  $img = [Convert]::ToBase64String([IO.File]::ReadAllBytes($imgPath))
  $payload = @{ message = $question; images = @($img); stream = $false } | ConvertTo-Json -Compress
  $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
  $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/v1/dialog/send' -Method Post -Body $bytes -ContentType 'application/json; charset=utf-8' -TimeoutSec 120
  return $r.data.reply
}
$f1 = Ask-VL 'logs\_frames\six\frame_01.jpg' '用一句话客观描述这张图的画面内容和人物动作'
Write-Host "F1(首帧): $f1"
$f4 = Ask-VL 'logs\_frames\six\frame_04.jpg' '用一句话客观描述这张图的画面内容和人物动作，并说明人物姿态与站立静止是否有区别'
Write-Host "F4(后段): $f4"
$f5 = Ask-VL 'logs\_frames\six\frame_05.jpg' '用一句话客观描述这张图的画面内容和人物动作'
Write-Host "F5(尾帧): $f5"
