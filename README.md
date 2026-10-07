# hl-vault-scanner

Hyperliquid vault screener & analyzer, v0.2. Pipeline: fetch → build → analyze → export.

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

## Build, analyze, export
    vaultscan report                      # all three on the latest snapshot
    vaultscan build   [--date D]          # raw JSON -> data/snapshots/D/tables/*.parquet
    vaultscan analyze [--date D]          # -> metrics.parquet, closed_metrics.parquet, equity.parquet, analysis.json
    vaultscan export  [--date D]          # -> reports/D/{report.html, screener.csv, closed_vaults.csv, summary.json}

**Tables** (`build`): `vaults` (list + details + positions summary, one row per vault), `history`
(account value and PnL for the day/week/month/allTime windows), `positions`, `fills`, `funding`,
`ledger` (deposits/withdrawals), `benchmarks` (BTC/ETH daily candles).

**Metrics** (`analyze`): returns are time-weighted (Modified Dietz per history interval, chained), so
deposits and withdrawals don't count as performance. 30-day metrics (return, annualized volatility,
Sharpe, Sortino, drawdown, beta/correlation to BTC) use daily points from the month window; all-time
metrics (return, max drawdown, and CAGR once a vault has 180 days of history) use the ~14-day allTime points. Also: leverage, largest-position
share, 30-day trades/volume/fees/funding/net flows (TVL ≥ $100k vaults), and risk flags
(`low_leader_stake`, `high_leverage`, `concentrated`, `deep_drawdown`, `young`, `deposits_closed`,
`tvl_mismatch`; thresholds in `config.yaml` under `analyze`).

**Ranking**: open, top-level vaults (HLP children are inside HLP) with TVL ≥ $10k, age ≥ 30 days and
≥ 10 daily returns. Score 0–100 = average percentile of 30-day Sharpe, all-time CAGR, all-time max
drawdown, log TVL and age. It is a screen, not a forecast.

**Report** (`export`): one self-contained HTML file (no external scripts, opens offline) with headline
numbers, risk-vs-return scatter, top-15 bars, growth of $1 for the top 3 vs BTC, drawdown and
closed-vault lifetime histograms, and a sortable, filterable screener table.

## Automation
`.github/workflows/nightly.yml` runs at 00:30 UTC: fetch → report. The raw snapshot and the report are
uploaded as 90-day artifacts; the manifest, `metrics.parquet` and the report go to the `data` branch
under `snapshots/<date>/` and `reports/<date>/` (latest copy in `reports/latest/`).
`.github/workflows/report.yml` (manual) re-runs build → analyze → export on an existing snapshot
artifact without refetching.

## Tests
    pytest -q      # offline, uses tests/fixtures

See docs/ENDPOINTS.md for verified fields, page limits and history resolution.
