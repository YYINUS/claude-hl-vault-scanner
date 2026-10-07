"""Analyze step: per-vault performance and risk metrics (`vaultscan analyze`).

Returns are time-weighted so deposits and withdrawals don't count as performance:
each interval between two history points uses Modified Dietz,
    r = dPnL / (AV_prev + 0.5 * flow),   flow = dAV - dPnL,
and the intervals are chained into an index. Hyperliquid serves the month window
at ~12 h spacing and allTime at ~14 days (docs/ENDPOINTS.md), so 30-day metrics are
daily and all-time metrics come from the coarse points.

Writes data/snapshots/<date>/tables/{metrics,closed_metrics,equity}.parquet and
analysis.json (headline numbers for the report).
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd

from ..store.build import latest_snapshot

log = logging.getLogger(__name__)
DEFAULTS = {
    "rank_min_tvl": 10_000,
    "rank_min_age_days": 30,
    "min_daily_points": 10,
    "min_denominator_usd": 100,
    "flags": {"low_leader_stake": 0.05, "high_leverage": 5.0, "concentrated": 0.6,
              "deep_drawdown": -0.5, "tvl_mismatch": 0.5},
}
# Always present in metrics.parquet, even when no vault has enough data for them.
METRIC_COLUMNS = [
    "return_7d", "return_30d", "volatility_30d", "sharpe_30d", "sortino_30d", "max_drawdown_30d",
    "beta_btc_30d", "corr_btc_30d", "daily_points_30d", "return_all", "cagr_all", "max_drawdown_all",
    "history_days", "pnl_all", "peak_account_value", "last_active", "trades_30d", "volume_30d",
    "fees_30d", "realized_pnl_30d", "coins_traded_30d", "fills_capped", "funding_30d", "net_flows_30d",
    "account_value", "total_ntl_pos", "n_positions", "largest_position_value", "leader_fraction",
    "allow_deposits", "created",
]


def twr_index(av: pd.Series, pnl: pd.Series, min_denom: float = 100.0) -> pd.Series:
    """Time-weighted return index (starts at 1) from aligned account value and PnL series."""
    df = pd.DataFrame({"av": av, "pnl": pnl}).dropna().sort_index()
    df = df[~df.index.duplicated(keep="last")]
    if len(df) < 2:
        return pd.Series(dtype=float)
    d_pnl = df["pnl"].diff()
    flow = df["av"].diff() - d_pnl
    denom = df["av"].shift() + 0.5 * flow
    r = (d_pnl / denom).where(denom >= min_denom)
    # Intervals with no capital (vault empty or not yet funded) carry no return.
    r = r.fillna(0.0).clip(lower=-1.0)
    r.iloc[0] = 0.0
    return (1 + r).cumprod()


def max_drawdown(idx: pd.Series) -> float | None:
    if len(idx) < 2:
        return None
    return float((idx / idx.cummax() - 1).min())


def _series(h: pd.DataFrame, window: str) -> pd.Series:
    w = h[h["window"] == window].set_index("time")
    return twr_index(w["account_value"], w["pnl"])


def _period_metrics(idx: pd.Series, btc: pd.Series, min_points: int) -> dict:
    out: dict = {}
    if len(idx) < 2:
        return out
    out["return_30d"] = float(idx.iloc[-1] / idx.iloc[0] - 1)
    out["max_drawdown_30d"] = max_drawdown(idx)
    daily = idx.resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    out["daily_points_30d"] = len(rets)
    if len(rets) < min_points:
        return out
    sd = rets.std()
    out["volatility_30d"] = float(sd * math.sqrt(365))
    out["sharpe_30d"] = float(rets.mean() / sd * math.sqrt(365)) if sd > 0 else None
    downside = rets[rets < 0]
    dd = math.sqrt((downside ** 2).mean()) if len(downside) else 0.0
    out["sortino_30d"] = float(rets.mean() / dd * math.sqrt(365)) if dd > 0 else None
    if len(btc):
        joined = pd.concat([rets.rename("v"), btc.rename("b")], axis=1, join="inner").dropna()
        if len(joined) >= min_points and joined["b"].var() > 0:
            out["beta_btc_30d"] = float(joined["v"].cov(joined["b"]) / joined["b"].var())
            out["corr_btc_30d"] = float(joined["v"].corr(joined["b"]))
    return out


def _all_time_metrics(idx: pd.Series, h_all: pd.DataFrame) -> dict:
    out: dict = {}
    if len(idx) < 2:
        return out
    total = float(idx.iloc[-1] / idx.iloc[0] - 1)
    days = (idx.index[-1] - idx.index[0]).total_seconds() / 86400
    out["return_all"] = total
    out["history_days"] = days
    out["max_drawdown_all"] = max_drawdown(idx)
    if days >= 30 and idx.iloc[-1] > 0:
        out["cagr_all"] = float((idx.iloc[-1] / idx.iloc[0]) ** (365 / days) - 1)
    out["pnl_all"] = float(h_all["pnl"].dropna().iloc[-1]) if h_all["pnl"].notna().any() else None
    out["peak_account_value"] = float(h_all["account_value"].max())
    alive = h_all[h_all["account_value"] > 0]
    out["last_active"] = alive["time"].max() if len(alive) else pd.NaT
    return out


def _activity(fills: pd.DataFrame, funding: pd.DataFrame, ledger: pd.DataFrame) -> pd.DataFrame:
    parts = []
    if len(fills):
        g = fills.groupby("vault_address")
        parts.append(pd.DataFrame({
            "trades_30d": g.size(), "volume_30d": g["notional"].sum(), "fees_30d": g["fee"].sum(),
            "realized_pnl_30d": g["closed_pnl"].sum(), "coins_traded_30d": g["coin"].nunique(),
            "fills_capped": g["history_capped"].any(),
            "fills_from": g["time"].min()}))
    if len(funding):
        parts.append(funding.groupby("vault_address")["usdc"].sum().rename("funding_30d").to_frame())
    if len(ledger):
        parts.append(ledger.groupby("vault_address")["net_flow"].sum().rename("net_flows_30d").to_frame())
    return pd.concat(parts, axis=1) if parts else pd.DataFrame()


def _pct_rank(s: pd.Series) -> pd.Series:
    return s.rank(pct=True, na_option="keep")


def _flags(r: pd.Series, f: dict) -> str:
    out = []
    if r.get("relationship") != "child" and pd.notna(r.get("leader_fraction")) \
            and r["leader_fraction"] < f["low_leader_stake"]:
        out.append("low_leader_stake")
    if pd.notna(r.get("leverage")) and r["leverage"] > f["high_leverage"]:
        out.append("high_leverage")
    if pd.notna(r.get("top_position_share")) and r["top_position_share"] > f["concentrated"] \
            and (r.get("n_positions") or 0) > 0:
        out.append("concentrated")
    if pd.notna(r.get("max_drawdown_all")) and r["max_drawdown_all"] < f["deep_drawdown"]:
        out.append("deep_drawdown")
    if pd.notna(r.get("age_days")) and r["age_days"] < 30:
        out.append("young")
    if r.get("allow_deposits") is False:
        out.append("deposits_closed")
    if r.get("relationship") == "normal" and pd.notna(r.get("tvl")) and r["tvl"] > 0 \
            and pd.notna(r.get("account_value")) \
            and abs(r["account_value"] - r["tvl"]) / r["tvl"] > f["tvl_mismatch"]:
        out.append("tvl_mismatch")
    return ",".join(out)


def analyze_tables(t: dict[str, pd.DataFrame], snapshot_date: str, cfg_analyze: dict | None = None) -> dict:
    a = {**DEFAULTS, **(cfg_analyze or {})}
    a["flags"] = {**DEFAULTS["flags"], **(cfg_analyze or {}).get("flags", {})}
    asof = pd.Timestamp(snapshot_date, tz="UTC")
    vaults, history = t["vaults"].copy(), t["history"]

    bm = t["benchmarks"]
    btc_close = bm[bm["coin"] == "BTC"].set_index("date")["close"].sort_index()
    btc_rets = btc_close.pct_change().dropna()

    perf, equity = [], []
    for addr, h in history.groupby("vault_address"):
        h = h.sort_values("time")
        row = {"vault_address": addr}
        idx_m = _series(h, "month")
        row.update(_period_metrics(idx_m, btc_rets, a["min_daily_points"]))
        idx_w = _series(h, "week")
        if len(idx_w) >= 2:
            row["return_7d"] = float(idx_w.iloc[-1] / idx_w.iloc[0] - 1)
        h_all = h[h["window"] == "allTime"]
        idx_a = _series(h, "allTime")
        row.update(_all_time_metrics(idx_a, h_all))
        perf.append(row)
        if len(idx_a):
            equity.append(pd.DataFrame({"vault_address": addr, "time": idx_a.index, "index": idx_a.values}))

    m = vaults.merge(pd.DataFrame(perf), on="vault_address", how="left") if perf else vaults
    act = _activity(t["fills"], t["funding"], t["ledger"])
    if len(act):
        m = m.merge(act, left_on="vault_address", right_index=True, how="left")
    for c in METRIC_COLUMNS:
        if c not in m:
            m[c] = pd.NaT if c in ("last_active", "created") else np.nan

    m["age_days"] = (asof - m["created"]).dt.total_seconds() / 86400 if "created" in m else np.nan
    if "account_value" in m:
        av = m["account_value"].where(m["account_value"] > 0)
        m["leverage"] = m["total_ntl_pos"] / av
        m["top_position_share"] = m["largest_position_value"] / m["total_ntl_pos"].where(m["total_ntl_pos"] > 0)
    m["flags"] = m.apply(lambda r: _flags(r, a["flags"]), axis=1)

    # Ranking universe: open, top-level (HLP children are inside HLP's numbers), large and old enough.
    eligible = (~m["is_closed"].astype(bool)) & (m["relationship"].fillna("normal") != "child") \
        & (m["tvl"].fillna(0) >= a["rank_min_tvl"]) & (m["age_days"].fillna(0) >= a["rank_min_age_days"]) \
        & (m.get("daily_points_30d", pd.Series(0, index=m.index)).fillna(0) >= a["min_daily_points"])
    m["ranked"] = eligible
    r = m[eligible]
    parts = pd.DataFrame({
        "sharpe": _pct_rank(r["sharpe_30d"]),
        "cagr": _pct_rank(r["cagr_all"]),
        "drawdown": _pct_rank(r["max_drawdown_all"]),  # closer to 0 ranks higher
        "tvl": _pct_rank(np.log10(r["tvl"])),
        "age": _pct_rank(r["age_days"]),
    })
    m.loc[eligible, "score"] = (parts.mean(axis=1, skipna=True) * 100).round(1)
    m.loc[eligible, "rank"] = m.loc[eligible, "score"].rank(ascending=False, method="first")

    closed = m[m["is_closed"].astype(bool) & m["has_details"].fillna(False).astype(bool)].copy()
    closed["lifetime_days"] = (closed["last_active"] - closed["created"]).dt.total_seconds() / 86400

    equity_df = pd.concat(equity, ignore_index=True) if equity else \
        pd.DataFrame(columns=["vault_address", "time", "index"])
    return {"metrics": m, "closed_metrics": closed, "equity": equity_df,
            "summary": summarize(m, closed, snapshot_date, a)}


def _med(s: pd.Series):
    s = s.dropna()
    return float(s.median()) if len(s) else None


def summarize(m: pd.DataFrame, closed: pd.DataFrame, date: str, a: dict) -> dict:
    open_ = m[~m["is_closed"].astype(bool)]
    top_level = open_[open_["relationship"].fillna("normal") != "child"]
    ranked = m[m["ranked"]]
    return {
        "snapshot_date": date,
        "vaults_listed": int(len(m)),
        "open_vaults": int(len(open_)),
        "closed_vaults": int(m["is_closed"].astype(bool).sum()),
        "tvl_total": float(top_level["tvl"].fillna(0).sum()),  # children already inside HLP
        "tracked_vaults": int(open_["has_details"].fillna(False).astype(bool).sum()),
        "ranked_vaults": int(len(ranked)),
        "closed_analyzed": int(len(closed)),
        "median_return_30d": _med(ranked.get("return_30d", pd.Series(dtype=float))),
        "median_max_drawdown_all": _med(ranked.get("max_drawdown_all", pd.Series(dtype=float))),
        "share_positive_30d": (float((ranked["return_30d"] > 0).mean())
                               if "return_30d" in ranked and len(ranked) else None),
        "median_closed_lifetime_days": _med(closed.get("lifetime_days", pd.Series(dtype=float))),
        "rank_rules": {k: a[k] for k in ("rank_min_tvl", "rank_min_age_days", "min_daily_points")},
    }


def run_analyze(cfg: dict, root: Path, date: str | None = None) -> dict:
    date = date or latest_snapshot(root, cfg)
    tdir = root / cfg["paths"]["snapshots_dir"] / date / "tables"
    if not (tdir / "vaults.parquet").exists():
        raise FileNotFoundError(f"{tdir} has no tables; run `vaultscan build` first")
    names = ("vaults", "history", "fills", "funding", "ledger", "benchmarks")
    t = {n: pd.read_parquet(tdir / f"{n}.parquet") for n in names}
    res = analyze_tables(t, date, cfg.get("analyze"))
    for n in ("metrics", "closed_metrics", "equity"):
        res[n].to_parquet(tdir / f"{n}.parquet", index=False)
    with open(tdir.parent / "analysis.json", "w") as f:
        json.dump(res["summary"], f, indent=2)
    log.info("analyzed %d vaults (%d ranked)", len(res["metrics"]), res["summary"]["ranked_vaults"])
    return res["summary"]
