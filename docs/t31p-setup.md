# Yealink T31P setup

Below, `<phone-ip>` is the phone's address (press **OK** when idle to see it) and `<pc-ip>` is this PC's
(`lan_ip`: `phone status` warns if it moves; `lan_ip` in `~/.claude-phone/config.toml` pins it). `<sip-port>` is
`sip_port` from the same file: **5070** while Linphone runs on this PC (it holds 5060 itself), else 5060.

## 1. DHCP reservation (on the router)

Recommended. The PC's address matters most: FreeSWITCH binds to it and the phone registers to it.
Reserve it on the router (the PC's MAC is the "Physical Address" of its adapter in `ipconfig /all`), then pin it
in `~/.claude-phone/config.toml` as `lan_ip` and run `phone restart`. `phone status` and the daemon log warn if
it changes. Reserving the phone's address too is optional.

## 2. Windows Firewall

The phone reaches three things on the PC: SIP and call audio (FreeSWITCH) and the session list (the daemon). From
an **admin** PowerShell in the repo:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\firewall.ps1
```

It opens UDP `<sip-port>`, UDP 16384-16484 (audio) and TCP 8766 (phonebook), for private networks and the local
subnet only, and prints the rules. The PC's network must be **Private** (Settings → Network → your connection).
Run it again after changing `sip_port` or `phonebook_port`.

## 3. Factory reset

Worth doing on a second-hand phone, which may still point at an old provider's provisioning server.

1. With the phone idle, press and hold **OK** for about 10 seconds until it asks "Reset to factory?". Press **OK**.
   (Or: Menu → Settings → Advanced Settings, password `admin` → Reset Config → Reset to Factory.)
2. Wait for it to reboot (2-3 minutes). Check its IP: press **OK** once when idle. Note it as `<phone-ip>`.

## 4. Web admin

1. Open **https://\<phone-ip\>** and accept the self-signed certificate warning. Port 80 is off.
2. Log in as `admin` / `admin`. Set a new admin password when asked (Security → Password if not).

## 5. Stop old provisioning

**Settings → Auto Provision**
- PNP Active: **Off**
- DHCP Active: **Off**
- Server URL: **empty**
- Confirm.

## 6. Register as extension 200

Get the SIP password on the PC: `phone secret sip`

**Account → Register → Account 1**
| Field | Value |
|---|---|
| Line Active | Enabled |
| Label | Claude |
| Display Name | your name |
| Register Name | 200 |
| User Name | 200 |
| Password | *(from `phone secret sip`)* |
| SIP Server 1 | `<pc-ip>`, port `<sip-port>` |
| Transport | UDP |
| Outbound Proxy | Disabled |

Confirm. The status should change to **Registered** within a few seconds. On the PC, `phone status` should show
`phone 200  sofia/internal/sip:200@<phone-ip>…`. If it stays unregistered, check step 2 and that `<pc-ip>` is the
address in `phone status`.

## 7. Codecs (G.722 first)

**Account → Codec → Account 1**: enabled list in this order: **G722, PCMU, PCMA**. Move everything else to Disabled. Confirm.

## 8. Message key and light

**Account → Advanced → Account 1**
- Voice Mail: **500**
- Subscribe for MWI: **Disabled** (FreeSWITCH sends unsolicited notifications; this is the default)

Confirm. The power LED flashes red when there are questions waiting. (Linphone ignored these notifications in
testing, so this is the first real check of the light.)

## 9. Time

**Settings → Time & Date**: your Time Zone, NTP server `pool.ntp.org`. Confirm.

## 10. Test, and make the desk phone the one that rings

1. Lift the handset and dial **600**: you should hear yourself (echo test).
2. On the PC, set `ring_ext = "200"` in `~/.claude-phone/config.toml` (or remove the line; 200 is the default) and
   run `phone restart daemon`. From now on `call_<name>` rings the desk phone instead of Linphone. Linphone can stay
   registered as 201 for dialling in; turn its auto-answer off.
3. Dial **500**: you hear the voicemail menu.
4. Run through the call flows once more on the desk phone: dial a session (301), let Claude call you, voicemail,
   operator (0), all-hands (9).

## Notes

- **DND:** the phone rejects calls with code 480. `call_<name>` treats that as "not answered" and leaves a voicemail.
- **Caller ID length:** the T31P shows 17 characters, then scrolls the rest. `callerid_max` (17) trims the branch first, then the repo.

## 11. Live session list

**Claude soft key (live, use this).** The phone fetches the list on every press.
**Dsskey → Programmable Key**: on the soft key that was Directory, set Type **XML Browser**, Label `Claude`,
Value `http://<pc-ip>:8766/sessions.xml`. Confirm. Press it, pick a session, press Dial.

**Directory phonebook (cached).** Remote phonebooks refresh at most hourly, and nested folders are
prefetched with the top level, so this list goes stale. Kept for completeness (Menu → Directory → Claude):
**Directory → Remote Phone Book**: Remote URL 1 `http://<pc-ip>:8766/phonebook.xml`, Display Name 1 `Claude`.

Both URLs are on the LAN only (the daemon refuses anything outside `lan_cidr`). To check them before the phone
arrives, open `http://<pc-ip>:8766/sessions.xml` in a browser; it lists the live sessions.

## Ringtones

Each repo gets its own ringtone (1-8), sent as `Alert-Info: <http://127.0.0.1>;info=RingN`. The T31P plays it with no setup.
`phone list` shows each repo's ring. To pin one: `[repo_ringtones]` in `~/.claude-phone/config.toml`, e.g. `lemon-stand = 3`.

## 12. Screen messages (optional, for `screen_push`)

1. On the PC, open **https://\<phone-ip\>** in a browser. It warns that the connection is not private
   (the phone's own certificate): click **Advanced**, then **Proceed**.
2. Log in as `admin` with the password you set in step 4.
3. **Features** tab → **Remote Control** (left menu).
4. **Push XML Server IP Address**: `<pc-ip>`. Confirm.
5. Set `screen_push = true` in `~/.claude-phone/config.toml` and run `phone restart daemon`.

Summaries of calls then show on the phone's screen.

## 13. DND key (optional)

So the phone's DND key and the daemon agree: switching DND on holds Claude's calls, and switching it off releases
them. With meeting alerts on (README: "The phone's screen and meetings"), the PC also switches DND on for each
meeting, and switching it off ends that meeting's hold early.

1. **Features → Remote Control → Action URI Allow IP List**: `<pc-ip>`. Confirm.
2. **Features → Action URL**: Open DND `http://<pc-ip>:8766/dnd/on`, Close DND `http://<pc-ip>:8766/dnd/off`. Confirm.
3. On the PC, store the phone's admin password in Windows Credential Manager:
   `.venv\Scripts\python -m keyring set claude-phone desk-admin`, then type the password at the prompt. Without it,
   only step 2 works: the DND key still controls the hold, but the PC can't switch the phone's DND itself.

After a wrong password the phone locks its web login for a few minutes; the daemon stops trying after
one refusal until it restarts.
