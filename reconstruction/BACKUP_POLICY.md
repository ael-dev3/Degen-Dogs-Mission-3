# Backup policy

## Commit to repo

- Source scripts.
- SQL files.
- Docs and runbooks.
- Verified config files.
- Small generated CSV/JSON required for the static site.
- Manifests and public search indexes.
- Archive schemas and reconciliation reports.

## Do not commit

- `.env`, `.env.local`, API keys, RPC secrets, private keys.
- `node_modules/`.
- `dist/`.
- `.cache/`.
- Local virtual environments.
- Huge raw logs without a storage plan.
- Local machine paths or runner-specific credentials.

## Consider GitHub Releases or external archive

- Large raw chain log bundles.
- Large SQLite databases.
- Full historical chain dumps.
- Zipped Dune exports.
- Long-term provenance bundles too large for normal git history.

## Snapshot cadence

- Commit small generated dashboard snapshots whenever the refresh pipeline publishes.
- Preserve archive manifests with every archive rebuild.
- Keep raw logs if they are needed for reproducibility, but store large files outside
  the normal repo unless intentionally approved.

## Recovery rule

A maintainer should be able to rebuild the dashboard from source files, checked-in
generated snapshots, public/onchain sources, and documented optional API keys.
