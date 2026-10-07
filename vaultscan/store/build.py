"""Build step: raw snapshot JSON -> flat Parquet tables (`vaultscan build`).

Reads data/snapshots/<date>/raw (plus the shared closed-vault cache) and writes
data/snapshots/<date>/tables/*.parquet. No network access; safe to re-run.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)
HISTORY_WINDOWS = ("day", "week", "month", "allTime")


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _read(path: Path):
    with open(path) as f:
        return json.load(f)


def _rows(obj) -> list:
    """Paged files are wrapped as {"rows": [...]}; recorded fixtures are bare lists."""
    if isinstance(obj, dict):
        return obj.get("rows") or []
    return obj or []


def _ts(ms) -> pd.Series:
    return pd.to_datetime(ms, unit="ms", utc=True)


def latest_snapshot(root: Path, cfg: dict) -> str:
    d = root / cfg["paths"]["snapshots_dir"]
    dates = sorted(p.name for p in d.iterdir() if (p / "raw").is_dir()) if d.exists() else []
    if not dates:
        raise FileNotFoundError(f"no snapshots under {d}; run `vaultscan fetch` first")
    return dates[-1]


def _details_files(snap: Path, closed_dir: Path) -> dict[str, Path]:
    """Address -> vaultDetails file: this snapshot's open vaults, then cached closed ones."""
    out: dict[str, Path] = {}
    included = snap / "closed_vaults_included.json"
    for a in (_read(included) if included.exists() else []):
        p = closed_dir / f"{a}.json"
        if p.exists():
            out[a] = p
    for p in sorted((snap / "raw" / "vault_details").glob("*.json")):
        out[p.stem] = p
    return out


def build_vaults(snap: Path, details: dict[str, Path]) -> tuple[pd.DataFrame, pd.DataFrame]:
    listed = {r["vault_address"]: r for r in _read(snap / "vaults_list_normalized.json")}
    vaults, history = [], []
    raw = snap / "raw"
    for addr in sorted(set(listed) | set(details)):
        rec = dict(listed.get(addr) or {"vault_address": addr})
        rec.pop("from_fallback", None)
        d = _read(details[addr]) if addr in details else None
        if d:
            rel = d.get("relationship") or {}
            followers = d.get("followers") or []
            rec.update({
                "name": rec.get("name") or d.get("name"),
                "leader": rec.get("leader") or (d.get("leader") or "").lower() or None,
                "relationship": rec.get("relationship") or rel.get("type"),
                "is_closed": bool(rec.get("is_closed") or d.get("isClosed")),
                "details_apr": _f(d.get("apr")),
                "leader_fraction": _f(d.get("leaderFraction")),
                "leader_commission": _f(d.get("leaderCommission")),
                "followers_n": len(followers),
                "followers_capped": len(followers) >= 100,
                "allow_deposits": d.get("allowDeposits"),
                "max_withdrawable": _f(d.get("maxWithdrawable")),
                "max_distributable": _f(d.get("maxDistributable")),
                "n_children": len((rel.get("data") or {}).get("childAddresses") or []),
            })
            for window, w in d.get("portfolio") or []:
                if window not in HISTORY_WINDOWS:
                    continue
                pnl = {t: _f(v) for t, v in w.get("pnlHistory") or []}
                for t, av in w.get("accountValueHistory") or []:
                    history.append((addr, window, t, _f(av), pnl.get(t)))
        cs = raw / "clearinghouse_state" / f"{addr}.json"
        if cs.exists():
            s = _read(cs)
            ms = s.get("marginSummary") or {}
            pos = [p.get("position") or {} for p in s.get("assetPositions") or []]
            vals = [abs(_f(p.get("positionValue")) or 0) for p in pos]
            rec.update({
                "account_value": _f(ms.get("accountValue")),
                "total_ntl_pos": _f(ms.get("totalNtlPos")),
                "margin_used": _f(ms.get("totalMarginUsed")),
                "n_positions": len(pos),
                "largest_position_value": max(vals, default=0.0),
            })
        rec["has_details"] = d is not None
        rec["has_positions"] = cs.exists()
        rec["has_tier3"] = (raw / "fills" / f"{addr}.json").exists()
        vaults.append(rec)
    v = pd.DataFrame(vaults)
    if "create_time_ms" in v:
        v["created"] = _ts(pd.to_numeric(v["create_time_ms"], errors="coerce"))
    h = pd.DataFrame(history, columns=["vault_address", "window", "time_ms", "account_value", "pnl"])
    h["time"] = _ts(h["time_ms"])
    return v, h.drop(columns="time_ms")


