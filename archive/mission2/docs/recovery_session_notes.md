# Mission 2 Recovery Session Notes

- Work timestamp UTC: `2026-05-28T23:22:48Z`
- Git branch: `mission2-archive-recovery`
- Starting commit: `83c459212460b228d6476edfad13e836a508e8f6`
- Repository: `ael-dev3/Degen-Dogs-Mission-3`

## Existing scaffold inspected

The repository already had an isolated `archive/mission2/` scaffold with config, Dune placeholders, SQL schema/marts, a local indexer, and package scripts. Generated raw/SQLite/CSV/JSON outputs from the onchain recovery run were present as untracked files at the start of this continuation.

## Tools and APIs available

- Git/GitHub CLI available in the repo environment.
- Degen Chain public RPC `https://rpc.degen.tips` available and working for `eth_chainId`, `eth_getCode`, `eth_call`, and chunked `eth_getLogs`.
- Dune API key: not available in the shell environment in this session.
- Public Dune browser/API attempts: blocked by auth/Cloudflare/403, so Dune IDs/SQL/results were not recovered.

## Verified data

- Degen Chain chain ID: `666666666`.
- Mission 2 auction house: `0x3620ca030a023bce87ec59a8b0e979bd7607fdbd`.
- Mission 2 Dog NFT: `0x77722fa8a43dfcc3e01c1db0b150b9db9d1e53dd`.
- WDEGEN bid currency: `0xeb54dacb4c2ccb64f8074eceea33b5ebb38e5387`.
- WOOF token metadata: `0x6D5EcD0509B47a78b750CA85cD1ec96D90f4cB3a`.
- WOOFx metadata: `0x58e90e043fe47d224cc475349992a90317acbd69`.
- Recovered onchain auction range: Dogs `201` through `589`.
- Row counts: `369` created, `1630` bids, `273` extensions, `369` settlements, `2641` raw lifecycle logs.

## Missing or unresolved data

- Dune dashboard URL/dashboard ID/query IDs.
- Official Dune SQL and result exports.
- Superfluid pool / pool manager details.
- MintClub bonding curve token details.
- Full reward/stream reconciliation.

## Files currently present under archive/mission2

- archive/mission2/README.md
- archive/mission2/SOURCES.md
- archive/mission2/TODO.md
- archive/mission2/config/mission2_blocks.candidates.json
- archive/mission2/config/mission2_blocks.verified.json
- archive/mission2/config/mission2_chain.json
- archive/mission2/config/mission2_chain.verified.json
- archive/mission2/config/mission2_contracts.candidates.json
- archive/mission2/config/mission2_contracts.unverified.json
- archive/mission2/config/mission2_contracts.verified.json
- archive/mission2/config/mission2_dune_recovery.json
- archive/mission2/config/mission2_event_abis.json
- archive/mission2/config/woof_vault_allocations.json
- archive/mission2/data/generated/manifest.json
- archive/mission2/data/generated/mission2_archive_manifest.json
- archive/mission2/data/generated/mission2_archive_metrics.csv
- archive/mission2/data/generated/mission2_archive_metrics.json
- archive/mission2/data/generated/mission2_auction_bids.csv
- archive/mission2/data/generated/mission2_auction_bids.json
- archive/mission2/data/generated/mission2_auction_created.csv
- archive/mission2/data/generated/mission2_auction_created.json
- archive/mission2/data/generated/mission2_auction_extended.csv
- archive/mission2/data/generated/mission2_auction_extended.json
- archive/mission2/data/generated/mission2_auction_settled.csv
- archive/mission2/data/generated/mission2_auction_settled.json
- archive/mission2/data/generated/mission2_auction_timeline.csv
- archive/mission2/data/generated/mission2_auction_timeline.json
- archive/mission2/data/generated/mission2_auction_winners.csv
- archive/mission2/data/generated/mission2_auction_winners.json
- archive/mission2/data/generated/mission2_bidder_leaderboard.csv
- archive/mission2/data/generated/mission2_bidder_leaderboard.json
- archive/mission2/data/generated/mission2_daily_activity.csv
- archive/mission2/data/generated/mission2_daily_activity.json
- archive/mission2/data/generated/mission2_dog_search_index.json
- archive/mission2/data/generated/mission2_parameter_updates.csv
- archive/mission2/data/generated/mission2_parameter_updates.json
- archive/mission2/data/generated/mission2_woof_vault_allocations.csv
- archive/mission2/data/generated/mission2_woof_vault_allocations.json
- archive/mission2/data/generated/reconciliation_summary.json
- archive/mission2/data/mission2_archive.sqlite
- archive/mission2/data/raw/mission2_auction_logs.meta.json
- archive/mission2/data/raw/mission2_index_gaps.csv
- archive/mission2/data/raw/mission2_raw_logs_24692180_26823905_20260529T001926Z.ndjson
- archive/mission2/data/raw/mission2_rpc_failures.json
- archive/mission2/data/sqlite/mission2.sqlite
- archive/mission2/dune/README.md
- archive/mission2/dune/dune_dashboards.json
- archive/mission2/dune/dune_queries.json
- archive/mission2/dune/hardcoded_constants.json
- archive/mission2/dune/query_ids.json
- archive/mission2/future_dashboard_notes.md
- archive/mission2/research_notes.md
- archive/mission2/scripts/README.md
- archive/mission2/scripts/recover_dune_queries.py
- archive/mission2/sql/marts.sql
- archive/mission2/sql/schema.sql

## Dune recovery attempts

- Browser: `https://dune.com/ael_dev/degen-dogs-mission-2` hit Cloudflare verification.
- Dune public GraphQL from the browser page returned HTTP 403 behind Cloudflare.
- r.jina mirrors for direct candidate slugs returned Dune 404 pages.
- r.jina Dune search for `Degen Dogs Mission 2`, `Degen Dogs`, `ael_dev`, `WOOF WOOFx Degen Dogs`, and `Degen Chain Degen Dogs` did not reveal verified Mission 2 dashboard/query IDs.
- `DUNE_API_KEY` was not present in the environment.
