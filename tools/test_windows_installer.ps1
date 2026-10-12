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
$desktopLink = Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'DeckLab.lnk'
if (Test-Path -LiteralPath $desktopLink) { throw 'Use a clean test account; a DeckLab desktop shortcut already exists.' }
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
    Run-Setup -Options @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/LANG=english', '/TASKS=desktopicon', "/DIR=`"$target`"")
    $languageFile = Join-Path $target 'data\ui-language.txt'
    if ((Get-Content -LiteralPath $languageFile -Raw).Trim() -ne 'en') { throw 'Installer did not save the English UI default.' }
    if (-not (Test-Path -LiteralPath $uninstaller)) { throw 'Uninstaller missing.' }
    $programFile = Join-Path $target 'README.md'
    Set-Content -LiteralPath $programFile -Value 'old program file' -Encoding ASCII
    # No /DIR: the second install must remember the directory from the first one.
    Run-Setup -Options @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/LANG=chinesesimplified')
    if ((Get-Content -LiteralPath $languageFile -Raw).Trim() -ne 'en') { throw 'Upgrade reset the existing UI language.' }
    if ((Get-ItemProperty $key).InstallLocation.TrimEnd('\') -ne $target) { throw 'Upgrade forgot the installation directory.' }
    if ((Get-Content -LiteralPath $sentinel -Raw).Trim() -ne 'logs and configuration must survive') { throw 'Upgrade damaged runtime data.' }
    $sourceReadme = Join-Path $root 'dist\seriald\README.md'
    if ((Get-FileHash -LiteralPath $programFile).Hash -ne (Get-FileHash -LiteralPath $sourceReadme).Hash) { throw 'Upgrade did not replace old program files.' }
    & (Join-Path $target 'seriald.exe') --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Installed host launcher failed.' }
    foreach ($name in @('Start-seriald.cmd', 'Start-seriald-LAN-Open.cmd', 'Start-seriald-LAN.cmd')) {
        if (-not (Test-Path -LiteralPath (Join-Path $target $name))) { throw "Missing launcher: $name" }
    }
    $iconName = (Get-Content -LiteralPath (Join-Path $target 'app-icon-name.txt') -Raw).Trim()
    $iconPath = Join-Path $target $iconName
    $sourceIcon = Join-Path $root 'packaging\decklab.ico'
    if ((Get-FileHash -LiteralPath $iconPath).Hash -ne (Get-FileHash -LiteralPath $sourceIcon).Hash) { throw 'Installed shortcut icon is wrong.' }
    $shell = New-Object -ComObject WScript.Shell
    $group = Join-Path ([Environment]::GetFolderPath('Programs')) 'DeckLab'
    $shortcutFiles = @(Get-ChildItem -LiteralPath $group -Filter '*.lnk')
    $launchLinks = @($shortcutFiles | ForEach-Object {
        # Inspect identical bytes under an ASCII filename: WScript on some
        # Windows locales silently treats Unicode link names as new links.
        $inspectionPath = Join-Path $env:TEMP ('decklab-shortcut-' + [guid]::NewGuid().ToString('N') + '.lnk')
        Copy-Item -LiteralPath $_.FullName -Destination $inspectionPath
        try { $shortcut = $shell.CreateShortcut($inspectionPath) }
        finally { Remove-Item -LiteralPath $inspectionPath }
        Write-Host "Shortcut $($_.Name): target=$($shortcut.TargetPath); args=$($shortcut.Arguments); icon=$($shortcut.IconLocation)"
        if ($shortcut.TargetPath -like '*Start-seriald*.cmd') { $shortcut }
    })
    if ($launchLinks.Count -ne 3) { throw "Expected three start-menu launch shortcuts; found $($launchLinks.Count) in $group." }
    if (-not (Test-Path -LiteralPath $desktopLink)) { throw 'Desktop shortcut is missing.' }
    $launchLinks += $shell.CreateShortcut($desktopLink)
    foreach ($shortcut in $launchLinks) {
        if ($shortcut.IconLocation -ne "$iconPath,0") { throw "Shortcut still references a program/default icon: $($shortcut.IconLocation)" }
    }
    $process = Start-Process -FilePath $uninstaller -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw 'Uninstall failed.' }
    if (-not (Test-Path -LiteralPath $sentinel)) { throw 'Uninstall removed user data.' }
    Write-Host 'Installer migration, remembered upgrade directory, runtime preservation, shortcut icons and uninstall passed.'
} finally {
    if (Test-Path -LiteralPath $uninstaller) {
        Start-Process -FilePath $uninstaller -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait | Out-Null
    }
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
}
