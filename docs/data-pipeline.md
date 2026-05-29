# Data pipeline

Main command:

```bash
npm run data
```

The command runs four steps:

1. `python3 scripts/build_dashboard.py`
2. `python3 scripts/fetch_farcaster_channel.py`
3. `python3 scripts/build_unified_dog_index.py`
4. `python3 scripts/archive_apply_usd_estimates.py`

## Mission 3 dashboard generation

`scripts/build_dashboard.py`:

- reads Base RPC endpoints from environment variables or public defaults,
- fetches current Base block and current auction contract state,
- scans Mission 3 auction-house logs from the configured start block,
- checks WOOF balances for discovered holders,
- fetches Dog metadata and rarity where needed,
- optionally resolves Farcaster identities when an API key is available,
- computes token/reward context,
- loads input rows into in-memory SQLite,
- executes `sql/mission3_dashboard.sql`,
- exports approved tables from `OUTPUT_TABLES`,
- writes `generated/*.csv`, `generated/*.json`, mirrored public files, `index.html`, and
  `README.md`.

## Farcaster channel snapshot

`scripts/fetch_farcaster_channel.py` writes the cached read-only community panel data
for `/degendogs`:

- `generated/farcaster_degendogs_channel.json`
- `public/generated/farcaster_degendogs_channel.json`

The source order is Hypersnap read API first, then a Snapchain-compatible direct node
when configured, then optional Neynar fallback only when `NEYNAR_FALLBACK_ENABLED=1`
and `NEYNAR_API_KEY` is present. Source failures write a graceful empty snapshot rather
than failing the whole dashboard build.

## Unified archive/search generation

`scripts/build_unified_dog_index.py` combines available Mission 1, Mission 2, and
Mission 3 archive search indexes into a public search index used by the dashboard search
box.

The browser reads:

- `public/generated/unified_dog_search_index.json`
- `public/generated/unified_dog_search_manifest.json`

Archive copies live under:

- `archive/data/generated/unified_dog_search_index.json`
- `archive/data/generated/unified_dog_search_manifest.json`

## Historical USD estimates

`scripts/archive_apply_usd_estimates.py` enriches unified archive records with
historical USD estimates where price provenance exists under `archive/prices/`.

## Source vs generated

Source of truth:

- `scripts/build_dashboard.py`
- `scripts/fetch_farcaster_channel.py`
- `scripts/build_unified_dog_index.py`
- `scripts/archive_apply_usd_estimates.py`
- `sql/mission3_dashboard.sql`
- `README.template.md`
- verified archive configs under `archive/*/config/`

Generated outputs:

- `generated/`
- `public/generated/`
- `generated/farcaster_degendogs_channel.json` and its `public/generated/` mirror
- `index.html`
- `README.md`
- `archive/data/generated/`
- `archive/dogs/by-id/`
- archive mission generated outputs where rebuild scripts own them
