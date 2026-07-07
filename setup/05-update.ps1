# Step 5 (whenever needed): update llama.cpp + llama-swap
# - skips everything when the installed versions already match the latest releases
# - verifies the CURRENT stack works first (= a valid restore point)
# - backup to <root>\backup\<timestamp>\
# - downloads the newest versions (via scripts 01 + 02)
# - verifies: GPU visible, API reachable, a real inference produced tokens
# - on any failure: automatic rollback to the previous state + error description
# No admin rights required (task permissions come from setup script 03).
$ErrorActionPreference = "Stop"

$Root       = Split-Path $PSScriptRoot -Parent
$TaskName   = "llama-swap\server"
$Port       = 9292
$Api        = "http://localhost:$Port"
$LlamaDir   = Join-Path $Root "bin\llama.cpp"
$SwapExe    = Join-Path $Root "bin\llama-swap\llama-swap.exe"
$BackupRoot = Join-Path $Root "backup"
$Stamp      = Get-Date -Format "yyyy-MM-dd_HHmmss"
$Backup     = Join-Path $BackupRoot $Stamp

function Get-StackVersions {
    # stderr redirection via cmd, not via PowerShell: llama-server prints its version
    # to stderr, and PS 5.1 turns redirected native stderr lines into error records
    $swap = (cmd /c "`"$SwapExe`" -version 2>&1" | Out-String).Trim() -replace "\s+", " "
    $srv  = (cmd /c "`"$LlamaDir\llama-server.exe`" --version 2>&1" | Select-String "version:" | Out-String).Trim()
    "llama-swap [$swap] / llama.cpp [$srv]"
}

function Start-Server { schtasks /Run /TN $TaskName | Out-Null }

function Stop-Server {
    try { Invoke-WebRequest "$Api/unload" -UseBasicParsing -TimeoutSec 60 | Out-Null } catch {}
    cmd /c "schtasks /End /TN $TaskName >nul 2>&1"
    Start-Sleep -Seconds 3
}

function Get-TestModel {
    # generic: first model in config.yaml whose cmd uses llama-server
    # (works on any machine regardless of model names; entries for other
    #  GPU apps managed by llama-swap are skipped automatically)
    $current = $null
    foreach ($line in (Get-Content (Join-Path $Root "config.yaml"))) {
        if ($line -match '^\s{2}([A-Za-z0-9._-]+):\s*(#.*)?$') { $current = $Matches[1]; continue }
        if ($current -and $line -match 'llama-server|\$\{llama\}') { return $current }
    }
    return $null
}

function Test-Stack {
    # returns $null when everything is fine, otherwise an error description

    # 1) does llama.cpp see the GPU? (most common failure: CUDA runtime DLLs missing)
    $backend = "cuda"
    $bf = Join-Path $LlamaDir ".backend"
    if (Test-Path $bf) { $backend = (Get-Content $bf -TotalCount 1).Trim() }
    if ($backend -ne "cpu") {
        $dev = (cmd /c "`"$LlamaDir\llama-server.exe`" --list-devices 2>&1" | Out-String)
        if ($dev -notmatch "(CUDA|Vulkan|HIP|ROCm|SYCL)\d") {
            return "llama-server does not detect a GPU - for CUDA builds this usually means the cudart runtime DLLs are missing. --list-devices returned: $($dev.Trim())"
        }
    }

    # 2) does the llama-swap API answer?
    $deadline = (Get-Date).AddSeconds(30); $models = $null
    while ((Get-Date) -lt $deadline -and -not $models) {
        try { $models = (Invoke-RestMethod "$Api/v1/models" -TimeoutSec 5).data.id } catch { Start-Sleep -Seconds 2 }
    }
    if (-not $models) { return "llama-swap API does not answer on $Api (task not running or config error - check the logs at $Api/ui)" }

    # 3) real inference test with an automatically selected model.
    #    Uses the completions endpoint (no chat template, so no thinking-token
    #    lottery) and only checks that tokens were produced - model-independent.
    $testModel = Get-TestModel
    if (-not $testModel) { return $null }  # no LLM in the config - the API check has to do
    try {
        $body = '{"model":"' + $testModel + '","prompt":"Hello","max_tokens":8}'
        $r = Invoke-RestMethod "$Api/v1/completions" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 300
        if (-not $r.usage -or $r.usage.completion_tokens -lt 1) {
            return "Inference test: model '$testModel' produced no tokens"
        }
    } catch {
        return "Inference test with model '$testModel' failed: $($_.Exception.Message)"
    }
    return $null
}

Write-Host "=== llama-swap stack update $Stamp ==="
Write-Host "Before: $(Get-StackVersions)"

# 0a) is an update needed at all? (GitHub release tags vs. installed versions)
try {
    $latestSwap = ((Invoke-RestMethod "https://api.github.com/repos/mostlygeek/llama-swap/releases/latest").tag_name -replace "\D", "")
    $latestCpp  = ((Invoke-RestMethod "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest").tag_name -replace "\D", "")
    $curSwap = if ((cmd /c "`"$SwapExe`" -version 2>&1" | Out-String) -match "v(\d+)") { $Matches[1] } else { "?" }
    $curCpp  = if ((cmd /c "`"$LlamaDir\llama-server.exe`" --version 2>&1" | Out-String) -match "version:\s*(\d+)") { $Matches[1] } else { "?" }
    if ($curSwap -eq $latestSwap -and $curCpp -eq $latestCpp) {
        Write-Host "Already up to date (llama-swap v$curSwap, llama.cpp b$curCpp) - nothing to do." -ForegroundColor Green
        exit 0
    }
    Write-Host ("Update available: llama-swap v{0} -> v{1}, llama.cpp b{2} -> b{3}" -f $curSwap, $latestSwap, $curCpp, $latestCpp)
} catch {
    Write-Host "Version check not possible ($($_.Exception.Message)) - proceeding with the update to be safe."
}

# 0b) pre-check: only update starting from a WORKING state
Start-Server
$pre = Test-Stack
if ($pre) {
    throw "NO update performed: the current state is already broken, so there would be no working restore point. Finding: $pre"
}
Write-Host "Pre-check ok - the current state works."

# 1) back up the working state
New-Item -ItemType Directory -Force $Backup | Out-Null
Copy-Item $SwapExe $Backup
Copy-Item $LlamaDir (Join-Path $Backup "llama.cpp") -Recurse
Write-Host "Backup created: $Backup"

# 2) stop the server (releases the exe files for overwriting)
Stop-Server

$failed = $null
try {
    # 3) replace llama.cpp cleanly (empty the folder so no stale DLLs get mixed in)
    Remove-Item "$LlamaDir\*" -Recurse -Force
    & (Join-Path $PSScriptRoot "01-llama-cpp.ps1")

    # 4) update the llama-swap binary
    & (Join-Path $PSScriptRoot "02-llama-swap.ps1")

    # 5) start and verify
    Start-Server
    $failed = Test-Stack
} catch {
    $failed = "Update step aborted: $($_.Exception.Message)"
}

if ($failed) {
    Write-Host ""
    Write-Host "UPDATE FAILED - restoring the previous state..." -ForegroundColor Red
    Write-Host "Error description: $failed" -ForegroundColor Red
    Stop-Server
    Remove-Item "$LlamaDir\*" -Recurse -Force -ErrorAction SilentlyContinue
    Copy-Item (Join-Path $Backup "llama.cpp\*") $LlamaDir -Recurse -Force
    Copy-Item (Join-Path $Backup "llama-swap.exe") $SwapExe -Force
    Start-Server
    $post = Test-Stack
    if ($post) {
        Write-Host "ROLLBACK VERIFICATION FAILED: $post" -ForegroundColor Red
        Write-Host "The untouched backup is at: $Backup" -ForegroundColor Red
        exit 1
    }
    Write-Host "Rollback successful - the previous state is running again: $(Get-StackVersions)"
    exit 1
}

Write-Host ""
Write-Host "UPDATE SUCCESSFUL." -ForegroundColor Green
Write-Host "After: $(Get-StackVersions)"

# 6) prune old backups (keep the last 3)
Get-ChildItem $BackupRoot -Directory | Sort-Object Name -Descending | Select-Object -Skip 3 | Remove-Item -Recurse -Force
Write-Host "Done."
