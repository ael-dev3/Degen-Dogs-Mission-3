# Degen Dogs Mission 3 SQL bundle archive

Archived upload: `degen_dogs_mission3_sql_bundle.zip`

Original zip SHA-256: `1b7a8c0758a345df12e1e631c28bd088539827be38f2062cc7b86057d8123538`

Classification: partial reconstruction bundle, not a full official Dune export.

Fix applied: extracted reconstructed auction SQL now uses current Mission 3 auction house `0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA`. The original uploaded zip is preserved unchanged for provenance.

Root cause: reconstructed auction SQL used `0x3620CA030a023BCE87EC59a8b0E979bD7607Fdbd`. Base RPC verification at block `46349443` showed that address has no code and 0 recent AuctionBid logs; `0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA` has code and recent AuctionBid activity.

Generated archive docs:

- `README.md`: this file-level inventory.
- `MANIFEST.json`: machine-readable file metadata and SHA-256 hashes.
- `ANALYSIS.md`: review report with Base RPC validation notes and applied fixes.

## Preserved/patched file inventory

| file | status | bytes | lines | notes |
| --- | --- | ---: | ---: | --- |
| `ANALYSIS.md` | analysis | 3986 | 80 | Local review report covering archive metadata, root-cause verification, applied SQL fixes, and remaining caveats. |
| `degen_dogs_mission3_sql_bundle.zip` | original-upload | 8325 |  | Original uploaded SQL bundle, preserved unchanged for provenance. |
| `source/degen_dogs_mission3_sql/README.md` | bundle-doc-patched | 2246 | 35 | README shipped inside the uploaded SQL bundle, updated to document the current patched auction-address state. Original zip preserves the pre-patch copy. |
| `source/degen_dogs_mission3_sql/queries/6236765_degen_dogs_auction_winners.reconstructed.sql` | reconstructed-sql-patched | 2796 | 81 | Reconstructed auction-winners SQL patched to current auction house, AuctionSettled-based winners, and exact wei/fractional ETH amounts. Original snippet address 0x3620... retained only in comments/provenance. Not official Dune SQL. |
| `source/degen_dogs_mission3_sql/queries/degen_dogs_most_recent_bids.reconstructed.sql` | reconstructed-sql-patched | 2618 | 73 | Reconstructed latest-bids SQL patched to current auction house with exact wei/fractional ETH amounts. Original snippet address 0x3620... retained only in comments/provenance. Not official Dune SQL. |
| `source/degen_dogs_mission3_sql/queries/superfluid_season_4_sup_rewards.needs_official_query_id.sql` | stub | 823 | 12 | Comment-only placeholder; requires official Dune query id/SQL before use. |
| `source/degen_dogs_mission3_sql/queries/superfluid_season_5_sup_rewards.needs_official_query_id.sql` | stub | 538 | 7 | Comment-only placeholder; requires official Dune query id/SQL before use. |
| `source/degen_dogs_mission3_sql/queries.yml` | config | 566 | 15 | YAML copy of query-id inventory and missing-query TODOs. |
| `source/degen_dogs_mission3_sql/query_ids.json` | config | 670 | 22 | Dune query id inventory: one known query id and missing ids for three dashboard cards. |
| `source/degen_dogs_mission3_sql/scripts/fetch_official_dune_sql.py` | helper-script | 2682 | 85 | Fetches official Dune SQL through the Dune Read Query API when DUNE_API_KEY and query ids are available. |

## Query status

- `source/degen_dogs_mission3_sql/queries/6236765_degen_dogs_auction_winners.reconstructed.sql`: known public query id, reconstructed SQL, patched to current auction house, `AuctionSettled` winners, and exact wei/fractional ETH amount fields.
- `source/degen_dogs_mission3_sql/queries/degen_dogs_most_recent_bids.reconstructed.sql`: reconstructed companion SQL, patched to current auction house with exact wei/fractional ETH amount fields; no official query id in bundle.
- `source/degen_dogs_mission3_sql/queries/superfluid_season_4_sup_rewards.needs_official_query_id.sql`: stub only.
- `source/degen_dogs_mission3_sql/queries/superfluid_season_5_sup_rewards.needs_official_query_id.sql`: stub only.

## Reproduction

To fetch official SQL later, fill missing ids in `source/degen_dogs_mission3_sql/query_ids.json`, set `DUNE_API_KEY`, and run the helper script from the archived bundle root.
