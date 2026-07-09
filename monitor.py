#!/usr/bin/env python3
"""
netwatch - Homelab ping monitor with btop-style TUI, web dashboard,
and web-based hosts.yaml editor. (Version: see netwatch/__init__.py.)

Usage:
    python monitor.py                  # TUI + web server
    python monitor.py --no-tui         # Headless + web server (for systemd)
    python monitor.py --no-web         # TUI only, no web
    python monitor.py --port 8080      # Custom port

Config: hosts.yaml in the same directory.
Dashboard: http://<pi-ip>:8080
"""

from netwatch.__main__ import main

if __name__ == "__main__":
    main()
