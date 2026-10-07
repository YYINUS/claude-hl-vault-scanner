"""Offline tests for the fetch layer, using recorded responses in tests/fixtures."""
import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from vaultscan.config import load_config
from vaultscan.fetch import vault_list
from vaultscan.fetch.client import WeightLimiter, request_weight
from vaultscan.fetch.runner import Snapshot

ROOT = Path(__file__).resolve().parents[1]
FX = ROOT / "tests" / "fixtures"
INFO = "https://api.hyperliquid.xyz/info"
LIST = "https://stats-data.hyperliquid.xyz/Mainnet/vaults"
HLP = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"
CHILD = "0x010461c14e146ac35fe42271bdc1134ee31c703a"
CLOSED_BIG = "0x00043d4a2c258892172dc6ce5f62e2e3936ae0dc"    # closed, PnL reached $14.1k
CLOSED_SMALL = "0x003d21eb79fcad447e4da9240655a002d290e0bc"  # closed, PnL never above $12
TINY = "0x00161c88c702c33d5b02f0648da891b89f14789c"          # open, TVL $109


def fx(name):
    return json.loads((FX / name).read_text())


@pytest.fixture
def cfg(tmp_path):
    c = load_config(ROOT / "config.yaml")
    c["rate_limit"]["weight_per_minute"] = 10**9  # no throttling in tests
    c["rate_limit"]["workers"] = 2
    c["api"]["backoff_base_s"] = 0.001
    c["benchmarks"]["coins"] = ["BTC"]
    return c


def test_weights():
    assert request_weight("vaultDetails") == 20
    assert request_weight("clearinghouseState") == 2
    assert request_weight("userFillsByTime", 2000) == 120
    assert request_weight("candleSnapshot", 400) == 26


def test_limiter_blocks_when_empty():
    lim = WeightLimiter(per_minute=600, utilization=1.0)  # 10 weight/s
    lim.tokens = 0
    t = time.monotonic()
    lim.acquire(5)
    assert 0.4 < time.monotonic() - t < 1.5


def test_normalize_recorded_list():
    recs = vault_list.normalize(fx("vault_list_sample.json"))
    by = {r["vault_address"]: r for r in recs}
    assert by[HLP]["relationship"] == "parent" and by[HLP]["tvl"] > 1e8
    assert by[CLOSED_BIG]["is_closed"] and by[CLOSED_BIG]["has_history"]
    assert by[CLOSED_BIG]["max_abs_pnl"] > 1000 > by[CLOSED_SMALL]["max_abs_pnl"]
    assert not by[TINY]["is_closed"] and by[TINY]["tvl"] < 10_000


def _router(fills_pages=None, fail_first=0):
    calls = {"n": 0, "fills": 0}
    details = {HLP: fx("vaultDetails_hlp_parent.json"), CHILD: fx("vaultDetails_hlp_child.json")}

    def info(request):
        calls["n"] += 1
        if calls["n"] <= fail_first:
            return httpx.Response(429)
        body = json.loads(request.content)
        t = body["type"]
        u = body.get("vaultAddress") or body.get("user")
        if t == "vaultDetails":
            d = details.get(u, {**fx("vaultDetails_hlp_child.json"), "vaultAddress": u, "relationship": {"type": "normal"}})
            return httpx.Response(200, json=d)
        if t == "clearinghouseState":
            return httpx.Response(200, json=fx("clearinghouseState_child.json"))
        if t == "userFillsByTime":
            calls["fills"] += 1
            if fills_pages:
                return httpx.Response(200, json=fills_pages(body))
            return httpx.Response(200, json=fx("userFillsByTime_child.json"))
        if t == "userFunding":
            return httpx.Response(200, json=fx("userFunding_child.json"))
        if t == "userNonFundingLedgerUpdates":
            return httpx.Response(200, json=fx("userNonFundingLedgerUpdates_hlp.json"))
        if t == "candleSnapshot":
            return httpx.Response(200, json=fx("candleSnapshot_BTC_1d.json"))
        return httpx.Response(400)

    return info, calls


