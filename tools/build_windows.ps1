#Requires -Version 5.1
param([switch]$UseCurrentPython)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') {
    throw 'Windows portable packages must be built on Windows.'
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $root
try {
    $bundle = Join-Path $root 'dist\seriald'
    if (Test-Path (Join-Path $bundle 'data')) {
        throw 'dist\seriald\data contains runtime data. Move the running installation elsewhere before rebuilding; the build must not erase it.'
    }
    if ($UseCurrentPython) { & python -m venv .venv }
    else { & py -3.12 -m venv .venv }
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required (install it with the py launcher).' }
    $python = Join-Path $root '.venv\Scripts\python.exe'
    $pyinstaller = Join-Path $root '.venv\Scripts\pyinstaller.exe'

    & $python -m pip install -r requirements-windows.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & $python -m unittest discover -s tests -p test_windows_portable.py
    if ($LASTEXITCODE -ne 0) { throw 'Windows portable tests failed.' }
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
    Copy-Item docs\WINDOWS.md -Destination (Join-Path $bundle 'README.md')
    & (Join-Path $bundle 'seriald.exe') --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Packaged host launcher failed.' }
    & (Join-Path $bundle 'seriald.exe') ctl --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Packaged CLI launcher failed.' }

    $exeHash = (Get-FileHash -LiteralPath (Join-Path $bundle 'seriald.exe') -Algorithm SHA256).Hash
    Set-Content -LiteralPath (Join-Path $bundle 'seriald.exe.sha256') -Value $exeHash -Encoding ASCII

    $archive = Join-Path $root 'dist\seriald-windows-portable.zip'
    Compress-Archive -Path $bundle -DestinationPath $archive -Force
    Write-Host "Built $archive"
    Write-Host "seriald.exe SHA-256: $exeHash"
    Write-Host 'Run Install-seriald.cmd from the source tree to install/update outside the build directory.'
} finally {
    Pop-Location
}
