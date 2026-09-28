"""Attention layer: the persisted alert ledger, its per-poller adapter, and (added by later
tasks) the pure builders behind /api/attention plus the on-demand explanation.

Nothing here talks to the network except helpers that callers inject (neighbor-table reader,
LLM caller), so every piece is unit-testable.
"""
import hashlib
import json
import logging
import threading
import time
from datetime import datetime

from netwatch.storage import InventoryDB

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


class AlertLedger:
    """Persisted alert conditions (table `alert_state`, created by HistoryDB.SCHEMA).

    A row with cleared_at IS NULL is active. `fire` returns True only when the caller should
    send a notification now: a brand-new condition, or one that cleared at least
    `cooldown_seconds` ago. An already-active condition (including one that was active before a
    restart) never re-notifies; a condition that flaps back inside the cooldown is shown again
    but does not re-notify.
    """

    _COLS = ("condition_id", "source", "severity", "title", "detail", "since", "notified_at")

    def __init__(self, history_db, cooldown_seconds=300):
        self.lock = history_db.lock      # share the DB lock, same pattern as InventoryDB
        self.conn = history_db.conn
        self._cooldown = cooldown_seconds

    def _cooldown_s(self):
        v = self._cooldown() if callable(self._cooldown) else self._cooldown
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 300

    @staticmethod
    def _now(now):
        return int(time.time() if now is None else now)

    def fire(self, condition_id, source, severity, title, detail, now=None):
        now = self._now(now)
        with self.lock:
            row = self.conn.execute(
                "SELECT cleared_at FROM alert_state WHERE condition_id = ?",
                (condition_id,)).fetchone()
            if row is not None and row[0] is None:
                self.conn.execute(
                    "UPDATE alert_state SET source = ?, severity = ?, title = ?, detail = ? "
                    "WHERE condition_id = ?", (source, severity, title, detail, condition_id))
                return False
            flapping = row is not None and (now - row[0]) < self._cooldown_s()
            self.conn.execute(
                "INSERT OR REPLACE INTO alert_state "
                "(condition_id, source, severity, title, detail, since, notified_at, cleared_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
                (condition_id, source, severity, title, detail, now))
            return not flapping

    def mark_notified(self, condition_id, now=None):
        with self.lock:
            self.conn.execute(
                "UPDATE alert_state SET notified_at = ? WHERE condition_id = ? AND cleared_at IS NULL",
                (self._now(now), condition_id))

    def clear(self, condition_id, now=None):
        with self.lock:
            cur = self.conn.execute(
                "UPDATE alert_state SET cleared_at = ? WHERE condition_id = ? AND cleared_at IS NULL",
                (self._now(now), condition_id))
            return cur.rowcount > 0

    def active(self, source=None, include_dismissed=False):
        sql = ("SELECT condition_id, source, severity, title, detail, since, notified_at "
               "FROM alert_state WHERE cleared_at IS NULL")
        args = ()
        if not include_dismissed:
            sql += " AND dismissed_at IS NULL"
        if source is not None:
            sql += " AND source = ?"
            args = (source,)
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        out = [dict(zip(self._COLS, r)) for r in rows]
        out.sort(key=lambda r: (SEVERITY_ORDER.get(r["severity"], 9), r["since"]))
        return out

    def active_ids(self, source):
        # Includes dismissed rows: the poller must still be able to clear them when the
        # condition resolves.
        return {r["condition_id"] for r in self.active(source, include_dismissed=True)}

    def dismiss(self, condition_id, now=None):
        with self.lock:
            cur = self.conn.execute(
                "UPDATE alert_state SET dismissed_at = ? WHERE condition_id = ? "
                "AND cleared_at IS NULL AND dismissed_at IS NULL",
                (self._now(now), condition_id))
            return cur.rowcount > 0

    def restore_all(self):
        with self.lock:
            cur = self.conn.execute(
                "UPDATE alert_state SET dismissed_at = NULL "
                "WHERE cleared_at IS NULL AND dismissed_at IS NOT NULL")
            return cur.rowcount

    def dismissed_count(self):
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM alert_state "
                "WHERE cleared_at IS NULL AND dismissed_at IS NOT NULL").fetchone()[0]

    def prune(self, older_than_days=30, now=None):
        cutoff = self._now(now) - int(older_than_days) * 86400
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM alert_state WHERE cleared_at IS NOT NULL AND cleared_at < ?", (cutoff,))
            return cur.rowcount


