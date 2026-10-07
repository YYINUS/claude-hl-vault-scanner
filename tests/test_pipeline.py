"""Offline tests for build -> analyze -> export, on a snapshot fetched from recorded fixtures."""
import json

import httpx
import pandas as pd
import pytest
import respx

from test_fetch import CHILD, CLOSED_BIG, HLP, INFO, LIST, _router, cfg, fx  # noqa: F401  (cfg is a fixture)
from vaultscan.analyze.metrics import max_drawdown, run_analyze, twr_index
from vaultscan.export.report import run_export
from vaultscan.fetch.runner import Snapshot
from vaultscan.store.build import run_build

DATE = "2026-10-05"


@pytest.fixture
def snapshot(cfg, tmp_path):
    with respx.mock:
        respx.get(LIST).mock(return_value=httpx.Response(200, json=fx("vault_list_sample.json")))
        info, _ = _router()
        respx.post(INFO).mock(side_effect=info)
        Snapshot(cfg, tmp_path, date=DATE).run()
    return tmp_path


def _idx(av, pnl):
    t = pd.date_range("2026-01-01", periods=len(av), freq="D", tz="UTC")
    return twr_index(pd.Series(av, index=t, dtype=float), pd.Series(pnl, index=t, dtype=float))


def test_twr_ignores_deposits():
    # +10% on 1,000, then a 9,000 deposit with no PnL, then +10% on 11,000.
    idx = _idx([1000, 1100, 10100, 11100], [0, 100, 100, 1100])
    assert idx.iloc[-1] == pytest.approx(1.1 * 1.0 * (1 + 1000 / 10100))
    # Withdrawal of nearly everything is not a loss.
    idx = _idx([1000, 200, 220], [0, 0, 20])
    assert idx.iloc[-1] == pytest.approx(1.1)


def test_twr_skips_unfunded_intervals():
    idx = _idx([0, 0, 1000, 1050], [0, 0, 0, 50])
    assert list(idx.round(4)) == [1.0, 1.0, 1.0, 1.05]


def test_max_drawdown():
    s = pd.Series([1.0, 1.2, 0.9, 1.3, 1.17])
    assert max_drawdown(s) == pytest.approx(0.9 / 1.2 - 1)


def test_build_tables(cfg, snapshot):
    res = run_build(cfg, snapshot)
    assert res["snapshot_date"] == DATE
    tdir = snapshot / "data/snapshots" / DATE / "tables"
    v = pd.read_parquet(tdir / "vaults.parquet").set_index("vault_address")
    assert v.loc[HLP, "relationship"] == "parent" and v.loc[HLP, "n_children"] == 7
    assert v.loc[CHILD, "has_positions"] and v.loc[CHILD, "account_value"] > 3e6
    assert v.loc[CLOSED_BIG, "is_closed"] and v.loc[CLOSED_BIG, "has_details"]
    h = pd.read_parquet(tdir / "history.parquet")
    assert set(h["window"]) == {"day", "week", "month", "allTime"}
    assert (h[(h.vault_address == HLP) & (h.window == "allTime")]).shape[0] == 100
    assert res["rows"]["positions"] > 0 and res["rows"]["fills"] > 0 and res["rows"]["benchmarks"] > 0
    led = pd.read_parquet(tdir / "ledger.parquet")
    assert (led["net_flow"].dropna() != 0).any()


def test_analyze_and_rank(cfg, snapshot):
    run_build(cfg, snapshot)
    cfg["analyze"] = {"min_daily_points": 5}
    s = run_analyze(cfg, snapshot)
    tdir = snapshot / "data/snapshots" / DATE / "tables"
    m = pd.read_parquet(tdir / "metrics.parquet").set_index("vault_address")
    hlp = m.loc[HLP]
    assert hlp["ranked"] and 0 < hlp["score"] <= 100
    assert -1 < hlp["max_drawdown_all"] <= 0 and hlp["history_days"] > 800
    assert pd.notna(hlp["return_30d"]) and pd.notna(hlp["volatility_30d"])
    # HLP children are never ranked (their numbers are inside HLP's).
    assert not m.loc[CHILD, "ranked"]
    assert "low_leader_stake" in hlp["flags"]
    assert "low_leader_stake" not in m.loc[CHILD, "flags"]
    assert pd.notna(m.loc[CHILD, "trades_30d"])
    c = pd.read_parquet(tdir / "closed_metrics.parquet")
    assert CLOSED_BIG in set(c["vault_address"])
    assert s["ranked_vaults"] >= 1 and s["tvl_total"] > 1e8
    assert json.loads((tdir.parent / "analysis.json").read_text())["snapshot_date"] == DATE


def test_export_report(cfg, snapshot):
    run_build(cfg, snapshot)
    cfg["analyze"] = {"min_daily_points": 5}
    run_analyze(cfg, snapshot)
    out = run_export(cfg, snapshot)
    rdir = snapshot / "reports" / DATE
    assert out["report"] == str(rdir / "report.html")
    html = (rdir / "report.html").read_text()
    assert "Hyperliquidity Provider (HLP)" in html and "</html>" in html
    assert "__DATA__" not in html
    screener = pd.read_csv(rdir / "screener.csv")
    assert screener.iloc[0]["vault_address"] == HLP and "score" in screener
    assert (rdir / "summary.json").exists()


def test_last_change_ignores_idle_balance():
    from vaultscan.analyze.metrics import last_change
    t = pd.date_range("2025-01-01", periods=6, freq="14D", tz="UTC")
    h = pd.DataFrame({"time": t, "account_value": [0, 5000, 7000, 312.5, 312.5, 312.5],
                      "pnl": [0, 0, 2000, -1000, -1000, -1000]})
    assert last_change(h) == t[3]
