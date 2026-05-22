# Degen Dogs Mission 3 SQL bundle archive

Archived upload: `degen_dogs_mission3_sql_bundle.zip`

Original zip SHA-256: `1b7a8c0758a345df12e1e631c28bd088539827be38f2062cc7b86057d8123538`

Classification: partial reconstruction bundle, not a full official Dune export.

Critical note: both reconstructed auction SQL files use `0x3620CA030a023BCE87EC59a8b0E979bD7607Fdbd`. During review, that address had no Base code. The current known Mission 3 auction house is `0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA`.

Generated archive docs:

- `README.md`: this file-level inventory.
- `MANIFEST.json`: machine-readable file metadata and SHA-256 hashes.
- `ANALYSIS.md`: review report with Base RPC validation notes.

## Preserved file inventory

| file | status | bytes | lines | notes |
| --- | --- | ---: | ---: | --- |
| `ANALYSIS.md` | analysis | 4557 | 96 | Local review report covering archive metadata, SQL status, Base RPC checks, and recommended fixes. |
| `degen_dogs_mission3_sql_bundle.zip` | original-upload | 8325 |  | Original uploaded SQL bundle, preserved for provenance. |
| `source/degen_dogs_mission3_sql/README.md` | bundle-doc | 2202 | 35 | README shipped inside the uploaded SQL bundle. |
| `source/degen_dogs_mission3_sql/queries/6236765_degen_dogs_auction_winners.reconstructed.sql` | reconstructed-sql | 2996 | 101 | Reconstructed auction SQL from public snippets, not verified official Dune SQL. Uses stale/unverified auction address 0x3620..., documented in ANALYSIS.md. |
| `source/degen_dogs_mission3_sql/queries/degen_dogs_most_recent_bids.reconstructed.sql` | reconstructed-sql | 2077 | 67 | Reconstructed auction SQL from public snippets, not verified official Dune SQL. Uses stale/unverified auction address 0x3620..., documented in ANALYSIS.md. |
| `source/degen_dogs_mission3_sql/queries/superfluid_season_4_sup_rewards.needs_official_query_id.sql` | stub | 823 | 12 | Comment-only placeholder; requires official Dune query id/SQL before use. |
| `source/degen_dogs_mission3_sql/queries/superfluid_season_5_sup_rewards.needs_official_query_id.sql` | stub | 538 | 7 | Comment-only placeholder; requires official Dune query id/SQL before use. |
| `source/degen_dogs_mission3_sql/queries.yml` | config | 566 | 15 | YAML copy of query-id inventory and missing-query TODOs. |
| `source/degen_dogs_mission3_sql/query_ids.json` | config | 670 | 22 | Dune query id inventory: one known query id and missing ids for three dashboard cards. |
| `source/degen_dogs_mission3_sql/scripts/fetch_official_dune_sql.py` | helper-script | 2682 | 85 | Fetches official Dune SQL through the Dune Read Query API when DUNE_API_KEY and query ids are available. |

## Query status

- `source/degen_dogs_mission3_sql/queries/6236765_degen_dogs_auction_winners.reconstructed.sql`: known public query id, reconstructed SQL, stale-address risk.
- `source/degen_dogs_mission3_sql/queries/degen_dogs_most_recent_bids.reconstructed.sql`: reconstructed companion SQL, no official query id in bundle, stale-address risk.
- `source/degen_dogs_mission3_sql/queries/superfluid_season_4_sup_rewards.needs_official_query_id.sql`: stub only.
- `source/degen_dogs_mission3_sql/queries/superfluid_season_5_sup_rewards.needs_official_query_id.sql`: stub only.

## Reproduction

To fetch official SQL later, fill missing ids in `source/degen_dogs_mission3_sql/query_ids.json`, set `DUNE_API_KEY`, and run the helper script from the archived bundle root.