class AlertGate:
    """Per-poller adapter over the ledger. With ledger=None it is inert and always says
    "notify", so the poller's own in-memory `_alert_state` remains the only dedupe (today's
    behavior). Ledger failures are logged and degrade to that same behavior."""

    def __init__(self, source, label, ledger=None):
        self.source = source
        self.label = label
        self.ledger = ledger
        # Pass tracking is per-thread: NAS/PBS passes can overlap (poller thread + a manual
        # refresh on a request thread) and must not see or reset each other's touched set.
        self._local = threading.local()

    def _touch(self, condition_id):
        touched = getattr(self._local, "touched", None)
        if touched is not None:
            touched.add(condition_id)

    def fire(self, condition_id, severity, message):
        self._touch(condition_id)
        if self.ledger is None:
            return True
        try:
            return self.ledger.fire(condition_id, self.source, severity, message, self.label)
        except Exception as e:
            logging.warning(f"AlertGate({self.source}): ledger fire failed: {e}")
            return True

    def clear(self, condition_id):
        self._touch(condition_id)
        if self.ledger is None:
            return
        try:
            self.ledger.clear(condition_id)
        except Exception as e:
            logging.warning(f"AlertGate({self.source}): ledger clear failed: {e}")

    def mark_notified(self, condition_id):
        if self.ledger is None:
            return
        try:
            self.ledger.mark_notified(condition_id)
        except Exception as e:
            logging.warning(f"AlertGate({self.source}): mark_notified failed: {e}")

    def active_ids(self):
        if self.ledger is None:
            return set()
        try:
            return self.ledger.active_ids(self.source)
        except Exception as e:
            logging.warning(f"AlertGate({self.source}): active_ids failed: {e}")
            return set()

    def begin_pass(self):
        self._local.touched = set()

    def end_pass(self, prefixes=None):
        """Clear ledger rows of this source that no fire/clear touched during the pass
        (conditions that vanished while netwatch was down or since). `prefixes` restricts this
        to level-triggered condition ids; edge-triggered ones must be left alone. Without a
        begin_pass() on this thread it is a no-op."""
        touched = getattr(self._local, "touched", None)
        if touched is None:
            return
        del self._local.touched
        if self.ledger is None:
            return
        for cid in self.active_ids() - touched:
            if prefixes is None or cid.startswith(tuple(prefixes)):
                self.clear(cid)


def fmt_duration(seconds):
    s = max(0, int(seconds))
    if s < 60:
        return "under a minute"
    m = s // 60
    if m < 60:
        return f"{m} min"
    h = m // 60
    if h < 48:
        return f"{h} h"
    return f"{h // 24} d"


def host_facts(hosts, since_by_ip=None):
    """Plain-dict view of HostState objects, so the builders stay pure and testable.
    `since_by_ip` ({ip: epoch}) seeds `first_down_at` from stored incidents, so "down for X"
    survives a restart (the earlier non-zero value wins)."""
    now = datetime.now()
    seeds = since_by_ip or {}
    out = []
    for h in hosts:
        mac = (h.specs or {}).get("mac")
        first = float(h.first_down_at or 0.0)
        seeded = float(seeds.get(h.ip) or 0.0)
        if seeded and (not first or seeded < first):
            first = seeded
        out.append({
            "name": h.name,
            "ip": h.ip,
            "mac": InventoryDB.normalize_mac(mac) if mac else "",
            "always_on": bool(h.always_on),
            "is_up": bool(h.is_up),
            "checked": h.last_checked is not None,
            "in_maintenance": bool(h.maintenance_until and h.maintenance_until > now),
            "first_down_at": first,
        })
    return out


