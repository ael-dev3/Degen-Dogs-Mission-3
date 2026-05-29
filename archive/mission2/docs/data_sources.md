# Mission 2 Data Sources

## Verified onchain sources

- Degen Chain RPC: `https://rpc.degen.tips`
- Chain ID: `666666666`
- Auction house: `0x3620ca030a023bce87ec59a8b0e979bd7607fdbd`
- Dog NFT: `0x77722fa8a43dfcc3e01c1db0b150b9db9d1e53dd`
- WDEGEN: `0xeb54dacb4c2ccb64f8074eceea33b5ebb38e5387`

Verification methods:

- `eth_chainId`
- `eth_getCode`
- `eth_call` contract getters and ERC20/ERC721 metadata calls
- Chunked `eth_getLogs` for auction lifecycle event topics

## Source repository references

- Mark Carey repo: <https://github.com/markcarey/degendogs>
- Auction-house event ABI source: `contracts/interfaces/IDogsAuctionHouse.sol`
- Auction-house implementation source: `contracts/DogsAuctionHouse.sol`
- WOOF vault allocation source: `woof-vault.json`

## Dune status

Dune provenance is not complete. The likely dashboard title/owner is `Degen Dogs Mission 2` by `ael_dev`, but dashboard URL, dashboard ID, query IDs, official SQL, and result exports were not recovered in this session.

No Dune IDs or SQL have been fabricated. See:

- `archive/mission2/dune/dune_dashboards.json`
- `archive/mission2/dune/dune_queries.json`
- `archive/mission2/dune/query_ids.json`

## Confidence levels

- `verified`: verified from onchain RPC calls, source repository files, or generated chain-log archive.
- `verified_onchain`: decoded directly from verified Degen Chain logs and contract getters.
- `verified_metadata_only`: token metadata verified, but protocol/accounting semantics not fully reconciled.
- `likely`: plausible external provenance but missing definitive ID/source in this archive.
- `unknown`: no source recovered yet.

## Dune recovery attempts

- Browser: `https://dune.com/ael_dev/degen-dogs-mission-2` hit Cloudflare verification.
- Dune public GraphQL from the browser page returned HTTP 403 behind Cloudflare.
- r.jina mirrors for direct candidate slugs returned Dune 404 pages.
- r.jina Dune search for `Degen Dogs Mission 2`, `Degen Dogs`, `ael_dev`, `WOOF WOOFx Degen Dogs`, and `Degen Chain Degen Dogs` did not reveal verified Mission 2 dashboard/query IDs.
- `DUNE_API_KEY` was not present in the environment.
