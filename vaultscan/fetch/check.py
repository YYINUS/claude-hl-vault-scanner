"""Connectivity and schema check for both Hyperliquid hosts (`vaultscan check`)."""
from __future__ import annotations

import time

import httpx

SUMMARY_KEYS = {"name", "vaultAddress", "leader", "tvl", "isClosed", "relationship", "createTimeMillis"}
PNL_WINDOWS = {"day", "week", "month", "allTime"}


def _timed(fn):
    t = time.monotonic()
    try:
        return fn(), None, round(time.monotonic() - t, 2)
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}", round(time.monotonic() - t, 2)


def check_api(info_url: str, timeout: float = 30) -> dict:
    def go():
        r = httpx.post(info_url, json={"type": "meta"}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    data, err, secs = _timed(go)
    ok = err is None and isinstance(data, dict) and "universe" in data
    return {"host": "api.hyperliquid.xyz", "ok": ok, "seconds": secs,
            "detail": f"{len(data['universe'])} perp markets" if ok else (err or "unexpected response")}


def check_vault_list(url: str, timeout: float = 120) -> dict:
    def go():
        r = httpx.get(url, timeout=timeout)
        r.raise_for_status()
        return r.content, r.json()
    res, err, secs = _timed(go)
    if err:
        hint = (" (proxy/policy denial: add stats-data.hyperliquid.xyz to allowed domains)"
                if "403" in err or "CONNECT" in err else "")
        return {"host": "stats-data.hyperliquid.xyz", "ok": False, "seconds": secs, "detail": err + hint}
    body, data = res
    problems = []
    if not isinstance(data, list) or not data:
        problems.append("expected a non-empty list")
        data = []
    missing = sum(1 for x in data if not SUMMARY_KEYS <= set((x.get("summary") or {}).keys()))
    if missing:
        problems.append(f"{missing} items missing summary fields")
    bad_pnls = sum(1 for x in data if {p[0] for p in x.get("pnls") or []} != PNL_WINDOWS)
    if bad_pnls:
        problems.append(f"{bad_pnls} items with unexpected pnl windows")
    n_open = sum(1 for x in data if not (x.get("summary") or {}).get("isClosed"))
    return {"host": "stats-data.hyperliquid.xyz", "ok": not problems, "seconds": secs,
            "detail": (f"{len(data):,} vaults ({n_open:,} open), {len(body)/1e6:.1f} MB"
                       + ("; SCHEMA: " + "; ".join(problems) if problems else ""))}


def run_checks(cfg: dict) -> list[dict]:
    a = cfg["api"]
    return [check_api(a["info_url"], a["timeout_s"]), check_vault_list(a["vault_list_url"])]
