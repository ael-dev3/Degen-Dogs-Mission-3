# Degen Dogs Mission 1 Archive — Polygon Era

Mission 1 is the Polygon production era of Degen Dogs.

Degen Dogs began as an ETHOnline 2021/testnet project by Mark Carey, then launched in production on Polygon around March 14, 2022. The project used one-Dog-at-a-time auctions, WETH bidding, Dog Biscuits/BSCT for bidders, Idle Finance WETH yield strategies, and Superfluid-powered streaming mechanics.

This folder is an independent/community-built historical archive. It is not official unless later approved by the creator/community. Treat data as verified only when backed by source docs, Polygon logs/receipts, Dune exports, PolygonScan, or explicit reconciliation notes.

## Archive status

- Chain: Polygon PoS, chain ID `137`.
- Bid currency: WETH, verified from the auction contract `weth()` and historical source scripts.
- Core contracts: stored in `config/mission1_contracts.verified.json`.
- Candidate/unknown constants: stored separately in `config/mission1_contracts.candidates.json` and `config/mission1_blocks.candidates.json`.
- Recovery method in this pass: PolygonScan auction-house transaction pages plus public Polygon RPC transaction receipts.
- Live Mission 3 dashboard UI: not modified by this archive module.

## Layout

```text
archive/mission1/
  README.md
  config/                 verified/candidate chain, contract, block, event constants
  docs/                   source, verification, schema, rebuild, reconciliation notes
  sql/                    SQLite schema + marts
  data/
    raw/                  raw receipt/log artifacts and failure manifests
    generated/            CSV/JSON marts, dog search index, manifest, summary
  dune/
    sql/                  recovered Dune SQL, if available later
    results/              recovered Dune CSV/JSON, if available later
```

## Key files

- `config/mission1_chain.verified.json`
- `config/mission1_contracts.verified.json`
- `config/mission1_contracts.candidates.json`
- `config/mission1_blocks.verified.json`
- `config/mission1_blocks.candidates.json`
- `config/mission1_events.verified.json`
- `docs/how_mission1_worked.md`
- `docs/how_to_rebuild.md`
- `docs/reconciliation_report.md`
- `data/generated/mission1_dog_bid_summary.csv`
- `data/generated/mission1_dog_bid_summary.json`
- `data/generated/mission1_dog_search_index.json`
- `data/generated/manifest.json`
- `data/generated/reconciliation_summary.json`
- `data/mission1_archive.sqlite`

## Rebuild

```bash
npm run archive:mission1:discover
npm run archive:mission1:index
npm run archive:mission1:reconcile
```

Optional env vars:

```bash
# Polygon RPC endpoint(s)
# PolygonScan API key
# Dune API key
```

No secrets are required for the default receipt-based recovery path. If secrets are used locally, keep them in environment variables and never commit `.env` files.
