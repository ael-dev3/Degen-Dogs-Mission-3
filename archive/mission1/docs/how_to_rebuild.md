# How to Rebuild the Mission 1 Archive

Run from the repo root.

## Default public-source rebuild

```bash
npm run archive:mission1:discover
npm run archive:mission1:index
npm run archive:mission1:reconcile
```

This default path uses public docs, PolygonScan transaction pages, and public Polygon RPC receipts. It does not require private keys or API keys.

## Full refresh

```bash
npm run archive:mission1:full
npm run archive:mission1:reconcile
```

`--full-refresh` re-scrapes PolygonScan auction-house transaction pages and refetches receipts.

## Optional environment variables

```bash
# Polygon RPC endpoint(s)
# PolygonScan API key
# Dune API key
```

The default indexer keeps Mission 1 verified contracts and block boundaries in committed config files, not ad-hoc environment variables. If an alternate contract or block range is needed later, add it to candidate config with evidence first, then promote it to verified config before indexing.

Do not commit `.env` files, private RPC keys, or API keys.

## Why receipt recovery?

Free Polygon RPC endpoints often prune 2022 `eth_getLogs` history or enforce very small log windows. During recovery, `polygon-bor-rpc.publicnode.com` returned a pruned-history error for March 2022 logs, `polygon-rpc.com` returned unauthorized, and `1rpc.io` limited `eth_getLogs` to 50 blocks. Public transaction receipts from `polygon.drpc.org` remained usable for known PolygonScan transaction hashes, so the first reproducible archive uses that path and clearly records gaps.

## Future stronger rebuild

For a complete archive-node pass, use a Polygon archive RPC and implement contiguous `eth_getLogs` over the verified block range for:

- Auction house events
- Dog NFT `Transfer`
- BSCT `Transfer`
- WETH / Idle / idleWETHx / treasury / Ukraine flows
- Superfluid stream events

Then compare results to the receipt-based archive and Dune exports.
