# Season 6 SUP reward projections

The dashboard keeps Season 6 SUP math auditable in generated data and docs, while the visible homepage shows only one compact estimate card:

```text
Season 6 SUP estimate
≈X SUP
≈$Y if current bid wins
Adjusted for prior S6 wins; estimate only.
```

The homepage does **not** expose the full pool/cap/XP formula block. Detailed fields stay in `generated/` and `public/generated/`.

## Source-of-truth config

`config/season6_sup_rewards.json` controls the model:

- `enabled`: `true`
- `sup_token`: `0xa69f80524381275a7ffdb3ae01c54150644c8792`
- `season_start_utc`: `2026-06-02T00:00:00Z`
- `season_end_utc`: `2026-09-01T00:00:00Z`, representing the Aug 31 campaign end as an exclusive UTC bound
- `total_allocation_sup`: `251340`
- `wallet_cap_sup`: `12500`
- `xp_per_settled_win`: `100`
- `reward_start_delay_days`: `0`
- `cap_level`: `wallet_estimate`
- `projection_model`: `time_weighted_xp_with_expected_future_daily_auctions`
- `expected_future_settlement_interval_seconds`: `86400`
- `visible_dashboard_mode`: `compact_final_estimate_only`

## Model

Season 6 rewards are estimates, not official accounting. The model assumes:

1. Rewards begin on Jun 2, 2026 with no modeled Season 5-style delay.
2. Each settled Dog auction inside the Season 6 window adds `100 XP` to the winner wallet at settlement time.
3. All settled wins have equal XP weight.
4. SUP flow is time-weighted across the campaign window.
5. During each interval, active wallets split that interval's SUP by active XP share.
6. If no wallet has active XP for an interval, that interval remains unallocated in generated metrics.
7. Wallet projections are capped at the configured `12,500 SUP` wallet-level estimate.
8. Cap overflow is not redistributed in this dashboard estimate.

Example behavior:

- With one active Season 6 winner, that wallet receives 100% of flow until another win settles.
- With two equal 100 XP winners, each receives 50% while both are active.
- With three equal 100 XP winners, each receives one third while all three are active.
- As more auctions settle, earlier winners are diluted for later time intervals.

## Current-bid estimate

For the current high bidder, the generator computes two full-campaign projections:

1. `without current win`: confirmed Season 6 wins plus configured future dilution events.
2. `with current win`: the same projection plus a hypothetical 100 XP event for the current high bidder at the estimated current auction settlement time.

The visible card uses only the cap-aware incremental difference:

```text
max(0,
  min(wallet_cap_sup, projected_total_with_current_win)
  - min(wallet_cap_sup, projected_total_without_current_win)
)
```

That means the homepage estimate:

- counts prior Season 6 wins by the same wallet,
- reduces the estimate when the wallet is already near the cap,
- never shows more than the configured `12,500 SUP` cap,
- uses the current SUP/USD price when available,
- changes when bids, settlements, auction timing, SUP price, or config change.

If there is no current high bidder, the visible card renders the neutral state `Bid to estimate S6 SUP`. If the wallet is already projected near the cap, it can render `≈0 SUP` with `Wallet estimate already near cap.`

## Future dilution projection

The current-bid estimate includes expected future dilution by adding an unknown future winner every `expected_future_settlement_interval_seconds` after the current auction estimate, defaulting to one daily settlement. These unknown future winners add XP to the denominator but are not attributed to the current bidder.

This avoids over-displaying a huge raw uncapped projection that assumes no later winners enter the XP pool.

## Generated outputs

- `generated/season6_metrics.csv/json` - Season 6 configuration, totals, SUP pricing, current-bid cap-aware estimate, prior-win counts, future-dilution flags, and legacy compatibility aliases.
- `generated/season6_sup_by_winner.csv/json` - wallet-level confirmed XP, raw projection, capped projection, cap remaining, and USD estimates.
- `generated/season6_sup_rewards_by_auction.csv/json` - settled Season 6 Dog wins with per-auction XP and wallet projection context.
- `generated/season6_sup_current_bidder_status.csv/json` - current high bidder prior wins, hypothetical current-win projection, raw incremental estimate, cap-aware incremental estimate, cap remaining, and future-dilution metadata.

The same files are mirrored under `public/generated/` for the hosted static dashboard.

## Validation

`python3 scripts/validate_dashboard_consistency.py` checks that:

- generated Season 6 files match their `public/generated/` mirrors,
- capped wallet projections do not exceed the configured cap,
- the current-bid cap-aware estimate does not exceed the cap or remaining cap room,
- prior Season 6 wins/XP in `mission3_metrics` match `season6_sup_current_bidder_status`,
- the compact visible card matches generated metrics,
- the compact card appears before Bid payback,
- the old verbose homepage strings stay out of `index.html`.
