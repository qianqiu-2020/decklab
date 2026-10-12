#Requires -Version 5.1
param([switch]$UseCurrentPython, [switch]$PortableOnly, [string]$ParamikoSource)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') {
    throw 'Windows portable packages must be built on Windows.'
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ($ParamikoSource) {
    $ParamikoSource = (Resolve-Path -LiteralPath $ParamikoSource).Path
    if (-not (Test-Path -LiteralPath $ParamikoSource -PathType Leaf) -or
        $ParamikoSource -notmatch '\.(tar\.gz|zip)$') {
        throw 'ParamikoSource must be a .tar.gz or .zip source archive.'
    }
}
Push-Location $root
try {
    & (Join-Path $PSScriptRoot 'test_windows_compiler_discovery.ps1')
    $bundle = Join-Path $root 'dist\seriald'
    if (Test-Path (Join-Path $bundle 'data')) {
        throw 'dist\seriald\data contains runtime data. Move the running installation elsewhere before rebuilding; the build must not erase it.'
    }
    if ($UseCurrentPython) { & python tools/prepare_windows_build.py $root }
    else { & py -3.12 tools/prepare_windows_build.py $root }
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required (install it with the py launcher).' }
    $python = Join-Path $root 'build\windows-venv\Scripts\python.exe'
    $pyinstaller = Join-Path $root 'build\windows-venv\Scripts\pyinstaller.exe'
    if ($ParamikoSource) {
        $sourceSuffix = if ($ParamikoSource.ToLowerInvariant().EndsWith('.tar.gz')) { '.tar.gz' } else { '.zip' }
        $sourceSnapshot = Join-Path $root ('build\windows-venv\paramiko-source' + $sourceSuffix)
        Copy-Item -LiteralPath $ParamikoSource -Destination $sourceSnapshot
        $ParamikoSource = $sourceSnapshot
    }

    & $python -c 'import sys; sys.exit(0 if sys.maxsize > 2**32 else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'The Windows x64 package requires 64-bit Python.' }

    & $python -m pip install -r requirements-windows.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    if ($ParamikoSource) {
        & $python -m pip install --no-deps --force-reinstall $ParamikoSource
        if ($LASTEXITCODE -ne 0) { throw 'Replacement Paramiko source installation failed.' }
    }
    & $python tools/check_public_docs.py
    if ($LASTEXITCODE -ne 0) { throw 'Public API/configuration documentation inventory failed.' }
    & $python -m unittest discover -s tests -p test_request_json.py
    if ($LASTEXITCODE -ne 0) { throw 'Request JSON validation tests failed.' }
    & $python -m unittest discover -s tests -p test_collector_injection.py
    if ($LASTEXITCODE -ne 0) { throw 'Collector initialization/isolation tests failed.' }
    & $python -m unittest discover -s tests -p test_serial_log.py
    if ($LASTEXITCODE -ne 0) { throw 'Serial archive writer tests failed.' }
    & $python -m unittest discover -s tests -p test_windows_build_env.py
    if ($LASTEXITCODE -ne 0) { throw 'Packaging environment isolation tests failed.' }
    & $python -m unittest discover -s tests -p test_windows_portable.py
    if ($LASTEXITCODE -ne 0) { throw 'Windows portable tests failed.' }
    & $python -m unittest discover -s tests -p test_session_contract.py
    if ($LASTEXITCODE -ne 0) { throw 'Shared SSH lifecycle and archive tests failed.' }
    & $python -m unittest discover -s tests -p test_protocol.py
    if ($LASTEXITCODE -ne 0) { throw 'Console dialogue/framing tests failed.' }
    & $python -m unittest discover -s tests -p test_transactions.py
    if ($LASTEXITCODE -ne 0) { throw 'Console transaction tests failed.' }
    & $python -m unittest discover -s tests -p test_profiles.py
    if ($LASTEXITCODE -ne 0) { throw 'Configuration tests failed.' }
    foreach ($suite in @('test_cli.py', 'test_schema.py', 'test_tasks.py', 'test_task_actions.py',
                         'test_transfer_artifacts.py', 'test_file_tasks.py',
                         'test_transfer_tasks.py', 'test_input_ownership.py',
                         'test_models.py', 'test_maintenance.py', 'test_agent_grants.py',
                         'test_ui_language.py', 'test_boot_compare.py', 'test_sse.py')) {
        & $python -m unittest discover -s tests -p $suite
        if ($LASTEXITCODE -ne 0) { throw "Native storage/task/collaboration tests failed: $suite" }
    }
    & $python -m unittest discover -s tests -p test_demo.py
    if ($LASTEXITCODE -ne 0) { throw 'Hardware-free demo tests failed.' }
    & $python -m unittest discover -s tests -p test_process_windows.py
    if ($LASTEXITCODE -ne 0) { throw 'Windows helper Job Object tests failed.' }
    & $python -m unittest discover -s tests -p test_serial_handoff.py
    if ($LASTEXITCODE -ne 0) { throw 'Serial handoff regression tests failed' }
    & $python -m unittest discover -s tests -p test_package_docs.py
    if ($LASTEXITCODE -ne 0) { throw 'Installed documentation tests failed.' }
    & $python -m unittest discover -s tests -p test_windows_notices.py
    if ($LASTEXITCODE -ne 0) { throw 'Windows distribution notice tests failed.' }
    & $python -m unittest discover -s tests -p test_bundle_manifest.py
    if ($LASTEXITCODE -ne 0) { throw 'Complete bundle integrity tests failed.' }
    & $python -m unittest discover -s tests -p test_hardware.py
    if ($LASTEXITCODE -ne 0) { throw 'Configured hardware task tests failed.' }
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

    $noticeArguments = @('tools/collect_windows_notices.py', (Join-Path $bundle 'third-party'), '--bundle', $bundle)
    if ($ParamikoSource) { $noticeArguments += @('--paramiko-source', $ParamikoSource) }
    & $python @noticeArguments
    if ($LASTEXITCODE -ne 0) { throw 'Third-party notice collection failed; refusing to publish incomplete packages.' }

    $launchers = Join-Path $root 'tools\windows-launchers'
    foreach ($name in @('Start-seriald.cmd', 'Start-seriald-LAN.cmd', 'Start-seriald-LAN.ps1',
                       'Start-seriald-LAN-Open.cmd', 'Start-seriald-LAN-Open.ps1', 'serialctl.cmd')) {
        Copy-Item -LiteralPath (Join-Path $launchers $name) -Destination $bundle
    }
    & $python tools/package_docs.py $bundle --platform windows
    if ($LASTEXITCODE -ne 0) { throw 'Installed documentation links failed validation.' }
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

    & $python tools/bundle_manifest.py $bundle
    if ($LASTEXITCODE -ne 0) { throw 'Complete bundle inventory failed.' }
    & $python tools/bundle_manifest.py $bundle --verify
    if ($LASTEXITCODE -ne 0) { throw 'Complete bundle integrity validation failed.' }

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
