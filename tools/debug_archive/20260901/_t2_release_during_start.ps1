# T2 retest: release(paint) during vLLM cold start must return within budget
# and the startup loop must self-terminate the vLLM subprocess.
$ErrorActionPreference = 'Stop'
$base = 'http://127.0.0.1:8765/api/v1'

function VllmProcCount {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -like '*runtime\py313\python.exe' }).Count
}

# 0. cleanup: make sure no vLLM is running before the test
if ((VllmProcCount) -gt 0) {
    $null = Invoke-RestMethod -Method Post -Uri "$base/models/release-for-module" `
        -ContentType 'application/json' -TimeoutSec 20 `
        -Body '{"module":"paint","timeout_ms":3000}'
    Start-Sleep -Seconds 6
}
Write-Host ("[0] cleanup done, py313 count=" + (VllmProcCount) + " (expect 0)")

# 1. trigger dialog warmup -> vLLM cold start (~157s window)
$warm = Invoke-RestMethod -Method Post -Uri "$base/models/warmup" `
    -ContentType 'application/json' -TimeoutSec 10 `
    -Body '{"feature":"dialog","model_id":"qwen3-vl-8b-awq"}'
Write-Host ("[1] warmup: started=" + $warm.data.started + " reason=" + $warm.data.reason)

# 2. give start() time to spawn vllm and enter health-poll loop
Start-Sleep -Seconds 15
Write-Host ("[2] py313 processes while starting: " + (VllmProcCount) + " (expect 2)")

# 3. release for paint (must return within ~3s budget)
$t0 = Get-Date
try {
    $rel = Invoke-RestMethod -Method Post -Uri "$base/models/release-for-module" `
        -ContentType 'application/json' -TimeoutSec 15 `
        -Body '{"module":"paint","timeout_ms":3000}'
    $dt = ((Get-Date) - $t0).TotalSeconds
    Write-Host ("[3] release returned in {0:N2}s success={1} completed={2}" -f $dt, $rel.success, $rel.data.completed)
} catch {
    $dt = ((Get-Date) - $t0).TotalSeconds
    Write-Host ("[3] FAILED: release threw after {0:N2}s: {1}" -f $dt, $_.Exception.Message)
}

# 4. wait for the startup loop to self-terminate, then count py313
Start-Sleep -Seconds 8
Write-Host ("[4] py313 processes after release: " + (VllmProcCount) + "  (expect 0)")

# 5. final vllm status
try {
    $st2 = Invoke-RestMethod -Uri "$base/models/vllm/status" -TimeoutSec 5
    Write-Host ("[5] vllm final: running=" + $st2.data.running + " healthy=" + $st2.data.healthy)
} catch { Write-Host "[5] status failed: $_" }
