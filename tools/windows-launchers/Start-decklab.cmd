@echo off
cd /d "%~dp0"
set "DECKLAB_AUTH_TOKEN="
set "DECKLAB_TRUST_LOOPBACK="
"%~dp0decklab.exe" --bind 127.0.0.1 %*
pause
