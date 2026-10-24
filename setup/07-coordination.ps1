# Optional ComfyUI integration. Run after the ordinary installation, while idle.
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ComfyRoot,
    [string]$Python,
    [int]$PublicPort = 9292,
    [int]$BackendPort = 9293,
    [int]$ComfyPort = 8188,
    [string[]]$LlmModels = @(),
    [string[]]$ComfyModelIds = @(),
    [switch]$AllowPersistentUnload,
    [string]$TaskFolder = 'llama-swap'
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$ComfyRoot = (Resolve-Path -LiteralPath $ComfyRoot).Path
$TaskPath = '\' + $TaskFolder.Trim('\') + '\'
$TaskName = $TaskFolder.Trim('\') + '\server'
$runtime = Join-Path $Root 'runtime'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
. (Join-Path $PSScriptRoot 'coordination-runtime.ps1')

# Never replace a different installation's shared task names.
$server = Get-ScheduledTask -TaskPath $TaskPath -TaskName server -ErrorAction Stop
$nativeExe = Join-Path $Root 'bin\llama-swap\llama-swap.exe'
$oldAction = $server.Actions | Select-Object -First 1
$markerPath = Join-Path $runtime 'coordination-installed.json'
$existing = $null
if (Test-Path -LiteralPath $markerPath) {
    $existing = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    $previous = Get-Content -LiteralPath (Join-Path $Root 'coordinator.json') -Raw | ConvertFrom-Json
    if (-not $PSBoundParameters.ContainsKey('PublicPort')) { $PublicPort = $previous.listen_port }
    if (-not $PSBoundParameters.ContainsKey('BackendPort')) { $BackendPort = [int]([Uri]$previous.backend_url).Port }
    if (-not $PSBoundParameters.ContainsKey('ComfyPort')) { $ComfyPort = [int]([Uri]$previous.comfy_url).Port }
}
if ($oldAction.Execute -ne $nativeExe -and (-not $existing -or $oldAction.Arguments -notlike ('*' + (Join-Path $Root 'coordination\launch.py') + '*'))) {
    throw "Task '$TaskName' belongs to another installation. No changes were made."
}
if ($server.State -eq 'Running') {
    $checkPort = $PublicPort
    if ($oldAction.Arguments -match '-listen\s+(?:127\.0\.0\.1)?:([0-9]+)') { $checkPort = [int]$Matches[1] }
    $running = (Invoke-RestMethod "http://127.0.0.1:$checkPort/running" -TimeoutSec 5).running
    if ($running.Count) { throw 'Unload all LLM models before installing coordination' }
}
try {
    $queue = Invoke-RestMethod "http://127.0.0.1:$ComfyPort/queue" -TimeoutSec 3
    if ($queue.queue_running.Count -or $queue.queue_pending.Count) { throw 'ComfyUI must have no running or pending workflows' }
} catch {
    if ($_.Exception.Message -notmatch 'actively refused|aktiv verweigert|Unable to connect|keine Verbindung') { throw }
}

$pythonExe = Get-CoordinatorPython $Root $Python
& $pythonExe -m pip install -r (Join-Path $Root 'coordination\requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Coordinator dependency installation failed' }
$arguments = @('-m', 'coordination.prepare', '--root', $Root, '--comfy-root', $ComfyRoot,
               '--public-port', $PublicPort, '--backend-port', $BackendPort, '--comfy-port', $ComfyPort, '--task-name', $TaskName)
foreach ($id in $LlmModels) { $arguments += @('--llm-model', $id) }
foreach ($id in $ComfyModelIds) { $arguments += @('--comfy-model', $id) }
if ($AllowPersistentUnload) { $arguments += '--allow-persistent-unload' }
elseif ($existing -and $previous.allow_persistent_unload) { $arguments += '--allow-persistent-unload' }
Push-Location $Root
try { & $pythonExe @arguments; if ($LASTEXITCODE -ne 0) { throw 'Coordination configuration preflight failed; running setup was not changed' } }
finally { Pop-Location }

$backup = Join-Path $Root ('backup\coordination-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $Root 'config.yaml') -Destination $backup
Export-ScheduledTask -TaskPath $TaskPath -TaskName server | Set-Content -LiteralPath (Join-Path $backup 'server.xml') -Encoding Unicode
foreach ($file in @('coordinator.json', 'runtime\coordination-installed.json', 'runtime\coordination-command.json')) {
    $source = Join-Path $Root $file
    if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination (Join-Path $backup ([IO.Path]::GetFileName($file))) }
}
$extension = Join-Path $ComfyRoot 'custom_nodes\llama_swap_coordination'
if (Test-Path -LiteralPath $extension) { Copy-Item -LiteralPath $extension -Destination (Join-Path $backup 'extension') -Recurse }
New-Item -ItemType Directory -Path $extension -Force | Out-Null
$basePython = (& $pythonExe -c 'import sys; print(sys._base_executable)').Trim()
$pythonw = Join-Path (Split-Path $basePython) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { throw 'Base pythonw.exe not found' }
$actionArgs = '"{0}" --config "{1}"' -f (Join-Path $Root 'coordination\launch.py'), (Join-Path $Root 'coordinator.json')
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $actionArgs -WorkingDirectory $Root

try {
    # Stop the confirmed-idle service before changing its watched configuration.
    Stop-ScheduledTask -TaskPath $TaskPath -TaskName server -ErrorAction Stop
    Copy-Item -LiteralPath (Join-Path $runtime 'coordination-staged\config.yaml') -Destination (Join-Path $Root 'config.yaml') -Force
    Copy-Item -LiteralPath (Join-Path $runtime 'coordination-staged\coordinator.json') -Destination (Join-Path $Root 'coordinator.json') -Force
    Copy-Item -Path (Join-Path $Root 'coordination\comfy_extension\*.py') -Destination $extension -Force
    @{ config_path = (Join-Path $Root 'coordinator.json') } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $extension 'settings.json') -Encoding UTF8
    $token = Join-Path $runtime 'coordination-token.txt'
    if (-not (Test-Path -LiteralPath $token)) { throw 'Worker token creation failed' }
    Set-ScheduledTask -TaskPath $TaskPath -TaskName server -Action $action -ErrorAction Stop | Out-Null
    $original = $backup
    if ($existing) { $original = $existing.backup }
    @{ backup = $original; last_install_backup = $backup; task_path = $TaskPath; task_name = $TaskName } | ConvertTo-Json | Set-Content -LiteralPath $markerPath -Encoding UTF8
    @{ execute = $pythonw; arguments = $actionArgs } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtime 'coordination-command.json') -Encoding UTF8
    Start-ScheduledTask -TaskPath $TaskPath -TaskName server -ErrorAction Stop
    $deadline = (Get-Date).AddSeconds(30)
    $ready = $false
    while ((Get-Date) -lt $deadline -and -not $ready) {
        try { $ready = $null -ne (Invoke-RestMethod "http://127.0.0.1:$PublicPort/coordination/status" -TimeoutSec 2).enabled }
        catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $ready) { throw 'Coordinator did not become ready; restoring the previous task and config' }
} catch {
    Stop-ScheduledTask -TaskPath $TaskPath -TaskName server -ErrorAction SilentlyContinue
    Copy-Item -LiteralPath (Join-Path $backup 'config.yaml') -Destination (Join-Path $Root 'config.yaml') -Force
    if (Test-Path -LiteralPath (Join-Path $backup 'coordinator.json')) {
        Copy-Item -LiteralPath (Join-Path $backup 'coordinator.json') -Destination (Join-Path $Root 'coordinator.json') -Force
    } elseif (Test-Path -LiteralPath (Join-Path $Root 'coordinator.json')) {
        $disabled = Get-Content -LiteralPath (Join-Path $Root 'coordinator.json') -Raw | ConvertFrom-Json
        $disabled.enabled = $false
        $disabled | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $Root 'coordinator.json') -Encoding UTF8
    }
    Register-ScheduledTask -TaskPath $TaskPath -TaskName server -Xml (Get-Content -LiteralPath (Join-Path $backup 'server.xml') -Raw) -Force -ErrorAction Stop | Out-Null
    if ($existing) { Copy-Item -LiteralPath (Join-Path $backup 'coordination-installed.json') -Destination $markerPath -Force }
    else { Remove-Item -LiteralPath $markerPath -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath (Join-Path $backup 'coordination-command.json')) {
        Copy-Item -LiteralPath (Join-Path $backup 'coordination-command.json') -Destination (Join-Path $runtime 'coordination-command.json') -Force
    } else { Remove-Item -LiteralPath (Join-Path $runtime 'coordination-command.json') -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath (Join-Path $backup 'extension')) {
        Copy-Item -Path (Join-Path $backup 'extension\*') -Destination $extension -Recurse -Force
    }
    Start-ScheduledTask -TaskPath $TaskPath -TaskName server
    throw
}
& (Join-Path $PSScriptRoot '04-tray-task.ps1') -TaskFolder $TaskFolder
Write-Host "Installed with coordination OFF. Restart idle ComfyUI once, then enable it in the tray. Backup: $original"
