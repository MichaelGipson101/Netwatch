# HTTPS/TLS for the Dashboard — Design Spec

**Date:** 2026-06-23
**Status:** Approved

---

## Overview

`monitor.py` currently serves the dashboard over plain HTTP on `0.0.0.0:8080`. That's fine on a trusted LAN, but the session cookie and login credentials travel in plaintext, which matters the moment access happens over anything less trusted than the local network — e.g. remote access while away from home.

This adds a real, browser-trusted HTTPS path using the user's own domain (`thelanternarchive.com`), scoped to the existing Tailscale network rather than the public internet — no port-forwarding, no public exposure, despite using a publicly-resolvable domain and a publicly-trusted certificate.

This is infrastructure-only: no changes to `monitor.py`'s code. The existing plain-HTTP LAN path (`0.0.0.0:8080`) stays exactly as it is, since the LAN automation clients (`siliconboard`, `home-scripts`, `hearthboard`) continue to use it directly.

---

## Architecture

```
Tailscale device                 applepi5 (100.82.194.34)
─────────────────                ─────────────────────────────────
https://netwatch.                 :443  Caddy ──TLS──┐
  thelanternarchive.com  ────────▶              (terminates here)
  (resolves to                                       │
   100.82.194.34,                                     ▼
   a Tailscale-only IP)                    127.0.0.1:8080  monitor.py
                                            (plain HTTP, unchanged)

LAN device (192.168.x.x)
─────────────────────────────────────────▶  0.0.0.0:8080  monitor.py
                                             (plain HTTP, unchanged — siliconboard,
                                              home-scripts, hearthboard keep using this)
```

`netwatch.thelanternarchive.com` is a real DNS A record pointing at `100.82.194.34` — Tailscale's private address space. The domain is publicly resolvable (anyone can look up the IP), but the IP itself is only reachable by devices on the tailnet; it is not internet-routable. This gives a publicly-trusted certificate (no browser warnings, works in any browser without installing a root CA) without ever exposing a port to the internet.

Caddy runs as a second systemd service on `applepi5`, terminates TLS, and reverse-proxies to `monitor.py` on `127.0.0.1:8080`. `monitor.py` is unaware of Caddy's existence — it keeps binding `0.0.0.0:8080` exactly as today, serving both the LAN-direct clients and Caddy's proxied traffic identically.

---

## DNS delegation

The domain is registered at Porkbun; DNS is currently delegated to Bluehost's nameservers (which hosts the main `thelanternarchive.com` site, untouched by this work). Bluehost has no usable API for ACME DNS-01 automation, so the `netwatch` subdomain is delegated separately to Cloudflare, which does:

1. Add `thelanternarchive.com` to a free Cloudflare account in DNS-only mode (Cloudflare's proxy/CDN is not used — only its DNS API).
2. In Cloudflare's DNS, add an `A` record: `netwatch.thelanternarchive.com` → `100.82.194.34`.
3. In Porkbun's advanced DNS editor, add subdomain-scoped `NS` records delegating `netwatch.thelanternarchive.com` to Cloudflare's nameservers, so authority for that one name passes to Cloudflare without touching the rest of the zone (the root domain and any other subdomains stay on Bluehost).
4. Generate a Cloudflare API token scoped to `Zone:DNS:Edit` on this one zone only, for Caddy's ACME DNS-01 plugin.

This is a one-time manual setup in both providers' dashboards — not something to automate or script.

---

## Caddy configuration

Caddy needs the Cloudflare DNS plugin built in (not part of the stock binary):

```bash
xcaddy build --with github.com/caddy-dns/cloudflare
```

`/etc/caddy/Caddyfile`:

```
netwatch.thelanternarchive.com {
    tls {
        dns cloudflare {env.CF_API_TOKEN}
    }
    reverse_proxy 127.0.0.1:8080
}
```

`CF_API_TOKEN` is supplied via an environment file loaded by Caddy's systemd unit (`EnvironmentFile=` directive), not committed to any repo or config file — same handling discipline as `auth.json`'s credentials today.

Caddy automatically requests, installs, and renews the certificate (Let's Encrypt, ~90-day lifetime, renewed with ~30 days of margin) with no further intervention.

---

## systemd integration

A new `caddy.service` unit (from the distro package or a custom one matching the binary built above), set to `After=netwatch.service` so the proxy comes up after the app it's fronting. `netwatch.service` itself (per the existing README) requires no changes.

---

## Error handling

- **ACME renewal failure** (expired/revoked Cloudflare API token, broken NS delegation, Cloudflare outage): Caddy logs the failure to its systemd journal and continues serving the last valid certificate until it actually expires — this degrades to a hard outage only if the underlying problem isn't fixed within the renewal margin (~30 days). `journalctl -u caddy` is the troubleshooting entry point; worth a one-line note in the README.
- **Caddy process down**: the Tailscale/HTTPS path goes down; the LAN plain-HTTP path is unaffected, since `monitor.py` doesn't depend on Caddy in any way.
- **Session/CSRF behavior**: unchanged. The `nw_session` cookie (`HttpOnly`, `SameSite=Strict`) and the CSRF token system both work identically whether the request arrived via Caddy or directly — Caddy is a transparent passthrough at the TLS layer only, it doesn't rewrite paths, strip headers, or alter cookies.

---

## Testing / verification

No `monitor.py` code changes means no new pytest coverage — this is purely operational/infrastructure work, verified manually:

1. From a Tailscale-connected device: `curl -v https://netwatch.thelanternarchive.com` → valid certificate chain (no `-k` needed), reaches the login page.
2. From a non-tailnet network (e.g. a phone on cellular data, Tailscale disconnected): confirm the connection times out or is refused — proving the domain's public DNS resolution does not translate into actual internet reachability.
3. `sudo systemctl status caddy` is active/running; `journalctl -u caddy` shows a successful ACME issuance log line.
4. The existing LAN HTTP path (`http://192.168.x.x:8080`) continues to work unchanged — confirms `siliconboard`, `home-scripts` (`netwatch_cmdb.py`, `netwatch_brief.py`), and `hearthboard` need zero changes.

---

## Out of scope

- Migrating the LAN automation clients to the HTTPS URL — explicitly deferred; they keep using the plain-HTTP LAN path.
- Tailscale Funnel or any actual internet exposure — this design is tailnet-only by design.
- Changes to `monitor.py` itself — this is purely a reverse-proxy/DNS/cert addition in front of the existing, unmodified server.
