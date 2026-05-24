# Degen Dogs Mission 3 Analytics

Static, cached analytics for Degen Dogs Mission 3 on Base. The public site serves approved, precomputed result tables and downloadable CSV/JSON exports; it does not expose arbitrary visitor-run SQL.

## Links

- Live dashboard: [https://ael-dev3.github.io/Degen-Dogs-Mission-3/](https://ael-dev3.github.io/Degen-Dogs-Mission-3/)
- Query layer: [`sql/mission3_dashboard.sql`](sql/mission3_dashboard.sql)
- Generated exports: [`generated/`](generated/)

## Current snapshot

| Field | Value |
| --- | --- |
| Network | base |
| Snapshot block | 46436685 |
| Snapshot time UTC | 2026-05-24 22:45:17 |
| Current auction | Dog #724 |
| Current bid | 0.00061 ETH ($1.28) |
| Current high bidder | 0x4119…72cb |
| Auction ends UTC | 2026-05-25 15:30:29 |
| Created / settled auctions | 135 / 134 |
| WOOF holders | 387 |
| Farcaster profiles resolved | 155 |

## Published datasets

| Table | CSV path | Rows | Downloads |
| --- | --- | --- | --- |
| mission3_metrics | `generated/mission3_metrics.csv` | 28 | [CSV](generated/mission3_metrics.csv) / [JSON](generated/mission3_metrics.json) |
| auction_feed | `generated/auction_feed.csv` | 11 | [CSV](generated/auction_feed.csv) / [JSON](generated/auction_feed.json) |
| current_latest_bid | `generated/current_latest_bid.csv` | 1 | [CSV](generated/current_latest_bid.csv) / [JSON](generated/current_latest_bid.json) |
| recent_auction_winners | `generated/recent_auction_winners.csv` | 10 | [CSV](generated/recent_auction_winners.csv) / [JSON](generated/recent_auction_winners.json) |
| current_auction | `generated/current_auction.csv` | 1 | [CSV](generated/current_auction.csv) / [JSON](generated/current_auction.json) |
| auction_timeline | `generated/auction_timeline.csv` | 135 | [CSV](generated/auction_timeline.csv) / [JSON](generated/auction_timeline.json) |
| auction_daily_activity | `generated/auction_daily_activity.csv` | 136 | [CSV](generated/auction_daily_activity.csv) / [JSON](generated/auction_daily_activity.json) |
| auction_bidder_leaderboard | `generated/auction_bidder_leaderboard.csv` | 100 | [CSV](generated/auction_bidder_leaderboard.csv) / [JSON](generated/auction_bidder_leaderboard.json) |
| season5_sup_by_winner | `generated/season5_sup_by_winner.csv` | 32 | [CSV](generated/season5_sup_by_winner.csv) / [JSON](generated/season5_sup_by_winner.json) |
| season5_sup_rewards_by_auction | `generated/season5_sup_rewards_by_auction.csv` | 60 | [CSV](generated/season5_sup_rewards_by_auction.csv) / [JSON](generated/season5_sup_rewards_by_auction.json) |
| auction_winners | `generated/auction_winners.csv` | 134 | [CSV](generated/auction_winners.csv) / [JSON](generated/auction_winners.json) |
| recent_bids | `generated/recent_bids.csv` | 100 | [CSV](generated/recent_bids.csv) / [JSON](generated/recent_bids.json) |
| top_woof_holders | `generated/top_woof_holders.csv` | 50 | [CSV](generated/top_woof_holders.csv) / [JSON](generated/top_woof_holders.json) |

## Data pipeline

1. Fetch Base RPC logs and contract calls from the private Mac mini runner.
2. Load decoded auction, WOOF, NFT metadata, and Farcaster identity rows into SQLite.
3. Execute the approved SQL query layer and publish cached CSV/JSON/table artifacts to GitHub Pages.
4. Refresh automatically from the private runner; the Mac mini is not the public host.

## Verified contracts

| Contract | Address |
| --- | --- |
| Auction house | 0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA |
| Degen Dogs NFT | 0x09154248fFDbaF8aA877aE8A4bf8cE1503596428 |
| WOOF token | 0x3e5c4FA0cAA794516eD0DF77f31daA534918d492 |

## Caveats

- The public site is a cached snapshot, not a live SQL database.
- Current-auction state and high bidder are taken from the on-chain `auction()` snapshot.
- Historical auction rows are reconstructed from verified Base auction-house events.
- Archived SQL bundles may contain reconstructed auction SQL, SUP reward stubs, and patched contract references; the active dashboard is generated from this repository's query layer and Base RPC data.

## Local development

```bash
npm ci
npm run data
npm run build
```

Install or refresh the hourly private-runner LaunchAgent:

```bash
npm run refresh:install
```
