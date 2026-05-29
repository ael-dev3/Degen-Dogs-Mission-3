# Mission 1 Reconciliation Report

Generated at UTC: `2026-05-28T23:51:14Z`
Indexer run: `mission1-20260528T235114Z`

## Status

- Recovery status: `partial_but_source_backed`
- Method: PolygonScan auction-house normal transaction pages plus public Polygon RPC transaction receipts
- Classification: source-backed partial archive, not a complete official accounting yet.

## Counts Recovered

- PolygonScan auction-house transactions found: `862`
- Receipts fetched: `862`
- Raw relevant logs stored: `8359`
- AuctionCreated rows: `182` (Dog IDs `1` to `200`)
- AuctionBid rows: `545` (Dog IDs `1` to `166`)
- AuctionSettled rows: `181` (Dog IDs `1` to `199`)
- Latest auction state from `auction()` eth_call: Dog `200`, amount `0` WETH, settled `False`.
- NFT mint-transfer distinct token IDs: `201` (range `0` to `200`)
- BSCT transfer rows: `708`

## 201 Polygon Dogs Claim

- Status: `exact_match_for_total_supply_and_mint_ids_with_dogmaster_reward_rule; final Dog 200 settlement remains open`
- Onchain Dog `totalSupply()` observed during discovery: `201`
- Receipt archive max created Dog ID: `200`
- DogMaster reward IDs excluded from auction coverage: `0, 11, 22, 33, 44, 55, 66, 77, 88, 99, 110, 121, 132, 143, 154, 165, 176, 187, 198`
- Notes: Dog NFT totalSupply() returned 201 and receipt-backed NFT mint transfers cover token IDs 0-200. Dog.sol explains non-auction IDs as dogMaster reward IDs: 0 and every 11th Dog. AuctionCreated rows cover all expected non-reward auction IDs through 200. Dog 200 is created but has no recovered settlement and the latest auction() state shows dogId 200, amount 0, settled false.

## Dog #1 / Ukraine Dog

- Status: `verified_from_medium_and_receipt_logs_for_dog1_presence; donation flow still needs full treasury/Ukraine reconciliation`
- Created tx: `0xa4fbd483360ceb134dc139058ae1c76efd3fb1fd6ce903a91c36886732012513`
- Settled tx: `0xc1fb04317739a5cec95f93318e802e5a15e8bc9e66dfe44ce4e2bdd5e64d88c0`
- Source note: Medium article published by Degen Dogs on 2022-03-14 states Dog #1 / Ukraine Dog was a 72h special auction with 100% donation.

## Dune

- Dune status: `no_api_key_public_ui_checked_no_mission1_exports_recovered`

## Known Gaps / Mismatches

- `created_without_settlement_ids` (investigate): AuctionCreated rows without recovered AuctionSettled rows for Dog IDs: 200

## Interpretation

The recovered archive verifies core Mission 1 Polygon contracts and captures a substantial receipt-backed auction/log dataset. It should not yet be treated as a final official Mission 1 accounting because free public RPC endpoints pruned old `eth_getLogs` history and Dune/PolygonScan API access was unavailable. Future reconciliation should use Dune exports, Etherscan V2/PolygonScan API with a key, or an archive Polygon RPC to compare every expected Dog ID and flow.
