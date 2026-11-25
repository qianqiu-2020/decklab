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

Write-Host "seriald LAN: listening on 0.0.0.0:$Port"
Write-Host 'Other computers only need a browser. Keep this window open.'
# Keep the original process handle so Windows PowerShell can read its exit code.
$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $exe
$startInfo.WorkingDirectory = $root
$startInfo.Arguments = "--bind 0.0.0.0 --port $Port"
$startInfo.UseShellExecute = $false
$startInfo.EnvironmentVariables['SERIALD_AUTH_TOKEN'] = $token
$startInfo.EnvironmentVariables['SERIALD_TRUST_LOOPBACK'] = '1'
$process = [System.Diagnostics.Process]::Start($startInfo)
try {
    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            if ($process.HasExited) { break }
            $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -OwningProcess $process.Id -ErrorAction SilentlyContinue
            if (!$listener) { Start-Sleep -Milliseconds 250; continue }
            $health = Invoke-RestMethod "http://127.0.0.1:$Port/healthz" -Headers @{Authorization="Bearer $token"} -TimeoutSec 2
            if ($health.pid -eq $process.Id) { $ready = $true; break }
            if ($process.HasExited) { break }
            Start-Sleep -Milliseconds 250
        } catch { if ($process.HasExited) { break }; Start-Sleep -Milliseconds 250 }
    }
    if ($ready) {
        $access = Invoke-RestMethod "http://127.0.0.1:$Port/api/access" -Headers @{Authorization="Bearer $token"} -TimeoutSec 15
        $profilePath = Join-Path $root 'lan-firewall.json'
        if ($access.share_enabled -eq $true -and $listener.LocalAddress -contains '0.0.0.0' -and
            (Test-Path -LiteralPath $profilePath -PathType Leaf)) {
            try {
                $ports = @(Get-NetTCPConnection -State Listen -OwningProcess $process.Id |
                    Where-Object { $_.LocalAddress -in @('0.0.0.0','::') } | Select-Object -ExpandProperty LocalPort)
                if (!$ports) { throw 'No LAN listeners found during the firewall check.' }
                $checks = @(& (Join-Path $root 'Get-seriald-LANFirewall.ps1') -Port $ports -Program $exe)
                if (!$checks) { throw 'No active firewall profile could be checked.' }
                foreach ($check in $checks) {
                    Write-Host "LAN firewall: $($check.Profile) TCP $($check.TCP) - $($check.Status)"
                }
                if ($checks.Status -contains 'Review') {
                    Write-Warning 'A restricted rule or block policy applies. Use the IT shortcut if LAN access fails.'
                } elseif ($checks.Status -contains 'Missing') {
                    $profileData = Get-Content -LiteralPath $profilePath -Raw -Encoding UTF8 | ConvertFrom-Json
                    if ($profileData.tool -notmatch '^[A-Za-z]:\\(?:[^\\":\r\n]+\\)*RunAsSpc\.exe$' -or
                        $profileData.policy -notmatch '^[A-Za-z]:\\(?:[^\\":\r\n]+\\)*[^\\":\r\n]+\.spc$' -or
                        !(Test-Path -LiteralPath $profileData.tool -PathType Leaf) -or
                        !(Test-Path -LiteralPath $profileData.policy -PathType Leaf)) {
                        throw 'IT firewall tool or policy is missing or invalid.'
                    }
                    Write-Host 'Starting the IT firewall tool for password-protected LAN access.'
                    $itStart = New-Object System.Diagnostics.ProcessStartInfo
                    $itStart.FileName = $profileData.tool
                    $itStart.WorkingDirectory = Split-Path $profileData.tool
                    $itStart.Arguments = '/cryptfile:"' + $profileData.policy + '" /quiet'
                    $itStart.UseShellExecute = $false
                    $itStart.EnvironmentVariables.Remove('SERIALD_AUTH_TOKEN')
                    $helper = [System.Diagnostics.Process]::Start($itStart)
                    try {
                        if (!$helper.WaitForExit(10000)) { Write-Warning 'IT firewall tool is still running.' }
                        elseif ($helper.ExitCode -ne 0) { Write-Warning "IT firewall tool exited with code $($helper.ExitCode)." }
                    } finally { $helper.Dispose() }
                }
            } catch { Write-Warning "LAN firewall setup: $($_.Exception.Message)" }
        }
        $share = Invoke-RestMethod "http://127.0.0.1:$Port/api/share-link" -Method Post -Headers @{Authorization="Bearer $token"} -TimeoutSec 3
        Write-Host 'Open one of these links in the remote browser; no client or pairing configuration is needed:'
        foreach ($address in $access.lan_hosts) { Write-Host ("  http://" + $address + ':' + $Port + $share.path) }
        Write-Host ("  Local: http://127.0.0.1:" + $Port + $share.path)
        Write-Host ('Link authorization expires in ' + ($share.expires_in / 3600) + ' hours. New links are available from Share access in the page.')
    } else { Write-Warning 'Access links are not ready. Open the local page and use Share access after startup.' }
} catch { Write-Warning 'Could not generate access links. Use Share access in the page; saved host configuration may override the launcher.' }
$process.WaitForExit()
$exitCode = $process.ExitCode
$process.Dispose()
if ($exitCode -ne 0) { throw "seriald exited with code $exitCode" }
