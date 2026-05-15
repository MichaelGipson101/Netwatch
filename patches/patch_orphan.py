#!/usr/bin/env python3
"""
netwatch patch: close orphaned incidents on host removal/IP change.

Bug: when a host's IP is changed in the editor (or a host is removed
entirely), any ongoing incident in the database for the old IP was left
dangling forever. The Topology and Hosts views would correctly show the
host with its new IP and live status, but the Events tab kept showing
the old incident as "ongoing" with the stale IP, never to be closed.

Root cause: reload_from_config keys hosts by IP. When you change a host's
IP, the old (oldIP -> HostState) entry doesn't match the new config, so a
new HostState is spawned for the new IP and the old one is stopped. The
incident_log was never told to close the incident for the dropped IP.

Fix:
  1. reload_from_config now closes any ongoing incident for IPs being
     stopped (covers IP-change AND deletion).
  2. load_initial reconciles at startup: closes any ongoing incident
     in the database whose host_ip is no longer in the config (covers
     the case where you edited hosts.yaml while netwatch was stopped).

Both fixes use the existing HistoryDB.close_incident() API, which
correctly stamps the ended_at timestamp and computes duration.

Must be applied AFTER patch_mobile.py.

Run once from ~/netwatch/:
    python3 patch_orphan_incidents.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_orphan.
Idempotent - safe to re-run.
"""

import os
import shutil
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_orphan"
SENTINEL = "_close_orphaned_incident"  # presence means already patched


PATCHES = [
    # ──── 1. IncidentLog: add a close-by-IP method that doesn't need a host obj.
    # The existing record_up() takes a host, but we need to close incidents for
    # hosts that no longer exist (so we have no host obj to pass).
    (
        '''    def record_up(self, host):
        if self.history_db:
            self.history_db.close_incident(host.ip)''',
        '''    def record_up(self, host):
        if self.history_db:
            self.history_db.close_incident(host.ip)

    def _close_orphaned_incident(self, ip):
        """Close any ongoing incident for an IP, used when a host has been
        removed from the config or had its IP changed. Unlike record_up, this
        doesn't need a HostState object."""
        if self.history_db:
            self.history_db.close_incident(ip)'''
    ),

    # ──── 2. reload_from_config: close incidents for IPs being stopped.
    # The block at the end of the function stops hosts whose IPs aren't in the
    # new config. We add an incident-close call there.
    (
        '''            for ip, existing in current_by_ip.items():
                if ip not in new_ips:
                    existing.stop_event.set()
            self.hosts = rebuilt''',
        '''            for ip, existing in current_by_ip.items():
                if ip not in new_ips:
                    existing.stop_event.set()
                    # Close any ongoing incident for this IP. Without this, the
                    # incident stays "ongoing" forever in the Events tab even
                    # though the host is no longer monitored.
                    if self.incident_log:
                        try:
                            self.incident_log._close_orphaned_incident(ip)
                        except Exception as e:
                            logging.warning(f"Could not close orphaned incident for {ip}: {e}")
            self.hosts = rebuilt'''
    ),

    # ──── 3. load_initial: reconcile orphaned incidents at startup.
    # If you edited hosts.yaml while netwatch was off, an incident might be
    # ongoing for an IP that no longer exists in the config. Clean it up.
    (
        '''    def load_initial(self, raw_hosts, default_interval):
        with self.lock:
            for h in raw_hosts:
                self.hosts.append(self._spawn(
                    h["name"], h["ip"], h.get("group", "General"),
                    h.get("interval", default_interval),
                    bool(h.get("always_on", True)),
                    h.get("specs") if isinstance(h.get("specs"), dict) else None,
                    h.get("notes", "") if isinstance(h.get("notes"), str) else "",
                    h.get("links") if isinstance(h.get("links"), dict) else None,
                    h.get("services") if isinstance(h.get("services"), list) else None,
                    bool(h.get("strict", False))
                ))''',
        '''    def load_initial(self, raw_hosts, default_interval):
        with self.lock:
            config_ips = set()
            for h in raw_hosts:
                config_ips.add(h["ip"])
                self.hosts.append(self._spawn(
                    h["name"], h["ip"], h.get("group", "General"),
                    h.get("interval", default_interval),
                    bool(h.get("always_on", True)),
                    h.get("specs") if isinstance(h.get("specs"), dict) else None,
                    h.get("notes", "") if isinstance(h.get("notes"), str) else "",
                    h.get("links") if isinstance(h.get("links"), dict) else None,
                    h.get("services") if isinstance(h.get("services"), list) else None,
                    bool(h.get("strict", False))
                ))
            # Reconcile orphans: any ongoing incident for an IP not in the
            # current config is stale (host was removed/renamed while netwatch
            # was offline). Close them so the Events tab is honest.
            if self.history_db and self.incident_log:
                try:
                    orphan_count = 0
                    for inc in self.history_db.list_incidents(limit=500):
                        if inc.get("ongoing") and inc.get("host_ip") not in config_ips:
                            self.incident_log._close_orphaned_incident(inc["host_ip"])
                            orphan_count += 1
                    if orphan_count:
                        logging.info(f"Reconciled {orphan_count} orphaned ongoing incident(s) at startup")
                except Exception as e:
                    logging.warning(f"Orphan reconciliation failed: {e}")'''
    ),
]


def main():
    if not os.path.isfile(TARGET):
        print(f"ERROR: {TARGET} not found.")
        sys.exit(1)

    content = open(TARGET).read()

    if SENTINEL in content:
        print(f"NOTE: '{SENTINEL}' found - patch already applied.")
        sys.exit(0)

    if "@media (max-width: 768px)" not in content:
        print("ERROR: This patch requires patch_mobile first.")
        sys.exit(1)

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    applied = 0
    for i, (old, new) in enumerate(PATCHES, 1):
        count = content.count(old)
        if count == 0:
            print(f"[FAIL] Patch #{i}: target not found")
            shutil.copy2(BACKUP, TARGET); sys.exit(1)
        if count > 1:
            print(f"[FAIL] Patch #{i}: matches {count}x")
            shutil.copy2(BACKUP, TARGET); sys.exit(1)
        content = content.replace(old, new, 1)
        applied += 1

    open(TARGET, "w").write(content)

    import ast
    try:
        ast.parse(open(TARGET).read())
        print("[OK] Resulting Python is valid")
    except SyntaxError as e:
        print(f"[FAIL] Syntax error: {e}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)

    print(f"[OK] Applied {applied} patches")
    print()
    print("Next steps:")
    print("  1. sudo systemctl restart netwatch")
    print("  2. Open the Events tab. Any orphaned ongoing incidents for IPs")
    print("     that no longer exist in your config will get closed at startup.")
    print("  3. Check the netwatch log for the reconciliation message:")
    print("     journalctl -u netwatch -n 30 | grep 'Reconciled'")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
