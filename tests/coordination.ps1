# Non-invasive optional-feature tests. Uses temporary HTTP servers and processes;
# never registers tasks, edits an installed ComfyUI, or loads a model.
param([string]$Python, [string]$LlamaSwapExe)
$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
if (-not $Python) { $Python = Join-Path $Root '.coord-env\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $Python)) { throw 'Use -Python with an interpreter containing coordination/requirements.txt dependencies' }
Push-Location $Root
try {
    # Explicit modules avoid importing the ComfyUI-only custom-node package.
    & $Python -m unittest coordination.test_coordination coordination.test_worker_availability coordination.test_prepare coordination.test_comfy_control coordination.test_tray_control -v
    if ($LASTEXITCODE -ne 0) { throw 'Coordination tests failed' }
    if ($LlamaSwapExe) {
        & $Python -m coordination.smoke_service --llama-swap-exe $LlamaSwapExe
        if ($LASTEXITCODE -ne 0) { throw 'Real service-launcher smoke test failed' }
    }
} finally { Pop-Location }
