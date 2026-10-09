# Remove execution limits from existing tasks without starting servers/models.
# Run as administrator if the current user cannot update the tasks.
$ErrorActionPreference = "Stop"

$backupDir = Join-Path (Split-Path $PSScriptRoot -Parent) ("backup\task-limits-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

# Back up both definitions before changing either task.
foreach ($name in @("server", "tray")) {
    Export-ScheduledTask -TaskPath "\llama-swap\" -TaskName $name |
        Set-Content -LiteralPath (Join-Path $backupDir "$name.xml") -Encoding Unicode
}

foreach ($name in @("server", "tray")) {
    $task = Get-ScheduledTask -TaskPath "\llama-swap\" -TaskName $name
    if ($task.Settings.ExecutionTimeLimit -ne "PT0S") {
        $settings = $task.Settings
        $settings.ExecutionTimeLimit = "PT0S"
        Set-ScheduledTask -TaskPath "\llama-swap\" -TaskName $name -Settings $settings -ErrorAction Stop | Out-Null
    }
    $updated = Get-ScheduledTask -TaskPath "\llama-swap\" -TaskName $name
    if ($updated.Settings.ExecutionTimeLimit -ne "PT0S") {
        throw "Execution limit for '$name' was not removed"
    }
    Write-Host "$name`: ExecutionTimeLimit=$($updated.Settings.ExecutionTimeLimit), State=$($updated.State)"
}
Write-Host "Task backups: $backupDir. No task was started or stopped."
