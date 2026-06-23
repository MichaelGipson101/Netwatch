# Install/Redeploy Story Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `--restore` mode to `monitor.py` for restoring data from a backup tarball, and an `install.sh` bootstrap script, so netwatch can be redeployed onto a fresh machine (the user's own disaster recovery) or installed fresh by a friend, without manual dependency/systemd wrangling.

**Architecture:** `restore_backup()` is a new pure function in `monitor.py` (alongside the existing `create_backup_tarball()`) that extracts `hosts.yaml`/`auth.json`/`netwatch.db` from a backup tarball; a new `--restore`/`--force` CLI flag pair in `main()` calls it and exits before any server setup happens. `install.sh` is a standalone shell script that handles the OS-level bootstrap (apt deps, git clone, systemd unit) and optionally shells out to `python3 monitor.py --restore`.

**Tech Stack:** Python stdlib (`tarfile`, `json`, `os`) for the restore logic — no new dependencies. POSIX shell (`bash`) for `install.sh`, targeting Debian/Ubuntu's `apt`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-23-packaging-install-design.md`
- `--restore` does NOT extract the tarball's bundled `monitor.py` — only `hosts.yaml`, `auth.json`, `netwatch.db`.
- `--restore` refuses to overwrite existing destination files unless `--force` is passed.
- `auth.json` is written with mode `0600` on restore, matching how `AuthManager._save()` writes it normally.
- `install.sh` targets Debian/Ubuntu (`apt`) only — no cross-distro support.
- The corrected Python dependency install is `sudo apt install python3-yaml python3-openpyxl`, not `pip3 install pyyaml openpyxl` (the latter fails with `externally-managed-environment` on PEP-668 systems, confirmed failing on this Debian 13 box).
- `install.sh` must work both piped (`curl -fsSL <url> | bash`) and run locally after a manual clone — verified by reading restore-prompt input from `/dev/tty`, not stdin.
- No automated tests apply to `install.sh` or the README changes — verification is manual, per the spec.

---

### Task 1: `restore_backup()` function and tests

**Files:**
- Modify: `monitor.py` (new function after `create_backup_tarball`, ~line 2175)
- Test: `tests/test_netwatch.py`

**Interfaces:**
- Consumes: `BACKUP_MANIFEST_VERSION` (monitor.py:2078, already exists), `create_backup_tarball(config_path, auth_path) -> (bytes, filename, manifest)` (monitor.py:2081, already exists, used only by the test fixture here, not by `restore_backup` itself).
- Produces: `restore_backup(tarball_path: str, config_path: str, force: bool = False) -> (bool, str)` — `(True, message)` on success, `(False, message)` on any expected failure (never raises for the failure modes this task covers). Used by Task 2's CLI wiring.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_netwatch.py`, in a new section near the end of the file (after the existing Proxmox-action tests, following the file's `# ── Section Name ──` comment convention):

