# Configuration

The default pipeline can run against public RPC defaults, but a reliable Base RPC
endpoint is recommended for recovery or scheduled refreshes.

Never commit `.env`, `.env.local`, API keys, RPC secrets, private keys, local cache
paths, or machine-specific paths.

## Safe local env file

```bash
cp .env.example .env.local 2>/dev/null || true
```

Fill only the values you need in `.env.local`.

## Mission 3 dashboard variables

| Variable | Purpose | Sensitive? |
| --- | --- | --- |
| `BASE_RPC_URL` | Single Base RPC endpoint; overrides contract and log RPC lists. | yes if provider-specific |
| `BASE_RPC_URLS` | Comma-separated fallback Base RPC endpoints for contract calls. | yes if provider-specific |
| `BASE_LOG_RPC_URLS` | Comma-separated endpoints used for `eth_getLogs` scans. | yes if provider-specific |
| `BASE_FROM_BLOCK` | First Base block scanned for Mission 3 logs. | no |
| `BASE_LOG_CHUNK` | Maximum block range per log request. | no |
| `BASE_LOG_WORKERS` | Concurrent log-fetch workers. | no |
| `BASE_RPC_BATCH_LIMIT` | JSON-RPC batch size for metadata/balance calls. | no |
| `DOG_METADATA_WORKERS` | Concurrent Dog metadata fetch workers. | no |
| `NEYNAR_API_KEY` | Optional Farcaster identity resolution. | yes |
| `WOOF_USD_PRICE` | Optional manual WOOF/USD override. | no |
| `SUP_USD_PRICE` | Optional manual SUP/USD override. | no |

## Archive variables

| Variable | Purpose | Sensitive? |
| --- | --- | --- |
| `POLYGON_RPC_URL` / `POLYGON_RPC_URLS` | Mission 1 Polygon archive recovery. | yes if provider-specific |
| `POLYGONSCAN_API_KEY` | Optional Mission 1 discovery helper. | yes |
| `DEGEN_RPC_URL` | Mission 2 Degen Chain RPC. | yes if provider-specific |
| `MISSION2_FROM_BLOCK`, `MISSION2_TO_BLOCK`, `MISSION2_LOG_CHUNK` | Mission 2 indexing bounds/tuning. | no |
| `MISSION2_AUCTION_HOUSE` | Mission 2 override, normally not needed when verified config exists. | no |
| `MISSION3_FROM_BLOCK`, `MISSION3_TO_BLOCK`, `MISSION3_LOG_CHUNK`, `MISSION3_LOG_WORKERS` | Mission 3 archive bounds/tuning. | no |
| `MISSION3_ARCHIVE_DB`, `MISSION3_OUTPUT_DIR` | Mission 3 archive local paths. | can reveal local paths |
| `COINGECKO_API_KEY` | Optional historical price fetching. | yes |
| `DUNE_API_KEY` | Optional Dune discovery/recovery work where query IDs are available. | yes |

## What works without secrets

- Static build from checked-in generated files.
- Most public Base refreshes using default RPCs, subject to rate limits.
- Unified archive rebuild from checked-in Mission 1/2/3 generated indexes.
- Historical USD estimate application from checked-in price tables.

## What benefits from API keys

- Reliable high-volume RPC scans.
- Farcaster profile resolution.
- Dune query recovery or export fetching, when query IDs are known.
- Historical price fetching with higher provider limits.
