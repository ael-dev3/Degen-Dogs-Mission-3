# Mission 2 Reconciliation Report

Updated UTC: `2026-05-28T23:22:48Z`

## Summary

The onchain Mission 2 auction archive is internally consistent and verified against Degen Chain logs and contract getters. Dune reconciliation is not complete because the Mission 2 dashboard/query IDs, official SQL, and result exports were not recoverable in this session.

## Local onchain totals

- AuctionCreated logs: `369`
- AuctionBid logs: `1630`
- AuctionExtended logs: `273`
- AuctionSettled logs: `369`
- Raw lifecycle logs: `2641`
- Unique bidders: `84`
- Dog ID range: `201-589`
- First created block: `24692180`
- Last settlement block: `26622432`
- Settled WDEGEN/DEGEN volume: `2,668,384` display units from exact 18-decimal raw amounts

## Dune comparisons

| Target | Local value | Dune value | Classification | Notes |
| --- | ---: | --- | --- | --- |
| AuctionCreated count | 369 | unavailable | missing in Dune recovery | Official Dune results not recovered. |
| AuctionBid count | 1630 | unavailable | missing in Dune recovery | Official Dune results not recovered. |
| AuctionExtended count | 273 | unavailable | missing in Dune recovery | Official Dune results not recovered. |
| AuctionSettled count | 369 | unavailable | missing in Dune recovery | Official Dune results not recovered. |
| Dog ID range | 201-589 | unavailable | missing in Dune recovery | Verified locally through chain logs and final NFT/auction state. |
| Settled volume | 2,668,384 | unavailable | missing in Dune recovery | Uses exact raw settlement amounts and WDEGEN 18 decimals. |

## Discrepancies

No local-vs-Dune discrepancies can be classified yet because no official Dune query results were recovered. This is not a match claim.

## Next actions

1. Recover Dune dashboard URL/query IDs from authenticated Dune UI.
2. Fetch official SQL/results through Dune API.
3. Compare counts, winner list, final bid amounts, total volume, top bidders, and daily activity.
4. Record each mismatch with dog ID / tx / log reference.
