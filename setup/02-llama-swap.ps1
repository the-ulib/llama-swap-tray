# Step 2: install/update the llama-swap binary into <root>\bin\llama-swap
# No admin rights required.
$ErrorActionPreference = "Stop"

$Root = Split-Path $PSScriptRoot -Parent
$bin = Join-Path $Root "bin\llama-swap"

Write-Host "Looking up the latest llama-swap release..."
$rel = Invoke-RestMethod "https://api.github.com/repos/mostlygeek/llama-swap/releases/latest"
$asset = $rel.assets | Where-Object { $_.name -match "windows_amd64\.zip$" } | Select-Object -First 1
if (-not $asset) { throw "No windows asset found in release $($rel.tag_name)." }
Write-Host "Release $($rel.tag_name) -> $($asset.name)"

$zip = Join-Path $env:TEMP $asset.name
$tmp = Join-Path $env:TEMP "llama-swap-extract"
Invoke-WebRequest $asset.browser_download_url -OutFile $zip
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Expand-Archive $zip -DestinationPath $tmp -Force

New-Item -ItemType Directory -Force $bin | Out-Null
# take the binary plus upstream license/readme - your config.yaml stays untouched
Copy-Item (Join-Path $tmp "llama-swap.exe") (Join-Path $bin "llama-swap.exe") -Force
foreach ($doc in @("LICENSE.md", "README.md")) {
    $src = Join-Path $tmp $doc
    if (Test-Path $src) { Copy-Item $src (Join-Path $bin ("UPSTREAM-" + $doc)) -Force }
}
Remove-Item $zip, $tmp -Recurse -Force

& "$bin\llama-swap.exe" -version
Write-Host "llama-swap binary is up to date. (First install: continue with setup\03-server-task.ps1 as administrator.)"
