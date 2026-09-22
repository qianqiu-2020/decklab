#Requires -Version 5.1
param([ValidateRange(1, 65535)][int]$Port = 8898)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe = Join-Path $root 'seriald.exe'
if (-not (Test-Path $exe)) { throw "seriald.exe not found: $exe" }

$data = Join-Path $root 'data'
New-Item -ItemType Directory -Path $data -Force | Out-Null
$tokenFile = Join-Path $data 'lan-token.dpapi'
if (Test-Path $tokenFile) {
    try {
        $secure = (Get-Content $tokenFile -Raw).Trim() | ConvertTo-SecureString
        $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
    } catch {
        throw 'Cannot decrypt the saved LAN token as this Windows user. Preserve data when upgrading; to reset the token, remove data\lan-token.dpapi.'
    }
} else {
    $random = [byte[]]::new(32)
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($random) }
    finally { $rng.Dispose() }
    $token = [Convert]::ToBase64String($random).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    ConvertTo-SecureString -String $token -AsPlainText -Force |
        ConvertFrom-SecureString | Set-Content -Path $tokenFile -Encoding ASCII
}

$env:SERIALD_AUTH_TOKEN = $token
Write-Host "seriald LAN: listening on 0.0.0.0:$Port"
Write-Host "Open http://<Windows-IP>:$Port/ from another computer."
Write-Host 'Browser username: seriald'
Write-Host "Browser password: $token"
Write-Host 'Keep this window open. Allow inbound TCP on the selected port for Private networks in Windows Firewall.'
& $exe --bind 0.0.0.0 --port $Port
if ($LASTEXITCODE -ne 0) { throw "seriald exited with code $LASTEXITCODE" }
