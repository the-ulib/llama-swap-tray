# Non-invasive installer/rollback tests. Every task/API operation is mocked;
# the real scripts run only against a freshly-created temporary file tree.
param([Parameter(Mandatory=$true)][string]$Python)
$ErrorActionPreference = 'Stop'
$SourceRoot = Split-Path $PSScriptRoot -Parent
$fixtureBase = Join-Path ([IO.Path]::GetTempPath()) ('tray-coordination-' + [Guid]::NewGuid().ToString('N'))
$oldPipCheck = $env:PIP_DISABLE_PIP_VERSION_CHECK
$oldNoIndex = $env:PIP_NO_INDEX
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:PIP_NO_INDEX = '1'

function Get-ScheduledTask {
    [CmdletBinding()]param($TaskPath, $TaskName)
    [pscustomobject]@{ State = 'Ready'; Actions = $global:TrayCoordFixture.Action }
}
function Export-ScheduledTask {
    [CmdletBinding()]param($TaskPath, $TaskName)
    '<Task />'
}
function Set-ScheduledTask {
    [CmdletBinding()]param($TaskPath, $TaskName, $Action)
    if ($global:TrayCoordFixture.FailSet) { throw 'injected task registration failure' }
    $global:TrayCoordFixture.Action = $Action
}
function Register-ScheduledTask {
    [CmdletBinding()]param($TaskPath, $TaskName, $Xml, [switch]$Force)
    $global:TrayCoordFixture.Action = [pscustomobject]@{ Execute = $global:TrayCoordFixture.NativeExe; Arguments = '-listen :19351' }
}
function New-ScheduledTaskAction {
    [CmdletBinding()]param($Execute, $Argument, $WorkingDirectory)
    [pscustomobject]@{ Execute = $Execute; Arguments = $Argument; WorkingDirectory = $WorkingDirectory }
}
function Stop-ScheduledTask {
    [CmdletBinding()]param($TaskPath, $TaskName)
    $global:TrayCoordFixture.Stops++
}
function Start-ScheduledTask {
    [CmdletBinding()]param($TaskPath, $TaskName)
    $global:TrayCoordFixture.Starts++
}
function Invoke-RestMethod {
    [CmdletBinding()]param([Parameter(Position=0)]$Uri, $TimeoutSec, $Method, $ContentType, $Body)
    if ($Method -eq 'Post' -and $Uri -like '*/coordination/settings') {
        $path = Join-Path $global:TrayCoordFixture.Root 'coordinator.json'
        $cfg = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
        $cfg.enabled = ($Body | ConvertFrom-Json).enabled
        $cfg | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $path -Encoding UTF8
    }
    [pscustomobject]@{ enabled = $false; queue_running = @(); queue_pending = @(); running = @(); llm_active = 0; llm_waiting = 0; comfy_active = $false; comfy_pending = 0 }
}

function New-Fixture([string]$Name) {
    $root = Join-Path $fixtureBase $Name
    $setup = Join-Path $root 'setup'
    $comfy = Join-Path $root 'independent ComfyUI'
    New-Item -ItemType Directory -Path $setup,$comfy -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $comfy 'main.py') -Value '# fixture' -Encoding UTF8
    Copy-Item -LiteralPath (Join-Path $SourceRoot 'coordination.example.json') -Destination $root
    $sourceCoord = Join-Path $SourceRoot 'coordination'
    foreach ($file in Get-ChildItem -LiteralPath $sourceCoord -Filter '*.py' -Recurse) {
        $relative = $file.FullName.Substring($sourceCoord.Length).TrimStart('\')
        $destination = Join-Path (Join-Path $root 'coordination') $relative
        New-Item -ItemType Directory -Path (Split-Path $destination) -Force | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $destination
    }
    Set-Content -LiteralPath (Join-Path $root 'coordination\requirements.txt') -Value ''
    foreach ($name in @('07-coordination.ps1','08-rollback-coordination.ps1')) {
        $text = [IO.File]::ReadAllText((Join-Path $SourceRoot "setup\$name"))
        $text = [regex]::Replace($text, '(?m)^#Requires[^\r\n]*\r?\n', '')
        [IO.File]::WriteAllText((Join-Path $setup $name), $text)
    }
    $helper = 'function Get-CoordinatorPython([string]$Root,[string]$Python) { return $Python }'
    Set-Content -LiteralPath (Join-Path $setup 'coordination-runtime.ps1') -Value $helper
    Set-Content -LiteralPath (Join-Path $setup '04-tray-task.ps1') -Value 'param($TaskFolder) # no task or tray process in fixture'
    $native = Join-Path $root 'bin\llama-swap\llama-swap.exe'
    @"
# keep user comments
models:
  language:
    cmd: llama-server -m weights
  paint-engine:
    cmd: python "$comfy\main.py"
groups:
  gpu:
    swap: true
    members:
      - language
      - paint-engine
"@ | Set-Content -LiteralPath (Join-Path $root 'config.yaml') -Encoding UTF8
    $global:TrayCoordFixture = @{ Root = $root; NativeExe = $native; Stops = 0; Starts = 0; FailSet = $false;
        Action = [pscustomobject]@{ Execute = $native; Arguments = '-listen :19351' } }
    return $root
}

