#Requires -Version 5.1
# Run on a clean Windows builder only. No host is started and no COM port is opened.
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this test on Windows.' }
$key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{51AB737C-BCEF-4C09-98EE-47ACD6A2CBBA}_is1'
if (Test-Path $key) { throw 'Use a clean test account; an existing decklab installation is registered.' }
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$versionSource = Get-Content -LiteralPath (Join-Path $root 'decklab_core\version.py') -Raw
if ($versionSource -notmatch "__version__\s*=\s*'([0-9]+\.[0-9]+\.[0-9]+)'") { throw 'Version missing.' }
$setup = Join-Path $root "dist\decklab-$($Matches[1])-windows-x64-setup.exe"
$target = Join-Path $env:TEMP ('decklab installer test ' + [guid]::NewGuid().ToString('N'))
$uninstaller = Join-Path $target 'unins000.exe'
$desktopLink = Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'DeckLab.lnk'
if (Test-Path -LiteralPath $desktopLink) { throw 'Use a clean test account; a DeckLab desktop shortcut already exists.' }
function Run-Setup([string[]]$Options) {
    $process = Start-Process -FilePath $setup -ArgumentList $Options -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Setup failed: $($process.ExitCode)" }
}

$bundle = Join-Path $root 'dist\decklab'
$driverScript = Join-Path $bundle 'Install-decklab-USB-Driver.ps1'
$driverAvailable = $false
if (Test-Path -LiteralPath $driverScript) {
    $command = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $driverScript + '"'), '-DetectOnly')
    $probe = Start-Process -FilePath $command -ArgumentList $arguments -Wait -PassThru
    if ($probe.ExitCode -notin @(0, 1)) { throw "USB driver detection did not run: $($probe.ExitCode)" }
    $driverAvailable = $probe.ExitCode -eq 0

    $syntax = [System.Management.Automation.Language.Parser]::ParseFile($driverScript, [ref]$null, [ref]$null)
    $detector = $syntax.Find({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Test-InstalledFlashDriver'
    }, $false)
    . ([scriptblock]::Create($detector.Extent.Text))
    $fixtureStore = Join-Path $env:TEMP ('decklab driver store ' + [guid]::NewGuid().ToString('N'))
    $fixturePackage = Join-Path $fixtureStore 'spacemit_usb.inf_amd64_fixture'
    $fixtureInf = Join-Path $fixturePackage 'spacemit_usb.inf'
    New-Item -ItemType Directory -Path $fixturePackage -Force | Out-Null
    try {
        if (Test-InstalledFlashDriver $fixtureStore) { throw 'An empty driver store matched.' }
        $driver = Get-Content -LiteralPath (Join-Path $bundle 'usb-driver\spacemit_usb.inf') -Raw
        $olderDriver = $driver -replace '(?m)^DriverVer[^\r\n]*', 'DriverVer = 01/01/2025,3.0.0.0'
        Set-Content -LiteralPath $fixtureInf -Value $olderDriver -Encoding ASCII
        if (-not (Test-InstalledFlashDriver $fixtureStore)) { throw 'An older compatible driver was not detected.' }
        Set-Content -LiteralPath $fixtureInf -Value ($olderDriver -replace 'PID_1001', 'PID_FFFF') -Encoding ASCII
        if (Test-InstalledFlashDriver $fixtureStore) { throw 'A driver missing a bootloader ID matched.' }
        Set-Content -LiteralPath $fixtureInf -Value ($olderDriver -replace 'AddService\s*=\s*WinUSB,', 'AddService = Other,') -Encoding ASCII
        if (Test-InstalledFlashDriver $fixtureStore) { throw 'A driver without the WinUSB service matched.' }
        Write-Host 'USB driver detection passed for empty, compatible older, partial and wrong-service stores.'
    } finally {
        Remove-Item -LiteralPath $fixtureStore -Recurse -Force
    }
}

