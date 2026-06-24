# NETWATCH

![Python 3](https://img.shields.io/badge/python-3-blue?logo=python&logoColor=white)
![Single file](https://img.shields.io/badge/deploy-single%20file-success)
![Raspberry Pi](https://img.shields.io/badge/runs%20on-Raspberry%20Pi-c51a4a?logo=raspberrypi&logoColor=white)
![No web framework](https://img.shields.io/badge/dependencies-stdlib%20%2B%20PyYAML%20%2B%20openpyxl-informational)
![License: MIT](https://img.shields.io/badge/license-MIT-yellow)

> Homelab ping monitor · inventory CMDB · topology visualizer · Proxmox/TrueNAS dashboard  
> Runs on a Raspberry Pi. Single Python file. No dependencies beyond stdlib + PyYAML + openpyxl.

---

## What it does

Netwatch watches your homelab 24/7 — pinging hosts, logging incidents, polling Proxmox and TrueNAS, and serving a web dashboard (dark or light) with live status, a full inventory database, an interactive network topology graph, and an AI assistant that can answer questions about all of it.

```
<pi-ip>:8080  ←  open in any browser
```

---

## Features

**Monitoring**
- Continuous ICMP ping with configurable intervals and timeouts
- Up / Down / Idle / Degraded / Pending status per host
- Uptime percentage tracked with sparkbar visualization
- Latency history charts (1h-7d) and 60-day daily uptime strip per host
- Incident log with timestamps and duration
- Push alerts via [ntfy](https://ntfy.sh)

**Servers tab (Proxmox / TrueNAS)**
- Pill toggle between Proxmox and TrueNAS panels
- Proxmox: per-node cards (CPU/RAM bars, uptime), guest table (VMs + LXCs) with live CPU/RAM, start/stop/reboot actions, and a link dot back to the matching inventory host
- TrueNAS: pool health metrics, vdev layout (including cache/log/spare/special/dedup devices), replication task status, and next-scrub estimate (accounts for the pool's own threshold setting and the NAS's configured timezone, not just its cron schedule)
- TrueNAS Alerts card: surfaces TrueNAS's own alert feed (WARNING and above), with an admin-only Dismiss button per alert category (silenced permanently via `hosts.yaml`'s `truenas_ignored_alert_klasses` setting) and an Acknowledge button for one-off per-instance dismissal proxied straight to TrueNAS's own alert API
- Background pollers (60s Proxmox, 900s TrueNAS) drive alerting via ntfy
- Credentials managed through the Settings panel / config wizard, stored server-side in `auth.json`

**Mira (AI assistant)**
- Chat bubble (bottom corner) backed by [OpenRouter](https://openrouter.ai) free-tier models (Llama, DeepSeek, Gemma, Nemotron)
- Context — hosts, Proxmox nodes/guests, TrueNAS pool/replication status — is built fresh on each message send, not on opening the panel
- Status ring reflects live network health (nominal / advisory / warning / critical)
- Usage modal for tracking OpenRouter key spend

**Wake-on-LAN**
- Send a magic packet to any host with a MAC address on record, from the host card or inventory drawer
- Subnet broadcast address auto-detected

**Inventory CMDB**
- 9 device types, each with type-specific fields:

  | Type | Icon | Fields |
  |---|---|---|
  | Host | 🖥 | CPU, RAM, OS, architecture, TPM |
  | VM | ⬜ | Hypervisor, vCPU, RAM/disk allocation |
  | Network | 🔲 | Port count, PoE budget, managed, uplink |
  | UPS | 🔋 | Capacity (VA/Wh), runtime, battery age |
  | Disk | 💾 | Capacity, interface, RPM, health |
  | Peripheral | 🔌 | Subtype, model |
  | Tablet | 📱 | Subtype, model |
  | Phone | 📱 | Subtype, model |
  | Printer | 🖨 | Subtype, model |

- Arbitrary connection edges (ethernet, WiFi, fiber, virtual, power, USB)
- XLSX import / export
- Network discovery via nmap
- Link inventory records to monitored hosts for live status in tables

**Topology graph**
- Force-directed D3.js graph — nodes, edges, clusters
- VM clustering with host grouping
- Per-type SVG icons inside nodes (9 icon types)
- Per-type fill colors; status glow (green up, amber degraded, red down)
- Pan, zoom, drag; fit-to-view button
- Tooltips with live status

**Dashboard**
- Responsive dark-mode UI with first-class light mode; compact mode for small screens
- Kiosk / fullscreen mode
- Toast notifications, full keyboard accessibility (focus trap, Enter/Space activation, ARIA states)
- PWA-installable with status-aware favicon (alert variant when a host is down)
- Inventory table with sort, filter, type chips
- Host drawer with uptime history sparkline
- Inventory drawer with full record detail and edit
- Settings panel + setup wizard for credentials (Proxmox, TrueNAS, OpenRouter, ntfy) and Proxmox TLS verification options (verify toggle + CA cert path)
- Settings > System tab with a Restart netwatch button (re-execs the process in place, no SSH needed)
- Self-hosted fonts and D3 — no CDN calls, works on a fully isolated LAN

**Security**
- Session-based auth (login required for all routes and API)
- Sessions invalidated on user deletion and password change
- Brute-force lockout — persists across restarts via SQLite
- Rotating `monitor.log` (10MB × 3) records status transitions and warnings

---

## Quick Install

```bash
curl -fsSL https://raw.githubusercontent.com/MichaelGipson101/Netwatch/main/install.sh | bash
```

This installs dependencies, clones the repo, seeds `hosts.yaml`, optionally restores from a backup tarball if you have one, and sets up the systemd service — then open the dashboard URL it prints and complete the first-run admin setup. See below for what it's doing under the hood, or to do it by hand.

---

## Setup

**1. Dependencies**
```bash
sudo apt install python3 nmap sqlite3 python3-yaml python3-openpyxl
```

**2. Configure hosts**
```bash
cp hosts.yaml.example hosts.yaml
# edit hosts.yaml — add your IPs, names, groups
```

**3. Run**
```bash
python3 monitor.py --no-tui --port 8080
```

**4. Create admin account** *(must run from the Pi itself)*
```bash
curl -X POST http://localhost:8080/api/auth/setup \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"yourpassword"}'
```

**5. Open the dashboard**
```
http://<pi-ip>:8080
```

---

## Run as a service

Create `/etc/systemd/system/netwatch.service`:

```ini
[Unit]
Description=Netwatch homelab monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<your-user>
WorkingDirectory=/home/<your-user>/netwatch
ExecStart=/usr/bin/python3 /home/<your-user>/netwatch/monitor.py --no-tui --port 8080
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=netwatch

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now netwatch
```

Logs: `journalctl -u netwatch -f`  
HTTP access log: `tail -f monitor.log`

---

## Files

```
monitor.py          — application core (~5,200 lines)
dashboard.html      — frontend shell (served by the Python server)
static/             — dashboard CSS/JS (main.css, core.js, topology.js, inventory.js,
                       proxmox.js, nas.js, ai-panel.js, settings.js, auth.js, utils.js,
                       vendored D3 + self-hosted fonts, icons/manifest)
hosts.yaml          — host list (ping targets)
hosts.yaml.example  — template
tests/              — pytest suite
docs/               — design specs and implementation plans
```

Data is stored next to `monitor.py`: `netwatch.db` (ping history, daily rollups,
inventory, login lockouts), `auth.json` (users + Proxmox/TrueNAS/OpenRouter/ntfy
credentials), `monitor.log` (rotating).

**Security notes:**
- The OpenRouter API key never reaches the browser — Mira's chat and usage
  lookups are proxied server-side through `/api/ai/chat` and `/api/ai/usage`.
- Proxmox API calls verify TLS certificates by default. If your Proxmox host
  uses its stock self-signed cert, either give it a real one (Datacenter ->
  ACME) or point the `proxmox_ca_cert` setting at Proxmox's own CA file
  (e.g. `/etc/pve/pve-root-ca.pem`) — see `hosts.yaml.example`, or set it from
  the Settings panel directly. Setting `proxmox_verify_ssl: false` (also a
  Settings panel toggle) disables verification entirely if needed.
  Note: Proxmox's own cluster CA commonly omits the X.509 Key Usage
  extension, which OpenSSL 3.x's strict validation policy would otherwise
  reject even with the correct CA pinned — netwatch already relaxes that one
  specific check for a pinned Proxmox CA, so this should just work.
- All `POST` requests require an `X-CSRF-Token` header matching a token
  issued in the `/api/auth/login` or `/api/auth/setup` response body
  (`csrf_token` field). The dashboard's own JS handles this automatically;
  any external script calling the API must capture `csrf_token` at login
  and send it back on every mutating request.

---

## Related

- **[Hearthboard](https://github.com/MichaelGipson101/hearthboard)** — a wall-mounted kiosk dashboard that reads Netwatch's API to display live network topology, host health, and incident history alongside weather and a clock. Runs on a Raspberry Pi; displayed on a Surface Pro 4 in fullscreen kiosk mode.

---

## License

[MIT](LICENSE)
