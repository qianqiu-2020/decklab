#Requires -Version 5.1
param(
    [ValidateRange(1,65535)][int[]]$Port = @(8898),
    [string]$Program = (Join-Path $PSScriptRoot 'seriald.exe')
)

function Test-PortList($Ports, [int]$Number) {
    foreach ($part in ($Ports -split ',')) {
        if ($part -eq '*' -or $part.Trim() -eq "$Number") { return $true }
        if ($part.Trim() -match '^(\d+)\s*-\s*(\d+)$' -and
            $Number -ge [int]$Matches[1] -and $Number -le [int]$Matches[2]) { return $true }
    }
    return $false
}

function Get-PortStatus($Rules, [string]$Exe, [int]$Number, [int]$Profile, [bool]$Enabled,
                        [bool]$BlockExceptions, [bool]$DefaultAllow) {
    if (!$Enabled) { return 'FirewallOff' }
    if ($BlockExceptions) { return 'Review' }
    $allowed = $DefaultAllow
    $scoped = $false
    foreach ($rule in $Rules) {
        if (!($rule.Profiles -band $Profile) -or $rule.Protocol -notin @(6,256)) { continue }
        if ($rule.ApplicationName -and [Environment]::ExpandEnvironmentVariables($rule.ApplicationName) -ne $Exe) { continue }
        if ($rule.ServiceName -or $rule.LocalAppPackageId) { continue }
        if ($rule.Protocol -eq 6 -and !(Test-PortList $rule.LocalPorts $Number)) { continue }
        # A matching block or restricted rule needs human inspection of its scope.
        # An unrelated application's rule must never suppress the setup tool.
        if ($rule.Action -eq 0) { return 'Review' }
        if ($rule.Action -ne 1) { continue }
        if ($rule.RemoteAddresses -notin @('*','LocalSubnet') -or $rule.LocalAddresses -ne '*' -or
            ($rule.Protocol -eq 6 -and $rule.RemotePorts -ne '*') -or $rule.Interfaces -or
            $rule.InterfaceTypes -ne 'All' -or $rule.SecureFlags -or $rule.LocalUserAuthorizedList -or
            $rule.RemoteUserAuthorizedList -or $rule.RemoteMachineAuthorizedList -or $rule.LocalUserOwner) {
            $scoped = $true
        } else {
            $allowed = $true
        }
    }
    if ($allowed) { return 'Allowed' }
    if ($scoped) { return 'Review' }
    return 'Missing'
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    $policy = New-Object -ComObject HNetCfg.FwPolicy2
    $mask = 0
    $adapters = @(Get-NetAdapter -Physical | Where-Object Status -eq 'Up')
    foreach ($connection in Get-NetConnectionProfile) {
        if ($connection.InterfaceIndex -in $adapters.ifIndex) {
            $mask = $mask -bor @{DomainAuthenticated=1; Private=2; Public=4}[[string]$connection.NetworkCategory]
        }
    }
    if (!$mask) { $mask = $policy.CurrentProfileTypes }
    if (!$mask) { throw 'No active Windows network profile.' }
    $rules = @($policy.Rules | Where-Object { $_.Enabled -and $_.Direction -eq 1 })
    foreach ($profile in 1,2,4) {
        if (!($mask -band $profile)) { continue }
        foreach ($number in ($Port | Sort-Object -Unique)) {
            $state = Get-PortStatus $rules $Program $number $profile ($policy.FirewallEnabled($profile)) `
                ($policy.BlockAllInboundTraffic($profile)) ($policy.DefaultInboundAction($profile) -eq 1)
            [pscustomobject]@{Profile=@{1='Domain';2='Private';4='Public'}[$profile]; TCP=$number; Status=$state}
        }
    }
}
