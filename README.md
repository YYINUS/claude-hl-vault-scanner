# hl-vault-scanner

Hyperliquid vault screener & analyzer, v0.1. Pipeline: fetch → build → analyze → export.
Built so far: `check` and `fetch` (Mon, Oct 5).

## Setup
    python -m venv .venv && . .venv/bin/activate
    pip install -e ".[dev]"

## Check connectivity
    vaultscan check                       # both hosts + vault list schema

## Fetch a snapshot
    vaultscan fetch                       # today's UTC date, resumes if re-run
    vaultscan fetch --force               # refetch everything
    vaultscan fetch --limit 20            # smoke test on 20 vaults
    vaultscan fetch --vault-list-file vaults.json   # use a saved stats-data response

Output: `data/snapshots/<date>/raw/{vault_list,vault_details,clearinghouse_state,fills,funding,ledger,candles}`,
plus `manifest.json` (counts, coverage, weight used) and `fetch_failures.jsonl`.
Exit code is non-zero when tier-2 coverage is below 95%.

Tiers (config.yaml): all listed vaults → TVL ≥ $10k or closed-with-history get details + positions →
TVL ≥ $100k also get 30 days of fills, funding and ledger updates. Requests share a weight-based
limiter set to 80% of Hyperliquid's 1,200/min, with exponential backoff on 429/5xx.

If the vault list endpoint is unreachable, the run falls back to `seed_vaults` + `data/known_vaults.json`
and adds child vaults found in parent details.

## Tests
    pytest -q      # offline, uses tests/fixtures

See docs/ENDPOINTS.md for verified fields, page limits and history resolution.