def _link(page, subview, params=None):
    return {"page": page, "subview": subview, "params": dict(params or {})}


_SOURCE_LINKS = {
    "nas":     lambda: _link("infra", "truenas"),
    "proxmox": lambda: _link("infra", "proxmox"),
    "pbs":     lambda: _link("infra", "proxmox"),
    "ups":     lambda: None,
}


def _norm(mac):
    return InventoryDB.normalize_mac(mac) if mac else ""


def _host_down_items(facts, records, parents, now):
    by_mac, by_ip = {}, {}
    for rec in records:
        mac = _norm(rec.get("mac"))
        if mac:
            by_mac[mac] = rec["id"]
        if rec.get("ip"):
            by_ip[rec["ip"]] = rec["id"]
    down = [f for f in facts
            if f["always_on"] and f["checked"] and not f["is_up"] and not f["in_maintenance"]]
    dev_of, down_by_dev = {}, {}
    for f in down:
        dev = by_mac.get(f["mac"]) if f["mac"] else None
        if dev is None:
            dev = by_ip.get(f["ip"])
        dev_of[f["ip"]] = dev
        if dev is not None:
            down_by_dev[dev] = f

    def root_of(f):
        dev = dev_of[f["ip"]]
        if dev is None:
            return f
        top, seen, cur = None, {dev}, parents.get(dev)
        while cur is not None and cur not in seen:
            seen.add(cur)
            if cur in down_by_dev:
                top = down_by_dev[cur]      # keep walking: the topmost down ancestor wins
            cur = parents.get(cur)
        return top or f

    groups = {}
    for f in down:
        root = root_of(f)
        g = groups.setdefault(root["ip"], {"fact": root, "affected": []})
        if root["ip"] != f["ip"]:
            g["affected"].append(f["ip"])

    items = []
    for ip, g in groups.items():
        f, affected = g["fact"], sorted(g["affected"])
        since = f["first_down_at"] or None
        detail = f"Down {fmt_duration(now - since)}" if since else "Down"
        if affected:
            detail += f" · root cause of {len(affected)} host alert{'s' if len(affected) != 1 else ''}"
        items.append({
            "id": f"host_down:{ip}", "kind": "host_down", "severity": "critical",
            "title": f"{f['name']} is down", "detail": detail, "since": since,
            "affected": affected, "root_ip": ip,
            "link": _link("monitor", "hosts", {"host": ip}),
        })
    return items


def _ledger_item(row, now):
    make_link = _SOURCE_LINKS.get(row["source"], lambda: None)
    return {
        "id": f"alert:{row['condition_id']}", "kind": "poller_condition",
        "severity": row["severity"], "title": row["title"],
        "detail": f"{row['detail']} · {fmt_duration(now - row['since'])}",
        "since": row["since"], "affected": [], "root_ip": None, "link": make_link(),
    }


def _drift_item(d, records):
    rec_id = next((r["id"] for r in records if _norm(r.get("mac")) == d["mac"]), None)
    return {
        "id": f"ip_drift:{d['mac']}", "kind": "ip_drift", "severity": "info",
        "title": f"{d['name']} moved to {d['seen_ip']}",
        "detail": f"Monitored at {d['monitored_ip']}", "since": None,
        "affected": [d["monitored_ip"]], "root_ip": None,
        "link": _link("lab", "inventory", {"inv": rec_id}) if rec_id is not None else None,
    }


def _sort_key(item):
    since = item["since"]
    return (SEVERITY_ORDER.get(item["severity"], 9), since if since is not None else float("inf"))


def _sentence(title):
    return title if title.endswith((".", "!", "?")) else title + "."


