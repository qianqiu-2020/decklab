#Requires -Version 5.1
param([switch]$UseCurrentPython, [switch]$LocalFast, [switch]$FastCompression, [switch]$ValidateRuntime, [string]$ParamikoSource,
      [string]$FlashExecutor, [string]$FlashNotices, [string]$FlashLoaders,
      [string]$FlashDriver, [string]$FlashComponents, [string]$DesktopRuntime,
      [string]$LanFirewallTool, [string]$LanFirewallPolicy, [switch]$CoreOnly)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') {
    throw 'Windows installers must be built on Windows.'
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $DesktopRuntime -and -not $CoreOnly) {
    $DesktopRuntime = Join-Path $root 'build\desktop-runtime\windows-amd64'
}
if ($DesktopRuntime -and -not (Test-Path -LiteralPath (Join-Path $DesktopRuntime 'runtime-manifest.json') -PathType Leaf)) {
    throw 'A prepared desktop SDK runtime-manifest.json is required before building.'
}
if ($DesktopRuntime) { $DesktopRuntime = [System.IO.Path]::GetFullPath($DesktopRuntime) }
$desktopSnapshot = $null
if ($CoreOnly -and ($FlashExecutor -or $FlashNotices -or $FlashLoaders -or $FlashDriver -or $FlashComponents)) {
    throw 'CoreOnly cannot be combined with flashing components.'
}
if (-not $CoreOnly) {
    if ($FlashExecutor -or $FlashNotices -or $FlashLoaders -or $FlashDriver) {
        if (-not ($FlashExecutor -and $FlashNotices -and $FlashLoaders -and $FlashDriver) -or $FlashComponents) {
            throw 'Complete installation requires executor, notices, RAM materials and USB driver together.'
        }
    } else {
        if (-not $FlashComponents) { $FlashComponents = Join-Path $root 'build\install-components\windows-amd64\manifest.json' }
        if (-not (Test-Path -LiteralPath $FlashComponents -PathType Leaf)) {
            throw 'Complete standalone installation requires the reviewed component manifest in build\install-components\windows-amd64. CoreOnly is an explicit compatibility/CI build without flashing.'
        }
    }
}
if ($ParamikoSource) {
    $ParamikoSource = (Resolve-Path -LiteralPath $ParamikoSource).Path
    if (-not (Test-Path -LiteralPath $ParamikoSource -PathType Leaf) -or
        $ParamikoSource -notmatch '\.(tar\.gz|zip)$') {
        throw 'ParamikoSource must be a .tar.gz or .zip source archive.'
    }
}
$elapsed = [Diagnostics.Stopwatch]::StartNew()
function Write-Phase([string]$Name) {
    Write-Host ('Build time {0:N2}s: {1}' -f $elapsed.Elapsed.TotalSeconds, $Name)
}
Push-Location $root
try {
    if ($LanFirewallTool -or $LanFirewallPolicy) {
        if ($LanFirewallTool -notmatch '^[A-Za-z]:\\(?:[^\\":\r\n]+\\)*RunAsSpc\.exe$' -or
            $LanFirewallPolicy -notmatch '^[A-Za-z]:\\(?:[^\\":\r\n]+\\)*[^\\":\r\n]+\.spc$') {
            throw 'Supply absolute local paths to RunAsSpc.exe and the approved firewall .spc file.'
        }
    }
    if (-not $LocalFast) { & (Join-Path $PSScriptRoot 'test_windows_compiler_discovery.ps1') }
    $bundle = Join-Path $root 'dist\decklab'
    if (Test-Path (Join-Path $bundle 'data')) {
        throw 'dist\decklab\data contains runtime data. Move the running installation elsewhere before rebuilding; the build must not erase it.'
    }
    # The environment and dist tree belong to one build at a time.
    $buildDir = [IO.Directory]::CreateDirectory((Join-Path $root 'build'))
    $buildLock = [IO.File]::Open((Join-Path $buildDir.FullName 'package.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    $prepareArgs = @('tools/prepare_windows_build.py', $root)
    if ($LocalFast) {
        $prepareArgs += '--local-fast'
        if ($ParamikoSource) { $prepareArgs += @('--paramiko-source', $ParamikoSource) }
    }
    if ($UseCurrentPython) { $cacheKey = & python @prepareArgs }
    else { $cacheKey = & py -3.12 @prepareArgs }
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required (install it with the py launcher).' }
    Write-Phase 'Packaging environment ready'
    $python = Join-Path $root 'build\windows-venv\Scripts\python.exe'
    $pyinstaller = Join-Path $root 'build\windows-venv\Scripts\pyinstaller.exe'
    $desktopArguments = @()
    if ($DesktopRuntime) {
        $desktopSnapshot = Join-Path $root ('build\desktop-runtime-freeze-' + [Guid]::NewGuid().ToString('N'))
        & $python tools/package_desktop_runtime.py $DesktopRuntime --platform windows-amd64 --destination $desktopSnapshot | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Prepared desktop SDK validation failed.' }
        $desktopManifestHash = (Get-FileHash -LiteralPath (Join-Path $desktopSnapshot 'runtime-manifest.json') -Algorithm SHA256).Hash
        $desktopArguments = @('--add-data', ($desktopSnapshot + ';desktop-runtime/freerdp'))
    }
    if ($ParamikoSource) {
        $sourceSuffix = if ($ParamikoSource.ToLowerInvariant().EndsWith('.tar.gz')) { '.tar.gz' } else { '.zip' }
        $sourceSnapshot = Join-Path $root ('build\windows-venv\paramiko-source' + $sourceSuffix)
        Copy-Item -LiteralPath $ParamikoSource -Destination $sourceSnapshot
        $ParamikoSource = $sourceSnapshot
    }

    & $python -c 'import sys; sys.exit(0 if sys.maxsize > 2**32 else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'The Windows x64 package requires 64-bit Python.' }

    if ($cacheKey -ne 'cached') {
        & $python -m pip install --disable-pip-version-check --no-compile -r requirements-windows.txt
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
        if ($ParamikoSource) {
            & $python -m pip install --disable-pip-version-check --no-compile --no-deps --force-reinstall $ParamikoSource
            if ($LASTEXITCODE -ne 0) { throw 'Replacement Paramiko source installation failed.' }
        }
        if ($LocalFast) {
            Set-Content -LiteralPath (Join-Path $root 'build/windows-venv/decklab-dependencies.sha256') -Value $cacheKey -Encoding ASCII
        }
    } else { Write-Host 'Reusing local packaging dependencies.' }
    Write-Phase 'Packaging dependencies ready'
    & $python tools/prepare_terminal_runtime.py
    if ($LASTEXITCODE -ne 0) { throw 'Verified terminal runtime preparation failed.' }
    & $python tools/check_public_docs.py
    if ($LASTEXITCODE -ne 0) { throw 'Public API/configuration documentation inventory failed.' }
    if (-not $LocalFast) { & (Join-Path $PSScriptRoot 'test_windows_regressions.ps1') -Python $python }

    Write-Phase 'Source checks passed'
    $frozenRestored = $false
    if ($LocalFast) {
        $dependencyStamp = (Get-Content -LiteralPath (Join-Path $root 'build/windows-venv/decklab-dependencies.sha256') -Raw).Trim()
        $keyArgs = @('tools/windows_frozen_inputs.py', 'key', $bundle, '--key', $dependencyStamp)
        if ($desktopSnapshot) { $keyArgs += @('--desktop', $desktopSnapshot) }
        $frozenKey = (& $python @keyArgs).Trim()
        if ($LASTEXITCODE -ne 0) { throw 'Could not identify frozen runtime inputs.' }
        & $python tools/windows_frozen_inputs.py restore $bundle --key $frozenKey
        if ($LASTEXITCODE -notin @(0,2)) { throw 'Frozen runtime restore failed.' }
        $frozenRestored = $LASTEXITCODE -eq 0
    }
    if (-not $frozenRestored) {
        & $pyinstaller --noconfirm --clean --onedir --name decklab `
            --paths . --additional-hooks-dir tools/windows_hooks `
            --hidden-import decklabctl `
            --hidden-import decklab_mcp `
            --hidden-import decklab_core.backend_server `
            --collect-submodules mcp.server `
            --collect-submodules mcp.shared `
            --collect-submodules mcp.client `
            --collect-data mcp `
            --collect-data mcp_types `
            --collect-all uvicorn `
            --collect-all vncdotool `
            --collect-all aardwolf `
            --hidden-import unicrypto.backends.pycryptodomex `
            --collect-all Cryptodome `
            --hidden-import decklab_core.desktop_providers `
            --hidden-import decklab_core.desktop_freerdp `
            --recursive-copy-metadata mcp `
            --icon packaging/decklab.ico `
            @desktopArguments `
            tools/windows_entrypoint.py
        if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
        if ($DesktopRuntime) {
            & $python tools/package_desktop_runtime.py $bundle --platform windows-amd64 --prune-frozen-aliases | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'Frozen desktop SDK alias reconciliation failed.' }
            $packagedDesktop = Join-Path $bundle '_internal\desktop-runtime\freerdp'
            & $python tools/package_desktop_runtime.py $packagedDesktop --platform windows-amd64 | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'Frozen desktop SDK validation failed.' }
            if ((Get-FileHash -LiteralPath (Join-Path $packagedDesktop 'runtime-manifest.json') -Algorithm SHA256).Hash -ne $desktopManifestHash) {
                throw 'Desktop SDK changed while freezing.'
            }
        }
        if ($LocalFast) {
            & $python tools/windows_frozen_inputs.py save $bundle --key $frozenKey
            if ($LASTEXITCODE -ne 0) { throw 'Could not cache the frozen runtime.' }
        }
    }
    & $python tools/windows_frozen_inputs.py stage $bundle
    if ($LASTEXITCODE -ne 0) { throw 'Could not stage the current application resources.' }

    Write-Phase 'Runtime frozen and application resources staged'
    $noticeArguments = @('tools/collect_windows_notices.py', (Join-Path $bundle 'third-party'), '--bundle', $bundle)
    if ($ParamikoSource) { $noticeArguments += @('--paramiko-source', $ParamikoSource) }
    & $python @noticeArguments
    if ($LASTEXITCODE -ne 0) { throw 'Third-party notice collection failed; refusing to publish incomplete packages.' }
    if (-not $CoreOnly) {
        $componentArguments = @('tools/install_components.py', '--platform', 'windows-amd64', '--windows-bundle', $bundle)
        if ($FlashComponents) { $componentArguments += @('--manifest', $FlashComponents) }
        else { $componentArguments += @('--executor', $FlashExecutor, '--notices', $FlashNotices, '--loaders', $FlashLoaders, '--driver', $FlashDriver) }
        & $python @componentArguments
        if ($LASTEXITCODE -ne 0) { throw 'Complete standalone installation inputs failed validation.' }
    } else {
        Set-Content -LiteralPath (Join-Path $bundle 'installation.json') -Value '{"profile":"core"}' -Encoding ASCII
    }

    $launchers = Join-Path $root 'tools\windows-launchers'
    foreach ($name in @('Start-decklab.cmd', 'Start-decklab-LAN.cmd', 'Start-decklab-LAN.ps1',
                       'Get-decklab-LANFirewall.ps1', 'Start-decklab-LAN-Open.cmd', 'Start-decklab-LAN-Open.ps1',
                       'decklabctl.cmd', 'decklab-admin.cmd', 'Start-decklab-Web.cmd', 'Start-decklab-Backend.cmd')) {
        Copy-Item -LiteralPath (Join-Path $launchers $name) -Destination $bundle
    }
    $lanProfile = Join-Path $bundle 'lan-firewall.json'
    if ($LanFirewallTool) {
        @{tool=$LanFirewallTool; policy=$LanFirewallPolicy} | ConvertTo-Json |
            Set-Content -LiteralPath $lanProfile -Encoding UTF8
    } elseif (Test-Path -LiteralPath $lanProfile) { Remove-Item -LiteralPath $lanProfile }
    & $python tools/package_docs.py $bundle --platform windows
    if ($LASTEXITCODE -ne 0) { throw 'Installed documentation links failed validation.' }
    $version = (& $python -c 'from decklab_core.version import __version__; print(__version__)').Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the bundle version.' }
    Set-Content -LiteralPath (Join-Path $bundle 'package-version.txt') -Value $version -Encoding ASCII

    & (Join-Path $bundle 'decklab.exe') --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Packaged host launcher failed.' }
    if (-not $LocalFast -or $ValidateRuntime) {
        & (Join-Path $bundle 'decklab.exe') ctl --help | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Packaged CLI launcher failed.' }
        & (Join-Path $bundle 'decklab.exe') admin resolve-conflict --help | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Packaged offline administration launcher failed.' }
        & $python tools/test_windows_mcp.py --executable (Join-Path $bundle 'decklab.exe')
        if ($LASTEXITCODE -ne 0) { throw 'Packaged MCP worker protocol smoke failed.' }
        if ($DesktopRuntime) {
            & $python tools/test_desktop_security.py --executable (Join-Path $bundle 'decklab.exe')
            if ($LASTEXITCODE -ne 0) { throw 'Packaged desktop transport security regression failed.' }
        }
    }

    Write-Phase 'Selected packaged runtime checks passed'
    # A content-specific ICO path makes shortcuts independent of the EXE icon cache.
    $iconSource = Join-Path $root 'packaging\decklab.ico'
    $iconHash = (Get-FileHash -LiteralPath $iconSource -Algorithm SHA256).Hash.ToLowerInvariant()
    $iconName = 'app-icon-' + $iconHash.Substring(0, 12) + '.ico'
    Copy-Item -LiteralPath $iconSource -Destination (Join-Path $bundle $iconName)
    Set-Content -LiteralPath (Join-Path $bundle 'app-icon-name.txt') -Value $iconName -Encoding ASCII

    $exeHash = (Get-FileHash -LiteralPath (Join-Path $bundle 'decklab.exe') -Algorithm SHA256).Hash
    Set-Content -LiteralPath (Join-Path $bundle 'decklab.exe.sha256') -Value $exeHash -Encoding ASCII

    & $python tools/bundle_manifest.py $bundle
    if ($LASTEXITCODE -ne 0) { throw 'Complete bundle inventory failed.' }

    & (Join-Path $PSScriptRoot 'build_windows_installer.ps1') -LocalFast:($LocalFast -or $FastCompression)
    Write-Phase 'Windows installer complete'
} finally {
    if ($desktopSnapshot -and (Test-Path -LiteralPath $desktopSnapshot)) {
        Remove-Item -LiteralPath $desktopSnapshot -Recurse -Force
    }
    if ($buildLock) { $buildLock.Dispose() }
    Pop-Location
}
