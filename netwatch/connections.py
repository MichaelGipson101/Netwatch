"""Pure rules for the connection graph (Netwatch 4.0 connections rework).

Everything here is I/O-free so InventoryDB, the HTTP handlers, the startup
migration and (later) the discovery reconciler share one definition of
"which end is the parent", "what ports does this device have" and "what is
wrong with this edge". Records are inventory dicts as returned by
InventoryDB.get()/list_all(), with `properties` already decoded to a dict.
"""
import hashlib
import json
import re

# Lower rank = further downstream. The lower-ranked end of an edge is the child.
TYPE_RANK = {
    "vm": 0,
    "disk": 1,
    "peripheral": 2, "phone": 2, "tablet": 2, "printer": 2,
    "host": 3,
    "ups": 4,
    "network": 5,
}

# Breaks ties between two network devices. Higher = further upstream.
NETWORK_ROLE_RANK = {"other": 0, "ap": 1, "switch": 2, "gateway": 3}
NETWORK_ROLES = tuple(NETWORK_ROLE_RANK)

# A port_count above this is treated as garbage rather than rendered as
# thousands of dropdown entries.
_MAX_PORT_COUNT = 512


def _dtype(rec):
    return str(rec.get("device_type") or "host").lower()


def type_rank(rec):
    return TYPE_RANK.get(_dtype(rec), TYPE_RANK["host"])


def network_role(rec):
    """The stored properties.network_role, or "other" if missing/unknown."""
    role = str((rec.get("properties") or {}).get("network_role") or "").lower()
    return role if role in NETWORK_ROLE_RANK else "other"


def infer_network_role(rec):
    """Guess a network role from the free-text `role` and `system` fields.
    Used once by the migration to seed properties.network_role."""
    text = f"{rec.get('role') or ''} {rec.get('system') or ''}".lower()
    if "gateway" in text or "router" in text:
        return "gateway"
    if "switch" in text:
        return "switch"
    if "access point" in text or re.search(r"\bap\b", text):
        return "ap"
    return "other"


def orient_edge(a, b, connection_type=None):
    """Decide which of two inventory records is the child.

    Returns (child, parent, ambiguous). The returned records are the same
    objects passed in. When the rules can't decide, returns (a, b, True) so
    the caller keeps the given order and can offer a swap.
    """
    ta, tb = _dtype(a), _dtype(b)
    if connection_type == "power" and (ta == "ups") != (tb == "ups"):
        return (a, b, False) if tb == "ups" else (b, a, False)
    ra, rb = type_rank(a), type_rank(b)
    if ra != rb:
        return (a, b, False) if ra < rb else (b, a, False)
    if ta == "network" and tb == "network":
        na = NETWORK_ROLE_RANK[network_role(a)]
        nb = NETWORK_ROLE_RANK[network_role(b)]
        if na != nb:
            return (a, b, False) if na < nb else (b, a, False)
    return (a, b, True)


def default_connection_type(child, parent):
    if _dtype(child) == "vm" and _dtype(parent) == "host":
        return "virtual"
    if _dtype(parent) == "network" and network_role(parent) == "ap":
        return "wifi"
    return "ethernet"


def normalize_port(value):
    """Trim; canonicalise purely numeric ports ("08" -> "8"); '' -> None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return str(int(s))
    return s


def resolve_ports(parent, live_ports=None):
    """A parent's port list, or None when its ports are free text.

    `live_ports` (from a discovery source, e.g. UniFi's port table) wins over
    properties.port_count. Always returns fresh dicts so callers can annotate
    them without mutating the source.
    """
    if live_ports:
        return [dict(p) for p in live_ports]
    raw = (parent.get("properties") or {}).get("port_count")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n <= 0 or n > _MAX_PORT_COUNT:
        return None
    return [{"name": str(i), "up": None, "speed_mbps": None, "poe": None}
            for i in range(1, n + 1)]


def validate_parent_port(parent, port, ports):
    """Return an error message, or None when `port` is acceptable."""
    port = normalize_port(port)
    if port is None or ports is None:
        return None
    if port in {p["name"] for p in ports}:
        return None
    return f"'{port}' is not a port on {parent.get('system') or 'this device'}"


def fingerprint(obj):
    """Short stable hash of a JSON-serialisable value (key order ignored)."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
