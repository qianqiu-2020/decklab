#Requires -Version 5.1
param([ValidateRange(1, 65535)][int]$Port = 8898)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe = Join-Path $root 'seriald.exe'
if (-not (Test-Path $exe)) { throw "seriald.exe not found: $exe" }
$env:SERIALD_AUTH_TOKEN = ''
$env:SERIALD_TRUST_LOOPBACK = ''
Write-Host "seriald LAN (no password): listening on 0.0.0.0:$Port"
Write-Host 'Use only on a trusted network. Keep this window open.'
& $exe --bind 0.0.0.0 --port $Port
if ($LASTEXITCODE -ne 0) { throw "seriald exited with code $LASTEXITCODE" }