try {
    # Simulate an existing unregistered portable directory, including user data.
    New-Item -ItemType Directory -Path $target | Out-Null
    Get-ChildItem -LiteralPath $bundle | Where-Object { $_.Name -ne 'data' } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $target -Recurse
    }
    $retiredHost = Join-Path $target 'seriald.exe'
    Move-Item -LiteralPath (Join-Path $target 'decklab.exe') -Destination $retiredHost
    New-Item -ItemType Directory -Path (Join-Path $target 'data') | Out-Null
    $sentinel = Join-Path $target 'data\preserve-me.txt'
    Set-Content -LiteralPath $sentinel -Value 'logs and configuration must survive' -Encoding ASCII
    $installLog = Join-Path $target 'data\install.log'
    $tasks = if ($driverAvailable) { 'desktopicon,usbdriver' } else { 'desktopicon' }
    Run-Setup -Options @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/LANG=english', "/TASKS=$tasks", "/DIR=`"$target`"", "/LOG=`"$installLog`"")
    if (Test-Path -LiteralPath $retiredHost) { throw 'Legacy host executable remained after the rename upgrade.' }
    $installResult = Get-Content -LiteralPath $installLog -Raw
    if ($driverAvailable -and $installResult -notmatch 'Compatible USB flashing driver detected; skipping driver task\.') {
        throw 'Setup did not skip the driver task when the Windows driver was already installed.'
    }
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
    $sourceReadme = Join-Path $root 'dist\decklab\README.md'
    if ((Get-FileHash -LiteralPath $programFile).Hash -ne (Get-FileHash -LiteralPath $sourceReadme).Hash) { throw 'Upgrade did not replace old program files.' }
    & (Join-Path $target 'decklab.exe') --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Installed host launcher failed.' }
    & (Join-Path $target 'platform-tools\adb.exe') version
    if ($LASTEXITCODE -ne 0) { throw 'Installed ADB failed to start.' }
    & (Join-Path $target 'platform-tools\fastboot.exe') --version
    if ($LASTEXITCODE -ne 0) { throw 'Installed Fastboot failed to start.' }
    foreach ($name in @('Start-decklab.cmd', 'Start-decklab-LAN-Open.cmd', 'Start-decklab-LAN.cmd')) {
        if (-not (Test-Path -LiteralPath (Join-Path $target $name))) { throw "Missing launcher: $name" }
    }
    $iconName = (Get-Content -LiteralPath (Join-Path $target 'app-icon-name.txt') -Raw).Trim()
    $iconPath = Join-Path $target $iconName
    $sourceIcon = Join-Path $root 'packaging\decklab.ico'
    if ((Get-FileHash -LiteralPath $iconPath).Hash -ne (Get-FileHash -LiteralPath $sourceIcon).Hash) { throw 'Installed shortcut icon is wrong.' }
    $shell = New-Object -ComObject WScript.Shell
    $files = New-Object -ComObject Scripting.FileSystemObject
    function Same-FilePath([string]$Actual, [string]$Expected) {
        if (-not $files.FileExists($Actual) -or -not $files.FileExists($Expected)) { return $false }
        return $files.GetFile($Actual).ShortPath -eq $files.GetFile($Expected).ShortPath
    }
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
        if ($shortcut.TargetPath -like '*Start-decklab*.cmd') { $shortcut }
    })
    if ($launchLinks.Count -ne 3) { throw "Expected three start-menu launch shortcuts; found $($launchLinks.Count) in $group." }
    if (-not (Test-Path -LiteralPath $desktopLink)) { throw 'Desktop shortcut is missing.' }
    $desktopShortcut = $shell.CreateShortcut($desktopLink)
    if (-not (Same-FilePath $desktopShortcut.TargetPath "$target\Start-decklab-LAN.cmd")) {
        throw "Desktop shortcut must start password-protected LAN sharing. Found: $($desktopShortcut.TargetPath)"
    }
    $launchLinks += $desktopShortcut
    foreach ($shortcut in $launchLinks) {
        if ($shortcut.IconLocation -notmatch '^(.*),0$' -or -not (Same-FilePath $Matches[1] $iconPath)) {
            throw "Shortcut still references a program/default icon: $($shortcut.IconLocation)"
        }
    }
    $verification = Join-Path $root 'dist\windows-install-verification.json'
    $python = Join-Path $root 'build\windows-venv\Scripts\python.exe'
    & $python (Join-Path $root 'tools\verify_windows_install.py') $target $setup $bundle $verification
    if ($LASTEXITCODE -ne 0) { throw 'Installed distribution content verification failed.' }
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