@respx.mock
def test_full_run_with_list(cfg, tmp_path):
    respx.get(LIST).mock(return_value=httpx.Response(200, json=fx("vault_list_sample.json")))
    info, calls = _router(fail_first=2)  # also exercises 429 retry
    respx.post(INFO).mock(side_effect=info)
    m = Snapshot(cfg, tmp_path, date="2026-10-05").run()
    raw = tmp_path / "data/snapshots/2026-10-05/raw"
    # Tiny open vault skipped; closed vault above the PnL bar goes to the closed cache only;
    # closed vault below the bar skipped; HLP children added from parent details.
    assert (raw / "vault_details" / f"{HLP}.json").exists()
    assert (raw / "vault_details" / f"{CHILD}.json").exists()
    assert not (raw / "vault_details" / f"{TINY}.json").exists()
    assert (tmp_path / "data/closed_vaults" / f"{CLOSED_BIG}.json").exists()
    assert not (tmp_path / "data/closed_vaults" / f"{CLOSED_SMALL}.json").exists()
    assert not (raw / "clearinghouse_state" / f"{CLOSED_BIG}.json").exists()
    assert m["coverage_tier2"] == 1.0 and m["coverage_closed"] == 1.0 and m["failures"] == 0
    assert m["http"]["retries"] >= 2
    # Tier 3 = TVL >= 100k: HLP (list TVL) + 7 children (TVL from positions fixture, $3.2M)
    assert m["tier3_vaults"] == 8
    assert (raw / "fills" / f"{CHILD}.json").exists()
    assert json.loads((tmp_path / "data/known_vaults.json").read_text())


@respx.mock
def test_fallback_when_list_blocked(cfg, tmp_path):
    respx.get(LIST).mock(return_value=httpx.Response(403))
    info, _ = _router()
    respx.post(INFO).mock(side_effect=info)
    m = Snapshot(cfg, tmp_path, date="2026-10-05").run()
    assert m["counts"]["vault_list_source"] == "fallback"
    assert m["counts"]["tier2_vaults"] == 8  # HLP seed + its 7 child vaults
    assert any(f["stage"] == "vault_list" for f in
               map(json.loads, (tmp_path / "data/snapshots/2026-10-05/fetch_failures.jsonl").read_text().splitlines()))


@respx.mock
def test_fills_paginate_backwards_and_flag_truncation(cfg, tmp_path):
    cfg["pagination"]["fills_max_pages"] = 3
    base = 1_791_000_000_000

    def pages(body):
        end = body["endTime"]
        # Every page is full (2000 rows) ending just before endTime -> never reaches start.
        return [{"tid": end - i, "oid": 1, "time": end - 2000 + i, "coin": "BTC"} for i in range(2000)]

    respx.get(LIST).mock(return_value=httpx.Response(200, json=fx("vault_list_sample.json")))
    info, calls = _router(fills_pages=pages)
    respx.post(INFO).mock(side_effect=info)
    snap = Snapshot(cfg, tmp_path, date="2026-10-05")
    snap.fetch_started_ms = base
    out = snap._fills(CHILD, base - 30 * 86_400_000, 3)
    assert len(out["pages"]) == 3 and out["truncated"]
    assert out["pages"][1]["endTime"] < out["pages"][0]["endTime"]
    assert len(out["rows"]) == 6000


@respx.mock
def test_resume_skips_existing(cfg, tmp_path):
    respx.get(LIST).mock(return_value=httpx.Response(200, json=fx("vault_list_sample.json")))
    info, calls = _router()
    respx.post(INFO).mock(side_effect=info)
    Snapshot(cfg, tmp_path, date="2026-10-05").run()
    first = calls["n"]
    Snapshot(cfg, tmp_path, date="2026-10-05").run()
    assert calls["n"] == first  # nothing refetched


@respx.mock
def test_closed_cache_shared_across_snapshots(cfg, tmp_path):
    respx.get(LIST).mock(return_value=httpx.Response(200, json=fx("vault_list_sample.json")))
    info, calls = _router()
    respx.post(INFO).mock(side_effect=info)
    m1 = Snapshot(cfg, tmp_path, date="2026-10-05").run()
    m2 = Snapshot(cfg, tmp_path, date="2026-10-06").run()
    assert m1["counts"]["closed_fetched_this_run"] == 1
    assert m2["counts"]["closed_fetched_this_run"] == 0 and m2["coverage_closed"] == 1.0


@respx.mock
def test_check_reports_blocked_list(cfg):
    from vaultscan.fetch.check import run_checks
    respx.post(INFO).mock(return_value=httpx.Response(200, json={"universe": [{"name": "BTC"}]}))
    respx.get(LIST).mock(return_value=httpx.Response(403))
    api, lst = run_checks(cfg)
    assert api["ok"] and not lst["ok"] and "allowed domains" in lst["detail"]


@respx.mock
def test_check_validates_list_schema(cfg):
    from vaultscan.fetch.check import run_checks
    respx.post(INFO).mock(return_value=httpx.Response(200, json={"universe": []}))
    respx.get(LIST).mock(return_value=httpx.Response(200, json=fx("vault_list_sample.json")))
    assert run_checks(cfg)[1]["ok"]
    respx.get(LIST).mock(return_value=httpx.Response(200, json=[{"summary": {"name": "x"}}]))
    bad = run_checks(cfg)[1]
    assert not bad["ok"] and "SCHEMA" in bad["detail"]
