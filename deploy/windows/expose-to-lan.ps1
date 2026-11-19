#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Expose the WSL2 Docker gateway (127.0.0.1:8642 inside WSL) on this PC's LAN IP,
  reachable only from the reverse-proxy host (e.g. Nginx Proxy Manager on a Raspberry Pi).

.DESCRIPTION
  The Docker stack runs in WSL2 (NAT networking) and the `gateway` container is
  published on 127.0.0.1:8642. WSL localhost forwarding already makes that
  reachable from Windows as http://127.0.0.1:8642, so we only need:
    1. a Windows port proxy  <LAN IP>:8642  ->  127.0.0.1:8642
    2. an inbound firewall rule for that port, limited to the proxy host's IP.
  This avoids touching .env (AOE2_BIND stays 127.0.0.1) and survives WSL IP changes.

.EXAMPLE
  .\expose-to-lan.ps1 -ListenAddress 192.168.1.188 -ProxyHost 192.168.1.20
.EXAMPLE
  .\expose-to-lan.ps1 -ListenAddress 192.168.1.188 -Remove
#>
param(
    [Parameter(Mandatory)] [string] $ListenAddress,
    [string] $ProxyHost,
    [int]    $Port = 8642,
    [switch] $Remove
)

$ErrorActionPreference = 'Stop'
$ruleName = "AoE2 gateway $Port (reverse proxy only)"

if ($Remove) {
    netsh interface portproxy delete v4tov4 listenaddress=$ListenAddress listenport=$Port | Out-Null
    Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    Write-Host "Removed port proxy and firewall rule for ${ListenAddress}:$Port"
    return
}

if (-not $ProxyHost) { throw "-ProxyHost (the reverse proxy's LAN IP) is required unless -Remove is given." }

if ((Get-Service iphlpsvc).Status -ne 'Running') { Start-Service iphlpsvc }

try {
    $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://127.0.0.1:$Port/health"
    Write-Host "Gateway reachable on Windows localhost: HTTP $($r.StatusCode)"
} catch {
    throw "Gateway is not reachable at http://127.0.0.1:$Port/health from Windows. Start the stack in WSL first (docker compose up -d)."
}

netsh interface portproxy delete v4tov4 listenaddress=$ListenAddress listenport=$Port 2>$null | Out-Null
netsh interface portproxy add v4tov4 listenaddress=$ListenAddress listenport=$Port connectaddress=127.0.0.1 connectport=$Port | Out-Null

Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP `
    -LocalAddress $ListenAddress -LocalPort $Port -RemoteAddress $ProxyHost -Profile Any | Out-Null

netsh interface portproxy show v4tov4
Write-Host "Done. From the proxy host run:  curl -i http://${ListenAddress}:$Port/health"
