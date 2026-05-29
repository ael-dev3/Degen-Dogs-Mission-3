# Unified Dog auction record schema

The unified search index is a static JSON array of normalized records. Each record is designed to support Mission 1 Polygon, Mission 2 Degen Chain, and Mission 3 Base auction/archive data without requiring live browser RPC calls.

```json
{
  "schema_version": 1,
  "dog_id": 723,
  "mission": 3,
  "era_label": "Mission 3",
  "chain": "Base",
  "chain_id": 8453,
  "status": "ongoing | settled | recovered | no_auction_* | unknown",
  "dog_image_url": "https://api.degendogs.club/images/723.png",
  "dog_item_url": "https://opensea.io/item/base/.../723",
  "auction_house": "0x...",
  "auction_created": {"block_number": 0, "block_time_utc": "...", "tx_hash": "0x..."},
  "settlement": {"settled": true, "block_number": 0, "block_time_utc": "...", "tx_hash": "0x..."},
  "winner_or_high_bidder": {
    "wallet": "0x...",
    "display": "@handle or 0x1234…abcd",
    "farcaster_fid": null,
    "farcaster_handle": null,
    "profile_url": null,
    "wallet_explorer_url": "https://basescan.org/address/0x..."
  },
  "amount": {
    "raw": "15410000000000000",
    "native": "0.01541",
    "native_symbol": "ETH",
    "price_asset_key": "ETH",
    "usd_estimate": "30.95",
    "usd_estimate_display": "$30.95",
    "usd_estimate_source": "coingecko",
    "usd_estimate_confidence": "high",
    "usd_estimate_time_basis": "settlement_block_time"
  },
  "bid_stats": {"bid_count": 0, "unique_bidder_count": 0, "last_bid_time_utc": "..."},
  "bid_tx_hashes": ["0x..."],
  "rarity": {"rank": 232, "total": 728, "display": "#232/728"},
  "traits": [{"trait_type": "Eyes", "value": "Glasses", "display": "Eyes: Glasses"}],
  "links": {"item": "...", "auction_tx": "...", "settlement_tx": "...", "explorer": "...", "repo_archive": "archive/dogs/by-id/723.json"},
  "source": {"confidence": "verified | partial | candidate | unknown", "sources": ["base_logs"], "notes": ""},
  "search_text": "normalized lowercase search blob"
}
```

## Required behavior

- Native raw amount remains the source of truth; USD values are derived estimates.
- Explorer links are chain-specific. Mission 1 uses PolygonScan, Mission 2 uses a documented Degen Chain explorer when available, and Mission 3 uses BaseScan.
- Mission 1/2 item links are omitted unless verified. Mission 3 OpenSea links are generated from the verified Base collection.
- Missing fields are `null` or empty strings, never fabricated values.
- `source.confidence` describes the archive record, while `amount.usd_estimate_confidence` describes only the historical price match.