```python
# ── restore_backup ───────────────────────────────────────────────────────────

def _build_fixture_backup(tmp_path):
    """Build a real backup tarball via create_backup_tarball, for restore tests."""
    from monitor import create_backup_tarball
    src_dir = tmp_path / "source"
    src_dir.mkdir()
    config_path = str(src_dir / "hosts.yaml")
    auth_path = str(src_dir / "auth.json")
    db_path = str(src_dir / "netwatch.db")
    monitor_path = str(src_dir / "monitor.py")

    with open(config_path, "w") as f:
        f.write("hosts: []\n")
    with open(auth_path, "w") as f:
        f.write('{"secret_key": "abc123", "users": {}}')
    with open(monitor_path, "w") as f:
        f.write("# fake monitor.py for fixture purposes\n")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (a INTEGER)")
    conn.commit()
    conn.close()

    data, filename, manifest = create_backup_tarball(config_path, auth_path)
    tarball_path = tmp_path / filename
    tarball_path.write_bytes(data)
    return str(tarball_path), manifest


def test_restore_backup_happy_path(tmp_path):
    from monitor import restore_backup
    tarball_path, manifest = _build_fixture_backup(tmp_path)

    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    dest_config_path = str(dest_dir / "hosts.yaml")

    ok, message = restore_backup(tarball_path, dest_config_path)

    assert ok is True
    assert (dest_dir / "hosts.yaml").exists()
    assert (dest_dir / "auth.json").exists()
    assert (dest_dir / "netwatch.db").exists()
    assert manifest["source_hostname"] in message
    assert oct(os.stat(dest_dir / "auth.json").st_mode)[-3:] == "600"
    # The bundled monitor.py in the tarball must NOT be extracted
    assert not (dest_dir / "monitor.py").exists()


def test_restore_backup_refuses_to_overwrite_without_force(tmp_path):
    from monitor import restore_backup
    tarball_path, _ = _build_fixture_backup(tmp_path)

    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    dest_config_path = str(dest_dir / "hosts.yaml")
    with open(dest_config_path, "w") as f:
        f.write("hosts: []\n")  # pre-existing file in the way

    ok, message = restore_backup(tarball_path, dest_config_path)

    assert ok is False
    assert "hosts.yaml" in message
    assert "--force" in message


def test_restore_backup_force_overwrites(tmp_path):
    from monitor import restore_backup
    tarball_path, _ = _build_fixture_backup(tmp_path)

    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    dest_config_path = str(dest_dir / "hosts.yaml")
    with open(dest_config_path, "w") as f:
        f.write("hosts: [{name: stale}]\n")

    ok, message = restore_backup(tarball_path, dest_config_path, force=True)

    assert ok is True
    with open(dest_config_path) as f:
        assert "stale" not in f.read()


def test_restore_backup_rejects_invalid_tarball(tmp_path):
    from monitor import restore_backup
    not_a_backup = tmp_path / "not-a-backup.tar.gz"
    import tarfile
    with tarfile.open(str(not_a_backup), "w:gz") as tar:
        info = tarfile.TarInfo(name="some-other-file.txt")
        data = b"hello"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    dest_config_path = str(tmp_path / "dest" / "hosts.yaml")
    ok, message = restore_backup(str(not_a_backup), dest_config_path)

    assert ok is False
    assert "not a valid netwatch backup" in message.lower()


def test_restore_backup_missing_tarball_file(tmp_path):
    from monitor import restore_backup
    ok, message = restore_backup(str(tmp_path / "does-not-exist.tar.gz"), str(tmp_path / "hosts.yaml"))
    assert ok is False
    assert "not found" in message.lower()


def test_restore_backup_warns_on_newer_manifest_version(tmp_path, monkeypatch):
    from monitor import restore_backup
    import monitor as _mon
    tarball_path, _ = _build_fixture_backup(tmp_path)

    # Pretend this monitor.py is older than the backup's manifest version
    monkeypatch.setattr(_mon, "BACKUP_MANIFEST_VERSION", 0)

    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    ok, message = restore_backup(tarball_path, str(dest_dir / "hosts.yaml"))

    assert ok is True
    assert "newer netwatch version" in message.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_netwatch.py -k restore_backup -v`
Expected: FAIL — `ImportError: cannot import name 'restore_backup' from 'monitor'`

- [ ] **Step 3: Implement**

Add `import sys` to monitor.py's top-level import line (line 16) — it's currently:

```python
import os, time, json, re, shutil, subprocess, threading, curses, yaml, argparse, logging
```

Change to:

```python
import os, sys, time, json, re, shutil, subprocess, threading, curses, yaml, argparse, logging
```

Add `restore_backup` to `monitor.py` directly after `create_backup_tarball` (after line 2180, before the `# ARP-based MAC detection` section comment at line 2182):

