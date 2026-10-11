#Requires -Version 5.1
param([switch]$UseCurrentPython, [switch]$PortableOnly)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') {
    throw 'Windows portable packages must be built on Windows.'
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $root
try {
    & (Join-Path $PSScriptRoot 'test_windows_compiler_discovery.ps1')
    $bundle = Join-Path $root 'dist\seriald'
    if (Test-Path (Join-Path $bundle 'data')) {
        throw 'dist\seriald\data contains runtime data. Move the running installation elsewhere before rebuilding; the build must not erase it.'
    }
    if ($UseCurrentPython) { & python -m venv .venv }
    else { & py -3.12 -m venv .venv }
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required (install it with the py launcher).' }
    $python = Join-Path $root '.venv\Scripts\python.exe'
    $pyinstaller = Join-Path $root '.venv\Scripts\pyinstaller.exe'

    & $python -c 'import sys; sys.exit(0 if sys.maxsize > 2**32 else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'The Windows x64 package requires 64-bit Python.' }

    & $python -m pip install -r requirements-windows.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & $python -m unittest discover -s tests -p test_windows_portable.py
    if ($LASTEXITCODE -ne 0) { throw 'Windows portable tests failed.' }
    & $python -m unittest discover -s tests -p test_session_contract.py
    if ($LASTEXITCODE -ne 0) { throw 'Shared SSH lifecycle and archive tests failed.' }
    & $python -m unittest discover -s tests -p test_transactions.py
    if ($LASTEXITCODE -ne 0) { throw 'Console transaction tests failed.' }
    & $python -m unittest discover -s tests -p test_profiles.py
    if ($LASTEXITCODE -ne 0) { throw 'Configuration tests failed.' }
    & $python -m unittest discover -s tests -p test_demo.py
    if ($LASTEXITCODE -ne 0) { throw 'Hardware-free demo tests failed.' }
    if (Get-Command node -ErrorAction SilentlyContinue) {
        & node tests/frontend.test.js
        if ($LASTEXITCODE -ne 0) { throw 'Frontend regression tests failed.' }
        & node tests/share.test.js
        if ($LASTEXITCODE -ne 0) { throw 'Agent share UI tests failed.' }
    } else {
        Write-Warning 'Node.js is unavailable; frontend regression tests were not run.'
    }

    & $pyinstaller --noconfirm --clean --onedir --name seriald `
        --hidden-import serialctl `
        --icon packaging/decklab.ico `
        --add-data 'index.html;.' `
        --add-data 'static;static' `
        --add-data 'vendor;vendor' `
        --add-data 'serialctl.py;.' `
        --add-data 'skills/seriald-board/SKILL.md;skills/seriald-board' `
        seriald_windows.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }

    $launchers = Join-Path $root 'tools\windows-launchers'
    foreach ($name in @('Start-seriald.cmd', 'Start-seriald-LAN.cmd', 'Start-seriald-LAN.ps1',
                       'Start-seriald-LAN-Open.cmd', 'Start-seriald-LAN-Open.ps1', 'serialctl.cmd')) {
        Copy-Item -LiteralPath (Join-Path $launchers $name) -Destination $bundle
    }
    foreach ($notice in @('LICENSE', 'NOTICE', 'THIRD_PARTY.md')) {
        Copy-Item -LiteralPath (Join-Path $root $notice) -Destination $bundle
    }
    Copy-Item docs\WINDOWS.md -Destination (Join-Path $bundle 'README.md')
    New-Item -ItemType Directory -Force -Path (Join-Path $bundle 'docs'), (Join-Path $bundle 'examples') | Out-Null
    Copy-Item docs\CONFIGURATION.md -Destination (Join-Path $bundle 'docs\CONFIGURATION.md')
    Copy-Item docs\DEMO.md -Destination (Join-Path $bundle 'docs\DEMO.md')
    Copy-Item docs\CONSOLE_TRANSACTIONS.md -Destination (Join-Path $bundle 'docs\CONSOLE_TRANSACTIONS.md')
    Copy-Item docs\STORAGE_UPGRADES.md -Destination (Join-Path $bundle 'docs\STORAGE_UPGRADES.md')
    Copy-Item examples\profiles.json -Destination (Join-Path $bundle 'examples\profiles.json')
    & (Join-Path $bundle 'seriald.exe') --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Packaged host launcher failed.' }
    & (Join-Path $bundle 'seriald.exe') ctl --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Packaged CLI launcher failed.' }

    # A content-specific ICO path makes shortcuts independent of the EXE icon cache.
    $iconSource = Join-Path $root 'packaging\decklab.ico'
    $iconHash = (Get-FileHash -LiteralPath $iconSource -Algorithm SHA256).Hash.ToLowerInvariant()
    $iconName = 'app-icon-' + $iconHash.Substring(0, 12) + '.ico'
    Copy-Item -LiteralPath $iconSource -Destination (Join-Path $bundle $iconName)
    Set-Content -LiteralPath (Join-Path $bundle 'app-icon-name.txt') -Value $iconName -Encoding ASCII

    $exeHash = (Get-FileHash -LiteralPath (Join-Path $bundle 'seriald.exe') -Algorithm SHA256).Hash
    Set-Content -LiteralPath (Join-Path $bundle 'seriald.exe.sha256') -Value $exeHash -Encoding ASCII

    $version = (& $python -c 'from seriald_core.version import __version__; print(__version__)').Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the bundle version.' }
    Set-Content -LiteralPath (Join-Path $bundle 'package-version.txt') -Value $version -Encoding ASCII

    $archive = Join-Path $root 'dist\seriald-windows-portable.zip'
    Compress-Archive -Path $bundle -DestinationPath $archive -Force
    Write-Host "Built $archive"
    Write-Host "seriald.exe SHA-256: $exeHash"
    if (-not $PortableOnly) { & (Join-Path $PSScriptRoot 'build_windows_installer.ps1') }
    if ($PortableOnly) { Write-Host 'ZIP only: run Install-seriald.cmd to install/update.' }
    else { Write-Host 'Distribute the setup EXE for installation and future upgrades.' }
} finally {
    Pop-Location
}
