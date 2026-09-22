#Requires -Version 5.1
param(
    [ValidateSet('Approve', 'Verify', 'Remove')][string]$Action = 'Approve',
    [string]$InstallDir = (Join-Path (Split-Path -Parent $PSScriptRoot) 'seriald-installed'),
    [ValidateRange(1, 65535)][int]$Port = 8898,
    [string]$ExpectedSha256,
    [string]$RemoteAddress = 'LocalSubnet'
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this script on Windows.' }
$admin = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $admin.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'An IT administrator must run this script in an elevated PowerShell window.'
}

$exe = [IO.Path]::GetFullPath((Join-Path $InstallDir 'seriald.exe'))
$ruleName = "seriald-lan-$Port"
if ($Action -eq 'Remove') {
    Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    Write-Host "Removed firewall rule $ruleName"
    return
}

if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw "Installed program not found: $exe" }
$actual = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToUpperInvariant()
if ($Action -eq 'Verify') {
    $rule = Get-NetFirewallRule -Name $ruleName -ErrorAction Stop
    $approved = [regex]::Match($rule.Description, 'SHA256=([0-9A-Fa-f]{64})').Groups[1].Value
    $application = $rule | Get-NetFirewallApplicationFilter
    if ($approved -ne $actual -or
        -not $application.Program.Equals($exe, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Firewall approval does not match the installed program. Current SHA-256: $actual"
    }
    Write-Host "Firewall approval matches $exe (SHA-256 $actual)"
    return
}

if ($ExpectedSha256 -notmatch '^[0-9A-Fa-f]{64}$') {
    throw 'IT must supply the independently reviewed 64-character SHA-256 as -ExpectedSha256.'
}
if ($actual -ne $ExpectedSha256.ToUpperInvariant()) {
    throw "SHA-256 mismatch for $exe. Actual: $actual"
}
$existing = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
if ($existing) { $existing | Remove-NetFirewallRule }
New-NetFirewallRule -Name $ruleName -DisplayName "seriald LAN TCP $Port" `
    -Description "Approved seriald.exe SHA256=$actual; reapprove after each upgrade" `
    -Direction Inbound -Action Allow -Enabled True -Profile Private,Domain `
    -Protocol TCP -LocalPort $Port -Program $exe -RemoteAddress $RemoteAddress `
    -EdgeTraversalPolicy Block | Out-Null
Write-Host "Approved $exe (SHA-256 $actual)"
Write-Host "Inbound TCP $Port for $RemoteAddress on Private/Domain networks; seriald runs without administrator rights."
