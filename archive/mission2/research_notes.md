# Mission 2 Research Notes

Prepared as an archival foundation for historical Mission 2 Degen Chain data.

## Dune discovery

A public Dune Discover listing has shown a dashboard titled `Degen Dogs Mission 2` by `ael_dev`. The public dashboard URL, dashboard ID, query IDs, and official raw SQL are not yet recovered. Dune pages can be JS-rendered and/or require UI/API access.

Do not fabricate query IDs or SQL. Use the official Dune Read Query endpoint only after query IDs are recovered.

## Mission 2 mechanics summary

Mission 2 moved Degen Dogs onto Degen Chain and Farcaster. Public docs and Mark Carey's Superfluid forum summary describe:

- Native DEGEN bids on Degen Chain.
- One Dog auctioned at a time.
- Farcaster frames/mini app as auction UI.
- 24 hour auctions, 1000 DEGEN reserve price, 10% minimum increment, and 5 minute time buffer.
- WOOF minted from yields / auction proceeds / trading fees.
- WOOF wrapped as WOOFx and streamed to Dog owners via Superfluid.
- Dog transfers redirect stream units to the new owner.

These should be treated as source-backed mechanics, but deployed contract addresses and exact historical ranges are still unverified.

## Mission 3 transition boundary

Mission 3 docs say Dogs teleported from Degen Chain to Base in January 2026 and Base daily auctions started with Dog #590. This makes Dog #590 a useful transition marker, but the last Mission 2 Degen Chain auction should not be asserted until recovered from Dune SQL and/or Degen Chain logs.

## Auction ABI source notes

`IDogsAuctionHouse.sol` defines the events used by the local indexer:

- `AuctionCreated(uint256 indexed dogId, uint256 startTime, uint256 endTime)`
- `AuctionBid(uint256 indexed dogId, address sender, uint256 value, bool extended)`
- `AuctionExtended(uint256 indexed dogId, uint256 endTime)`
- `AuctionSettled(uint256 indexed dogId, address winner, uint256 amount)`
- parameter update events for time buffer, duration, reserve price, and minimum bid increment.

The local indexer computes event topic hashes from signatures at runtime.

## Currency caveat

Mission 2 docs say bids used native DEGEN. The auction source can accept native value when the deployed `weth` config is zero or equivalent, otherwise it can use an ERC20 path. Store raw wei amounts exactly and only label/display converted DEGEN amounts after deployed contract configuration and decimals are verified.
