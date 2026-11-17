@echo off
setlocal
rem Podcast Q^&A Studio - double-click to install (first time) and run.
cd /d "%~dp0"
title Podcast Q^&A Studio
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-and-run.ps1"
echo.
pause
