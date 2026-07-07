@echo off
setlocal
title llama-swap-tray installation

:: --- self-elevation: restart with UAC when admin rights are missing ---
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo ==================================================
echo  llama-swap-tray installation
echo ==================================================
echo.
echo Prerequisites - please check, otherwise close this window:
echo   1. config.yaml exists (copy config.example.yaml and adjust the paths)
echo   2. Your GGUF models are downloaded (paths referenced in config.yaml)
echo   3. Optional: antivirus exclusion for this folder (see README)
echo   4. Non-NVIDIA GPU? Set $Backend in setup\01-llama-cpp.ps1 first
echo.
echo Steps: download llama.cpp, download llama-swap, register the server
echo task (port 9292, firewall, permissions) and build + register the tray app.
echo.
pause

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; & '%~dp0setup\01-llama-cpp.ps1'; & '%~dp0setup\02-llama-swap.ps1'; & '%~dp0setup\03-server-task.ps1'; & '%~dp0setup\04-tray-task.ps1'; Write-Host ''; Write-Host '=== INSTALLATION COMPLETED SUCCESSFULLY ===' -ForegroundColor Green"
if %errorlevel% neq 0 (
    echo.
    echo INSTALLATION FAILED - check the error message above.
    echo Individual steps can be re-run from the setup\ folder.
)
echo.
pause
