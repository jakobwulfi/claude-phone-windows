# Opens Windows Firewall for a desk phone on the LAN: SIP and call audio to FreeSWITCH, and the phonebook
# (live session list, DND keys) to the daemon. Private networks and the local subnet only.
# Run once from an admin PowerShell in the repo:  powershell -ExecutionPolicy Bypass -File scripts\firewall.ps1
# Run it again after changing sip_port or phonebook_port; it replaces its own rules ("claude-phone *").
#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw "No virtualenv at $python. Set up claude-phone first (README)." }

# The ports come from claude-phone's own config, so they can't drift from what FreeSWITCH and the daemon use.
# The code goes in on stdin: Windows PowerShell 5.1 strips the double quotes from a native command's arguments.
$json = @'
import json
from claude_phone import cli, config
s = config.load()
print(json.dumps({"sip": s.sip_port, "rtp": f"{cli.RTP_START}-{cli.RTP_END}", "phonebook": s.phonebook_port,
                  "freeswitch": s.freeswitch_exe}))
'@ | & $python -
# A failing native command doesn't stop the script ($ErrorActionPreference covers cmdlets only).
if ($LASTEXITCODE -ne 0 -or -not $json) { throw "Couldn't read claude-phone's settings (see the Python error above)." }
$settings = $json | ConvertFrom-Json

Get-NetFirewallRule -DisplayName 'claude-phone *' -ErrorAction SilentlyContinue | Remove-NetFirewallRule

$common = @{ Direction = 'Inbound'; Action = 'Allow'; Profile = 'Private'; RemoteAddress = 'LocalSubnet' }
New-NetFirewallRule @common -DisplayName 'claude-phone SIP' -Protocol UDP -LocalPort $settings.sip `
    -Program $settings.freeswitch | Out-Null
New-NetFirewallRule @common -DisplayName 'claude-phone call audio' -Protocol UDP -LocalPort $settings.rtp `
    -Program $settings.freeswitch | Out-Null
# No -Program: the venv's python.exe hands over to the base interpreter, which is what actually listens.
New-NetFirewallRule @common -DisplayName 'claude-phone phonebook' -Protocol TCP -LocalPort $settings.phonebook | Out-Null

Get-NetFirewallRule -DisplayName 'claude-phone *' |
    Format-Table DisplayName, Enabled, Profile, @{ n = 'Ports'; e = { ($_ | Get-NetFirewallPortFilter).LocalPort } } -AutoSize