```python
def restore_backup(tarball_path, config_path, force=False):
    """Restore hosts.yaml, auth.json, and netwatch.db from a backup tarball
    built by create_backup_tarball().

    Deliberately does NOT extract the tarball's bundled monitor.py: the
    tarball is meant to be a fully self-contained emergency artifact usable
    on its own, but when --restore runs after a fresh git clone (the normal
    redeploy path), overwriting freshly-cloned code with whatever version
    made the backup would silently downgrade it.

    Returns (ok, message). Never raises for expected failure conditions
    (missing/invalid tarball, conflicting destination files) - callers can
    print the message and exit without a traceback.
    """
    import tarfile

    if not os.path.isfile(tarball_path):
        return False, f"Backup file not found: {tarball_path}"

    try:
        tar = tarfile.open(tarball_path, "r:gz")
    except (tarfile.TarError, OSError) as e:
        return False, f"Could not open backup tarball: {e}"

    with tar:
        try:
            manifest_member = tar.getmember("netwatch/metadata.json")
        except KeyError:
            return False, "Not a valid netwatch backup (missing netwatch/metadata.json)"

        manifest = json.loads(tar.extractfile(manifest_member).read().decode("utf-8"))

        warning = ""
        backup_version = manifest.get("manifest_version", 0)
        if backup_version > BACKUP_MANIFEST_VERSION:
            warning = (
                f"Warning: this backup was made by a newer netwatch version "
                f"(manifest v{backup_version}, this is v{BACKUP_MANIFEST_VERSION}) "
                f"- restore may be incomplete.\n"
            )

        config_dir = os.path.dirname(os.path.abspath(config_path))
        targets = {
            "netwatch/hosts.yaml":  os.path.join(config_dir, "hosts.yaml"),
            "netwatch/auth.json":   os.path.join(config_dir, "auth.json"),
            "netwatch/netwatch.db": os.path.join(config_dir, "netwatch.db"),
        }

        if not force:
            existing = [dest for dest in targets.values() if os.path.exists(dest)]
            if existing:
                listing = "\n".join(f"  - {p}" for p in existing)
                return False, (
                    "Refusing to overwrite existing files (use --force to overwrite):\n"
                    f"{listing}"
                )

        os.makedirs(config_dir, exist_ok=True)
        restored = []
        for arcname, dest in targets.items():
            try:
                member = tar.getmember(arcname)
            except KeyError:
                continue  # e.g. auth.json may be absent if no admin was ever set up
            with tar.extractfile(member) as src, open(dest, "wb") as out:
                out.write(src.read())
            if arcname == "netwatch/auth.json":
                os.chmod(dest, 0o600)
            restored.append(dest)

    files_listing = "\n".join(f"  - {p}" for p in restored)
    message = (
        f"{warning}"
        f"Restored backup from {manifest.get('source_hostname', 'unknown')}, "
        f"created {manifest.get('created_iso', 'unknown')}, "
        f"netwatch v{manifest.get('netwatch_version', 'unknown')}\n"
        f"Files written:\n{files_listing}"
    )
    return True, message
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_netwatch.py -k restore_backup -v`
Expected: 6 passed

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `python3 -m pytest tests/test_netwatch.py -v 2>&1 | tail -10`
Expected: 145 passed (139 existing + 6 new)

- [ ] **Step 6: Commit**

```bash
git add monitor.py tests/test_netwatch.py
git commit -m "feat: add restore_backup() to restore hosts.yaml/auth.json/netwatch.db from a backup tarball"
```

---

### Task 2: Wire `--restore`/`--force` into the CLI

**Files:**
- Modify: `monitor.py` (`main()`, ~line 4716-4723)
- Test: `tests/test_netwatch.py`

**Interfaces:**
- Consumes: `restore_backup(tarball_path, config_path, force=False) -> (bool, str)` (Task 1).
- Produces: `python3 monitor.py --restore <path> [--force]` as a working CLI invocation that exits before any server/auth/db setup runs.

