#!/usr/bin/env bash
# Commit a snapshot's small outputs to the `data` branch.
# Usage: publish-data.sh <date>   (run from the repo root, after `vaultscan report`)
set -euo pipefail
D="$1"
SNAP="data/snapshots/$D"
REP="reports/$D"
OUT="$(mktemp -d)"

mkdir -p "$OUT/snapshots/$D"
for f in manifest.json vaults_list_normalized.json fetch_failures.jsonl analysis.json; do
  [ -f "$SNAP/$f" ] && cp "$SNAP/$f" "$OUT/snapshots/$D/"
done
[ -f "$SNAP/tables/metrics.parquet" ] && cp "$SNAP/tables/metrics.parquet" "$OUT/snapshots/$D/"
if [ -d "$REP" ]; then
  mkdir -p "$OUT/reports/$D" "$OUT/reports/latest"
  cp "$REP"/* "$OUT/reports/$D/"
  cp "$REP"/* "$OUT/reports/latest/"
fi
[ -f data/known_vaults.json ] && cp data/known_vaults.json "$OUT/"
rm -rf reports  # copied above; keeps the branch switch from touching local files

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git fetch origin data || true
if git rev-parse --verify origin/data >/dev/null 2>&1; then
  git switch -C data origin/data
else
  git switch --orphan data && git rm -rf --quiet . || true
fi
rm -rf "snapshots/$D" "reports/$D" reports/latest
cp -r "$OUT"/* .
git add snapshots reports known_vaults.json 2>/dev/null || git add snapshots known_vaults.json
git commit -m "snapshot $D" || echo "nothing to commit"
git push origin data
