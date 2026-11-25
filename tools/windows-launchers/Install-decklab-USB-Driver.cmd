@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-decklab-USB-Driver.ps1" %*
if errorlevel 1 pause
