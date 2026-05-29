# How to Rebuild the Mission 2 Archive

## Requirements

- Python 3
- npm dependencies for the Mission 3 repo if running full repo checks
- Degen Chain RPC access
- Optional: `DUNE_API_KEY` for Dune SQL/result recovery after query IDs are known

## Check static config

```bash
npm run archive:mission2:check
```

## Recover Dune SQL/results, if query IDs are known

1. Open Dune as an authenticated user.
2. Find the `Degen Dogs Mission 2` dashboard by `ael_dev`.
3. Record query IDs in `archive/mission2/dune/query_ids.json`.
4. Run:

```bash
DUNE_API_KEY=... npm run archive:mission2:discover
```

If no query IDs are recorded, the helper exits cleanly and does not fabricate anything.

## Re-index onchain logs

The recovered verified command is:

```bash
DEGEN_RPC_URL=https://rpc.degen.tips MISSION2_AUCTION_HOUSE=0x3620ca030a023bce87ec59a8b0e979bd7607fdbd MISSION2_FROM_BLOCK=24692180 MISSION2_TO_BLOCK=26823905 MISSION2_LOG_CHUNK=10000 npm run archive:mission2:index
```

This fetches raw logs, decodes them into SQLite, and writes generated CSV/JSON files.

## Rebuild derived exports/reconciliation

```bash
npm run archive:mission2:reconcile
npm run archive:mission2:build
```

Current derived outputs include winners, bidder leaderboard, daily activity, timeline, dog search index, manifest, raw metadata, and Dune reconciliation summary.

## Output locations

- Raw logs: `archive/mission2/data/raw/mission2_raw_logs_24692180_26823905_20260529T001926Z.ndjson`
- Raw metadata: `archive/mission2/data/raw/mission2_auction_logs.meta.json`
- SQLite: `archive/mission2/data/sqlite/mission2.sqlite`
- SQLite archive alias: `archive/mission2/data/mission2_archive.sqlite`
- Generated CSV/JSON: `archive/mission2/data/generated/`
- Manifest: `archive/mission2/data/generated/manifest.json`

## Caveats

- Dune reconciliation is blocked until query IDs, official SQL, and official result exports are recovered.
- Reward and stream accounting is not complete.
- Mission 2 is not wired into the live Mission 3 dashboard UI.
