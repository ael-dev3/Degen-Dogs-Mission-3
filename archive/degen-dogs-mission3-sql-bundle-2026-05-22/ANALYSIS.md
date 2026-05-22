# Degen Dogs Mission 3 SQL bundle analysis

## Archive

- Original zip: `/Users/marko/.hermes/cache/documents/doc_50cd97934618_degen_dogs_mission3_sql_bundle.zip`
- SHA-256: `1b7a8c0758a345df12e1e631c28bd088539827be38f2062cc7b86057d8123538`
- Extracted archive folder: `archive/degen-dogs-mission3-sql-bundle-2026-05-22/`
- Entries in original upload: 11 total, 8 files, 3 dirs
- Uncompressed size in original upload: 12,554 bytes
- Safety: no zip path traversal paths detected before extraction
- Original zip is preserved unchanged for provenance.

## High-level finding

This is not a full official Dune export. It is a partial reconstruction bundle:

- 1 known Dune query id: `6236765` for `Degen Dogs - Auction Winners`.
- 3 missing query ids:
  - `Degen Dogs - Most Recent Bids / DegenDogs Latest Auctions`
  - `Superfluid Season 4 $SUP rewards`
  - `Superfluid season 5 $SUP rewards`
- 2 SQL files are reconstructed auction queries.
- 2 SQL files are intentional stubs, not runnable reward queries.
- The helper script requires `DUNE_API_KEY` and missing query ids to fetch official SQL.

## Root cause fixed: stale/wrong auction contract in reconstructed SQL

The reconstructed auction queries originally used:

```text
0x3620CA030a023BCE87EC59a8b0E979bD7607Fdbd
```

Live Base RPC verification at block `46349443`:

- `0x3620CA030a023BCE87EC59a8b0E979bD7607Fdbd`: no code, 0 AuctionBid logs in the last 10,000 blocks.
- Current Mission 3 auction house `0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA`: code present, 7,389 bytes, 1 AuctionBid log in the last 10,000 blocks.

Fix applied to the extracted reconstructed SQL on `2026-05-22T22:19:34Z`:

- Both reconstructed auction queries now filter `base.logs.contract_address` to `0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA`.
- The stale `0x3620CA030a023BCE87EC59a8b0E979bD7607Fdbd` value remains only in comments/provenance notes.
- `6236765_degen_dogs_auction_winners.reconstructed.sql` now decodes `AuctionSettled` (`0xc9f72b276a388619c6d185d146697036241880c36654b1a3ffdad07c24038d99`) instead of inferring winners from latest `AuctionBid` rows.
- Auction amount fields now keep exact wei as `amount_wei` plus fractional ETH as `amount_eth` instead of rounding to whole ETH.
- `degen_dogs_most_recent_bids.reconstructed.sql` keeps the `AuctionBid` event topic (`0x1159164c56f277e6fc99c11731bd380e0347deb969b75523398734c252706ea3`) and returns latest bids from the current auction house.

## Current SQL status

### `6236765_degen_dogs_auction_winners.reconstructed.sql`

- Uses `base.logs` filtered by current auction house `0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA`.
- Decodes winners from `AuctionSettled(uint256,address,uint256)`.
- Decodes token id from `topic1`.
- Decodes winner address from `data[1..32]` and amount from `data[33..64]`.
- Joins `dune.neynar.dataset_farcaster_profile_with_addresses` for Farcaster names.
- Emits exact `amount_wei` plus fractional `amount_eth`.

### `degen_dogs_most_recent_bids.reconstructed.sql`

- Uses `base.logs` filtered by current auction house `0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA`.
- Decodes `AuctionBid(uint256,address,uint256,bool)` events.
- Returns latest 100 bids ordered by `block_time DESC`.
- Emits exact `amount_wei` plus fractional `amount_eth`.

### SUP reward files

Both Season 4 and Season 5 files are stubs. They contain context/comments only and no executable SQL.

## Helper script validation

- `scripts/fetch_official_dune_sql.py` compiles successfully with Python.
- It calls `https://api.dune.com/api/v1/query/{query_id}` with header `X-DUNE-API-KEY`.
- It skips entries where `query_id` is null.
- It writes official SQL to `queries/query_<id>_<slug>.official.sql` when Dune returns `query_sql`.

## Remaining caveats

1. The patched files are still reconstructed SQL, not official Dune exports.
2. Exact official SQL still requires Dune query ids/API access.
3. SUP reward files remain TODO placeholders until official query ids/SQL are available.
