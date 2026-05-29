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
- `mission1_daily_activity`
- `mission1_archive_metrics`
- `mission1_reward_context`
- `mission1_future_dashboard_bridge`

Amounts from onchain data are stored as exact raw integer strings. Decimal display fields are included only when token decimals are verified, currently WETH/BSCT/Idle token 18-decimal contexts.