def build_positions(raw: Path) -> pd.DataFrame:
    rows = []
    for p in sorted((raw / "clearinghouse_state").glob("*.json")):
        for ap in _read(p).get("assetPositions") or []:
            x = ap.get("position") or {}
            lev = x.get("leverage") or {}
            szi = _f(x.get("szi")) or 0.0
            rows.append({
                "vault_address": p.stem, "coin": x.get("coin"), "szi": szi,
                "side": "long" if szi > 0 else "short",
                "leverage": _f(lev.get("value")), "leverage_type": lev.get("type"),
                "entry_px": _f(x.get("entryPx")), "position_value": _f(x.get("positionValue")),
                "unrealized_pnl": _f(x.get("unrealizedPnl")), "liquidation_px": _f(x.get("liquidationPx")),
                "margin_used": _f(x.get("marginUsed")),
            })
    return pd.DataFrame(rows, columns=["vault_address", "coin", "szi", "side", "leverage", "leverage_type",
                                       "entry_px", "position_value", "unrealized_pnl", "liquidation_px",
                                       "margin_used"])


def build_fills(raw: Path) -> pd.DataFrame:
    rows = []
    for p in sorted((raw / "fills").glob("*.json")):
        obj = _read(p)
        capped = bool(obj.get("history_capped") or obj.get("truncated")) if isinstance(obj, dict) else False
        for x in _rows(obj):
            px, sz = _f(x.get("px")) or 0.0, _f(x.get("sz")) or 0.0
            rows.append({"vault_address": p.stem, "time_ms": x.get("time"), "coin": x.get("coin"),
                         "side": x.get("side"), "dir": x.get("dir"), "px": px, "sz": sz,
                         "notional": px * sz, "closed_pnl": _f(x.get("closedPnl")),
                         "fee": _f(x.get("fee")), "history_capped": capped})
    df = pd.DataFrame(rows, columns=["vault_address", "time_ms", "coin", "side", "dir", "px", "sz",
                                     "notional", "closed_pnl", "fee", "history_capped"])
    df["time"] = _ts(df["time_ms"])
    return df.drop(columns="time_ms")


def build_funding(raw: Path) -> pd.DataFrame:
    rows = []
    for p in sorted((raw / "funding").glob("*.json")):
        for x in _rows(_read(p)):
            d = x.get("delta") or {}
            rows.append({"vault_address": p.stem, "time_ms": x.get("time"), "coin": d.get("coin"),
                         "usdc": _f(d.get("usdc")), "szi": _f(d.get("szi")),
                         "funding_rate": _f(d.get("fundingRate"))})
    df = pd.DataFrame(rows, columns=["vault_address", "time_ms", "coin", "usdc", "szi", "funding_rate"])
    df["time"] = _ts(df["time_ms"])
    return df.drop(columns="time_ms")


def build_ledger(raw: Path) -> pd.DataFrame:
    rows = []
    for p in sorted((raw / "ledger").glob("*.json")):
        for x in _rows(_read(p)):
            d = x.get("delta") or {}
            t = d.get("type")
            amt = _f(d.get("usdc"))
            if amt is None:  # withdrawals report the amount under other keys
                amt = _f(d.get("netWithdrawnUsd") or d.get("requestedUsd"))
            flow = None if amt is None else (amt if t == "vaultDeposit" else -abs(amt) if t == "vaultWithdraw" else None)
            rows.append({"vault_address": p.stem, "time_ms": x.get("time"), "type": t,
                         "usdc": amt, "net_flow": flow})
    df = pd.DataFrame(rows, columns=["vault_address", "time_ms", "type", "usdc", "net_flow"])
    df["time"] = _ts(df["time_ms"])
    return df.drop(columns="time_ms")


def build_benchmarks(raw: Path) -> pd.DataFrame:
    rows = []
    for p in sorted((raw / "candles").glob("*.json")):
        for c in _read(p):
            rows.append({"coin": c.get("s") or p.stem.split("_")[0], "time_ms": c["t"],
                         "open": _f(c.get("o")), "high": _f(c.get("h")), "low": _f(c.get("l")),
                         "close": _f(c.get("c")), "volume": _f(c.get("v"))})
    df = pd.DataFrame(rows, columns=["coin", "time_ms", "open", "high", "low", "close", "volume"])
    df["date"] = _ts(df["time_ms"]).dt.normalize()
    return df.drop(columns="time_ms")


def run_build(cfg: dict, root: Path, date: str | None = None) -> dict:
    date = date or latest_snapshot(root, cfg)
    snap = root / cfg["paths"]["snapshots_dir"] / date
    raw = snap / "raw"
    out = snap / "tables"
    out.mkdir(parents=True, exist_ok=True)
    details = _details_files(snap, root / cfg["paths"]["closed_vaults_dir"])
    vaults, history = build_vaults(snap, details)
    tables = {
        "vaults": vaults,
        "history": history,
        "positions": build_positions(raw),
        "fills": build_fills(raw),
        "funding": build_funding(raw),
        "ledger": build_ledger(raw),
        "benchmarks": build_benchmarks(raw),
    }
    counts = {}
    for name, df in tables.items():
        df.to_parquet(out / f"{name}.parquet", index=False)
        counts[name] = len(df)
        log.info("built %s: %d rows", name, len(df))
    return {"snapshot_date": date, "tables_dir": str(out), "rows": counts}
