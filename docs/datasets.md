# Datasets

Generated CSV/JSON outputs are written by `npm run data`. The current manifest is
`generated/manifest.csv` and `generated/manifest.json`.

Row counts below are from the inspected snapshot at block `46635266`. They can change
after each refresh.

## Primary dashboard datasets

| Table | Path | Rows |
| --- | --- | --- |
| `auction_feed` | `generated/auction_feed.csv` | 11 |
| `current_latest_bid` | `generated/current_latest_bid.csv` | 1 |
| `recent_auction_winners` | `generated/recent_auction_winners.csv` | 10 |
| `current_auction` | `generated/current_auction.csv` | 1 |

## Analytics datasets

| Table | Path | Rows |
| --- | --- | --- |
| `auction_timeline` | `generated/auction_timeline.csv` | 138 |
| `auction_daily_activity` | `generated/auction_daily_activity.csv` | 140 |
| `auction_bidder_leaderboard` | `generated/auction_bidder_leaderboard.csv` | 100 |
| `auction_winners` | `generated/auction_winners.csv` | 137 |
| `recent_bids` | `generated/recent_bids.csv` | 100 |

## Reward and token datasets

| Table | Path | Rows |
| --- | --- | --- |
| `mission3_metrics` | `generated/mission3_metrics.csv` | generated |
| `season5_sup_by_winner` | `generated/season5_sup_by_winner.csv` | generated |
| `season5_sup_rewards_by_auction` | `generated/season5_sup_rewards_by_auction.csv` | generated |
| `season6_metrics` | `generated/season6_metrics.csv` | generated |
| `season6_sup_by_winner` | `generated/season6_sup_by_winner.csv` | generated |
| `season6_sup_rewards_by_auction` | `generated/season6_sup_rewards_by_auction.csv` | generated |
| `season6_sup_current_bidder_status` | `generated/season6_sup_current_bidder_status.csv` | generated |
| `top_woof_holders` | `generated/top_woof_holders.csv` | generated |

Season 6 SUP projection methodology, cap treatment, and current-bidder projection fields are documented in [`season6-sup-rewards.md`](season6-sup-rewards.md).

## Archive/search datasets

| Table | Path | Rows |
| --- | --- | --- |
| `historical_dog_search` | `generated/historical_dog_search.csv` | 728 |
| `historical_dog_report` | `generated/historical_dog_report.csv` | 4 |

Additional unified search files:

- `public/generated/unified_dog_search_index.json` - browser search index (708 records
  in the inspected snapshot).
- `public/generated/unified_dog_search_manifest.json` - unified search build metadata.
- `archive/data/generated/unified_dog_search_index.json` - archive copy.
- `archive/dogs/by-id/<dog_id>.json` - per-Dog archive records.

The hosted feed/search UI reads `public/generated/unified_dog_search_index.json`
client-side, renders only the current page of results, and keeps the latest-10 default
state. Mission filters and highest-USD sorting use the generated record fields already
present in that static index; missing Mission rows or missing USD estimates remain
visible/degrade gracefully instead of being fabricated.

## Full manifest

The generated manifest is the source of truth for current row counts after each refresh:

- [`generated/manifest.csv`](../generated/manifest.csv)
- [`generated/manifest.json`](../generated/manifest.json)

Published reward/token tables now include Season 6 projection outputs alongside the existing Season 5 reward estimates. Do not rely on hard-coded row counts in docs; rerun `npm run data` and inspect the manifest for the current snapshot.

## Notes

- Do not hand-edit generated data long term. Update the generator, SQL, or archive
  source files, then rerun `npm run data`.
- `generated/` and `public/generated/` intentionally contain small static snapshots
  required by the public site.
- Large raw logs or SQLite databases should stay out of normal commits unless a backup
  policy explicitly allows them.
