# Historical USD estimates

This folder contains the reproducible historical-pricing layer for Degen Dogs Mission 1/2/3 archive data.

The layer estimates USD values from exact native auction amounts:

```text
native_amount * historical_usd_price = estimated_usd_value
```

These values are estimates, not official accounting. Exact raw onchain amounts remain the source of truth.

## Commands

```bash
npm run archive:prices:fetch
npm run archive:prices:apply
npm run archive:prices:validate
```

Equivalent direct commands:

```bash
python3 scripts/archive_fetch_historical_prices.py
python3 scripts/archive_apply_usd_estimates.py
python3 scripts/archive_validate_usd_estimates.py
```

## Outputs

- `archive/prices/data/generated/historical_prices_daily.json` / `.csv`
- `archive/prices/data/generated/price_manifest.json`
- `archive/prices/data/generated/auction_usd_estimates.json` / `.csv`
- updated `archive/data/generated/unified_dog_search_index.json` and `public/generated/unified_dog_search_index.json` amount USD estimate fields

Missing prices are explicit (`price_confidence: "missing"`) and are never filled with zero.
