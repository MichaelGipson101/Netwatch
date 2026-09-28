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

    def active(self, source=None):
        sql = ("SELECT condition_id, source, severity, title, detail, since, notified_at "
               "FROM alert_state WHERE cleared_at IS NULL")
        args = ()
        if source is not None:
            sql += " AND source = ?"
            args = (source,)
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        out = [dict(zip(self._COLS, r)) for r in rows]
        out.sort(key=lambda r: (SEVERITY_ORDER.get(r["severity"], 9), r["since"]))
        return out

    def active_ids(self, source):
        return {r["condition_id"] for r in self.active(source)}

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
        self._touched = set()

    def fire(self, condition_id, severity, message):
        self._touched.add(condition_id)
        if self.ledger is None:
            return True
        try:
            return self.ledger.fire(condition_id, self.source, severity, message, self.label)
        except Exception as e:
            logging.warning(f"AlertGate({self.source}): ledger fire failed: {e}")
            return True

    def clear(self, condition_id):
        self._touched.add(condition_id)
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
        self._touched = set()

    def end_pass(self, prefixes=None):
        """Clear ledger rows of this source that no fire/clear touched during the pass
        (conditions that vanished while netwatch was down or since). `prefixes` restricts this
        to level-triggered condition ids; edge-triggered ones must be left alone."""
        if self.ledger is None:
            return
        for cid in self.active_ids() - self._touched:
            if prefixes is None or cid.startswith(tuple(prefixes)):
                self.clear(cid)
