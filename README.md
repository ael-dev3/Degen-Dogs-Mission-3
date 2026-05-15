# Degen Dogs Mission 3

Professional dashboard rebuild for **Degen Dogs Mission 3**: a dark, minimal GitHub Pages site plus Dune-ready SQL for WOOF, holders, streams, and auction analytics.

**Live site:** https://ael-dev3.github.io/Degen-Dogs-Mission-3/

**Dune dashboard:** https://dune.com/ael_dev/degen-dogs-mission-3

## What is included

- Modern 2026 dark website, no generic gradient SaaS treatment
- DuneSQL query set in [`sql/`](./sql)
- Dashboard manifest in [`dashboard/dune-dashboard.json`](./dashboard/dune-dashboard.json)
- Source and address notes in [`docs/data-sources.md`](./docs/data-sources.md)
- GitHub Actions deployment to GitHub Pages

## Dashboard panels

1. **Mission 3 KPI strip** — WOOF supply, transfer volume, holders, DEX volume, and latest activity.
2. **WOOF market activity** — daily price, notional volume, buys/sells, and active traders.
3. **Transfer-ledger distribution** — ERC20 transfer-ledger reconstruction and balance buckets, with realtime streaming deltas tracked separately.
4. **Superfluid stream updates** — CFA flow updates filtered to the WOOF super token.
5. **Auction flow** — Nouns-style auction events on Base once the auction contract address is confirmed.
6. **Contract discovery** — helper query to identify/verify Mission 3 NFT and auction contracts on Base.

## Verified constants

| Item | Value |
| --- | --- |
| Network | Base Mainnet |
| Token | Degen Dogs WOOF |
| Symbol | WOOF |
| Contract | `0x3e5c4FA0cAA794516eD0DF77f31daA534918d492` |
| Decimals | `18` |
| Total supply | `100,000,000,000 WOOF` |
| Mission 3 auction cadence | 24 hours |
| Holder stream window | 90 days |

## Local development

```bash
npm install
npm run dev
```

## Build

```bash
npm run build
```

## Notes

The public Mission 3 docs do not currently publish Base Mainnet contract addresses for the Dog NFT or auction house. The SQL therefore keeps auction/NFT panels parameterized and includes a discovery query rather than hard-coding stale Polygon addresses.
