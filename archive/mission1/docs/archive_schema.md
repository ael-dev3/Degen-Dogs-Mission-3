# Mission 1 Archive Schema

SQLite database:

`archive/mission1/data/mission1_archive.sqlite`

Schema:

- `mission1_raw_logs`
- `mission1_index_runs`
- `mission1_index_state`
- `mission1_index_gaps`
- `mission1_auction_created`
- `mission1_auction_bids`
- `mission1_auction_extended`
- `mission1_auction_settled`
- `mission1_nft_transfers`
- `mission1_bid_tokens_transfers`
- `mission1_treasury_transfers`
- `mission1_stream_events`
- `mission1_idle_events`
- `mission1_donation_events`
- `mission1_governance_events`

Derived marts:

- `mission1_auction_winners`
- `mission1_recent_bids`
- `mission1_bidder_leaderboard`
- `mission1_auction_timeline`
- `mission1_dog_bid_summary`
- `mission1_daily_activity`
- `mission1_archive_metrics`
- `mission1_reward_context`
- `mission1_future_dashboard_bridge`

Amounts from onchain data are stored as exact raw integer strings. Decimal display fields are included only when token decimals are verified, currently WETH/BSCT/Idle token 18-decimal contexts.

`mission1_dog_bid_summary` is the per-Dog coverage table: it has one row for every minted Mission 1 Dog token ID `0-200`, classifies DogMaster reward mints vs auction Dogs, and records first/last/highest recovered bid fields plus explicit zero-bid statuses. The flat `mission1_auction_bids` table remains the source of individual bid rows, and `mission1_dog_search_index.json` embeds each Dog's recovered `bid_history` array for archive/search consumers.
