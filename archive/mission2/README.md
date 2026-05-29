# Degen Dogs Mission 2 Archive

This folder preserves a reproducible local archive of Degen Dogs Mission 2 historical auction data from Degen Chain.

It is archival infrastructure, not a live dashboard integration. The current public Mission 3 dashboard remains Mission 3-focused and does not surface Mission 2 unless explicitly enabled later.

## Current status

- Onchain auction archive: recovered and verified from Degen Chain logs.
- Dune dashboard/query metadata: not recovered beyond the likely title/owner; no query IDs or SQL are fabricated.
- Reward/stream accounting: partial context only; not reconciled.
- Future dashboard integration: prepared through local CSV/JSON/SQLite outputs, not enabled.

## Verified onchain constants

- Chain: Degen Chain (`666666666`)
- Auction house: `0x3620ca030a023bce87ec59a8b0e979bd7607fdbd`
- Dog NFT: `0x77722fa8a43dfcc3e01c1db0b150b9db9d1e53dd`
- WDEGEN bid currency: `0xeb54dacb4c2ccb64f8074eceea33b5ebb38e5387`
- WOOF: `0x6D5EcD0509B47a78b750CA85cD1ec96D90f4cB3a`
- WOOFx metadata: `0x58e90e043fe47d224cc475349992a90317acbd69`
- Recovered auction range: Dogs `201-589`

See:

- `config/mission2_chain.verified.json`
- `config/mission2_contracts.verified.json`
- `config/mission2_blocks.verified.json`

## Recovered outputs

- Raw logs: `data/raw/mission2_raw_logs_24692180_26823905_20260528T221540Z.ndjson`
- Raw metadata: `data/raw/mission2_auction_logs.meta.json`
- SQLite: `data/sqlite/mission2.sqlite`
- SQLite archive alias: `data/mission2_archive.sqlite`
- Generated exports: `data/generated/`
- Manifest: `data/generated/manifest.json`

Current onchain counts:

| Table | Rows |
| --- | ---: |
| Raw lifecycle logs | 2641 |
| AuctionCreated | 369 |
| AuctionBid | 1630 |
| AuctionExtended | 273 |
| AuctionSettled | 369 |
| WOOF vault allocation source rows | 18 |

## Dune recovery status

The likely Dune target is `Degen Dogs Mission 2` by `ael_dev`, but the dashboard URL, dashboard ID, query IDs, official SQL, and official result exports are not recovered.

Files intentionally preserve this missing state:

- `dune/dune_dashboards.json`
- `dune/dune_queries.json`
- `dune/query_ids.json`
- `dune/hardcoded_constants.json`

To continue Dune recovery, authenticate in Dune, record query IDs in `dune/query_ids.json`, set `DUNE_API_KEY`, then run:

```bash
npm run archive:mission2:discover
```

## Rebuild commands

```bash
npm run archive:mission2:check
npm run archive:mission2:index
npm run archive:mission2:reconcile
npm run archive:mission2:build
```

Verified re-index environment:

```bash
DEGEN_RPC_URL=https://rpc.degen.tips
MISSION2_AUCTION_HOUSE=0x3620ca030a023bce87ec59a8b0e979bd7607fdbd
MISSION2_FROM_BLOCK=24692180
MISSION2_TO_BLOCK=26823905
MISSION2_LOG_CHUNK=10000
```

## Documentation

- `docs/recovery_session_notes.md`
- `docs/data_sources.md`
- `docs/how_to_rebuild.md`
- `docs/reconciliation_report.md`
- `docs/dashboard_integration_later.md`

## Caveats

- This archive is independent community infrastructure, not official Degen Dogs accounting.
- Dune reconciliation is pending.
- Superfluid/WOOFx stream allocation and MintClub reward accounting are not complete.
- Amounts are stored as exact raw integer strings; display values use verified 18-decimal WDEGEN metadata.
