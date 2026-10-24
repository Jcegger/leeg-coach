# Re-binds the WSL -> Windows portproxy for the Live Client API (port 2999) and
# ensures the firewall rule exists. Needs admin; live.py launches it elevated
# (one UAC prompt) when it sees the proxy isn't listening, e.g. after a reboot.
$ErrorActionPreference = 'Continue'
$log = Join-Path $env:windir 'Temp\leeg_portproxy.log'
function Log($m) { "$(Get-Date -Format s) $m" | Out-File -Append -Encoding utf8 $log }

$ip = $null
for ($i = 0; $i -lt 30 -and -not $ip; $i++) {
    $ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
           Where-Object InterfaceAlias -like 'vEthernet (WSL*' |
           Select-Object -First 1).IPAddress
    if (-not $ip) { Start-Sleep -Seconds 2 }
}
if (-not $ip) { Log 'vEthernet (WSL) not found; is WSL running?'; exit 1 }

# Drop every existing :2999 v4tov4 rule (stale gateway IPs included), then re-add.
netsh interface portproxy show v4tov4 | Select-String '^\s*(\d+\.\d+\.\d+\.\d+)\s+2999\s' | ForEach-Object {
    $old = $_.Matches[0].Groups[1].Value
    netsh interface portproxy delete v4tov4 listenport=2999 listenaddress=$old | Out-Null
}
netsh interface portproxy add v4tov4 listenport=2999 listenaddress=$ip connectport=2999 connectaddress=127.0.0.1 | Out-Null

if (-not (Get-NetFirewallRule -DisplayName 'WSL LeagueLiveAPI' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName 'WSL LeagueLiveAPI' -Direction Inbound -Protocol TCP -LocalPort 2999 -Action Allow | Out-Null
    Log 'firewall rule recreated'
}
Log "portproxy bound on ${ip}:2999"
