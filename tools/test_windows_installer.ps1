#Requires -Version 5.1
# Run on a clean Windows builder only. No host is started and no COM port is opened.
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this test on Windows.' }
$key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{51AB737C-BCEF-4C09-98EE-47ACD6A2CBBA}_is1'
if (Test-Path $key) { throw 'Use a clean test account; an existing seriald installation is registered.' }
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$versionSource = Get-Content -LiteralPath (Join-Path $root 'seriald_core\version.py') -Raw
if ($versionSource -notmatch "__version__\s*=\s*'([0-9]+\.[0-9]+\.[0-9]+)'") { throw 'Version missing.' }
$setup = Join-Path $root "dist\seriald-$($Matches[1])-windows-x64-setup.exe"
$target = Join-Path $env:TEMP ('seriald installer test ' + [guid]::NewGuid().ToString('N'))
$uninstaller = Join-Path $target 'unins000.exe'
function Run-Setup([string[]]$Options) {
    $process = Start-Process -FilePath $setup -ArgumentList $Options -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Setup failed: $($process.ExitCode)" }
}
try {
    # Simulate an existing unregistered portable directory, including user data.
    New-Item -ItemType Directory -Path $target | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $root 'dist\seriald') | Where-Object { $_.Name -ne 'data' } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $target -Recurse
    }
    New-Item -ItemType Directory -Path (Join-Path $target 'data') | Out-Null
    $sentinel = Join-Path $target 'data\preserve-me.txt'
    Set-Content -LiteralPath $sentinel -Value 'logs and configuration must survive' -Encoding ASCII
    Run-Setup -Options @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=`"$target`"")
    if (-not (Test-Path -LiteralPath $uninstaller)) { throw 'Uninstaller missing.' }
    $programFile = Join-Path $target 'README.md'
    Set-Content -LiteralPath $programFile -Value 'old program file' -Encoding ASCII
    # No /DIR: the second install must remember the directory from the first one.
    Run-Setup -Options @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART')
    if ((Get-ItemProperty $key).InstallLocation.TrimEnd('\') -ne $target) { throw 'Upgrade forgot the installation directory.' }
    if ((Get-Content -LiteralPath $sentinel -Raw).Trim() -ne 'logs and configuration must survive') { throw 'Upgrade damaged runtime data.' }
    $sourceReadme = Join-Path $root 'dist\seriald\README.md'
    if ((Get-FileHash -LiteralPath $programFile).Hash -ne (Get-FileHash -LiteralPath $sourceReadme).Hash) { throw 'Upgrade did not replace old program files.' }
    & (Join-Path $target 'seriald.exe') --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Installed host launcher failed.' }
    foreach ($name in @('Start-seriald.cmd', 'Start-seriald-LAN-Open.cmd', 'Start-seriald-LAN.cmd')) {
        if (-not (Test-Path -LiteralPath (Join-Path $target $name))) { throw "Missing launcher: $name" }
    }
    $process = Start-Process -FilePath $uninstaller -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw 'Uninstall failed.' }
    if (-not (Test-Path -LiteralPath $sentinel)) { throw 'Uninstall removed user data.' }
    Write-Host 'Installer migration, remembered upgrade directory, runtime preservation and uninstall passed.'
} finally {
    if (Test-Path -LiteralPath $uninstaller) {
        Start-Process -FilePath $uninstaller -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait | Out-Null
    }
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
}
