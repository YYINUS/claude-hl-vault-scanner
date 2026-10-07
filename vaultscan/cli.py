"""vaultscan command-line tool: fetch | build | analyze | export."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import load_config


def cmd_fetch(args) -> int:
    from .fetch import Snapshot
    cfg = load_config(args.config)
    snap = Snapshot(cfg, Path(args.root), date=args.date, force=args.force,
                    limit=args.limit, vault_list_file=args.vault_list_file)
    manifest = snap.run()
    print(json.dumps(manifest, indent=2))
    cov = manifest.get("coverage_tier2") or 0
    return 0 if cov >= 0.95 else 1


def cmd_check(args) -> int:
    from .fetch.check import run_checks
    results = run_checks(load_config(args.config))
    for r in results:
        print(f"{'OK  ' if r['ok'] else 'FAIL'} {r['host']:<28} {r['seconds']:>6.2f}s  {r['detail']}")
    return 0 if all(r["ok"] for r in results) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vaultscan")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--root", default=".")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="test both Hyperliquid hosts and the vault list schema")
    f = sub.add_parser("fetch", help="fetch a raw snapshot")
    f.add_argument("--date", help="snapshot date folder (default: today UTC)")
    f.add_argument("--force", action="store_true", help="refetch files already on disk")
    f.add_argument("--limit", type=int, help="cap tier-2 vaults (for smoke tests)")
    f.add_argument("--vault-list-file", help="use a saved vault list JSON instead of stats-data")
    for name in ("build", "analyze", "export"):
        sub.add_parser(name, help=f"{name} (not built yet)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "fetch":
        return cmd_fetch(args)
    print(f"'{args.cmd}' is scheduled for a later day of the build.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
