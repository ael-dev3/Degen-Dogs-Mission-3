# Unified auction search implementation notes

_Last updated: 2026-05-29T11:16:53Z_

## Current implementation surfaces

- `scripts/build_dashboard.py` builds the static HTML dashboard, exports CSV/JSON tables under `generated/` and `public/generated/`, and currently renders the primary auction table from SQLite tables produced by `sql/mission3_dashboard.sql`.
- `sql/mission3_dashboard.sql` creates the canonical Mission 3 feed tables (`current_auction`, `auction_feed`, `auction_winners`, `auction_timeline`, etc.) from decoded Base auction logs plus local Dog metadata.
- Existing mission archives already produce generated dog search indexes:
  - `archive/mission1/data/generated/mission1_dog_search_index.json` (Polygon, WETH verified in config)
  - `archive/mission2/data/generated/mission2_dog_search_index.json` (Degen Chain, WDEGEN/DEGEN verified in config)
  - `archive/mission3/data/generated/mission3_dog_search_index.json` (Base ETH logs)
- `generated/historical_dog_search.json` is a combined generated lookup, but it is table-shaped and has been visible as a separate lower dashboard table rather than acting as the main auction feed/search layer.

## Verified vs incomplete data

- Mission 3 is the live source of truth for current auction state and recent Base auction display.
- Mission 1 archive data is verified from Polygon receipts/logs and docs, but includes non-auction special cases such as Dogmaster reward mints. Those records must not be presented as fake auctions.
- Mission 2 archive data is verified from Degen Chain logs/contract getters. The auction contract exposes a `weth()` token that is verified as WDEGEN; display is normalized as DEGEN for auction value while provenance notes preserve the wrapped token detail.
- Farcaster identity coverage is best for current/recent Mission 3 rows. Mission 1/2 wallet identity remains optional and should fall back to chain-specific wallet links.

## Safest integration path

1. Keep the existing Mission 3 auction feed row/column visual style as the canonical initial view.
2. Generate a normalized static cross-mission index at `public/generated/unified_dog_search_index.json` and `archive/data/generated/unified_dog_search_index.json`.
3. Show only the latest 10 feed rows initially.
4. On search, load the unified index in the browser and render matching records in the same row format, hiding the default feed while search results are active.
5. Keep Mission 1/2 data searchable only from verified archive records and avoid generating unverified OpenSea/item links.
6. Keep advanced/generated archive datasets available as files, but do not render them as the main user-facing bottom dashboard experience.