function Assert-True($Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

try {
    Assert-True ((Get-Command Set-ScheduledTask).CommandType -eq 'Function') 'Task mutation was not mocked'
    $root = New-Fixture 'successful install with spaces'
    $original = [IO.File]::ReadAllText((Join-Path $root 'config.yaml'))
    & (Join-Path $root 'setup\07-coordination.ps1') -ComfyRoot (Join-Path $root 'independent ComfyUI') -Python $Python -PublicPort 19351 -BackendPort 19352 -ComfyPort 19353 -ComfyModelIds paint-engine
    $cfg = Get-Content -LiteralPath (Join-Path $root 'coordinator.json') -Raw | ConvertFrom-Json
    Assert-True (-not $cfg.enabled) 'Install must default off'
    Assert-True ($cfg.llm_models -contains 'language') 'LLM model discovery failed'
    Assert-True ($global:TrayCoordFixture.Action.Execute -like '*pythonw.exe') 'Task did not use the base GUI interpreter'
    Assert-True ([IO.File]::ReadAllText((Join-Path $root 'config.yaml')) -notmatch '  paint-engine:') 'ComfyUI process entry was not migrated'
    Assert-True (Test-Path -LiteralPath (Join-Path $root 'independent ComfyUI\custom_nodes\llama_swap_coordination\settings.json')) 'Extension configuration missing'
    & (Join-Path $root 'setup\08-rollback-coordination.ps1')
    Assert-True ([IO.File]::ReadAllText((Join-Path $root 'config.yaml')) -eq $original) 'Rollback did not preserve exact original configuration'
    Assert-True ($global:TrayCoordFixture.Action.Execute -eq $global:TrayCoordFixture.NativeExe) 'Rollback did not restore native task'
    Write-Host '[PASS] installer and rollback with custom paths/ports'

    $root = New-Fixture 'injected failure'
    $original = [IO.File]::ReadAllText((Join-Path $root 'config.yaml'))
    $global:TrayCoordFixture.FailSet = $true
    $failed = $false
    try { & (Join-Path $root 'setup\07-coordination.ps1') -ComfyRoot (Join-Path $root 'independent ComfyUI') -Python $Python -PublicPort 19351 -BackendPort 19352 -ComfyPort 19353 -ComfyModelIds paint-engine }
    catch { $failed = $true }
    Assert-True $failed 'Injected registration failure was ignored'
    Assert-True ([IO.File]::ReadAllText((Join-Path $root 'config.yaml')) -eq $original) 'Failed install did not restore original config'
    Assert-True (-not (Test-Path (Join-Path $root 'runtime\coordination-installed.json'))) 'Failed install left an active marker'
    Write-Host '[PASS] failed task update restores previous configuration'

    $root = New-Fixture 'foreign task'
    $global:TrayCoordFixture.Action.Execute = 'another-installation.exe'
    $failed = $false
    try { & (Join-Path $root 'setup\07-coordination.ps1') -ComfyRoot (Join-Path $root 'independent ComfyUI') -Python $Python }
    catch { $failed = $true }
    Assert-True ($failed -and $global:TrayCoordFixture.Stops -eq 0) 'Foreign installation was modified'
    Write-Host '[PASS] another installation task is never replaced'
} finally {
    $env:PIP_DISABLE_PIP_VERSION_CHECK = $oldPipCheck
    $env:PIP_NO_INDEX = $oldNoIndex
    Remove-Variable -Name TrayCoordFixture -Scope Global -ErrorAction SilentlyContinue
    $resolved = [IO.Path]::GetFullPath($fixtureBase)
    if (-not $resolved.StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()), [StringComparison]::OrdinalIgnoreCase)) { throw 'Fixture cleanup escaped temp directory' }
    Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction SilentlyContinue
}
