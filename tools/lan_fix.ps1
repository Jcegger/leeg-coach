# Makes the leeg page reachable from a phone/tablet on the home network (`leeg --lan`).
# Forwards <this PC's LAN IP>:<Port> to the page inside WSL (<WslIp>:<Port>) and allows it
# through the firewall for Private networks / the local subnet only. Needs admin; live.py
# launches it elevated (one UAC prompt) when the rule is missing or WSL's IP changed.
# Listens on the LAN IP only, never 0.0.0.0: a 0.0.0.0 rule would also grab Windows'
# own localhost:<Port>, which is how the desktop browser reaches WSL (same trap as :2999).
param([int]$Port = 8765, [string]$WslIp)
$ErrorActionPreference = 'Continue'
$log = Join-Path $env:windir 'Temp\leeg_lan.log'
$out = Join-Path $env:windir 'Temp\leeg_lan.txt'
function Log($m) { "$(Get-Date -Format s) $m" | Out-File -Append -Encoding utf8 $log }

if (-not $WslIp) { Log 'no WSL IP given'; exit 1 }
# The adapter with a default route that isn't WSL/Hyper-V: the home network.
$cfg = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.InterfaceAlias -notlike 'vEthernet*' } |
       Select-Object -First 1
$lan = $cfg.IPv4Address.IPAddress | Select-Object -First 1
if (-not $lan) { Log 'no LAN adapter with a default gateway'; exit 1 }

# Drop old :$Port rules on any non-loopback address (stale LAN or WSL IPs), then add ours.
netsh interface portproxy show v4tov4 | Select-String "^\s*(\d+\.\d+\.\d+\.\d+)\s+$Port\s" | ForEach-Object {
    $old = $_.Matches[0].Groups[1].Value
    if ($old -ne '127.0.0.1' -and $old -ne '0.0.0.0') {
        netsh interface portproxy delete v4tov4 listenport=$Port listenaddress=$old | Out-Null
    }
}
netsh interface portproxy add v4tov4 listenport=$Port listenaddress=$lan connectport=$Port connectaddress=$WslIp | Out-Null

$name = "leeg page (LAN $Port)"
Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $name -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow `
    -Profile Private -RemoteAddress LocalSubnet | Out-Null

$lan | Out-File -Encoding ascii $out
Log "forwarding ${lan}:$Port -> ${WslIp}:$Port (network: $($cfg.NetProfile.NetworkCategory))"
if ($cfg.NetProfile.NetworkCategory -ne 'Private') {
    Log "network '$($cfg.NetProfile.Name)' is $($cfg.NetProfile.NetworkCategory), not Private: the firewall rule won't apply"
}