This task is harder to unit-test directly since `main()` isn't structured for that (it doesn't return a value, it calls `sys.exit`) — so this task is verified by direct CLI invocation rather than a pytest function, then a smoke-test is added to confirm the exit-early behavior specifically.

- [ ] **Step 1: Implement the CLI wiring**

In `monitor.py`, modify the `main()` function's argument setup (lines 4717-4723):

```python
    parser = argparse.ArgumentParser(description="Netwatch - homelab ping monitor")
    parser.add_argument("--config", default="hosts.yaml")
    parser.add_argument("--no-tui", action="store_true", help="Headless / systemd mode")
    parser.add_argument("--no-web", action="store_true", help="Disable web dashboard")
    parser.add_argument("--port",   type=int, default=8080, help="Web server port")
    parser.add_argument("--log",    default="monitor.log")
    parser.add_argument("--restore", metavar="TARBALL",
                         help="Restore hosts.yaml/auth.json/netwatch.db from a backup tarball, then exit")
    parser.add_argument("--force", action="store_true",
                         help="With --restore, overwrite existing hosts.yaml/auth.json/netwatch.db")
    args = parser.parse_args()

    if args.restore:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.config)
        ok, message = restore_backup(args.restore, config_path, force=args.force)
        print(message)
        sys.exit(0 if ok else 1)
```

Place this `if args.restore:` block immediately after `args = parser.parse_args()` and before the `from logging.handlers import RotatingFileHandler` line — it must run before logging setup, `AuthManager`, `HistoryDB`, or any other server-startup code, since `--restore` is a standalone action that should do nothing else.

- [ ] **Step 2: Verify manually**

```bash
cd /tmp && mkdir -p restore-cli-test/source restore-cli-test/dest
cd restore-cli-test/source
echo "hosts: []" > hosts.yaml
echo '{"secret_key": "abc", "users": {}}' > auth.json
python3 -c "import sqlite3; c = sqlite3.connect('netwatch.db'); c.execute('CREATE TABLE t (a int)'); c.commit()"
cp /home/mgipson/netwatch/monitor.py .
python3 - <<'EOF'
import sys
sys.path.insert(0, ".")
from monitor import create_backup_tarball
data, filename, manifest = create_backup_tarball("hosts.yaml", "auth.json")
open(f"../{filename}", "wb").write(data)
print(filename)
EOF
cd ../dest
python3 /home/mgipson/netwatch/monitor.py --restore ../*.tar.gz --config hosts.yaml
```

Expected output: a line starting with `Restored backup from ...`, exit code 0 (`echo $?`), and `ls` showing `hosts.yaml`, `auth.json`, `netwatch.db` now present in `dest/`. Clean up afterward: `cd /tmp && rm -rf restore-cli-test`.

- [ ] **Step 3: Add a smoke-test confirming early exit (no server startup) for `--restore`**

Add to `tests/test_netwatch.py`, in the same restore section as Task 1:

```python
def test_restore_cli_exits_before_server_startup(tmp_path):
    """--restore should print a message and exit without ever importing/starting
    AuthManager, HistoryDB, etc. Run as a subprocess so we observe the real
    argparse + main() path, not just the restore_backup() function in isolation."""
    import subprocess
    tarball_path, _ = _build_fixture_backup(tmp_path)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    monitor_py = os.path.join(os.path.dirname(os.path.abspath(_mon.__file__)), "monitor.py")
    result = subprocess.run(
        [sys.executable, monitor_py, "--restore", tarball_path, "--config",
         str(dest_dir / "hosts.yaml")],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert "Restored backup from" in result.stdout
    assert (dest_dir / "hosts.yaml").exists()
```

This test needs `import sys` at the top of `tests/test_netwatch.py` if not already present — check first with `grep -n "^import sys" tests/test_netwatch.py`; add it near the other stdlib imports (line 1-9 block) if missing.

- [ ] **Step 4: Run the new test and the full suite**

