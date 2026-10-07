# Endpoint verification — 2026-10-05 (UTC)

Checked live against `https://api.hyperliquid.xyz/info` on 2026-10-05 02:15–02:30 UTC.
Trimmed copies of each response are in `tests/fixtures/`.

| Endpoint | Status | Fields confirmed | Limits / gotchas |
| --- | --- | --- | --- |
| `stats-data.hyperliquid.xyz/Mainnet/vaults` | OK (allowed 2026-10-05) | list of `{apr, pnls, summary}`; `summary{name, vaultAddress, leader, tvl, isClosed, relationship{type}, createTimeMillis}`; `pnls` = `day, week, month, allTime`, ~12 points each | 14.2 MB, 9,476 vaults (3,094 open, 6,382 closed). **No depositor count.** `vaultscan check` validates this schema |
| `vaultSummaries` | Works, returns `[]` | — | Not usable for discovery |
| `vaultDetails` | OK | `name, vaultAddress, leader, description, portfolio, apr, followerState, leaderFraction, leaderCommission, followers, maxDistributable, maxWithdrawable, isClosed, relationship, allowDeposits, alwaysCloseOnWithdraw` | `followers` capped at 100 rows; `relationship.data.childAddresses` on parents |
| `vaultDetails.portfolio` | OK | 8 windows: `day, week, month, allTime` + `perp*` versions; each has `accountValueHistory`, `pnlHistory`, `vlm` | See resolution table below |
| `clearinghouseState` | OK | `marginSummary{accountValue,totalNtlPos,totalRawUsd,totalMarginUsed}`, `crossMarginSummary`, `assetPositions[].position{coin,szi,leverage,entryPx,positionValue,unrealizedPnl,liquidationPx,marginUsed,cumFunding}` | Weight 2 |
| `userFillsByTime` | OK | `coin, px, sz, side, time, startPosition, dir, closedPnl, hash, oid, crossed, fee, tid, feeToken` | 2,000 rows/page, ascending, **most recent first-served**: page backwards via `endTime`. HLP Strategy A exposed only ~2,006 fills (~20 min) |
| `userFunding` | OK | `time, hash, delta{type,coin,usdc,szi,fundingRate,nSamples}` | 500 rows/page, ascending from `startTime`. HLP strategy vaults: ~128k rows / 30 days |
| `userNonFundingLedgerUpdates` | OK | `time, hash, delta{type,vault,usdc}`; types seen: `vaultDeposit, vaultWithdraw` | 2,000 rows/page, ascending |
| `candleSnapshot` (1d) | OK | `t, T, s, i, o, c, h, l, v, n` | Up to 5,000 candles/call |
| `leadingVaults`, `userVaultEquities` | OK | `address, name` / `vaultAddress, equity, lockedUntilTimestamp` | Possible discovery aids, not used yet |

## PnL history resolution (HLP, 2026-10-05)

| Window | Points | Span | Median spacing |
| --- | --- | --- | --- |
| day | 46 | 24 h | 26 min |
| week | 65 | 7 days | 2.9 h |
| month | 46 | 30 days | 12.3 h |
| allTime | 100 | since 2023-05-10 | **14 days** |

Implication: daily return, volatility, Sharpe/Sortino and drawdown can only be computed precisely
for the last 30 days. 90-day and all-time metrics come from 2-week points. Our nightly snapshots
build true daily history from 2026-10-05 onward.

## Open questions for the hand-check (task 8)

- HLP's listed TVL ($181.6M) = parent account value ($42.0M) + its 7 children's listed TVL ($139.6M,
  incl. Strategy X $100.4M). So HLP's portfolio history is consolidated: analyze HLP at parent level and
  never add children on top of it.
- Child vaults report `leaderFraction` = 1.0 (wholly owned by HLP), so leader-stake flags must
  skip HLP children.

## Vault list snapshot (2026-10-05 02:40 UTC)

| Group | Vaults |
| --- | --- |
| All listed | 9,476 |
| Open | 3,094 (1 parent, 7 HLP children) |
| Open, TVL ≥ $10k (tier 2) | 235 |
| TVL ≥ $100k (tier 3) | 83 |
| Closed with any PnL history | 5,195 |
| Closed, PnL ever ≥ $1k (fetched, cached) | 1,837 |

Listed TVL sums to $421.8M, but this double counts: HLP's $181.6M already includes its child vaults.
HLP Strategy X lists $100.4M TVL yet its clearinghouse account value was $1,000 on Oct 5. To check in task 8.
