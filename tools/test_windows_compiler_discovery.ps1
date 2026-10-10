#Requires -Version 5.1
# Filesystem/registry fixtures only; runs on PowerShell without Windows or Inno.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'windows_installer_compiler.ps1')
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('seriald-iscc-' + [guid]::NewGuid().ToString('N'))
$registered = @{}
$pathCompiler = $null
function Get-ItemProperty($LiteralPath, $ErrorAction) { return $registered[$LiteralPath] }
function Get-Command($Name, $CommandType, $ErrorAction) {
    if ($pathCompiler) { return [pscustomobject]@{ Source = $pathCompiler } }
}
function Make-Compiler([string]$Directory) {
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    $file = Join-Path $Directory 'ISCC.exe'
    Set-Content -LiteralPath $file -Value 'fixture only' -Encoding ASCII
    return $file
}
function Assert-Path([string]$Expected) {
    $actual = Find-InnoSetupCompiler
    if ($actual -ne $Expected) { throw "Expected '$Expected', found '$actual'" }
}
$envNames = @('ProgramFiles(x86)', 'ProgramFiles', 'ProgramW6432', 'LOCALAPPDATA', 'USERPROFILE')
$saved = @{}
try {
    foreach ($name in $envNames) {
        $saved[$name] = [Environment]::GetEnvironmentVariable($name)
        [Environment]::SetEnvironmentVariable($name, $null)
    }
    New-Item -ItemType Directory -Path $fixture | Out-Null
    $custom = Make-Compiler (Join-Path $fixture 'custom path with spaces')
    foreach ($base in @('HKCU:\Software', 'HKLM:\Software',
                        'HKCU:\Software\WOW6432Node', 'HKLM:\Software\WOW6432Node')) {
        $registered.Clear()
        $registered["$base\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"] =
            [pscustomobject]@{ InstallLocation = (Split-Path $custom) }
        Assert-Path $custom
    }
    # A stale registry entry must not hide the valid standard directory.
    $registered.Clear()
    $registered['HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1'] =
        [pscustomobject]@{ InstallLocation = (Join-Path $fixture 'removed installation') }
    $env:LOCALAPPDATA = Join-Path $fixture 'local'
    $perUser = Make-Compiler (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6')
    Assert-Path $perUser
    $pathCompiler = $custom
    Assert-Path $custom
    if ((Find-InnoSetupCompiler -ExplicitPath $perUser) -ne $perUser) { throw 'Explicit compiler did not take precedence.' }
    $pathCompiler = $null
    Remove-Item -LiteralPath $perUser
    $env:ProgramW6432 = Join-Path $fixture 'native programs'
    $native = Make-Compiler (Join-Path $env:ProgramW6432 'Inno Setup 6')
    Assert-Path $native
    Remove-Item -LiteralPath $native
    if ($null -ne (Find-InnoSetupCompiler)) { throw 'Missing compiler was not reported.' }
    $rejected = $false
    try { Find-InnoSetupCompiler -ExplicitPath (Join-Path $fixture 'missing.exe') | Out-Null }
    catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid explicit compiler was accepted.' }
    Write-Host 'Compiler discovery passed: registry views, custom/user/native paths, PATH, stale entries and explicit override.'
} finally {
    foreach ($name in $envNames) { [Environment]::SetEnvironmentVariable($name, $saved[$name]) }
    if (Test-Path -LiteralPath $fixture) { Remove-Item -LiteralPath $fixture -Recurse -Force }
}
