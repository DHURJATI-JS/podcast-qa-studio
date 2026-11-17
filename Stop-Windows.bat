@echo off
setlocal
rem Podcast Q^&A Studio - stop all services. Your data is kept.
cd /d "%~dp0"
echo Stopping Podcast Q^&A Studio (your data is kept)...
docker compose stop
echo.
pause
