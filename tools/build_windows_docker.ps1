#Requires -Version 5.1
param()

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this builder on Windows.' }
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$osType = (& docker info --format '{{.OSType}}' 2>$null).Trim()
if ($LASTEXITCODE -ne 0 -or $osType -ne 'windows') {
    throw 'Docker Desktop must be running in Windows containers mode.'
}
$image = 'seriald-windows-builder:local'
$archive = Join-Path $root 'dist\seriald-windows-portable.zip'
$oldBuildKit = $env:DOCKER_BUILDKIT
$container = $null
try {
    # Docker's classic builder supports Windows containers more consistently.
    $env:DOCKER_BUILDKIT = '0'
    & docker build --isolation=hyperv -f (Join-Path $root 'tools\Dockerfile.windows') -t $image $root
    if ($LASTEXITCODE -ne 0) { throw 'Windows container build failed.' }
    $container = (& docker create $image).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $container) { throw 'Could not create the build container.' }
    New-Item -ItemType Directory -Path (Join-Path $root 'dist') -Force | Out-Null
    & docker cp "${container}:C:/src/dist/seriald-windows-portable.zip" $archive
    if ($LASTEXITCODE -ne 0) { throw 'Could not copy the package from the build container.' }
    $versionSource = Get-Content -LiteralPath (Join-Path $root 'seriald_core\version.py') -Raw
    if ($versionSource -notmatch "__version__\s*=\s*'([0-9]+\.[0-9]+\.[0-9]+)'") { throw 'Package version was not found.' }
    $setupName = "seriald-$($Matches[1])-windows-x64-setup.exe"
    foreach ($name in @($setupName, "$setupName.sha256")) {
        & docker cp "${container}:C:/src/dist/$name" (Join-Path $root "dist\$name")
        if ($LASTEXITCODE -ne 0) { throw "Could not copy $name from the build container." }
    }
    Write-Host "Built $(Join-Path $root "dist\$setupName")"
    Write-Host "Built $archive"
    Write-Host "Package SHA-256: $((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash)"
} finally {
    if ($container) { & docker rm $container | Out-Null }
    $env:DOCKER_BUILDKIT = $oldBuildKit
}
