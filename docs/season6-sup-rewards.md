# Season 6 SUP reward projections

The dashboard includes a Season 6 SUP projection surface for settled Mission 3 Dog wins and the current high bidder.

## Model

- Reward pool: `251,340 SUP`.
- Wallet-level cap: `12,500 SUP`, described in the UI as `12,500 SUP per wallet-level estimate`.
- XP accrual: `100 XP per settled Dog win`.
- XP window: starts at `2026-06-02T00:00:00Z`.
- Reward allocation window: starts at `2026-06-02T00:00:00Z`.
- Campaign end: `2026-08-31T23:59:59Z`.

The generator treats each settled Dog win as a time-stamped XP event. Between any two adjacent events, active XP is the cumulative wallet XP that exists at the start of that interval. The interval's SUP slice is prorated by elapsed seconds / campaign duration and split by wallet XP share. If no wallet has active XP for an interval, that slice remains `unallocated`; cap overflow is not redistributed in this estimate.

## Current bidder projection

`season6_sup_current_bidder_status` adds a hypothetical event for the current high bidder at the current auction end time. The dashboard shows:

- the raw full-campaign projection if the current bid wins,
- the cap-limited projection,
- estimated USD equivalents when a SUP/USD price is available,
- prior confirmed Season 6 wins/XP for the same wallet.

This is a projection for dashboard context, not official reward accounting.

## Generated outputs

- `generated/season6_metrics.csv/json` - Season 6 configuration, aggregate totals, pricing, and current bidder summary metrics.
- `generated/season6_sup_by_winner.csv/json` - wallet-level confirmed XP, raw projection, capped projection, cap remaining, and USD estimates.
- `generated/season6_sup_rewards_by_auction.csv/json` - settled Season 6 Dog wins with per-auction XP and wallet projection context.
- `generated/season6_sup_current_bidder_status.csv/json` - current high bidder prior and hypothetical projection fields.

The same JSON/CSV files are mirrored under `public/generated/` for the hosted static dashboard.

## Validation

`python3 scripts/validate_dashboard_consistency.py` checks that:

- Season 6 generated files match their public mirrors,
- capped wallet projections do not exceed the configured cap,
- the rendered dashboard Season 6 surface matches `mission3_metrics`,
- the current bidder projection shown in HTML is not stale.
