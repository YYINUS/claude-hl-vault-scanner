# Handoff for the Claude session that pushes this repo

Repo: https://github.com/YYINUS/claude-hl-vault-scanner

1. Copy the contents of this folder into the repo root. Keep `.gitignore` and `.github/workflows/nightly.yml`
   (add `*.egg-info/` to `.gitignore`).
2. `pip install -e ".[dev]" && pytest -q`: expect 10 passed.
   `vaultscan check` needs network access to api.hyperliquid.xyz and stats-data.hyperliquid.xyz.
   If this session's network policy blocks them, skip it: the nightly job runs on GitHub's runners, which can reach both.
3. Commit and push to main.
4. Trigger the `nightly-snapshot` workflow (workflow_dispatch) and report the result.
   The repo owner must first set Settings → Actions → General → Workflow permissions to "Read and write".
   Expected: about 80 min on the first run (1,837 closed vaults fetched once, then cached), about 45 min afterwards.
   It creates a `data` branch with the manifest and the normalized vault list. The raw snapshot is a 90-day artifact.
