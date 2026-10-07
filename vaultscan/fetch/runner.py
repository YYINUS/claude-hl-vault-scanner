"""Tiered snapshot fetch: vault list -> details/positions -> fills/funding/ledger.

Every raw response is written to data/snapshots/<date>/raw/ before any parsing.
Re-running the same day resumes: files already on disk are skipped unless
`force=True`.
"""
from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import vault_list
from .client import HLClient, WeightLimiter

log = logging.getLogger(__name__)
DAY_MS = 86_400_000
PAGE = {"userFillsByTime": 2000, "userFunding": 500, "userNonFundingLedgerUpdates": 2000}


def now_ms() -> int:
    return int(time.time() * 1000)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    with open(path) as f:
        return json.load(f)


class Snapshot:
    def __init__(self, cfg: dict, root: Path, date: str | None = None, force: bool = False,
                 limit: int | None = None, vault_list_file: str | None = None):
        self.cfg = cfg
        self.root = root
        self.date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.dir = root / cfg["paths"]["snapshots_dir"] / self.date
        self.raw = self.dir / "raw"
        self.force = force
        self.limit = limit
        self.vault_list_file = vault_list_file
        self.failures: list[dict] = []
        self.counts: dict[str, int] = {}
        self.notes: list[str] = []
        a, rl = cfg["api"], cfg["rate_limit"]
        self.limiter = WeightLimiter(rl["weight_per_minute"], rl["utilization"])
        self.client = HLClient(a["info_url"], a["vault_list_url"], self.limiter, a["timeout_s"],
                               a["max_retries"], a["backoff_base_s"], a["backoff_max_s"])
        self.fetch_started_ms = now_ms()
        self.closed_dir = root / cfg["paths"]["closed_vaults_dir"]
        self.closed_sel: list[str] = []

    # ---------- helpers ----------
    def _fail(self, stage: str, addr: str | None, err: Exception) -> None:
        rec = {"stage": stage, "vault": addr, "error": f"{type(err).__name__}: {err}", "time_ms": now_ms()}
        self.failures.append(rec)
        with open(self.dir / "fetch_failures.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
        log.error("FAIL %s %s: %s", stage, addr, err)

    def _bump(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def _run_parallel(self, stage: str, addrs: list[str], fn: Callable[[str], Any]) -> None:
        workers = self.cfg["rate_limit"]["workers"]
        done, t0 = 0, time.time()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fn, a): a for a in addrs}
            for fut in as_completed(futs):
                a = futs[fut]
                try:
                    fut.result()
                    self._bump(f"{stage}_ok")
                except Exception as e:  # noqa: BLE001
                    self._fail(stage, a, e)
                done += 1
                if done % 50 == 0 or done == len(addrs):
                    log.info("%s: %d/%d (%.0fs, weight used %d)", stage, done, len(addrs),
                             time.time() - t0, self.limiter.total_weight)

    # ---------- stage 1: discovery ----------
    def discover(self) -> list[dict]:
        p = self.raw / "vault_list.json"
        raw = None
        if p.exists() and not self.force:
            raw = read_json(p)
        elif self.vault_list_file:
            raw = read_json(Path(self.vault_list_file))
            write_json(p, raw)
            self.notes.append(f"vault list loaded from file {self.vault_list_file}")
        else:
            try:
                raw = self.client.vault_list()
                write_json(p, raw)
            except Exception as e:  # noqa: BLE001
                self._fail("vault_list", None, e)
        if raw is not None:
            records = vault_list.normalize(raw)
            self.counts["vault_list_source"] = "stats-data" if not self.vault_list_file else "file"
        else:
            known = self.root / self.cfg["paths"]["known_vaults_file"]
            addrs = list(self.cfg.get("seed_vaults", []))
            if known.exists():
                addrs += read_json(known)
            records = vault_list.from_addresses(sorted(set(a.lower() for a in addrs)))
            self.counts["vault_list_source"] = "fallback"
            self.notes.append("vault list endpoint unreachable; used seed + last known addresses "
                              "(child vaults are added from parent details)")
        seen = {r["vault_address"] for r in records}
        for s in self.cfg.get("seed_vaults", []):
            if s.lower() not in seen:
                records += vault_list.from_addresses([s])
        write_json(self.dir / "vaults_list_normalized.json", records)
        self.counts["vaults_listed"] = len(records)
        return records

    # ---------- stage 2: details + positions ----------
    def _details(self, addr: str) -> None:
        p = self.raw / "vault_details" / f"{addr}.json"
        if self.force or not p.exists():
            write_json(p, self.client.info({"type": "vaultDetails", "vaultAddress": addr}))

    def _positions(self, addr: str) -> None:
        p = self.raw / "clearinghouse_state" / f"{addr}.json"
        if self.force or not p.exists():
            write_json(p, self.client.info({"type": "clearinghouseState", "user": addr}))

    def _closed_details(self, addr: str) -> None:
        # Closed vaults don't change, so their details live in a cache shared by
        # all snapshots and are fetched once.
        p = self.closed_dir / f"{addr}.json"
        if self.force or not p.exists():
            write_json(p, self.client.info({"type": "vaultDetails", "vaultAddress": addr}))

    def tier2(self, records: list[dict]) -> list[str]:
        t = self.cfg["tiers"]
        sel = [r["vault_address"] for r in records
               if not r["is_closed"] and (r.get("from_fallback") or (r["tvl"] or 0) >= t["tier2_min_tvl"])]
        if self.limit:
            sel = sel[: self.limit]
        self._run_parallel("vault_details", sel, self._details)
        # Expand: child vaults named in parent details that the list didn't include.
        extra = []
        known = {r["vault_address"] for r in records}
        for a in sel:
            p = self.raw / "vault_details" / f"{a}.json"
            if p.exists():
                rel = (read_json(p) or {}).get("relationship") or {}
                for c in (rel.get("data") or {}).get("childAddresses", []) if rel.get("type") == "parent" else []:
                    if c.lower() not in known:
                        extra.append(c.lower())
                        known.add(c.lower())
        if extra:
            self.notes.append(f"added {len(extra)} child vaults from parent details")
            self._run_parallel("vault_details", extra, self._details)
            sel += extra
        self._run_parallel("clearinghouse_state", sel, self._positions)
        self.counts["tier2_vaults"] = len(sel)

        # Closed vaults: details only (no open positions), cached across snapshots.
        if t["tier2_include_closed_with_history"]:
            min_pnl = t.get("closed_min_abs_pnl", 0)
            closed = [r["vault_address"] for r in records
                      if r["is_closed"] and r["has_history"] and (r.get("max_abs_pnl") or 0) >= min_pnl]
            if self.limit:
                closed = closed[: self.limit]
            todo = [a for a in closed if self.force or not (self.closed_dir / f"{a}.json").exists()]
            self._run_parallel("closed_details", todo, self._closed_details)
            write_json(self.dir / "closed_vaults_included.json", closed)
            self.counts["closed_vaults"] = len(closed)
            self.counts["closed_fetched_this_run"] = len(todo)
            self.closed_sel = closed
        return sel

    # ---------- stage 3: fills, funding, ledger ----------
    def _paged_forward(self, req_type: str, addr: str, start: int, max_pages: int) -> dict:
        pages, rows, seen, cur = [], [], set(), start
        truncated = False
        for i in range(max_pages):
            data = self.client.info({"type": req_type, "user": addr, "startTime": cur, "endTime": self.fetch_started_ms})
            pages.append({"startTime": cur, "n": len(data)})
            for x in data:
                k = json.dumps(x, sort_keys=True)
                if k not in seen:
                    seen.add(k)
                    rows.append(x)
            if len(data) < PAGE[req_type]:
                break
            last = max(x["time"] for x in data)
            cur = last if last > cur else cur + 1
        else:
            truncated = True
        # Rows run forward from startTime, so a truncated result covers
        # [startTime, covered_to_ms] only.
        return {"request": {"type": req_type, "user": addr, "startTime": start, "endTime": self.fetch_started_ms},
                "pages": pages, "truncated": truncated,
                "covered_to_ms": max((x["time"] for x in rows), default=None), "rows": rows}

    def _fills(self, addr: str, start: int, max_pages: int) -> dict:
        # userFillsByTime returns the most recent PAGE fills in the window (ascending),
        # so we page backwards by moving endTime.
        pages, rows, seen, end = [], [], set(), self.fetch_started_ms
        truncated, reached_start = False, False
        for _ in range(max_pages):
            data = self.client.info({"type": "userFillsByTime", "user": addr, "startTime": start,
                                     "endTime": end, "aggregateByTime": False})
            pages.append({"endTime": end, "n": len(data)})
            for x in data:
                k = (x.get("tid"), x.get("oid"), x.get("time"))
                if k not in seen:
                    seen.add(k)
                    rows.append(x)
            if len(data) < PAGE["userFillsByTime"]:
                reached_start = True
                break
            first = min(x["time"] for x in data)
            end = first if first < end else end - 1
        if not reached_start:
            truncated = True
        rows.sort(key=lambda x: x["time"])
        # The API only serves a vault's most recent fills (observed: ~2,000 for HLP
        # strategy vaults on 2026-10-05, documented as up to 10,000). A short page
        # after >= 2,000 rows means we hit that horizon, not the start of the window.
        history_capped = (not truncated) and len(rows) >= PAGE["userFillsByTime"]
        return {"request": {"type": "userFillsByTime", "user": addr, "startTime": start,
                            "endTime": self.fetch_started_ms},
                "pages": pages, "truncated": truncated, "history_capped": history_capped,
                "covered_from_ms": rows[0]["time"] if rows else None,
                "covered_to_ms": rows[-1]["time"] if rows else None, "rows": rows}

    def _tier3_one(self, addr: str) -> None:
        t, pg = self.cfg["tiers"], self.cfg["pagination"]
        start = self.fetch_started_ms - t["tier3_lookback_days"] * DAY_MS
        jobs = [
            ("fills", lambda: self._fills(addr, start, pg["fills_max_pages"])),
            ("funding", lambda: self._paged_forward("userFunding", addr, start, pg["funding_max_pages"])),
            ("ledger", lambda: self._paged_forward("userNonFundingLedgerUpdates", addr, start, pg["ledger_max_pages"])),
        ]
        errs = []
        for name, fn in jobs:
            p = self.raw / name / f"{addr}.json"
            if p.exists() and not self.force:
                continue
            try:
                write_json(p, fn())
            except Exception as e:  # noqa: BLE001
                errs.append(f"{name}: {e}")
        if errs:
            raise RuntimeError("; ".join(errs))

    def tier3(self, tier2_addrs: list[str], records: list[dict]) -> list[str]:
        min_tvl = self.cfg["tiers"]["tier3_min_tvl"]
        tvl = {r["vault_address"]: r["tvl"] for r in records}
        sel = []
        for a in tier2_addrs:
            v = tvl.get(a)
            if v is None:  # fallback/child vaults: read TVL from positions
                p = self.raw / "clearinghouse_state" / f"{a}.json"
                if p.exists():
                    v = float(read_json(p)["marginSummary"]["accountValue"])
            if (v or 0) >= min_tvl:
                sel.append(a)
        self._run_parallel("tier3", sel, self._tier3_one)
        self.counts["tier3_vaults"] = len(sel)
        return sel

    # ---------- benchmarks ----------
    def benchmarks(self) -> None:
        b = self.cfg["benchmarks"]
        for coin in b["coins"]:
            p = self.raw / "candles" / f"{coin}_{b['interval']}.json"
            if p.exists() and not self.force:
                continue
            try:
                end = self.fetch_started_ms
                start = end - b["lookback_days"] * DAY_MS
                rows, cur = [], start
                while True:  # candleSnapshot caps at 5,000 candles per call
                    data = self.client.info({"type": "candleSnapshot", "req": {
                        "coin": coin, "interval": b["interval"], "startTime": cur, "endTime": end}})
                    rows += [c for c in data if not rows or c["t"] > rows[-1]["t"]]
                    if len(data) < 5000:
                        break
                    cur = data[-1]["t"] + 1
                write_json(p, rows)
                self._bump("benchmarks_ok")
            except Exception as e:  # noqa: BLE001
                self._fail("candles", coin, e)

    # ---------- orchestration ----------
    def run(self) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        records = self.discover()
        t2 = self.tier2(records)
        t3 = self.tier3(t2, records)
        self.benchmarks()
        # Update the fallback address list with everything we've seen.
        known_p = self.root / self.cfg["paths"]["known_vaults_file"]
        known = set(read_json(known_p)) if known_p.exists() else set()
        known |= {r["vault_address"] for r in records} | set(t2)
        write_json(known_p, sorted(known))

        n2 = len(t2)
        ok2 = sum(1 for a in t2 if (self.raw / "vault_details" / f"{a}.json").exists()
                  and (self.raw / "clearinghouse_state" / f"{a}.json").exists())
        manifest = {
            "snapshot_date": self.date,
            "fetch_started_utc": datetime.fromtimestamp(self.fetch_started_ms / 1000, timezone.utc).isoformat(),
            "fetch_finished_utc": datetime.now(timezone.utc).isoformat(),
            "duration_s": round(time.time() - t0, 1),
            "counts": self.counts,
            "coverage_tier2": round(ok2 / n2, 4) if n2 else None,
            "coverage_closed": (round(sum((self.closed_dir / f"{a}.json").exists() for a in self.closed_sel)
                                      / len(self.closed_sel), 4) if self.closed_sel else None),
            "tier3_vaults": len(t3),
            "failures": len(self.failures),
            "http": self.client.stats,
            "weight_used": round(self.limiter.total_weight),
            "notes": self.notes,
        }
        write_json(self.dir / "manifest.json", manifest)
        self.client.close()
        return manifest
