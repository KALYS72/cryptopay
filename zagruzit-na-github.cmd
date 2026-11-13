@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0zagruzit-na-github.ps1"
echo.
pause
