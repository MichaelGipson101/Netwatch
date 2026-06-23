# Install/Redeploy Story — Design Spec

**Date:** 2026-06-23
**Status:** Approved

---

## Overview

Netwatch's current install story is manual: clone the repo, `apt install` a few OS packages, `pip3 install pyyaml openpyxl`, copy `hosts.yaml.example`, run `monitor.py` by hand, then hand-write a systemd unit from the README's example (which hardcodes a specific username and path). This has two problems:

1. **It's broken today.** This Debian 13 system enforces PEP 668 (`externally-managed-environment`); a plain `pip3 install pyyaml openpyxl` fails outright. `python3-yaml` and `python3-openpyxl` are real Debian packages and the correct fix — no venv, no `--break-system-packages` override needed.
2. **It doesn't cover redeployment.** The existing "Download backup" feature produces a tarball (`monitor.py`, `hosts.yaml`, `auth.json`, `netwatch.db`) but there's no counterpart to restore one, and no scripted path from "fresh Pi" to "running service."

This adds two pieces: a `--restore` mode on `monitor.py` for the data half of a redeploy, and `install.sh` for the OS/bootstrap half — usable both for the user's own disaster-recovery redeploys and for a friend's first install on their own network.

---

## Architecture

Two independent, separately useful pieces:

**`monitor.py --restore <tarball>`** — a one-shot CLI action (extracts and exits, does not start the server) that restores `hosts.yaml`, `auth.json`, and `netwatch.db` from a backup tarball built by the existing `create_backup_tarball()`. It deliberately does not extract the tarball's bundled `monitor.py`: that copy exists so the tarball is a fully self-contained emergency artifact usable entirely on its own (`tar xzf backup.tar.gz && cd netwatch && python3 monitor.py`, no git needed), but when `--restore` runs after a fresh `git clone` — the normal redeploy path — overwriting the just-cloned code with whatever version was running at backup time would silently downgrade it.

**`install.sh`** (new file, repo root) — bootstraps a fresh machine: installs OS + Python deps via `apt` (the corrected dependency list), clones the repo if not already inside a checkout, seeds `hosts.yaml` from the example if missing, optionally invokes `--restore` if given a tarball path, generates the systemd unit using the actual invoking user and path, and enables the service. Works both piped (`curl -fsSL <raw-url> | bash`) for a friend's first install, and run locally (`./install.sh`) after a manual clone for anyone who wants to read it first.

