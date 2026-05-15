#!/usr/bin/env python3
"""
netwatch patch: backup download button.

Adds an admin-only "Download backup" button to the nav. Clicking it
bundles your entire netwatch state into a tarball and streams it back
as a browser download:

  netwatch-backup-<hostname>-<isodate>.tar.gz
    netwatch/monitor.py        - so you don't need to rerun all patches
    netwatch/hosts.yaml        - your config
    netwatch/auth.json         - users + secret key (mode 0600 preserved)
    netwatch/netwatch.db       - consistent SQLite snapshot
    netwatch/metadata.json     - manifest for restore-time verification

The SQLite snapshot uses sqlite3's online .backup API, so it's
consistent and safe to take while netwatch is actively writing pings.

This patch ships only the button. Companion scripts (cron-based remote
backup to TrueNAS, restore script, fresh-Pi bootstrap) are designed but
not part of this patch - we'll add them later if desired.

Must be applied AFTER patch_inventory_styling.py.

Run once from ~/netwatch/:
    python3 patch_backup.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_backup.
Idempotent - safe to re-run.
"""

import os
import shutil
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_backup"
SENTINEL = "create_backup_tarball"  # presence means already patched


# ─── New backend module: backup helpers ───────────────────────────────────────

NEW_BACKUP_MODULE = '''# ============================================================================
# Backup
# ============================================================================

BACKUP_MANIFEST_VERSION = 1


def create_backup_tarball(config_path, auth_path):
    """Build a complete backup tarball in memory and return (bytes, filename).

    The SQLite snapshot uses sqlite3's online .backup API for consistency.
    Other files are read normally.

    `config_path` is the path to hosts.yaml; the netwatch directory and
    db path are derived from it. `auth_path` is the auth.json location
    (may not exist yet if no users are configured).
    """
    import io
    import json as _json
    import sqlite3
    import socket
    import tarfile
    import tempfile
    from datetime import datetime as _dt

    netwatch_dir = os.path.dirname(os.path.abspath(config_path))
    db_path      = os.path.join(netwatch_dir, "netwatch.db")
    monitor_path = os.path.join(netwatch_dir, "monitor.py")
    hostname     = socket.gethostname() or "unknown"
    iso_now      = _dt.now().strftime("%Y-%m-%dT%H-%M-%S")
    filename     = f"netwatch-backup-{hostname}-{iso_now}.tar.gz"

    manifest = {
        "manifest_version": BACKUP_MANIFEST_VERSION,
        "netwatch_version": VERSION,
        "created_at":       int(time.time()),
        "created_iso":      _dt.now().isoformat(),
        "source_hostname":  hostname,
        "files":            {},
    }

    # 1) Make a consistent SQLite snapshot to a temp file. We can\'t
    # tar.add() the live db directly because WAL writes might be active.
    snapshot_path = None
    if os.path.isfile(db_path):
        fd, snapshot_path = tempfile.mkstemp(prefix="nw_backup_", suffix=".db")
        os.close(fd)
        try:
            src = sqlite3.connect(db_path)
            try:
                dst = sqlite3.connect(snapshot_path)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        except Exception:
            # If snapshot fails for any reason, clean up and re-raise
            try: os.unlink(snapshot_path)
            except OSError: pass
            raise

    # 2) Build the tarball in memory.
    try:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            def _add(real_path, arcname, mode_override=None):
                if not os.path.isfile(real_path):
                    return
                ti = tar.gettarinfo(real_path, arcname=arcname)
                if mode_override is not None:
                    ti.mode = mode_override
                # Strip uid/gid - they\'re meaningless across machines
                ti.uid = 0; ti.gid = 0
                ti.uname = ""; ti.gname = ""
                with open(real_path, "rb") as f:
                    tar.addfile(ti, f)
                manifest["files"][os.path.basename(arcname)] = os.path.getsize(real_path)

            _add(monitor_path, "netwatch/monitor.py")
            _add(config_path,  "netwatch/hosts.yaml")
            _add(auth_path,    "netwatch/auth.json", mode_override=0o600)
            if snapshot_path:
                # Snapshot lands under the original db filename
                ti = tar.gettarinfo(snapshot_path, arcname="netwatch/netwatch.db")
                ti.uid = 0; ti.gid = 0
                ti.uname = ""; ti.gname = ""
                with open(snapshot_path, "rb") as f:
                    tar.addfile(ti, f)
                manifest["files"]["netwatch.db"] = os.path.getsize(snapshot_path)

            # Manifest goes in last so all file sizes are populated
            manifest_bytes = _json.dumps(manifest, indent=2).encode("utf-8")
            ti = tarfile.TarInfo(name="netwatch/metadata.json")
            ti.size = len(manifest_bytes)
            ti.mtime = int(time.time())
            ti.mode = 0o644
            tar.addfile(ti, io.BytesIO(manifest_bytes))

        return buf.getvalue(), filename, manifest
    finally:
        # Always clean up the snapshot file, even if tar.add fails
        if snapshot_path:
            try: os.unlink(snapshot_path)
            except OSError: pass


# ============================================================================
# Wake-on-LAN
# ============================================================================'''


