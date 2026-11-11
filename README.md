# netmon

Home internet monitor for a Cisco Meraki MX. Logs outages, packet loss, latency, bandwidth use, and real speed tests to SQLite, and sends push (and email) alerts through [ntfy](https://ntfy.sh).

Runs on **macOS** (launchd) or **Raspberry Pi / Linux** (systemd). Python 3.8+ standard library only.

## What it collects

| Data | Source | Default interval |
|---|---|---|
| Up/down state | local ping to 1.1.1.1, 8.8.8.8, 9.9.9.9 | 10 s |
| WAN loss, latency, jitter | Meraki API (backfills 30 days on first run) | 10 min |
| Bandwidth used | Meraki API | 10 min |
| Uplink status, public IP changes | Meraki API | 10 min |
| Download/upload speed | `networkQuality` (macOS) or `speedtest-cli` (Linux) | 60 min |

Outages are classified as **WAN** (router reachable, internet not) or **LAN** (router unreachable).

## Alerts

- "Internet is back" with the outage duration, queued during the outage and sent on recovery
- Public IP changes (often an ISP line reset)
- Slow speed tests, if you set your plan's speeds
- A daily report

Push alerts go to an ntfy topic. Set `email` to also get recovery alerts and the daily report by email through ntfy.

## Install

1. Create a Meraki API key (Dashboard > My profile > API access). Read-only access is enough.
2. `cp netmon.example.ini netmon.ini` and set `serial` to your MX serial, plus `ntfy_url` and optionally `email`. Use a long random ntfy topic: anyone who knows it can read and send your alerts.
3. Install. The script detects the OS:
   - macOS: `./install.sh`
   - Raspberry Pi / Linux: `sudo ./install.sh`
4. Run `netmon test-alert` to confirm notifications arrive.

The installer asks for the API key and stores it outside the repo (`~/.meraki_key` on macOS, `/etc/netmon/env` on Linux).

## Usage

```
netmon report [--days N] [--sync]     outages, uptime, latency, data used, speeds
netmon export outages > outages.csv   CSV export: outages, speedtests, loss_latency, usage, events
netmon test-alert                     send a test notification
```

Logs: `tail -f ~/Library/Logs/netmon.log` (macOS) or `journalctl -u netmon -f` (Linux).