Run: `python3 -m pytest tests/test_netwatch.py -k restore -v`
Expected: 7 passed (6 from Task 1 + this one)

Run: `python3 -m pytest tests/test_netwatch.py -v 2>&1 | tail -10`
Expected: 146 passed

- [ ] **Step 5: Commit**

```bash
git add monitor.py tests/test_netwatch.py
git commit -m "feat: add --restore/--force CLI flags for restoring from a backup tarball"
```

---

### Task 3: `install.sh` bootstrap script

**Files:**
- Create: `install.sh` (repo root)

**Interfaces:**
- Consumes: `--restore`/`--force` (Task 2).
- Produces: a standalone, executable `install.sh` — no other task depends on it.

No automated tests apply to this task (shell deployment script, not application code, per the spec) — verification is manual.

- [ ] **Step 1: Write `install.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/MichaelGipson101/Netwatch.git"
INSTALL_DIR="${NETWATCH_INSTALL_DIR:-$HOME/netwatch}"

echo "[install] Netwatch installer"

if ! command -v apt >/dev/null 2>&1; then
    echo "[install] ERROR: this script requires a Debian/Ubuntu system with apt." >&2
    exit 1
fi

echo "[install] Installing OS and Python dependencies..."
sudo apt update
sudo apt install -y python3 nmap sqlite3 git python3-yaml python3-openpyxl

if [ -f "./monitor.py" ]; then
    echo "[install] Already inside a netwatch checkout, using $(pwd)"
    INSTALL_DIR="$(pwd)"
else
    if [ -d "$INSTALL_DIR" ]; then
        echo "[install] $INSTALL_DIR already exists, using it as-is"
    else
        echo "[install] Cloning netwatch into $INSTALL_DIR..."
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi
    cd "$INSTALL_DIR"
fi

if [ ! -f "hosts.yaml" ]; then
    echo "[install] Seeding hosts.yaml from hosts.yaml.example..."
    cp hosts.yaml.example hosts.yaml
fi

RESTORE_PATH=""
if [ -t 0 ] || [ -e /dev/tty ]; then
    read -r -p "[install] Restore from a backup tarball? Enter path or press Enter to skip: " RESTORE_PATH < /dev/tty || true
else
    echo "[install] No terminal available for interactive prompts; skipping restore prompt."
    echo "[install] You can restore later with: python3 $INSTALL_DIR/monitor.py --restore <tarball>"
fi

if [ -n "$RESTORE_PATH" ]; then
    echo "[install] Restoring from $RESTORE_PATH..."
    python3 "$INSTALL_DIR/monitor.py" --restore "$RESTORE_PATH"
fi

CURRENT_USER="$(whoami)"
SERVICE_PATH="/etc/systemd/system/netwatch.service"

echo "[install] Writing systemd unit at $SERVICE_PATH..."
sudo tee "$SERVICE_PATH" > /dev/null <<EOF
[Unit]
Description=Netwatch homelab ping monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/monitor.py --no-tui --port 8080
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=netwatch

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now netwatch

LAN_IP="$(hostname -I | awk '{print $1}')"
echo ""
echo "[install] Done. Netwatch is running."
echo "[install] Dashboard: http://${LAN_IP}:8080"
echo "[install] Open it in a browser to complete the first-run admin setup."
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x install.sh
```

- [ ] **Step 3: Verify the restore-prompt TTY handling specifically**

This is the trickiest part of the script to get right — confirm it doesn't hang or error when stdin is genuinely not a terminal (simulating the `curl | bash` case):

```bash
echo "" | bash -n install.sh
```

Expected: no syntax errors printed (this only checks shell syntax, not behavior — full behavioral verification happens in Step 4 on a real or disposable machine).

- [ ] **Step 4: Manual end-to-end verification**

Run this in a throwaway environment — a fresh Debian/Ubuntu container or VM, NOT this production Pi, since it installs system packages and a systemd service:

