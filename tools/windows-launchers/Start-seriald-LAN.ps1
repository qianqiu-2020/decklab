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
$env:SERIALD_TRUST_LOOPBACK = '1'
Write-Host "seriald LAN: listening on 0.0.0.0:$Port"
Write-Host 'Other computers only need a browser. Keep this window open.'
$process = Start-Process -FilePath $exe -WorkingDirectory $root -ArgumentList '--bind','0.0.0.0','--port',"$Port" -NoNewWindow -PassThru
try {
    $access = $null
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            $access = Invoke-RestMethod "http://127.0.0.1:$Port/api/access" -Headers @{Authorization="Bearer $token"} -TimeoutSec 1
            break
        } catch { if ($process.HasExited) { break }; Start-Sleep -Milliseconds 250 }
    }
    if ($access) {
        $share = Invoke-RestMethod "http://127.0.0.1:$Port/api/share-link" -Method Post -Headers @{Authorization="Bearer $token"} -TimeoutSec 3
        Write-Host 'Open one of these links in the remote browser; no client or pairing configuration is needed:'
        foreach ($address in $access.lan_hosts) { Write-Host ("  http://" + $address + ':' + $Port + $share.path) }
        Write-Host ("  Local: http://127.0.0.1:" + $Port + $share.path)
        Write-Host ('Link authorization expires in ' + ($share.expires_in / 3600) + ' hours. New links are available from Share access in the page.')
    } else { Write-Warning 'Access links are not ready. Open the local page and use Share access after startup.' }
} catch { Write-Warning 'Could not generate access links. Use Share access in the page; saved host configuration may override the launcher.' }
$process.WaitForExit()
if ($process.ExitCode -ne 0) { throw "seriald exited with code $($process.ExitCode)" }
