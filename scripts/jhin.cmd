@echo off
REM Shim so `jhin` works from cmd.exe and PowerShell alike. The installer
REM copies this next to jhin.ps1 and puts that directory on PATH.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0jhin.ps1" %*
