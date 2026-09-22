@echo off
cd /d "%~dp0"
set "SERIALD_AUTH_TOKEN="
set "SERIALD_TRUST_LOOPBACK="
"%~dp0seriald.exe" --bind 127.0.0.1 %*
pause
