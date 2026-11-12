@echo off
cd /d "%~dp0"
"%~dp0seriald.exe" web %*
pause