def _verdict(facts, items, dismissed=0):
    roots = [i for i in items if i["kind"] == "host_down"]
    affected = sum(len(i["affected"]) for i in roots)
    counts = {
        "hosts_total": len(facts),
        "hosts_up": sum(1 for f in facts if f["is_up"]),
        "hosts_down": len(roots) + affected,
        "affected": affected,
        "maintenance": sum(1 for f in facts if f["in_maintenance"]),
        "dismissed": int(dismissed or 0),
    }
    problems = [i for i in items if i["severity"] in ("critical", "warning")]
    if any(i["severity"] == "critical" for i in items):
        level = "down"
    elif problems:
        level = "warn"
    else:
        level = "ok"
    if not facts:
        headline = "No hosts are being monitored yet."
    elif not problems:
        headline = "Everything looks good."
    elif len(problems) == 1:
        top = problems[0]
        headline = _sentence(top["title"])
        if top["kind"] == "host_down" and top["affected"]:
            n = len(top["affected"])
            headline += f" {n} host{'s' if n != 1 else ''} unreachable."
    else:
        headline = f"{len(problems)} problems need attention. {_sentence(problems[0]['title'])}"
    return {"level": level, "headline": headline, "counts": counts}


def build_attention(facts, records, parents, ledger_rows, suggestions_pending, drift, now=None, dismissed=0):
    now = time.time() if now is None else float(now)
    items = _host_down_items(facts, records, parents, now)
    items.extend(_ledger_item(r, now) for r in ledger_rows)
    if suggestions_pending and suggestions_pending > 0:
        n = int(suggestions_pending)
        items.append({
            "id": "connection_suggestions", "kind": "connection_suggestions", "severity": "info",
            "title": f"{n} connection suggestion{'s' if n != 1 else ''}", "detail": "Review in Lab",
            "since": None, "affected": [], "root_ip": None, "link": _link("lab", "connections"),
        })
    items.extend(_drift_item(d, records) for d in drift)
    items.sort(key=_sort_key)
    return {"generated": datetime.fromtimestamp(now).isoformat(),
            "verdict": _verdict(facts, items, dismissed), "items": items}


def check_ip_drift(facts, records, neighbors):
    """Hosts whose MAC is currently seen only at IPs other than the monitored one.

    Only hosts that are checked AND currently down are considered: a device that changed IP
    stops answering at its old monitored IP, whereas a healthy host that is reachable off-LAN
    (Tailscale/routed) or multi-homed keeps answering and would be a false positive. Hosts in
    maintenance are skipped. No same-subnet restriction: a cross-subnet move is still drift."""
    rec_mac_by_ip = {}
    for rec in records:
        if rec.get("ip") and rec.get("mac"):
            rec_mac_by_ip[rec["ip"]] = _norm(rec["mac"])
    out = []
    for f in facts:
        if f["in_maintenance"] or not f["checked"] or f["is_up"]:
            continue
        mac = f["mac"] or rec_mac_by_ip.get(f["ip"], "")
        if not mac:
            continue
        seen = neighbors.get(mac)
        if not seen or f["ip"] in seen:
            continue
        out.append({"mac": mac, "name": f["name"], "monitored_ip": f["ip"],
                    "seen_ip": sorted(seen)[0]})
    return out


class IPDriftMonitor:
    """Periodically compares each monitored host's MAC against the Pi's neighbor table and
    keeps the latest drift list for the attention endpoint (which never shells out itself)."""

    INTERVAL_SECONDS = 300

    def __init__(self, host_manager, inventory_db, read_neighbors):
        self._hm = host_manager
        self._inv = inventory_db
        self._read = read_neighbors
        self._lock = threading.Lock()
        self._drift = []

    def refresh(self):
        facts = host_facts(self._hm.list_hosts()) if self._hm else []
        records = self._inv.list_all() if self._inv else []
        drift = check_ip_drift(facts, records, self._read())
        with self._lock:
            self._drift = drift
        return list(drift)

    def get(self):
        with self._lock:
            return list(self._drift)

    def start(self, stop_event):
        def _loop():
            while not stop_event.is_set():
                try:
                    self.refresh()
                except Exception as e:
                    logging.warning(f"IPDriftMonitor: pass failed: {e}")
                stop_event.wait(self.INTERVAL_SECONDS)
        threading.Thread(target=_loop, daemon=True, name="ip-drift").start()


