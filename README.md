# netwatch

Homelab ping monitor + inventory + topology visualization, designed to run on a Raspberry Pi.

A single-file Python application (`monitor.py`) that serves a web dashboard for monitoring host availability, tracking network connections, and visualizing your homelab as a force-directed graph.

## Features

- Live ping monitoring with configurable intervals and timeouts
- Incident logging with ntfy push alerts
- Full inventory CMDB (hosts, VMs, network gear, UPS, disks, peripherals)
- Connection tracking (ethernet, WiFi, fiber, virtual, power, USB)
- Force-directed topology graph with VM clustering
- Kiosk fullscreen mode for dashboard displays
- XLSX import/export for inventory
- Network discovery via nmap
- Authenticated single-user (admin)

## Setup

1. Install dependencies:
```bash
   sudo apt install python3 python3-pip nmap sqlite3
   pip3 install pyyaml openpyxl
```

2. Copy `hosts.yaml.example` to `hosts.yaml` and configure your hosts.

3. Run:
```bash
   python3 monitor.py --no-tui --port 8080
```

4. First-run admin setup (must run from the Pi itself for security):
```bash
   curl -X POST http://localhost:8080/api/auth/setup \
     -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"yourpassword"}'
```

5. Open the dashboard at `http://<pi-ip>:8080`, log in.

## systemd

For production use, create `/etc/systemd/system/netwatch.service`:

```ini
[Unit]
Description=Netwatch homelab ping monitor
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

Then: `sudo systemctl enable --now netwatch`

## Development history

The `patches/` directory preserves the historical patch chain that built netwatch from earlier versions. Going forward, changes happen as direct commits to `monitor.py`.

## License

Personal project — not currently distributed.
