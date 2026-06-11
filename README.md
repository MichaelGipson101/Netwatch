# NETWATCH

> Homelab ping monitor · inventory CMDB · topology visualizer  
> Runs on a Raspberry Pi. Single Python file. No dependencies beyond stdlib + PyYAML + openpyxl.

---

## What it does

Netwatch watches your homelab 24/7 — pinging hosts, logging incidents, and serving a dark-mode web dashboard with live status, a full inventory database, and an interactive network topology graph.

```
192.168.6.90:8080  ←  open in any browser
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
- Responsive dark-mode UI; compact mode for small screens
- Kiosk / fullscreen mode
- Inventory table with sort, filter, type chips
- Host drawer with uptime history sparkline
- Inventory drawer with full record detail and edit

**Security**
- Session-based auth (login required for all routes and API)
- Sessions invalidated on user deletion and password change
- Brute-force lockout — persists across restarts via SQLite
- Rotating `monitor.log` (10MB × 3) records status transitions and warnings

---

## Setup

**1. Dependencies**
```bash
sudo apt install python3 python3-pip nmap sqlite3
pip3 install pyyaml openpyxl
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
User=mgipson
WorkingDirectory=/home/mgipson/netwatch
ExecStart=/usr/bin/python3 /home/mgipson/netwatch/monitor.py --no-tui --port 8080
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
monitor.py          — application core (~3,900 lines)
dashboard.html      — frontend shell (served by the Python server)
static/             — dashboard CSS/JS (main.css, core.js, topology.js, ...)
hosts.yaml          — host list (ping targets)
hosts.yaml.example  — template
tests/              — pytest suite
docs/               — design specs and implementation plans
```

Data is stored next to `monitor.py`: `netwatch.db` (ping history, daily rollups,
inventory, login lockouts), `auth.json` (users), `monitor.log` (rotating).

---

## Related

- **[Hearthboard](https://github.com/MichaelGipson101/hearthboard)** — a wall-mounted kiosk dashboard that reads Netwatch's API to display live network topology, host health, and incident history alongside weather and a clock. Runs on a Raspberry Pi; displayed on a Surface Pro 4 in fullscreen kiosk mode.

---

## License

Personal project — not currently distributed.
