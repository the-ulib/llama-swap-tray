# Integration test - INVASIVE: performs a REAL installation on this machine
# (scheduled tasks, firewall rule, downloads). Meant for a DISPOSABLE system:
# Windows Sandbox (see tests\sandbox.wsb) or a throwaway VM.
#
# Uses the cpu backend (VMs have no GPU passthrough) and a ~1 MB test model,
# so it exercises the full pipeline: downloads, task registration, permissions,
# tray build, live inference, stop/start, and the update script's version check.
#
#   powershell -ExecutionPolicy Bypass -File tests\integration.ps1 [-Force]
param([switch]$Force)
$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$Api = "http://localhost:9292"
$script:fail = 0

function Assert([string]$name, $cond) {
    if ($cond) { Write-Host "[PASS] $name" }
    else { Write-Host "[FAIL] $name" -ForegroundColor Red; $script:fail++ }
}

# --- safety guards: never wreck a real installation by accident ---------------
cmd /c "schtasks /Query /TN llama-swap\server >nul 2>&1"
if ($LASTEXITCODE -eq 0 -and -not $Force) {
    throw "A 'llama-swap\server' task already exists on this machine - this looks like a REAL installation. Run this test in a disposable VM/sandbox, or pass -Force if you know what you are doing."
}
if ((Test-Path "$Root\config.yaml") -and -not $Force) {
    throw "config.yaml already exists - refusing to overwrite it. Pass -Force on a disposable system."
}

# --- arrange: cpu backend, tiny model, minimal config --------------------------
$env:LLAMASWAP_BACKEND = "cpu"

$modelDir = Join-Path $PSScriptRoot "models"
New-Item -ItemType Directory -Force $modelDir | Out-Null
$model = Join-Path $modelDir "stories260K.gguf"
if (-not (Test-Path $model)) {
    Write-Host "Downloading tiny test model (~1 MB)..."
    Invoke-WebRequest "https://huggingface.co/ggml-org/models/resolve/main/tinyllamas/stories260K.gguf" -OutFile $model
}

@"
macros:
  llama: $Root\bin\llama.cpp\llama-server.exe

models:
  test-model:
    cmd: >
      `${llama} --port `${PORT}
      -m "$model"
      -c 1024
    ttl: 300

groups:
  gpu-heavy:
    swap: true
    exclusive: false
    members:
      - "test-model"
"@ | Set-Content "$Root\config.yaml" -Encoding ascii
Write-Host "Test config.yaml written."

# --- act: run the real installation steps --------------------------------------
& (Join-Path $Root "setup\01-llama-cpp.ps1")
& (Join-Path $Root "setup\02-llama-swap.ps1")
& (Join-Path $Root "setup\03-server-task.ps1")
& (Join-Path $Root "setup\04-tray-task.ps1")

# --- assert ---------------------------------------------------------------------
Write-Host ""
Write-Host "=== assertions ==="

$models = $null
try { $models = (Invoke-RestMethod "$Api/v1/models" -TimeoutSec 30).data.id } catch {}
Assert "API answers and lists test-model" ($models -contains "test-model")

$tokens = 0
try {
    $body = '{"model":"test-model","prompt":"Once upon a time","max_tokens":8}'
    $r = Invoke-RestMethod "$Api/v1/completions" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 120
    $tokens = $r.usage.completion_tokens
} catch {}
Assert "inference produced tokens" ($tokens -ge 1)

Assert "tray exe was built" (Test-Path (Join-Path $Root "swap-tray.exe"))
Assert "tray process is running" ([bool](Get-Process -Name "swap-tray" -ErrorAction SilentlyContinue))

# stop/start through the task (this is what the tray does; also proves the
# no-UAC permission grant, since this script itself may run unelevated afterwards)
cmd /c "schtasks /End /TN llama-swap\server >nul 2>&1"
Start-Sleep -Seconds 3
$down = $true
try { Invoke-RestMethod "$Api/running" -TimeoutSec 3 | Out-Null; $down = $false } catch {}
Assert "server stops via task" $down

schtasks /Run /TN "llama-swap\server" | Out-Null
$deadline = (Get-Date).AddSeconds(20); $upAgain = $false
while ((Get-Date) -lt $deadline -and -not $upAgain) {
    try { Invoke-RestMethod "$Api/running" -TimeoutSec 3 | Out-Null; $upAgain = $true } catch { Start-Sleep -Seconds 2 }
}
Assert "server restarts via task" $upAgain

# update script: freshly installed means the version check should short-circuit
$updOut = & (Join-Path $Root "setup\05-update.ps1") 2>&1 | Out-String
Assert "update script reports 'already up to date'" ($updOut -match "Already up to date")

Write-Host ""
if ($script:fail -eq 0) { Write-Host "INTEGRATION TEST PASSED" -ForegroundColor Green }
else { Write-Host "$script:fail ASSERTION(S) FAILED" -ForegroundColor Red }
exit $script:fail
