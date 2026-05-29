# How Mission 1 Worked

Mission 1 was the Polygon production era of Degen Dogs.

## Origin

Degen Dogs started as a Mark Carey ETHOnline 2021 project. The public `markcarey/degendogs` repo describes it as an ETHOnline 2021 submission powered by Superfluid, Uniswap, Chainlink, Compound, DAI by Maker, and Skynet. The original hackathon/testnet deployment is separate from the later Polygon production launch and should not be mixed with Mission 1 production data.

## Polygon launch

A Degen Dogs Medium article was published on March 14, 2022. A Superfluid forum post later states that Degen Dogs launched in production on Polygon on “3.14 of 2022.” Polygon made the auction, minting, DeFi, and streaming interactions cheap enough to run frequently.

## Auctions

Dogs were not minted directly by users. The auction house sold one Dog at a time. When an auction settled, the contract issued the Dog to the winner and created the next auction.

Verified auction events:

- `AuctionCreated(uint256 indexed dogId, uint256 startTime, uint256 endTime)`
- `AuctionBid(uint256 indexed dogId, address sender, uint256 value, bool extended)`
- `AuctionExtended(uint256 indexed dogId, uint256 endTime)`
- `AuctionSettled(uint256 indexed dogId, address winner, uint256 amount)`

The auction contract `weth()` points to Polygon WETH, so bid amounts are treated as WETH base-unit integers unless a specific reconciliation proves otherwise.

## Dog #1 / Ukraine Dog

The Degen Dogs Medium article says Dog #1 was the Ukraine Dog, a special first auction lasting 72 hours where 100% of proceeds were donated. The same article says that after the first auction, 10% of auction proceeds were donated to Unchain Ukraine.

This archive treats the Dog #1 Ukraine-auction statement as source-verified from the Medium article. Full donation-flow accounting remains a TODO until the Ukraine contract, WETH, Idle, and treasury transfers are reconciled end-to-end.

## Auction proceeds and rewards

The Medium article describes this flow after auctions:

1. A charitable donation was made.
2. 50% of proceeds were shared with pre-existing Dog holders over 365 days using Superfluid.
3. Remaining proceeds went to the DAO treasury.
4. All bidders, including non-winning bidders, received Dog Biscuits / BSCT.
5. Before treasury/stream distribution, WETH proceeds were deposited into Idle Finance WETH Best Yield; resultant idleWETH was used for treasury/streams.

The contracts and source scripts verify the WETH, Idle WETH, Dog NFT, auction house, BSCT, governance, treasury, and Ukraine contract addresses. The exact reward accounting is not yet official or complete in this archive.

## Transition to Mission 2

Mission 2 later moved Degen Dogs from Polygon to Degen Chain / Farcaster. Current docs and public context describe 201 Dogs joining on Polygon before Mission 2. This archive verifies Dog NFT `totalSupply() = 201` on Polygon, but preserves open reconciliation notes for the exact Dog ID range and auction/mint edge cases.
