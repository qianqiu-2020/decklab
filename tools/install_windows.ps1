#Requires -Version 5.1
param(
    [string]$Package,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'seriald')
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this installer on Windows.' }
$sourceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $Package) { $Package = Join-Path $sourceRoot 'dist\seriald-windows-portable.zip' }
if (-not (Test-Path -LiteralPath $Package -PathType Leaf)) {
    throw "Package not found: $Package. Run Build-seriald.cmd first."
}
$target = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$source = $sourceRoot.TrimEnd('\')
if ($target.Equals($source, [StringComparison]::OrdinalIgnoreCase) -or
    $target.StartsWith($source + '\', [StringComparison]::OrdinalIgnoreCase) -or
    $source.StartsWith($target + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'InstallDir must be separate from the source and build tree.'
}

$installedExe = Join-Path $target 'seriald.exe'
if (Test-Path -LiteralPath $installedExe) {
    $running = Get-CimInstance Win32_Process -Filter "Name='seriald.exe'" |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.Equals($installedExe, [StringComparison]::OrdinalIgnoreCase) }
    if ($running) { throw 'Stop the installed seriald window before updating; its COM ports are still open.' }
}

$stage = Join-Path $env:TEMP ('seriald-install-' + [guid]::NewGuid().ToString('N'))
$backup = Join-Path $env:TEMP ('seriald-backup-' + [guid]::NewGuid().ToString('N'))
$mutationStarted = $false
$keepBackup = $false
try {
    Expand-Archive -LiteralPath $Package -DestinationPath $stage
    $bundle = Join-Path $stage 'seriald'
    if (-not (Test-Path -LiteralPath (Join-Path $bundle 'seriald.exe') -PathType Leaf)) {
        throw 'The ZIP is not a seriald Windows package.'
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
    New-Item -ItemType Directory -Path (Join-Path $target 'data') -Force | Out-Null
    Write-Host "Installed seriald to $target"
    Write-Host "Runtime data stays in $(Join-Path $target 'data')"
    Write-Host 'Start one of: Start-seriald.cmd, Start-seriald-LAN-Open.cmd, Start-seriald-LAN.cmd (password).'
} catch {
    $installError = $_
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