NOTHING_TO_EXPLAIN = "Nothing needs attention right now."

EXPLAIN_SYSTEM_PROMPT = (
    "You are Mira, the assistant inside Netwatch, a homelab monitor. You will be given the list "
    "of things that currently need attention. In 2 to 4 plain sentences, say what most likely "
    "links them and what to check first. You only know what is in the list: do not invent "
    "hosts, causes, or numbers. No markdown and no bullet points.")


def _problems(items):
    return [i for i in items if i["severity"] in ("critical", "warning")]


def explain_key(items):
    """Stable digest of the item set (ids and start times), independent of order."""
    material = json.dumps(sorted((i["id"], i.get("since")) for i in items))
    return hashlib.sha256(material.encode()).hexdigest()


def build_explain_messages(items, now):
    lines = []
    for i in items:
        bits = [f"[{i['severity']}] {i['title']}"]
        if i.get("detail"):
            bits.append(i["detail"])
        if i.get("since"):
            bits.append(f"for {fmt_duration(now - i['since'])}")
        if i.get("affected"):
            bits.append("also affected: " + ", ".join(i["affected"]))
        lines.append("- " + " — ".join(bits))
    return [
        {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
        {"role": "user", "content": "Currently needing attention:\n" + "\n".join(lines)},
    ]


def openrouter_complete(api_key, model, messages, timeout=45):
    """One non-streaming chat completion; returns the assistant text. Raises on any failure."""
    import urllib.request
    payload = json.dumps({"model": model, "messages": messages, "max_tokens": 300}).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://netwatch.local",
            "X-Title": "Mira (Netwatch)",
        })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode())
    return body["choices"][0]["message"]["content"] or ""


class Explainer:
    """One-shot plain-language explanation of the current problems. Cached per item set and
    rate limited so repeated taps cannot hammer the upstream API."""

    MIN_INTERVAL_SECONDS = 30

    def __init__(self, complete=None):
        self._complete = complete or openrouter_complete
        self._lock = threading.Lock()
        self._key = None
        self._text = None
        self._last_call = None      # None: no call yet, so the first one is never rate limited

    def explain(self, items, api_key, model, now=None):
        now = time.time() if now is None else float(now)
        generated = datetime.fromtimestamp(now).isoformat()
        problems = _problems(items)
        if not problems:
            return 200, {"explanation": NOTHING_TO_EXPLAIN, "cached": False, "generated": generated}
        if not (api_key or "").strip():
            return 404, {"error": "ai_not_configured"}
        key = explain_key(problems)
        with self._lock:
            if key == self._key and self._text:
                return 200, {"explanation": self._text, "cached": True, "generated": generated}
            wait = 0 if self._last_call is None else self.MIN_INTERVAL_SECONDS - (now - self._last_call)
            if wait > 0:
                if self._text:
                    return 200, {"explanation": self._text, "cached": True, "stale": True,
                                 "generated": generated}
                return 429, {"error": "rate_limited", "retry_after": int(wait) + 1}
            self._last_call = now
            try:
                text = (self._complete(api_key, model, build_explain_messages(problems, now)) or "").strip()
            except Exception as e:
                logging.warning(f"attention explain failed: {e}")
                return 502, {"error": "explanation unavailable"}
            if not text:
                return 502, {"error": "empty explanation"}
            self._key, self._text = key, text
            return 200, {"explanation": text, "cached": False, "generated": generated}
