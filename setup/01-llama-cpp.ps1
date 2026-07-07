# Step 1: install/update llama.cpp into <root>\bin\llama.cpp
# No admin rights required.
# For CUDA this downloads BOTH zips (main build + CUDA runtime DLLs) - the runtime
# zip is easy to miss and without it llama.cpp silently falls back to CPU inference!
$ErrorActionPreference = "Stop"

# Backend of the llama.cpp build to install:
#   cuda   - NVIDIA GPUs (default)
#   vulkan - AMD / Intel / NVIDIA via Vulkan
#   cpu    - no GPU acceleration
$Backend = "cuda"

$Root = Split-Path $PSScriptRoot -Parent
$dest = Join-Path $Root "bin\llama.cpp"
New-Item -ItemType Directory -Force $dest | Out-Null

Write-Host "Looking up the latest llama.cpp release..."
$rel = Invoke-RestMethod "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"

if ($Backend -eq "cuda") {
    $assets = $rel.assets | Where-Object { $_.name -match "bin-win-cuda-([\d\.]+)-x64\.zip$" }
    if (-not $assets) { throw "No CUDA windows assets found in release $($rel.tag_name)." }
    # pick the highest CUDA version (releases often ship several in parallel)
    $ver = ($assets | ForEach-Object {
        [version]($_.name -replace ".*cuda-([\d\.]+)-x64\.zip$", '$1')
    } | Sort-Object -Descending | Select-Object -First 1).ToString()
    # this intentionally matches BOTH the build zip and the cudart runtime zip
    $downloads = $assets | Where-Object { $_.name -match "cuda-$([regex]::Escape($ver))-x64" }
} else {
    $downloads = @($rel.assets | Where-Object { $_.name -match "bin-win-$Backend.*x64\.zip$" } | Select-Object -First 1)
    if (-not $downloads) { throw "No '$Backend' windows asset found in release $($rel.tag_name)." }
}
Write-Host "Release $($rel.tag_name) -> $($downloads.name -join ', ')"

foreach ($a in $downloads) {
    $zip = Join-Path $env:TEMP $a.name
    Write-Host "Downloading $($a.name)..."
    Invoke-WebRequest $a.browser_download_url -OutFile $zip
    Expand-Archive $zip -DestinationPath $dest -Force
    Remove-Item $zip
}
# remember the backend so the update script knows what to expect
Set-Content (Join-Path $dest ".backend") $Backend

Write-Host ""
Write-Host "Verification (must list your GPU - unless backend is cpu):"
& "$dest\llama-server.exe" --list-devices
