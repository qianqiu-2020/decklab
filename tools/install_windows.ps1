#Requires -Version 5.1
param(
    [string]$Package,
    [string]$InstallDir,
    [string]$ExpectedSha256
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this installer on Windows.' }
$sourceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $InstallDir) { $InstallDir = Join-Path $sourceRoot 'seriald-installed' }
if (-not $Package) { $Package = Join-Path $sourceRoot 'dist\seriald-windows-portable.zip' }
if (-not (Test-Path -LiteralPath $Package -PathType Leaf)) {
    throw "Package not found: $Package. Run Build-seriald.cmd first."
}
$target = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$source = $sourceRoot.TrimEnd('\')
$defaultTarget = [IO.Path]::GetFullPath((Join-Path $sourceRoot 'seriald-installed')).TrimEnd('\')
$legacyRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'seriald')).TrimEnd('\')
$legacyData = Join-Path $legacyRoot 'data'
$targetData = Join-Path $target 'data'
if ($target.Equals($source, [StringComparison]::OrdinalIgnoreCase) -or
    $source.StartsWith($target + '\', [StringComparison]::OrdinalIgnoreCase) -or
    ($target.StartsWith($source + '\', [StringComparison]::OrdinalIgnoreCase) -and
     -not $target.Equals($defaultTarget, [StringComparison]::OrdinalIgnoreCase))) {
    throw 'InstallDir must be separate from source and build files; use seriald-installed or a directory outside the repository.'
}

$installedExe = Join-Path $target 'seriald.exe'
$migrateLegacy = -not (Test-Path -LiteralPath $targetData) -and
    -not $target.Equals($legacyRoot, [StringComparison]::OrdinalIgnoreCase) -and
    (Test-Path -LiteralPath $legacyData -PathType Container)
if ((Test-Path -LiteralPath $installedExe) -or $migrateLegacy) {
    $legacyExe = Join-Path $legacyRoot 'seriald.exe'
    $running = Get-CimInstance Win32_Process -Filter "Name='seriald.exe'" |
        Where-Object { $_.ExecutablePath -and
            ($_.ExecutablePath.Equals($installedExe, [StringComparison]::OrdinalIgnoreCase) -or
             ($migrateLegacy -and $_.ExecutablePath.Equals($legacyExe, [StringComparison]::OrdinalIgnoreCase))) }
    if ($running) { throw 'Stop the running seriald window before installing; its COM ports and data are still open.' }
}

$stage = Join-Path $env:TEMP ('seriald-install-' + [guid]::NewGuid().ToString('N'))
$backup = Join-Path $env:TEMP ('seriald-backup-' + [guid]::NewGuid().ToString('N'))
$mutationStarted = $false
$keepBackup = $false
$legacyCopyStarted = $false
try {
    Expand-Archive -LiteralPath $Package -DestinationPath $stage
    $bundle = Join-Path $stage 'seriald'
    if (-not (Test-Path -LiteralPath (Join-Path $bundle 'seriald.exe') -PathType Leaf)) {
        throw 'The ZIP is not a seriald Windows package.'
    }
    $digestFile = Join-Path $bundle 'seriald.exe.sha256'
    if (-not (Test-Path -LiteralPath $digestFile -PathType Leaf)) {
        throw 'The package has no executable checksum. Rebuild it before installing.'
    }
    $expected = (Get-Content -LiteralPath $digestFile -Raw).Trim()
    $actual = (Get-FileHash -LiteralPath (Join-Path $bundle 'seriald.exe') -Algorithm SHA256).Hash
    if ($expected -notmatch '^[0-9A-Fa-f]{64}$' -or $actual -ne $expected) {
        throw 'The package executable does not match its SHA-256 manifest.'
    }
    if ($ExpectedSha256 -and
        ($ExpectedSha256 -notmatch '^[0-9A-Fa-f]{64}$' -or $actual -ne $ExpectedSha256)) {
        throw "The package executable does not match the IT-approved SHA-256. Actual: $actual"
    }
    if (Test-Path -LiteralPath (Join-Path $bundle 'data')) {
        throw 'The package unexpectedly contains runtime data; refusing to replace installed data.'
    }
    New-Item -ItemType Directory -Path $target -Force | Out-Null
    New-Item -ItemType Directory -Path $backup | Out-Null
    $oldProgram = @(Get-ChildItem -LiteralPath $target -Force | Where-Object { $_.Name -ne 'data' })
    foreach ($item in $oldProgram) {
        Copy-Item -LiteralPath $item.FullName -Destination $backup -Recurse -Force
    }
    $mutationStarted = $true
    foreach ($item in $oldProgram) { Remove-Item -LiteralPath $item.FullName -Recurse -Force }
    foreach ($item in (Get-ChildItem -LiteralPath $bundle -Force)) {
        Copy-Item -LiteralPath $item.FullName -Destination $target -Recurse -Force
    }
    if ($migrateLegacy) {
        $legacyCopyStarted = $true
        Copy-Item -LiteralPath $legacyData -Destination $target -Recurse
        Write-Host "Copied previous runtime data from $legacyData"
    }
    New-Item -ItemType Directory -Path $targetData -Force | Out-Null
    Write-Host "Installed seriald to $target"
    Write-Host "seriald.exe SHA-256: $actual"
    Write-Host "Runtime data stays in $(Join-Path $target 'data')"
    Write-Host 'Start one of these files in the installed directory:'
    foreach ($name in @('Start-seriald.cmd', 'Start-seriald-LAN-Open.cmd', 'Start-seriald-LAN.cmd')) {
        Write-Host "  $(Join-Path $target $name)"
    }
} catch {
    $installError = $_
    if ($legacyCopyStarted -and (Test-Path -LiteralPath $targetData)) {
        Remove-Item -LiteralPath $targetData -Recurse -Force
    }
    if ($mutationStarted) {
        try {
            foreach ($item in (Get-ChildItem -LiteralPath $target -Force | Where-Object { $_.Name -ne 'data' })) {
                Remove-Item -LiteralPath $item.FullName -Recurse -Force
            }
            foreach ($item in (Get-ChildItem -LiteralPath $backup -Force)) {
                Copy-Item -LiteralPath $item.FullName -Destination $target -Recurse -Force
            }
            Write-Warning 'Update failed; previous program files restored. Runtime data was untouched.'
        } catch {
            $keepBackup = $true
            throw "Update failed and rollback failed: $installError; rollback: $_; backup: $backup"
        }
    }
    throw $installError
} finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
    if (-not $keepBackup -and (Test-Path -LiteralPath $backup)) { Remove-Item -LiteralPath $backup -Recurse -Force }
}
