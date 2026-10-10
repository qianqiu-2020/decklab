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
if (-not $Compiler) {
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) { $Compiler = $command.Source }
    else {
        foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, $env:LOCALAPPDATA)) {
            if (-not $base) { continue }
            foreach ($subdir in @('Inno Setup 6', 'Programs\Inno Setup 6')) {
                $candidate = Join-Path $base "$subdir\ISCC.exe"
                if (Test-Path -LiteralPath $candidate -PathType Leaf) { $Compiler = $candidate; break }
            }
            if ($Compiler) { break }
        }
    }
}
if (-not $Compiler -or -not (Test-Path -LiteralPath $Compiler -PathType Leaf)) {
    throw 'Install Inno Setup 6 first: winget install --id JRSoftware.InnoSetup -e -s winget -i. For ZIP only, use Build-seriald.cmd -PortableOnly.'
}
if (-not (Test-Path -LiteralPath (Join-Path (Split-Path $Compiler) 'Languages\ChineseSimplified.isl'))) {
    throw 'Inno Setup 6.5 or later with the bundled Chinese language is required.'
}
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
