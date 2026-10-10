#Requires -Version 5.1
# Shared compiler discovery; keep it separate so tests never invoke an installer.
function Get-InnoSetupInstallLocations {
    foreach ($base in @('HKCU:\Software', 'HKLM:\Software',
                        'HKCU:\Software\WOW6432Node', 'HKLM:\Software\WOW6432Node')) {
        $key = "$base\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"
        $entry = Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue
        if ($entry -and $entry.InstallLocation) {
            [Environment]::ExpandEnvironmentVariables($entry.InstallLocation.Trim('"'))
        }
    }
}

function Find-InnoSetupCompiler([string]$ExplicitPath) {
    if ($ExplicitPath) {
        if (-not (Test-Path -LiteralPath $ExplicitPath -PathType Leaf)) {
            throw "Compiler does not exist: $ExplicitPath"
        }
        return (Resolve-Path -LiteralPath $ExplicitPath).Path
    }
    $command = Get-Command ISCC.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($command -and (Test-Path -LiteralPath $command.Source -PathType Leaf)) { return $command.Source }
    foreach ($location in @(Get-InnoSetupInstallLocations)) {
        $candidate = Join-Path $location 'ISCC.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    $bases = @(${env:ProgramFiles(x86)}, $env:ProgramFiles, $env:ProgramW6432, $env:LOCALAPPDATA)
    if ($env:USERPROFILE) { $bases += Join-Path $env:USERPROFILE 'AppData\Local' }
    foreach ($base in $bases) {
        if (-not $base) { continue }
        foreach ($subdir in @('Inno Setup 6', 'Programs\Inno Setup 6')) {
            $candidate = Join-Path $base "$subdir\ISCC.exe"
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        }
    }
    return $null
}
