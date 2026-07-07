# Smoke test - NON-INVASIVE: runs anywhere, changes nothing on the system.
# Checks that all scripts parse, the tray app compiles, and the repo is complete.
#   powershell -ExecutionPolicy Bypass -File tests\smoke.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$script:fail = 0

function Check([string]$name, [scriptblock]$test) {
    try { & $test | Out-Null; Write-Host "[PASS] $name" }
    catch { Write-Host "[FAIL] $name : $($_.Exception.Message)" -ForegroundColor Red; $script:fail++ }
}

Check "expected files present" {
    foreach ($f in @("install.cmd", "update.cmd", "config.example.yaml", "LICENSE", "README.md",
                     "setup\01-llama-cpp.ps1", "setup\02-llama-swap.ps1", "setup\03-server-task.ps1",
                     "setup\04-tray-task.ps1", "setup\05-update.ps1", "setup\swap-tray.cs")) {
        if (-not (Test-Path (Join-Path $Root $f))) { throw "$f missing" }
    }
}

Check "all setup scripts parse (PowerShell 5.1)" {
    foreach ($f in Get-ChildItem "$Root\setup" -Filter *.ps1) {
        $errs = $null
        [System.Management.Automation.PSParser]::Tokenize((Get-Content $f.FullName -Raw), [ref]$errs) | Out-Null
        if ($errs -and $errs.Count -gt 0) { throw "$($f.Name): $($errs[0].Message)" }
    }
}

Check "test scripts parse" {
    foreach ($f in Get-ChildItem $PSScriptRoot -Filter *.ps1) {
        $errs = $null
        [System.Management.Automation.PSParser]::Tokenize((Get-Content $f.FullName -Raw), [ref]$errs) | Out-Null
        if ($errs -and $errs.Count -gt 0) { throw "$($f.Name): $($errs[0].Message)" }
    }
}

Check "tray app compiles with the inbox C# compiler" {
    $csc = Join-Path $env:windir "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
    if (-not (Test-Path $csc)) { throw "csc.exe not found at $csc" }
    $out = Join-Path $env:TEMP "swap-tray-smoke.exe"
    & $csc /nologo /target:winexe /out:$out /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:System.Web.Extensions.dll "$Root\setup\swap-tray.cs"
    if ($LASTEXITCODE -ne 0) { throw "csc reported errors" }
    Remove-Item $out -Force -ErrorAction SilentlyContinue
}

Check "config.example.yaml: model auto-detection finds an LLM entry" {
    # same heuristic the update script uses to pick its verification model
    $current = $null; $found = $null
    foreach ($line in (Get-Content "$Root\config.example.yaml")) {
        if ($line -match '^\s{2}([A-Za-z0-9._-]+):\s*(#.*)?$') { $current = $Matches[1]; continue }
        if ($current -and $line -match 'llama-server|\$\{llama\}') { $found = $current; break }
    }
    if (-not $found) { throw "no llama-server entry detected in the example config" }
}

Check "no leftover machine-specific paths in tracked files" {
    $hits = Get-ChildItem $Root -Recurse -File -Exclude *.exe |
        Where-Object { $_.FullName -notmatch '\\(bin|backup|\.git|tests\\models)\\' } |
        Select-String -Pattern 'C:\\Users\\[a-z]' -AllMatches
    if ($hits) { throw "found user-specific path in: $($hits[0].Path)" }
}

Write-Host ""
if ($script:fail -eq 0) { Write-Host "SMOKE TEST PASSED" -ForegroundColor Green } else { Write-Host "$script:fail CHECK(S) FAILED" -ForegroundColor Red }
exit $script:fail
