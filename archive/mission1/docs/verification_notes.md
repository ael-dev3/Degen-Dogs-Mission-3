# Mission 1 Verification Notes

Generated at UTC: `2026-05-28T23:51:14Z`

Verified means backed by at least one public source plus onchain/PolygonScan evidence. Candidate means source-backed but not yet reconciled end-to-end. Unknown means intentionally left blank.

## Verified in this pass

- Polygon PoS chain ID `137`.
- Mission 1 Dog NFT, auction house, BSCT, governance, treasury, donation, WETH, Idle WETH, and Superfluid-related addresses from docs/GitHub plus PolygonScan verified pages/onchain calls.
- Dog NFT `name() = Degen Dogs`, `symbol() = DOG`, `totalSupply() = 201` from Polygon RPC calls during discovery.
- BSCT `name() = Dog Biscuits`, `symbol() = BSCT`, `decimals() = 18` from Polygon RPC calls during discovery.
- Dog #1 Ukraine auction claim from the Degen Dogs Medium article plus receipt-backed AuctionCreated/AuctionSettled rows for Dog #1.
- DogMaster reward mint rule from `Dog.sol`: token ID `0` and every `11th` Dog were minted to the dogMaster, explaining why `totalSupply() = 201` while auction rows skip those IDs.

## Not fully verified yet

- Complete treasury, Idle, Superfluid stream, and Unchain Ukraine transfer accounting.
- Dog #200 final state beyond latest `auction()` result; archive currently observes Dog 200 created with amount 0 and unsettled at latest block.
- Complete Dune dashboard/query recovery, because public Dune UI returned HTTP 403 and no `DUNE_API_KEY` was available.
