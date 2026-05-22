# Degen Dogs Mission 3 SQL bundle analysis

## Archive

- Original zip: `/Users/marko/.hermes/cache/documents/doc_50cd97934618_degen_dogs_mission3_sql_bundle.zip`
- SHA-256: `1b7a8c0758a345df12e1e631c28bd088539827be38f2062cc7b86057d8123538`
- Extracted to: `/Users/marko/.hermes/cache/documents/degen_dogs_mission3_sql_bundle_extracted`
- Entries: 11 total, 8 files, 3 dirs
- Uncompressed size: 12,554 bytes
- Safety: no zip path traversal paths detected before extraction

## Contents

- `degen_dogs_mission3_sql/README.md`
- `degen_dogs_mission3_sql/query_ids.json`
- `degen_dogs_mission3_sql/queries.yml`
- `degen_dogs_mission3_sql/scripts/fetch_official_dune_sql.py`
- `degen_dogs_mission3_sql/queries/6236765_degen_dogs_auction_winners.reconstructed.sql`
- `degen_dogs_mission3_sql/queries/degen_dogs_most_recent_bids.reconstructed.sql`
- `degen_dogs_mission3_sql/queries/superfluid_season_4_sup_rewards.needs_official_query_id.sql`
- `degen_dogs_mission3_sql/queries/superfluid_season_5_sup_rewards.needs_official_query_id.sql`

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

## Critical issue: stale/wrong auction contract in reconstructed SQL

Both reconstructed auction queries use:

```text
0x3620CA030a023BCE87EC59a8b0E979bD7607Fdbd
```

Live Base RPC check at block `46348182`:

- `0x3620CA030a023BCE87EC59a8b0E979bD7607Fdbd`: no code, 0 AuctionBid logs in the last 10,000 blocks.
- Current known Mission 3 auction house `0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA`: code present, 7,389 bytes, 1 AuctionBid log in the last 10,000 blocks.
- Dog NFT `0x09154248fFDbaF8aA877aE8A4bf8cE1503596428`: code present.
- WOOF token `0x3e5c4FA0cAA794516eD0DF77f31daA534918d492`: code present.

Impact: as written, the two reconstructed auction queries are likely empty or stale on Base. Before production use, replace the auction contract with the current auction house or fetch the official Dune SQL to confirm why the snippet used `0x3620...`.

## SQL notes

### `6236765_degen_dogs_auction_winners.reconstructed.sql`

- Uses `base.logs` filtered by `AuctionBid(uint256,address,uint256,bool)` topic.
- Decodes token id from `topic1`.
- Decodes bidder from `data[13..32]` and amount from `data[33..64]`.
- Joins `dune.neynar.dataset_farcaster_profile_with_addresses` for Farcaster names.
- Infers winner as latest bid per token id, then excludes the current dog using `token_id < max(token_id)`.

Concerns:

- Uses stale/wrong contract address.
- Amount is rounded to 0 decimals and cast to `BIGINT`, losing fractional ETH bid precision.
- Winner inference would be safer from `AuctionSettled` events, not only latest bid and max token id.

### `degen_dogs_most_recent_bids.reconstructed.sql`

- Same event decode and Farcaster join.
- Returns latest 100 bids ordered by `block_time DESC`.

Concerns:

- Uses stale/wrong contract address.
- No official Dune query id in bundle.

### SUP reward files

Both Season 4 and Season 5 files are stubs. They contain context/comments only and no executable SQL.

## Helper script validation

- `scripts/fetch_official_dune_sql.py` compiles successfully with Python.
- It calls `https://api.dune.com/api/v1/query/{query_id}` with header `X-DUNE-API-KEY`.
- It skips entries where `query_id` is null.
- It writes official SQL to `queries/query_<id>_<slug>.official.sql` when Dune returns `query_sql`.

## Recommended next steps

1. If the goal is to recover the original Dune dashboard SQL, get the missing Dune query ids from the dashboard/query URLs and run the helper with `DUNE_API_KEY`.
2. If the goal is to make these queries usable now, patch both reconstructed auction queries to use `0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA` and keep the stale address only as a comment.
3. For the winners table, prefer decoding `AuctionSettled` events or cross-checking against settlement state instead of relying only on latest bid per token.
4. Preserve ETH precision in the winners query instead of `ROUND(..., 0)::BIGINT`.
5. Treat the SUP reward files as TODO placeholders until official query ids/SQL are available.
