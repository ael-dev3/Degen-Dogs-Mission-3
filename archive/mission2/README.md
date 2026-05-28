# Degen Dogs Mission 2 Archive

This folder is the archival foundation for Degen Dogs Mission 2 historical data on Degen Chain.

It is an independent community-built archive scaffold. It is not official Degen Dogs accounting, it is incomplete until verified, and it is not yet integrated into the live Mission 3 dashboard UI.

## Purpose

Mission 2 moved Degen Dogs activity from Polygon into the Degen Chain / Farcaster ecosystem. The archive exists to preserve recoverable assumptions, source links, event ABIs, Dune query provenance, and a Dune-independent archival path before historical Degen Chain data becomes harder to search.

The current goal is data recovery and reproducibility, not a public visual dashboard feature.

## Current status

- Mission 2 Dune dashboard title is known: `Degen Dogs Mission 2` by `ael_dev`.
- Dune query IDs, dashboard URL, and raw official SQL are not yet recovered.
- Degen Chain network configuration is recorded in `config/mission2_chain.json`.
- Auction event ABI fragments are preserved from Mark Carey's `IDogsAuctionHouse.sol` in `config/mission2_event_abis.json`.
- Mission 2 deployed contract addresses remain unverified placeholders in `config/mission2_contracts.unverified.json`.
- WOOF Vault allocation source data is preserved in `config/woof_vault_allocations.json` with caveats.
- A local indexer exists at `scripts/archive_mission2_index.py`, but refuses to run until a verified auction house address and block range are supplied.

## What is known

- Network: Degen Chain, chain ID `666666666`.
- Native gas currency: DEGEN.
- Mission 2 docs describe native DEGEN bids, 24 hour auctions, 1000 DEGEN reserve price, 10% minimum bid increment, and 5 minute time buffer.
- Mark's source repo exposes the auction event shapes used by the local indexer.
- Mission 3 started on Base at Dog #590 according to current docs, so Dog #590 is a transition marker. The last Mission 2 auction and exact Mission 2 Dog ID range are still unverified.

## What is not recovered yet

Do not treat any of these as production facts until verified from Dune SQL, explorer verified contracts, official docs, or Mark's source/deployment records:

- Mission 2 Dune dashboard URL/slug/dashboard ID.
- Mission 2 Dune query IDs and official SQL.
- Mission 2 deployed Degen Chain Dog NFT address.
- Mission 2 deployed Degen Chain auction house address.
- Mission 2 WOOF, WOOFx, MintClub token, Superfluid pool, or pool manager addresses.
- Mission 2 exact deployment/start block.
- Mission 2 exact Dog ID range.
- Mission 2 token decimals for display calculations.
- Whether the deployed auction house used native DEGEN directly or an ERC20/wrapped path.

## Archive layout

```text
archive/mission2/
  config/        verified, inferred, and unverified constants
  dune/          Dune recovery notes, query ID tracker, official SQL snapshots
  sql/           local SQLite schema and future marts
  data/          local raw logs, SQLite DBs, generated CSV/JSON outputs
  scripts/       archive-local helper scripts and docs
```

## Recovering Dune SQL

1. Open Dune and search for owner `ael_dev`, title `Degen Dogs Mission 2`, chain `Degen`.
2. Record the dashboard URL/slug/dashboard ID in `dune/query_ids.json`.
3. Add every query ID used by the dashboard to `dune/query_ids.json`.
4. If `DUNE_API_KEY` is available, run:

```bash
python3 archive/mission2/scripts/recover_dune_queries.py
```

The script uses official Dune API endpoints only. It saves official SQL under `archive/mission2/dune/queries/`, latest result snapshots under `archive/mission2/dune/results/`, and extracted constants in `archive/mission2/dune/hardcoded_constants.json`.

Do not rewrite or beautify official SQL in place. Ported/local versions belong in `archive/mission2/sql/ported/`.

## Running the local indexer

The indexer intentionally fails loudly until a verified auction house address and verified start block are supplied.

Check static config only:

```bash
npm run archive:mission2:check
```

Run indexing after verification:

```bash
MISSION2_AUCTION_HOUSE=0x... \
MISSION2_FROM_BLOCK=123456 \
MISSION2_TO_BLOCK=latest \
DEGEN_RPC_URL=https://rpc.degen.tips \
npm run archive:mission2
```

Generated outputs are local archive artifacts:

- Raw logs: `archive/mission2/data/raw/*.ndjson`
- SQLite: `archive/mission2/data/sqlite/mission2.sqlite`
- CSV/JSON summaries: `archive/mission2/data/generated/`
- Manifest: `archive/mission2/data/generated/mission2_archive_manifest.json`

The indexer stores raw logs before decoding, uses `(chain_id, tx_hash, log_index)` idempotent keys, and stores raw amounts as exact strings rather than floats.

## Future dashboard integration

Mission 2 data should stay disabled from the live Mission 3 dashboard until the core contracts, block ranges, and Dune comparison checks are verified. Future integration ideas live in `future_dashboard_notes.md`.

## Disclaimer

This archive is independent, incomplete until verified, and not official Degen Dogs accounting. It is meant to preserve historical Mission 2 data sources and create a careful local indexing path.