# ─── Patches ──────────────────────────────────────────────────────────────────

def main():
    if not os.path.isfile(TARGET):
        print(f"ERROR: {TARGET} not found.")
        sys.exit(1)

    content = open(TARGET).read()

    if SENTINEL in content:
        print(f"NOTE: '{SENTINEL}' found - patch already applied.")
        sys.exit(0)

    if "inv-edit-form" not in content:
        print("ERROR: This patch requires patch_inventory_styling first.")
        sys.exit(1)

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    # ── 1. Insert backup module before "# Wake-on-LAN" anchor
    OLD = '''# ============================================================================
# Wake-on-LAN
# ============================================================================'''
    if content.count(OLD) != 1:
        print(f"[FAIL] WoL anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW_BACKUP_MODULE, 1)
    print("[OK] Inserted backup module")

    # ── 2. Add /api/backup POST endpoint. Anchor on the existing /api/auth/users
    # POST handler since that's a clean, unique anchor.
    OLD = '''            if self.path == "/api/auth/users":
                # Create a new user (admin only)
                if not self._require_auth(admin_only=True):
                    return'''
    NEW = '''            if self.path == "/api/backup":
                # Bundle entire netwatch state into a tar.gz and stream it back.
                # Admin-only because the bundle contains auth.json (password hashes).
                if not self._require_auth(admin_only=True):
                    return
                try:
                    auth_path_local = auth_manager.path if auth_manager else None
                    data, filename, manifest = create_backup_tarball(config_path, auth_path_local)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/gzip")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Content-Disposition",
                                     f'attachment; filename="{filename}"')
                    self.send_header("X-Netwatch-Backup-Version", str(manifest["manifest_version"]))
                    self.send_header("X-Netwatch-Source", manifest["source_hostname"])
                    self.end_headers()
                    self.wfile.write(data)
                    logging.info(f"Backup downloaded: {filename} ({len(data)} bytes)")
                except Exception as e:
                    logging.exception("Backup failed")
                    self._send_json(500, {"error": f"backup failed: {e}"})
                return

            if self.path == "/api/auth/users":
                # Create a new user (admin only)
                if not self._require_auth(admin_only=True):
                    return'''
    if content.count(OLD) != 1:
        print(f"[FAIL] /api/auth/users POST anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added POST /api/backup endpoint")

    # ── 3. Frontend: extend updateAuthUI() to add a Backup button when admin
    OLD = '''  if(_authState.logged_in){
    navAuth.innerHTML = '<span style="color:var(--muted);font-size:11px">' + escapeHtml(_authState.username) + (_authState.admin ? ' (admin)' : '') + '</span>'
      + '<button class="btn btn-ghost" onclick="logout()" style="font-size:11px;padding:4px 10px">Log out</button>';
  } else {'''
    NEW = '''  if(_authState.logged_in){
    const adminTools = _authState.admin
      ? '<button class="btn btn-ghost" onclick="downloadBackup()" style="font-size:11px;padding:4px 10px" title="Download a complete backup tarball">Download backup</button>'
      : '';
    navAuth.innerHTML = '<span style="color:var(--muted);font-size:11px">' + escapeHtml(_authState.username) + (_authState.admin ? ' (admin)' : '') + '</span>'
      + adminTools
      + '<button class="btn btn-ghost" onclick="logout()" style="font-size:11px;padding:4px 10px">Log out</button>';
  } else {'''
    if content.count(OLD) != 1:
        print(f"[FAIL] updateAuthUI anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added Backup button to nav")

    # ── 4. Add downloadBackup() JS function. Anchor on logout() to land near
    # the other auth-related JS.
    OLD = '''async function logout(){
  await fetch('/api/auth/logout', { method: 'POST' });
  _authState = { logged_in: false, username: null, admin: false, setup_required: false };
  updateAuthUI();
}'''
    NEW = '''async function logout(){
  await fetch('/api/auth/logout', { method: 'POST' });
  _authState = { logged_in: false, username: null, admin: false, setup_required: false };
  updateAuthUI();
}

async function downloadBackup(){
  // Trigger the backup endpoint and let the browser handle the download.
  // We use fetch instead of a plain link so we can show progress + errors.
  const btn = event && event.target;
  const origText = btn ? btn.textContent : null;
  if(btn){ btn.disabled = true; btn.textContent = 'Building...'; }
  try {
    const res = await fetch('/api/backup', { method: 'POST' });
    if(!res.ok){
      let msg = 'Backup failed (HTTP ' + res.status + ')';
      try { const j = await res.json(); if(j.error) msg = j.error; } catch(e){}
      alert(msg);
      return;
    }
    const blob = await res.blob();
    // Get filename from Content-Disposition header, or fall back
    const dispo = res.headers.get('Content-Disposition') || '';
    const m = dispo.match(/filename="?([^";]+)"?/);
    const filename = m ? m[1] : 'netwatch-backup.tar.gz';
    // Trigger a download
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch(e){
    alert('Backup failed: ' + e.message);
  } finally {
    if(btn){ btn.disabled = false; btn.textContent = origText || 'Download backup'; }
  }
}'''
    if content.count(OLD) != 1:
        print(f"[FAIL] logout() anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added downloadBackup() JS")

    # ── 5. Bump version + footer
    if 'VERSION = "3.11"' in content:
        content = content.replace('VERSION = "3.11"', 'VERSION = "3.12"', 1)
    content = content.replace('netwatch v3.11 - raspberry pi', 'netwatch v3.12 - raspberry pi', 1)
    print("[OK] Version bumped to 3.12")

    open(TARGET, "w").write(content)

    import ast
    try:
        ast.parse(open(TARGET).read())
        print("[OK] Resulting Python is valid")
    except SyntaxError as e:
        print(f"[FAIL] Syntax error: {e}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)

    print()
    print("Next steps:")
    print("  1. sudo systemctl restart netwatch")
    print("  2. Refresh dashboard - if logged in as admin, you'll see")
    print("     'Download backup' next to your username in the nav.")
    print("  3. Click it. A tarball downloads named:")
    print("     netwatch-backup-<your-pi-hostname>-<timestamp>.tar.gz")
    print("  4. Inspect with:  tar -tzf netwatch-backup-*.tar.gz")
    print("     You'll see: monitor.py, hosts.yaml, auth.json, netwatch.db,")
    print("     and metadata.json")
    print("  5. Save it somewhere safe (NAS, USB drive, cloud, etc.)")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
