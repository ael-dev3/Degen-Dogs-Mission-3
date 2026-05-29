# Datasets

Generated CSV/JSON outputs are written by `npm run data`. The current manifest is `generated/manifest.csv` and `generated/manifest.json`.

Row counts below are from the inspected snapshot at block `46635266`. They can change after each refresh.

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
| `mission3_metrics` | `generated/mission3_metrics.csv` | 52 |
| `season5_sup_by_winner` | `generated/season5_sup_by_winner.csv` | 32 |
| `season5_sup_rewards_by_auction` | `generated/season5_sup_rewards_by_auction.csv` | 63 |
| `top_woof_holders` | `generated/top_woof_holders.csv` | 50 |

## Archive/search datasets

| Table | Path | Rows |
| --- | --- | --- |
| `historical_dog_search` | `generated/historical_dog_search.csv` | 728 |
| `historical_dog_report` | `generated/historical_dog_report.csv` | 4 |

Additional unified search files:

- `public/generated/unified_dog_search_index.json` - browser search index (708 records in the inspected snapshot).
- `public/generated/unified_dog_search_manifest.json` - unified search build metadata.
- `archive/data/generated/unified_dog_search_index.json` - archive copy.
- `archive/dogs/by-id/<dog_id>.json` - per-Dog archive records.

## Full manifest

| Table | CSV path | Rows | CSV | JSON |
| --- | --- | --- | --- | --- |
| `mission3_metrics` | `generated/mission3_metrics.csv` | 52 | [`CSV`](generated/mission3_metrics.csv) | [`JSON`](generated/mission3_metrics.json) |
| `auction_feed` | `generated/auction_feed.csv` | 11 | [`CSV`](generated/auction_feed.csv) | [`JSON`](generated/auction_feed.json) |
| `historical_dog_search` | `generated/historical_dog_search.csv` | 728 | [`CSV`](generated/historical_dog_search.csv) | [`JSON`](generated/historical_dog_search.json) |
| `historical_dog_report` | `generated/historical_dog_report.csv` | 4 | [`CSV`](generated/historical_dog_report.csv) | [`JSON`](generated/historical_dog_report.json) |
| `current_latest_bid` | `generated/current_latest_bid.csv` | 1 | [`CSV`](generated/current_latest_bid.csv) | [`JSON`](generated/current_latest_bid.json) |
| `recent_auction_winners` | `generated/recent_auction_winners.csv` | 10 | [`CSV`](generated/recent_auction_winners.csv) | [`JSON`](generated/recent_auction_winners.json) |
| `current_auction` | `generated/current_auction.csv` | 1 | [`CSV`](generated/current_auction.csv) | [`JSON`](generated/current_auction.json) |
| `auction_timeline` | `generated/auction_timeline.csv` | 138 | [`CSV`](generated/auction_timeline.csv) | [`JSON`](generated/auction_timeline.json) |
| `auction_daily_activity` | `generated/auction_daily_activity.csv` | 140 | [`CSV`](generated/auction_daily_activity.csv) | [`JSON`](generated/auction_daily_activity.json) |
| `auction_bidder_leaderboard` | `generated/auction_bidder_leaderboard.csv` | 100 | [`CSV`](generated/auction_bidder_leaderboard.csv) | [`JSON`](generated/auction_bidder_leaderboard.json) |
| `season5_sup_by_winner` | `generated/season5_sup_by_winner.csv` | 32 | [`CSV`](generated/season5_sup_by_winner.csv) | [`JSON`](generated/season5_sup_by_winner.json) |
| `season5_sup_rewards_by_auction` | `generated/season5_sup_rewards_by_auction.csv` | 63 | [`CSV`](generated/season5_sup_rewards_by_auction.csv) | [`JSON`](generated/season5_sup_rewards_by_auction.json) |
| `auction_winners` | `generated/auction_winners.csv` | 137 | [`CSV`](generated/auction_winners.csv) | [`JSON`](generated/auction_winners.json) |
| `recent_bids` | `generated/recent_bids.csv` | 100 | [`CSV`](generated/recent_bids.csv) | [`JSON`](generated/recent_bids.json) |
| `top_woof_holders` | `generated/top_woof_holders.csv` | 50 | [`CSV`](generated/top_woof_holders.csv) | [`JSON`](generated/top_woof_holders.json) |

## Notes

- Do not hand-edit generated data long term. Update the generator, SQL, or archive source files, then rerun `npm run data`.
- `generated/` and `public/generated/` intentionally contain small static snapshots required by the public site.
- Large raw logs or SQLite databases should stay out of normal commits unless a backup policy explicitly allows them.
