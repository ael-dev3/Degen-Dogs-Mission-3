# Mission 3 Verification Notes

## Verified in repo

- Current Mission 3 dashboard uses Base and the verified contract constants in `scripts/build_dashboard.py`.
- Default Mission 3 scan starts at Base block `40500000`.
- Existing generated dashboard outputs include Dog #590 and later Mission 3 auctions.

## Verified by public Base RPC during archive setup

- `eth_chainId` returned `8453`.
- Verified contracts returned non-empty bytecode.
- Dog #590 `AuctionCreated` was found in Base tx `0x875be581184c855641547daf148a091a709a3001740ee0ac26ea85a55a5a4400` at block `40564762`.

## Operational note

Public RPCs may reject large log ranges. Use `MISSION3_LOG_CHUNK=10000` or lower if a provider returns HTTP 413 or a range-limit error.
