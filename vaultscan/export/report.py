"""Export step: screener CSV + self-contained HTML report (`vaultscan export`).

Writes reports/<date>/{report.html, screener.csv, closed_vaults.csv, summary.json}.
The report has no external dependencies: data is embedded as JSON and the charts
are drawn as SVG by a small inline script, so it opens offline.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .. import __version__
from ..store.build import latest_snapshot

log = logging.getLogger(__name__)
TEMPLATE = Path(__file__).with_name("template.html")

SCREENER_COLUMNS = [
    "rank", "score", "vault_address", "name", "leader", "tvl", "account_value", "age_days",
    "return_7d", "return_30d", "volatility_30d", "sharpe_30d", "sortino_30d", "max_drawdown_30d",
    "beta_btc_30d", "corr_btc_30d", "return_all", "cagr_all", "max_drawdown_all", "pnl_all",
    "leverage", "n_positions", "top_position_share", "leader_fraction", "leader_commission",
    "followers_n", "trades_30d", "volume_30d", "fees_30d", "funding_30d", "net_flows_30d", "flags",
]
CLOSED_COLUMNS = ["vault_address", "name", "leader", "created", "last_active", "lifetime_days",
                  "peak_account_value", "pnl_all", "return_all", "max_drawdown_all"]
DD_BINS = [(-1.01, -0.5, "≤−50%"), (-0.5, -0.4, "−40%"), (-0.4, -0.3, "−30%"), (-0.3, -0.2, "−20%"),
           (-0.2, -0.1, "−10%"), (-0.1, -0.05, "−5%"), (-0.05, 0.001, "0–5%")]
LIFE_BINS = [(0, 30, "<30"), (30, 90, "30–90"), (90, 180, "90–180"), (180, 365, "180–365"),
             (365, 730, "1–2 yr"), (730, 1e9, "2 yr+")]


def _clean(v):
    if v is None or v is pd.NaT:
        return None
    if isinstance(v, (float, np.floating)):
        return None if math.isnan(v) or math.isinf(v) else round(float(v), 6)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    return v


def _records(df: pd.DataFrame, cols: list[str]) -> list[dict]:
    cols = [c for c in cols if c in df]
    return [{c: _clean(v) for c, v in zip(cols, row)} for row in df[cols].itertuples(index=False)]


def _bins(values: pd.Series, bins) -> list[dict]:
    v = values.dropna()
    return [{"label": lbl, "n": int(((v >= lo) & (v < hi)).sum())} for lo, hi, lbl in bins]


def _equity_series(m: pd.DataFrame, equity: pd.DataFrame, bench: pd.DataFrame, n: int = 3) -> list[dict]:
    top = m[m["ranked"]].sort_values("rank").head(n)
    out = []
    for r in top.itertuples():
        e = equity[equity["vault_address"] == r.vault_address].sort_values("time")
        if len(e) < 2:
            continue
        pts = [[int(t.timestamp() * 1000), round(float(v), 4)] for t, v in zip(e["time"], e["index"])]
        out.append({"name": r.name or r.vault_address[:10], "points": pts})
    btc = bench[bench["coin"] == "BTC"].sort_values("date")
    if out and len(btc):
        start = min(s["points"][0][0] for s in out)
        btc = btc[btc["date"] >= pd.Timestamp(start, unit="ms", tz="UTC").normalize()]
        if len(btc) >= 2:
            base = btc["close"].iloc[0]
            # Weekly points are plenty at this scale and keep the file small.
            b = btc.iloc[::7] if len(btc) > 200 else btc
            out.append({"name": "BTC", "points": [[int(t.timestamp() * 1000), round(float(c / base), 4)]
                                                  for t, c in zip(b["date"], b["close"])]})
    return out


def build_payload(m: pd.DataFrame, closed: pd.DataFrame, equity: pd.DataFrame,
                  bench: pd.DataFrame, summary: dict) -> dict:
    ranked = m[m["ranked"]].sort_values("rank")
    return {
        "version": __version__,
        "summary": {k: _clean(v) if not isinstance(v, dict) else v for k, v in summary.items()},
        "vaults": _records(ranked, SCREENER_COLUMNS),
        "equity": _equity_series(m, equity, bench),
        "drawdown_bins": _bins(ranked["max_drawdown_all"], DD_BINS),
        "lifetime_bins": _bins(closed["lifetime_days"], LIFE_BINS) if "lifetime_days" in closed else [],
    }


def render_html(payload: dict) -> str:
    # "</" inside a <script> block would end it early.
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.read_text().replace("__DATA__", data)


def run_export(cfg: dict, root: Path, date: str | None = None) -> dict:
    date = date or latest_snapshot(root, cfg)
    snap = root / cfg["paths"]["snapshots_dir"] / date
    tdir = snap / "tables"
    if not (tdir / "metrics.parquet").exists():
        raise FileNotFoundError(f"{tdir} has no metrics; run `vaultscan analyze` first")
    m = pd.read_parquet(tdir / "metrics.parquet")
    closed = pd.read_parquet(tdir / "closed_metrics.parquet")
    equity = pd.read_parquet(tdir / "equity.parquet")
    bench = pd.read_parquet(tdir / "benchmarks.parquet")
    summary = json.loads((snap / "analysis.json").read_text())

    out = root / cfg["paths"].get("reports_dir", "reports") / date
    out.mkdir(parents=True, exist_ok=True)
    ranked = m[m["ranked"]].sort_values("rank")
    ranked[[c for c in SCREENER_COLUMNS if c in ranked]].to_csv(out / "screener.csv", index=False)
    closed.sort_values("pnl_all", ascending=False)[[c for c in CLOSED_COLUMNS if c in closed]] \
        .to_csv(out / "closed_vaults.csv", index=False)
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    (out / "report.html").write_text(render_html(build_payload(m, closed, equity, bench, summary)))
    log.info("report written to %s", out)
    return {"snapshot_date": date, "report": str(out / "report.html"), "screener": str(out / "screener.csv"),
            "ranked_vaults": int(len(ranked))}
