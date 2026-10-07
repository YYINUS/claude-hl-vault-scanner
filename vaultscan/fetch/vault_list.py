"""Normalize the vault list from stats-data (undocumented) into flat records.

The endpoint is undocumented, so parsing is defensive: every field is looked up
under several candidate keys, and the raw response is always saved first.
"""
from __future__ import annotations

from typing import Any


def _get(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return default


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _max_abs_pnl(item: dict) -> float:
    m = 0.0
    for entry in item.get("pnls") or []:
        series = entry[1] if isinstance(entry, (list, tuple)) and len(entry) > 1 else []
        for v in series or []:
            fv = _f(v)
            if fv is not None:
                m = max(m, abs(fv))
    return m


def _has_history(item: dict) -> bool:
    pnls = item.get("pnls")
    if not pnls:
        return False
    for entry in pnls:
        series = entry[1] if isinstance(entry, (list, tuple)) and len(entry) > 1 else None
        if series and any(_f(v) not in (None, 0.0) for v in series):
            return True
    return False


def normalize(raw: Any) -> list[dict]:
    items = raw if isinstance(raw, list) else _get(raw, "vaults", "data", default=[])
    out: list[dict] = []
    for item in items:
        s = item.get("summary", item) if isinstance(item, dict) else {}
        addr = _get(s, "vaultAddress", "address")
        if not addr:
            continue
        rel = _get(s, "relationship", default={}) or {}
        out.append(
            {
                "vault_address": addr.lower(),
                "name": _get(s, "name"),
                "leader": (_get(s, "leader") or "").lower() or None,
                "tvl": _f(_get(s, "tvl", "accountValue")),
                "apr": _f(_get(item, "apr", default=_get(s, "apr"))),
                "is_closed": bool(_get(s, "isClosed", default=False)),
                "relationship": rel.get("type") if isinstance(rel, dict) else rel,
                "parent": (rel.get("data") or {}).get("parent") if isinstance(rel, dict) else None,
                "create_time_ms": _get(s, "createTimeMillis", "createTime"),
                "has_history": _has_history(item),
                "max_abs_pnl": _max_abs_pnl(item),
            }
        )
    return out


def from_addresses(addresses: list[str]) -> list[dict]:
    """Fallback records when only addresses are known (no list endpoint)."""
    return [
        {"vault_address": a.lower(), "name": None, "leader": None, "tvl": None, "apr": None,
         "is_closed": False, "relationship": None, "parent": None,
         "create_time_ms": None, "has_history": True, "max_abs_pnl": None, "from_fallback": True}
        for a in addresses
    ]