These two pieces are independent: `--restore` is useful without `install.sh` (e.g. restoring onto a machine that already has netwatch installed), and `install.sh` is useful without ever calling `--restore` (a friend's brand-new install with no prior data).

---

## `--restore` behavior

```
python3 monitor.py --restore backup.tar.gz [--force] [--config hosts.yaml]
```

- Opens the tarball and requires a `netwatch/metadata.json` entry; if absent, errors `"not a valid netwatch backup"` and exits 1.
- Reads `manifest_version` from the manifest. If it's newer than this `monitor.py`'s `BACKUP_MANIFEST_VERSION`, prints a warning ("backup was made by a newer netwatch version, restore may be incomplete") but proceeds — the format has been stable, and a hard version gate isn't warranted yet.
- For each of `hosts.yaml`, `auth.json`, `netwatch.db` (resolved relative to `--config`'s directory, defaulting to `monitor.py`'s own directory, matching how `config_path` is resolved elsewhere in the file): if the destination already exists and `--force` was not passed, aborts before extracting anything, listing every conflicting file in one message.
- `auth.json` is written with mode `0600`, matching how `AuthManager._save()` writes it normally and how it's stored inside the tarball.
- On success, prints the manifest's `source_hostname`, `created_iso`, and `netwatch_version` (e.g. `"Restored backup from applepi5, created 2026-06-20T03:00:00, netwatch v3.45"`) and exits 0.
- This is a standalone action: when `--restore` is passed, `monitor.py` does the restore and exits — it does not also start the TUI or web server in the same invocation.

---

## `install.sh` behavior

- `set -euo pipefail` throughout, so any failed step stops the script visibly rather than continuing into a half-configured state.
- Bails immediately with a clear message if `apt` is not found — targets Debian/Ubuntu-based systems only, matching the project's actual deployment target. No cross-distro support is attempted.
- `sudo apt update && sudo apt install -y python3 nmap sqlite3 git python3-yaml python3-openpyxl`.
- Detects whether it's already running from inside a netwatch checkout (`monitor.py` present in `$PWD`); if not, clones `https://github.com/MichaelGipson101/Netwatch.git` into `~/netwatch` and `cd`s there. This single detection is what lets the same script work both piped (no checkout exists yet) and run locally (already inside one).
- If `hosts.yaml` is missing in the checkout, copies it from `hosts.yaml.example`.
- Prompts for an optional backup tarball path, reading explicitly from the controlling terminal (`read -p "..." path < /dev/tty`) rather than stdin — necessary because under `curl | bash`, stdin is the script source itself, not a real input stream. If no TTY is available at all (fully unattended invocation), skips the prompt silently and prints a one-line reminder that `--restore` can be run manually afterward. If a non-empty path is given, runs `python3 monitor.py --restore "$path"` and stops the script if that fails (before touching systemd).
- Generates `/etc/systemd/system/netwatch.service` from a template substituting the actual `$(whoami)` and `$(pwd)` for `User=`, `WorkingDirectory=`, and the `ExecStart=` path — the README's current example hardcodes a specific username and path, which is wrong for anyone but the original author. Writes it via `sudo tee`, then `sudo systemctl daemon-reload && sudo systemctl enable --now netwatch`.
- Prints the dashboard URL using the machine's primary LAN IP (`hostname -I | awk '{print $1}'`) and a one-line reminder to open it and complete the first-run admin setup wizard in the browser — that flow already exists and needs no new CLI step.

---

## Error handling

- `install.sh`'s `set -euo pipefail` means an `apt` failure, a clone failure, or a systemd-write failure stops the script immediately with the underlying error visible — no silent partial state.
- If `install.sh` invokes `--restore` and it fails (bad tarball, file conflicts without `--force`), the script prints the failure and stops before touching systemd: the end state is a clean checkout with no service running yet, not a service running against partially-restored or missing data.
- `--restore` itself validates before extracting anything (manifest presence, destination conflicts) so a failed restore never leaves a half-written `netwatch.db` or `auth.json` in place.

---

## Documentation changes

- `README.md`'s "Setup" section's dependency step changes from `pip3 install pyyaml openpyxl` to `sudo apt install python3-yaml python3-openpyxl` (fixing the PEP-668 failure), and gains a short "Quick install" subsection pointing at `install.sh` as the recommended path, with the manual steps kept below for anyone who wants to see exactly what it does.
- The "Run as a service" section's hardcoded example systemd unit stays as a reference (useful documentation of what the unit looks like), but is no longer the only path — `install.sh` generates the equivalent unit automatically with correct values.

---

## Testing / verification

- `--restore` gets full pytest coverage: a fixture tarball built via the existing `create_backup_tarball()` in a temp dir, covering: happy-path restore, rejection when a destination file already exists without `--force`, rejection when `metadata.json` is missing (a tarball that isn't a netwatch backup at all), and the newer-manifest-version warning path.
- `install.sh` is a deployment script, not application code — no automated test. Verified manually: run it end-to-end in a throwaway environment (a fresh Debian container, or a spare SD card image) and confirm the dashboard comes up and `systemctl status netwatch` reports clean.

---

## Out of scope

- Python packaging (`pip install netwatch-monitor`, PyPI publishing) — considered and rejected during brainstorming; fights the project's single-file ethos for no clear benefit to a homelab tool that isn't a general-purpose library.
- Docker image — not requested; `install.sh` + systemd already matches how this project is actually run today.
- Cross-distro support beyond Debian/Ubuntu — not the project's actual deployment target.
- Restoring the `static/` directory from the backup tarball — it was never included in the tarball (only `monitor.py`, `hosts.yaml`, `auth.json`, `netwatch.db`) and adding it is unrelated to this work; `install.sh`'s `git clone` already provides `static/` for the redeploy case.
