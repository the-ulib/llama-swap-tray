# Step 4: compile the tray app (a real windowless Windows application) and
# register it as a logon task. RUN AS ADMINISTRATOR. Idempotent.
# The C# compiler (csc.exe, .NET Framework) is preinstalled on every Windows.
# NOTE: deliberately NOT a shortcut in shell:startup and NOT a hidden PowerShell -
# both patterns are regularly flagged by antivirus heuristics.
#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

$TaskName = "llama-swap\tray"
$Root = Split-Path $PSScriptRoot -Parent
$Src = Join-Path $PSScriptRoot "swap-tray.cs"
$Exe = Join-Path $Root "swap-tray.exe"
$csc = Join-Path $env:windir "Microsoft.NET\Framework64\v4.0.30319\csc.exe"

# 1) compile when the exe is missing or the source is newer
$needBuild = (-not (Test-Path $Exe)) -or ((Get-Item $Src).LastWriteTime -gt (Get-Item $Exe).LastWriteTime)
if ($needBuild) {
    # stop running instances so the exe can be replaced
    Get-Process -Name "swap-tray" -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 1
    & $csc /nologo /target:winexe /out:$Exe /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:System.Web.Extensions.dll $Src
    if ($LASTEXITCODE -ne 0) { throw "Compilation of swap-tray.cs failed" }
    Write-Host "swap-tray.exe compiled."
} else {
    Write-Host "swap-tray.exe is up to date."
}

# 2) register the logon task pointing at the exe (quoted for paths with spaces)
schtasks /Create /TN $TaskName /TR "`"$Exe`"" /SC ONLOGON /F | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not register task '$TaskName'" }
# Keep the tray available after three days, including while in gaming mode.
$traySettings = (Get-ScheduledTask -TaskPath "\llama-swap\" -TaskName "tray").Settings
$traySettings.ExecutionTimeLimit = "PT0S"
Set-ScheduledTask -TaskPath "\llama-swap\" -TaskName "tray" -Settings $traySettings -ErrorAction Stop | Out-Null
Write-Host "Logon task '$TaskName' -> $Exe"

# 3) stop old instances, start fresh
Get-Process -Name "swap-tray" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1
schtasks /Run /TN $TaskName | Out-Null
Write-Host "Tray started - the icon appears in the taskbar (possibly behind the ^ overflow arrow)."
