@echo off
title llama-swap-tray update
:: Update with version check, backup, verification and automatic rollback.
:: No admin rights required (task permissions come from the installation).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\05-update.ps1"
echo.
pause
