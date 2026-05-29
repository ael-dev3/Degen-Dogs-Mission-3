# Historical USD methodology

## Time basis

- Settled/final auction values use settlement block time when available.
- Live/current auction values use current/latest auction time only when marked as current values.
- If settlement time is missing, the pipeline falls back to last bid time, then auction-created time, and records the basis.

## Assets

- Mission 1: Polygon WETH is verified in `archive/mission1/config/mission1_contracts.verified.json`; WETH is priced with ETH/USD.
- Mission 2: Degen Chain auction token is verified as WDEGEN/DEGEN; USD uses DEGEN historical price.
- Mission 3: Base ETH auctions use ETH/USD.

## Source priority

1. Dune daily prices if Dune access/schema is verified.
2. CoinGecko historical market chart range by verified coin ID.
3. DefiLlama coin prices as a documented fallback/cross-check.
4. DEX-derived TWAP only as a last resort with liquidity caveats.
5. `null` / missing when no reliable price exists.

The current implementation fetches CoinGecko daily historical rows and stores provenance on every row.

## Matching

The applier uses same UTC date first. If missing, it may use the nearest prior daily price within three days and lowers confidence to `medium`. Otherwise USD fields remain null/missing.

## Arithmetic

Python `Decimal` is used for all native amount × price calculations. Display fields round to two decimals while raw estimate strings preserve more precision.
