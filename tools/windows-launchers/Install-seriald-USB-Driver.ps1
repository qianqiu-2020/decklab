#Requires -Version 5.1
param([switch]$Elevated, [switch]$DetectOnly)
$ErrorActionPreference = 'Stop'

function Test-InstalledFlashDriver([string]$Store) {
    # The vendor package covers both generations of Fastboot hardware IDs.
    # Compare the supported IDs and service so older TitanFlasher versions
    # can be reused, including when no board is plugged in.
    foreach ($package in @(Get-ChildItem -LiteralPath $Store -Directory -Filter 'spacemit_usb.inf_amd64_*' -ErrorAction SilentlyContinue)) {
        $models = @{}
        $winUsb = $false
        $section = ''
        foreach ($line in @(Get-Content -LiteralPath (Join-Path $package.FullName 'spacemit_usb.inf') -ErrorAction SilentlyContinue)) {
            $entry = ($line -split ';', 2)[0].Trim()
            if ($entry -match '^\[([^\]]+)\]$') {
                $section = $Matches[1]
            } elseif ($section -eq 'Google.NTamd64' -and $entry -match '=\s*USB_Install\s*,\s*USB\\(VID_[0-9A-F]{4}&PID_[0-9A-F]{4})\s*$') {
                $models[$Matches[1]] = $true
            } elseif ($section -eq 'USB_Install.Services' -and $entry -match '^AddService\s*=\s*WinUSB\s*,') {
                $winUsb = $true
            }
        }
        if ($winUsb -and $models.ContainsKey('VID_18D1&PID_4E11') -and $models.ContainsKey('VID_361C&PID_1001')) {
            return $true
        }
    }
    return $false
}

if (Test-InstalledFlashDriver (Join-Path $env:SystemRoot 'System32\DriverStore\FileRepository')) {
    Write-Host 'A compatible USB flashing driver is already installed.'
    exit 0
}
if ($DetectOnly) { exit 1 }

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$profile = Get-Content -LiteralPath (Join-Path $root 'installation.json') -Raw | ConvertFrom-Json
if ($profile.profile -ne 'complete') { throw 'This installation does not contain a USB driver package.' }
$directory = Join-Path $root 'usb-driver'
foreach ($entry in $profile.driver_files.PSObject.Properties) {
    $path = Join-Path $directory $entry.Name
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value.sha256) {
        throw 'USB driver files changed; reinstall the complete package.'
    }
}
$inf = Join-Path $directory 'spacemit_usb.inf'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$admin = [Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    if ($Elevated) { throw 'Administrator approval is required to install the USB driver.' }
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $MyInvocation.MyCommand.Path + '"'), '-Elevated')
    $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -Verb RunAs -Wait -PassThru
    exit $process.ExitCode
}
$catalog = Join-Path $directory 'WinUSB\spacemit_amd64.cat'
if ((Get-AuthenticodeSignature -LiteralPath $catalog).Status -ne 'Valid') {
    # The vendor package uses its own catalog CA. Let the vendor-signed
    # installer handle first-time setup, with normal Windows publisher approval.
    $installer = Join-Path $directory 'driverinstall.exe'
    $signature = Get-AuthenticodeSignature -LiteralPath $installer
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*91330110MA7BHW4W7T*') {
        throw 'USB vendor installer signature is not trusted. Reinstall the complete package.'
    }
    $process = Start-Process -FilePath $installer -WorkingDirectory $directory -Wait -PassThru
    if ($process.ExitCode -notin @(0,3010)) { throw "Vendor USB installation failed: $($process.ExitCode)" }
}
& (Join-Path $env:SystemRoot 'System32\pnputil.exe') /add-driver $inf /install
if ($LASTEXITCODE -notin @(0, 3010)) { throw "USB driver installation failed: $LASTEXITCODE" }
Write-Host 'USB flashing driver is installed.'
