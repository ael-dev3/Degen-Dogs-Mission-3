# Data sources and address notes

## Primary sources

- Dune dashboard target: <https://dune.com/ael_dev/degen-dogs-mission-3>
- Mission 3 introduction: <https://docs.degendogs.club/introduction.md>
- WOOF token docs: <https://docs.degendogs.club/basics/woof.md>
- Auctions docs: <https://docs.degendogs.club/basics/auctions.md>
- Streamonomics docs: <https://docs.degendogs.club/basics/streamonomics.md>

## Verified Base token

- `$WOOF`: `0x3e5c4FA0cAA794516eD0DF77f31daA534918d492`
- Symbol: `WOOF`
- Name: `Degen Dogs WOOF`
- Decimals: `18`
- Total supply: `100,000,000,000 WOOF`

Verified with Base `eth_call` against `https://mainnet.base.org`.

## Contract caveat

The public docs say Mission 3 moved to Base Mainnet, but the current Contracts page still says `TODO` for updated Degen contracts and lists older PolygonScan addresses. Those older Dog NFT, auction house, and TokenVestor addresses have no code on Base Mainnet.

For that reason:

- WOOF panels are hard-coded to the verified Base WOOF token.
- Auction and NFT panels use Dune parameters.
- `sql/06_contract_discovery.sql` exists to verify the Mission 3 Base NFT and auction contracts before final Dune wiring.

## Dune tables used

- `erc20_base.evt_Transfer` for transfer-ledger balances and movement. For WOOF, do not interpret transfer-ledger balances as complete realtime Superfluid balances without also reviewing stream state.
- `dex.trades`
- `base.logs`
- `nft.transfers`
- `nft.trades`

If a decoded Superfluid table exists for the current Dune namespace, `sql/04_superfluid_streams.sql` can be swapped from raw `base.logs` decoding to the decoded CFA event table.
