#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'windows-launchers\Get-decklab-LANFirewall.ps1')
$exe = 'C:\Example\decklab.exe'
$allow = @{Action=1; Protocol=6; Profiles=3; ApplicationName=$exe; InterfaceTypes='All';
           LocalAddresses='*'; RemoteAddresses='LocalSubnet'; LocalPorts='8897-8899'; RemotePorts='*'}
function Expect($Expected,$Rules,[int]$Port=8898) {
    $actual = Get-PortStatus $Rules $exe $Port 1 $true $false $false
    if ($actual -ne $Expected) { throw "Expected $Expected for $Port, got $actual" }
}
Expect Allowed @($allow)
Expect Missing @($allow) 8443
foreach ($change in @(@{ApplicationName='C:\Old\decklab.exe'},@{Profiles=4},@{Protocol=17})) {
    $other = $allow.Clone()
    foreach ($key in $change.Keys) { $other[$key] = $change[$key] }
    Expect Missing @($other)
}
$block = $allow.Clone()
$block.Action = 0
Expect Review @($allow,$block)
$restricted = $allow.Clone()
$restricted.RemoteAddresses = '192.0.2.5'
Expect Review @($restricted)
if ((Get-PortStatus @() $exe 8898 1 $false $false $false) -ne 'FirewallOff') { throw 'Disabled profile' }
if ((Get-PortStatus @($allow) $exe 8898 1 $true $true $false) -ne 'Review') { throw 'Blocked profile' }
Write-Output 'Windows LAN firewall rule checks passed'
