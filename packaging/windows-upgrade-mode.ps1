# The old installation may predate this installer. Inspect its running launcher
# before Restart Manager closes it. Return only a known mode and numeric port.
param([Parameter(Mandatory=$true)][string]$InstallDir)
$ErrorActionPreference = 'Stop'
$executables = @('decklab.exe','seriald.exe') | ForEach-Object { Join-Path $InstallDir $_ }
$session = (Get-Process -Id $PID).SessionId
$hostsFound = @(Get-CimInstance Win32_Process -Filter "Name='decklab.exe' OR Name='seriald.exe'" |
    Where-Object { $_.ExecutablePath -in $executables })
# Restart Manager cannot close the console of an installation in another logon
# session. Report it before Setup tries to replace files held open by that host.
if (@($hostsFound | Where-Object SessionId -ne $session).Count -gt 0) { exit 4 }
$launches = @()
foreach ($proc in $hostsFound) {
    $argsText = $proc.CommandLine -replace '^\s*(?:"[^"]+"|\S+)\s*', ''
    if ($argsText -notmatch '^--bind\s+(127\.0\.0\.1|0\.0\.0\.0)(?:\s+--port\s+(\d{1,5}))?\s*$') { continue }
    $bind = $Matches[1]
    $port = if ($Matches[2]) { [int]$Matches[2] } else { 8898 }
    if ($port -lt 1 -or $port -gt 65535) { continue }
    $mode = 1
    if ($bind -eq '0.0.0.0') {
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.ParentProcessId)"
        if (!$parent -or $parent.Name -ne 'powershell.exe' -or $parent.CreationDate -gt $proc.CreationDate) { continue }
        $prefix = [regex]::Escape((Join-Path $InstallDir 'Start-')) + '(?:decklab|seriald)-LAN'
        if ($parent.CommandLine -notmatch ('(?i)\s-File\s+"?' + $prefix + '(-Open)?\.ps1"?(?:\s|$)')) { continue }
        $mode = if ($Matches[1]) { 3 } else { 2 }
    }
    $launches += $mode * 65536 + $port
}
if ($launches.Count -eq 1) { exit $launches[0] }
exit 0
