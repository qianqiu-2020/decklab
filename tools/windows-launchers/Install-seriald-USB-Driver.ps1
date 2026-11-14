#Requires -Version 5.1
param([switch]$Elevated)
$ErrorActionPreference = 'Stop'
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
$expected = (Get-FileHash -LiteralPath $inf -Algorithm SHA256).Hash
# An identical staged package needs neither another elevation nor a device reset.
$store = Join-Path $env:SystemRoot 'System32\DriverStore\FileRepository'
foreach ($package in @(Get-ChildItem -LiteralPath $store -Directory -Filter 'spacemit_usb.inf_amd64_*' -ErrorAction SilentlyContinue)) {
    $existing = Join-Path $package.FullName 'spacemit_usb.inf'
    if ((Test-Path -LiteralPath $existing) -and (Get-FileHash -LiteralPath $existing -Algorithm SHA256).Hash -eq $expected) {
        Write-Host 'USB flashing driver is already installed.'
        exit 0
    }
}
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
