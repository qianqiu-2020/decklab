#Requires -Version 5.1
param([string]$Compiler)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this builder on Windows.' }
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$bundle = Join-Path $root 'dist\seriald'
$exe = Join-Path $bundle 'seriald.exe'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Build the Windows program bundle first.' }
if (Test-Path -LiteralPath (Join-Path $bundle 'data')) {
    throw 'The bundle contains runtime data; refusing to include it in the installer.'
}
$versionSource = Get-Content -LiteralPath (Join-Path $root 'seriald_core\version.py') -Raw
if ($versionSource -notmatch "__version__\s*=\s*'([0-9]+\.[0-9]+\.[0-9]+)'") { throw 'Package version was not found.' }
$version = $Matches[1]
$bundleVersion = Join-Path $bundle 'package-version.txt'
if (-not (Test-Path -LiteralPath $bundleVersion) -or (Get-Content -LiteralPath $bundleVersion -Raw).Trim() -ne $version) {
    throw 'Bundle version does not match the source. Rebuild with Build-seriald.cmd.'
}
$manifest = Join-Path $bundle 'seriald.exe.sha256'
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { throw 'Executable checksum is missing.' }
$expected = (Get-Content -LiteralPath $manifest -Raw).Trim()
$actual = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash
if ($expected -notmatch '^[0-9A-Fa-f]{64}$' -or $actual -ne $expected) { throw 'Executable checksum mismatch.' }
. (Join-Path $PSScriptRoot 'windows_installer_compiler.ps1')
$Compiler = Find-InnoSetupCompiler -ExplicitPath $Compiler
if (-not $Compiler) {
    throw 'ISCC.exe was not found in PATH, installation registry or standard directories. Pass -Compiler "D:\path\to\Inno Setup 6\ISCC.exe" to this script. The portable ZIP has already been built.'
}
Write-Host "Installer compiler: $Compiler"
$output = Join-Path $root 'dist'
& $Compiler "/DAppVersion=$version" "/DBundleDir=$bundle" "/DOutputDir=$output" (Join-Path $root 'packaging\seriald-windows.iss')
if ($LASTEXITCODE -ne 0) { throw 'Windows installer compilation failed.' }
$setup = Join-Path $output "seriald-$version-windows-x64-setup.exe"
if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) { throw 'Installer output is missing.' }
$hash = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash
Set-Content -LiteralPath "$setup.sha256" -Value $hash -Encoding ASCII
Write-Host "Built $setup"
Write-Host "Installer SHA-256: $hash"
Write-Host 'Run the setup EXE. Select your existing installation directory on the first upgrade; later upgrades remember it.'
