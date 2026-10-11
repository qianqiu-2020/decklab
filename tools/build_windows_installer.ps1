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
$notices = Join-Path $bundle 'third-party\inventory.json'
if (-not (Test-Path -LiteralPath $notices -PathType Leaf)) {
    throw 'Third-party inventory is missing. Rebuild with Build-seriald.cmd.'
}
$noticePython = Join-Path $root 'build\windows-venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $noticePython -PathType Leaf)) {
    throw 'The packaging interpreter is missing. Rebuild with Build-seriald.cmd.'
}
& $noticePython (Join-Path $PSScriptRoot 'collect_windows_notices.py') (Join-Path $bundle 'third-party') --verify
if ($LASTEXITCODE -ne 0) { throw 'Third-party notice inventory validation failed.' }
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
$iconManifest = Join-Path $bundle 'app-icon-name.txt'
if (-not (Test-Path -LiteralPath $iconManifest -PathType Leaf)) {
    throw 'The bundle has no shortcut icon. Rebuild the program with Build-seriald.cmd, then install the new setup EXE.'
}
$iconName = (Get-Content -LiteralPath $iconManifest -Raw).Trim()
if ($iconName -notmatch '^app-icon-[0-9a-f]{12}\.ico$') { throw 'Invalid shortcut icon manifest.' }
$bundleIcon = Join-Path $bundle $iconName
$sourceIcon = Join-Path $root 'packaging\decklab.ico'
if (-not (Test-Path -LiteralPath $bundleIcon -PathType Leaf) -or
    (Get-FileHash -LiteralPath $bundleIcon -Algorithm SHA256).Hash -ne
    (Get-FileHash -LiteralPath $sourceIcon -Algorithm SHA256).Hash) {
    throw 'The bundle icon is missing or outdated. Rebuild with Build-seriald.cmd.'
}
$output = Join-Path $root 'dist'
& $Compiler "/DAppVersion=$version" "/DBundleDir=$bundle" "/DOutputDir=$output" "/DAppIcon=$iconName" (Join-Path $root 'packaging\seriald-windows.iss')
if ($LASTEXITCODE -ne 0) { throw 'Windows installer compilation failed.' }
$setup = Join-Path $output "seriald-$version-windows-x64-setup.exe"
if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) { throw 'Installer output is missing.' }
$hash = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash
Set-Content -LiteralPath "$setup.sha256" -Value $hash -Encoding ASCII
Write-Host "Built $setup"
Write-Host "Installer SHA-256: $hash"
Write-Host 'Run the setup EXE. Select your existing installation directory on the first upgrade; later upgrades remember it.'
