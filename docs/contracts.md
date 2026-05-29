# Contracts

Verified Mission 3 contract addresses used by the live dashboard pipeline.

| Contract | Address |
| --- | --- |
| Auction House | `0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA` |
| Degen Dogs NFT | `0x09154248fFDbaF8aA877aE8A4bf8cE1503596428` |
| WOOF token | `0x3e5c4FA0cAA794516eD0DF77f31daA534918d492` |
| SUP token | `0xa69f80524381275A7fFdb3AE01c54150644c8792` |

## Network

- Base Mainnet
- Chain ID: `8453`
- Auction currency: ETH

## Source of truth

- Runtime constants in `scripts/build_dashboard.py`.
- Verified Mission 3 archive config in `archive/mission3/config/`.
- Analytics logic in `sql/mission3_dashboard.sql`.

Update contract constants only after verifying the source of truth and running the full
data/build validation suite.