```bash
docker run -it --rm debian:13 bash
# inside the container:
apt update && apt install -y curl sudo
useradd -m -s /bin/bash testuser && echo "testuser ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
su - testuser
curl -fsSL https://raw.githubusercontent.com/MichaelGipson101/Netwatch/main/install.sh | bash
```

(Containers don't run systemd by default, so `systemctl enable --now` will likely fail inside a bare `docker run` — if so, that's a container limitation, not a script bug; the meaningful checks here are: apt installs succeed, the clone succeeds, `hosts.yaml` gets seeded, and the script doesn't hang waiting for input. For the systemd-specific check, prefer a spare Pi SD card, a VM with systemd, or `systemd-nspawn` instead of plain `docker run`.)

Expected: script completes without hanging, prints the final "Done. Netwatch is running." message (or a clear systemd error if running in an environment without systemd — not a silent hang).

- [ ] **Step 5: Commit**

```bash
git add install.sh
git commit -m "feat: add install.sh bootstrap script for fresh installs and redeploys"
```

---

### Task 4: README updates

**Files:**
- Modify: `README.md` (Setup section)

**Interfaces:** none — documentation only.

- [ ] **Step 1: Fix the broken pip3 step and add a Quick Install section**

In `README.md`, find the "## Setup" section's "**1. Dependencies**" block:

```markdown
**1. Dependencies**
```bash
sudo apt install python3 python3-pip nmap sqlite3
pip3 install pyyaml openpyxl
```
```

Replace it with:

```markdown
**1. Dependencies**
```bash
sudo apt install python3 nmap sqlite3 python3-yaml python3-openpyxl
```
```

(Removing `python3-pip` and the `pip3 install` line entirely — `python3-yaml`/`python3-openpyxl` are real Debian packages, and installing via `pip3` directly fails with `externally-managed-environment` on PEP-668-enforcing systems like Debian 13+/Ubuntu 23.04+.)

Immediately before the existing "## Setup" heading, add:

```markdown
## Quick Install

```bash
curl -fsSL https://raw.githubusercontent.com/MichaelGipson101/Netwatch/main/install.sh | bash
```

This installs dependencies, clones the repo, seeds `hosts.yaml`, optionally restores from a backup tarball if you have one, and sets up the systemd service — then open the dashboard URL it prints and complete the first-run admin setup. See below for what it's doing under the hood, or to do it by hand.

```

- [ ] **Step 2: Verify the markdown renders sensibly**

```bash
grep -n "## Quick Install\|## Setup\|pip3\|python3-yaml" README.md
```

Expected: `## Quick Install` appears once, directly before `## Setup`; no remaining reference to `pip3 install` anywhere in the dependencies step; `python3-yaml` and `python3-openpyxl` both appear in the apt install line.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: fix broken pip3 install step, add Quick Install section pointing at install.sh"
```

---

## Self-Review Notes

- **Spec coverage:** `restore_backup()` behavior (Task 1: happy path, force-overwrite, missing-tarball, invalid-tarball, manifest-version warning — all 4 spec-listed test cases plus 2 extra edge cases), CLI wiring and early-exit behavior (Task 2), `install.sh`'s apt fix / clone detection / hosts.yaml seeding / TTY-safe restore prompt / systemd generation (Task 3), README's pip3 fix and Quick Install section (Task 4). The spec's "Out of scope" items (PyPI packaging, Docker, cross-distro, `static/` in the tarball) have correctly no corresponding task.
- **Placeholder scan:** none found — `install.sh`'s `NETWATCH_INSTALL_DIR` environment variable is a real, intentional override mechanism (lets advanced users redirect the clone location), not an unfilled placeholder.
- **Type consistency:** `restore_backup(tarball_path, config_path, force=False) -> (bool, str)` is used identically in Task 1's tests, Task 2's CLI wiring, and Task 2's manual verification — no signature drift.
