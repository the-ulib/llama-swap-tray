#Requires -RunAsAdministrator
[CmdletBinding()]
param([string]$Backup)
$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$markerPath = Join-Path $Root 'runtime\coordination-installed.json'
$installed = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
if (-not $Backup) { $Backup = $installed.backup }
$Backup = (Resolve-Path -LiteralPath $Backup).Path
if (-not (Test-Path -LiteralPath (Join-Path $Backup 'server.xml'))) { throw 'Not a coordination backup' }
$cfg = Get-Content -LiteralPath (Join-Path $Root 'coordinator.json') -Raw | ConvertFrom-Json
$api = 'http://127.0.0.1:' + $cfg.listen_port
$status = Invoke-RestMethod "$api/coordination/status" -TimeoutSec 5
if ($status.llm_active -or $status.llm_waiting -or $status.comfy_active -or $status.comfy_pending) { throw 'Wait until both queues are idle' }
try {
    $queue = Invoke-RestMethod ($cfg.comfy_url + '/queue') -TimeoutSec 3
    if ($queue.queue_running.Count -or $queue.queue_pending.Count) { throw 'Wait until ComfyUI is idle' }
} catch {
    if ($_.Exception.Message -notmatch 'actively refused|aktiv verweigert|Unable to connect|keine Verbindung') { throw }
}
$snapshot = Join-Path $Root ('backup\before-coordination-rollback-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $snapshot -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $Root 'config.yaml') -Destination $snapshot
Invoke-RestMethod "$api/coordination/settings" -Method Post -ContentType application/json -Body '{"enabled":false}' | Out-Null
Stop-ScheduledTask -TaskPath $installed.task_path -TaskName server -ErrorAction Stop
Copy-Item -LiteralPath (Join-Path $Backup 'config.yaml') -Destination (Join-Path $Root 'config.yaml') -Force
Register-ScheduledTask -TaskPath $installed.task_path -TaskName server -Xml (Get-Content -LiteralPath (Join-Path $Backup 'server.xml') -Raw) -Force -ErrorAction Stop | Out-Null
Remove-Item -LiteralPath $markerPath
Start-ScheduledTask -TaskPath $installed.task_path -TaskName server -ErrorAction Stop
Write-Host "Ordinary llama-swap task/config restored. Current config saved at $snapshot. Restart the tray."
