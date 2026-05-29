# Price source notes

- `ethereum` CoinGecko ID was verified by the implementation script/API check.
- `degen-base` CoinGecko ID was verified by the implementation script/API check.
- Polygon WETH contract `0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619` is verified in the Mission 1 contract config and is priced via ETH/USD.
- The Dune SQL file is a template only. Do not treat it as verified Dune schema until tested against the current Dune catalog.
- DefiLlama keys are configured for future fallback but are not mixed silently with CoinGecko output.
