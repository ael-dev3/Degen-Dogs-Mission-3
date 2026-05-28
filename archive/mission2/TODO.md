# Mission 2 Archive TODO

## Human recovery checklist

- [ ] Recover exact Dune Mission 2 dashboard URL.
- [ ] Recover every dashboard query ID from Dune UI.
- [ ] Confirm Dune API access can read the recovered queries.
- [ ] Recover deployed Mission 2 auction house address.
- [ ] Recover deployed Mission 2 Dog NFT address.
- [ ] Recover Mission 2 WOOF/WOOFx token addresses.
- [ ] Recover Superfluid pool and pool manager addresses.
- [ ] Recover MintClub bonding curve token address.
- [ ] Recover deployment block / first relevant auction block.
- [ ] Recover final Degen Chain auction before Base migration.
- [ ] Find official Mark Carey/Farcaster casts announcing Mission 2 contract deployments.

## Index/reconciliation checklist

- [ ] Run `archive/mission2/scripts/recover_dune_queries.py` after query IDs are known.
- [ ] Fill only verified addresses into a non-placeholder contracts config.
- [ ] Run `npm run archive:mission2:check`.
- [ ] Run `npm run archive:mission2` for the verified block range.
- [ ] Compare `AuctionCreated` count with Dune dashboard count.
- [ ] Compare `AuctionSettled` count with Dune dashboard count.
- [ ] Compare total raw bid volume with Dune dashboard.
- [ ] Compare top bidders and winners with Dune dashboard.
- [ ] Confirm Dog ID range against Mission 3 start at Dog #590.
- [ ] Document gaps in `mission2_index_gaps` and the manifest.
