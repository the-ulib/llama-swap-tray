# Step 3: register llama-swap as a boot task (runs as SYSTEM, available before login)
# RUN AS ADMINISTRATOR. Idempotent - safe to re-run any time
# (e.g. to change the port or to repair task permissions).
#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

$TaskName = "llama-swap\server"   # lives in its own Task Scheduler folder
$Port = 9292                      # must match the Api constant in setup\swap-tray.cs

$Root = Split-Path $PSScriptRoot -Parent
$Exe = Join-Path $Root "bin\llama-swap\llama-swap.exe"
$Config = Join-Path $Root "config.yaml"

if (-not (Test-Path $Exe)) { throw "llama-swap.exe not found - run setup\02-llama-swap.ps1 first." }
if (-not (Test-Path $Config)) { throw "config.yaml not found - copy config.example.yaml to config.yaml and adjust the paths first." }

# quotes around paths so installs in folders with spaces work
$Cmd = "`"$Exe`" -config `"$Config`" -watch-config -listen :$Port"

# 1) create/overwrite the boot task (the Task Scheduler folder is created automatically)
schtasks /Create /TN $TaskName /TR $Cmd /SC ONSTART /RU SYSTEM /RL HIGHEST /F | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not register task '$TaskName'" }
# Keep the server running: override Task Scheduler's default 72-hour limit.
$serverSettings = (Get-ScheduledTask -TaskPath "\llama-swap\" -TaskName "server").Settings
$serverSettings.ExecutionTimeLimit = "PT0S"
Set-ScheduledTask -TaskPath "\llama-swap\" -TaskName "server" -Settings $serverSettings -ErrorAction Stop | Out-Null
Write-Host "Task '$TaskName' registered: $Cmd"

# 2) firewall rule so other machines on the LAN can reach the API
$ruleName = "llama-swap $Port"
if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Private | Out-Null
    Write-Host "Firewall rule '$ruleName' (TCP $Port) created"
}

# 3) allow local users to start/stop the task WITHOUT a UAC prompt (for the tray app).
#    BU = BUILTIN\Users (SDDL alias): language-independent, covers every local user,
#    and avoids the trap where a standard user elevates with a different admin account.
$svc = New-Object -ComObject Schedule.Service
$svc.Connect()
$task = $svc.GetFolder("\llama-swap").GetTask("server")
# request only owner/group/DACL (7) so an appended ACE lands inside the DACL section
$sd = $task.GetSecurityDescriptor(7)
# IMPORTANT: check for the full ACE, not just a SID - the creator's SID always
# appears in the descriptor as the task owner and would defeat the check
$ace = "(A;;GRGWGX;;;BU)"
if ($sd -notmatch [regex]::Escape($ace)) {
    $task.SetSecurityDescriptor($sd + $ace, 0)
    Write-Host "Local users may now start/stop the task without UAC"
} else {
    Write-Host "User permission was already in place"
}

# 4) (re)start: end a possibly running instance, start with the current settings
cmd /c "schtasks /End /TN $TaskName >nul 2>&1"
Start-Sleep -Seconds 2
schtasks /Run /TN $TaskName | Out-Null
Start-Sleep -Seconds 3
try {
    $models = (Invoke-RestMethod "http://localhost:$Port/v1/models" -TimeoutSec 10).data.id
    Write-Host "llama-swap is running on :$Port with models: $($models -join ', ')"
} catch {
    Write-Host "WARNING: llama-swap does not answer on :$Port yet - check the logs at http://localhost:$Port/ui" -ForegroundColor Yellow
}
Write-Host "Continue with setup\04-tray-task.ps1 (as administrator)."
