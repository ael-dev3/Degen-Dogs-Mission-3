# Degen Dogs Mission 3 — Dune SQL bundle

Source dashboard: `https://dune.com/ael_dev/degen-dogs-mission-3`

## What is included

| File | Status |
| --- | --- |
| `queries/6236765_degen_dogs_auction_winners.reconstructed.sql` | Query id and title found publicly; SQL reconstructed from indexed snippets. |
| `queries/degen_dogs_most_recent_bids.reconstructed.sql` | Companion query reconstructed from the same event parsing; title/card found publicly, but no query id was recovered. |
| `queries/superfluid_season_4_sup_rewards.needs_official_query_id.sql` | Stub: dashboard card found, official SQL needs query id/API. |
| `queries/superfluid_season_5_sup_rewards.needs_official_query_id.sql` | Stub: dashboard card found, official SQL needs query id/API. |
| `scripts/fetch_official_dune_sql.py` | Helper script to pull official SQL once query ids are known. |
| `query_ids.json` / `queries.yml` | Config files for known and missing query ids. |

## Important caveats

Dune's public dashboard page is rendered by JavaScript and did not expose all query SQL through static HTML. Dune's official Read Query API returns the `query_sql` field, but it requires a Dune API key. Dune's own query-management docs say that, for dashboards, the owner can click the dashboard's GitHub button to see the query ids, then run `pull_from_dune.py` to generate `/query_{id}.sql` files.

The reconstructed auction SQL has been patched to use the current Mission 3 Base Auction House (`0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA`). The original indexed snippets used `0x3620CA030a023BCE87EC59a8b0E979bD7607Fdbd`, which later Base RPC verification showed has no code; fetch official Dune SQL if exact upstream source fidelity is required.

## Pulling official SQL

1. Get the missing query ids from Dune:
   - On the dashboard, click the GitHub button to reveal dashboard query ids, or
   - click each chart title and copy the first number from `https://dune.com/queries/<query_id>/<visualization_id>`.
2. Fill in the null `query_id` values in `query_ids.json`.
3. Run:

```bash
# Set Dune API credentials in your local shell, then run:
python scripts/fetch_official_dune_sql.py query_ids.json
```

Official SQL files will be written to `queries/query_<id>_<name>.official.sql`.
