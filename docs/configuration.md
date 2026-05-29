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
| `NEYNAR_API_KEY` | Optional Farcaster identity resolution and last-resort channel feed fallback. | yes |
| `WOOF_USD_PRICE` | Optional manual WOOF/USD override. | no |
| `SUP_USD_PRICE` | Optional manual SUP/USD override. | no |

## Farcaster channel feed variables

The dashboard includes a cached read-only `/degendogs` channel panel. The local
runner writes `generated/farcaster_degendogs_channel.json` and
`public/generated/farcaster_degendogs_channel.json`; browser code only reads that
static JSON and never calls secret-key APIs.

Source priority is direct/open first:

1. Hypersnap read API.
2. Snapchain-compatible direct node endpoint, if configured/available.
3. Other open Farcaster-compatible reads, if added later.
4. Neynar only as explicit fallback.

| Variable | Purpose | Sensitive? |
| --- | --- | --- |
| `FARCASTER_FEED_ENABLED` | Set `0` to write a disabled empty snapshot; defaults to enabled. | no |
| `FARCASTER_CHANNEL_ID` | Channel id to snapshot; defaults to `degendogs`. | no |
| `FARCASTER_CHANNEL_LIMIT` | Recent casts to keep, clamped to 1-50; defaults to 30. | no |
| `HYPERSNAP_BASE_URL` | Hypersnap/Snapchain public node base; defaults to `https://haatz.quilibrium.com` when unset. Set blank to disable in controlled tests. | no |
| `HYPERSNAP_READ_API_URL` | Optional Hypersnap read API endpoint/base override. | no |
| `SNAPCHAIN_RPC_URL` | Optional Snapchain-compatible HTTP base for `/v1/castsByParent`. | no, unless private infra |
| `FARCASTER_DIRECT_TIMEOUT_SECONDS` | Timeout for direct Farcaster reads; defaults to 15. | no |
| `NEYNAR_FALLBACK_ENABLED` | Must be `1` before Neynar channel-feed fallback is attempted. | no |
| `NEYNAR_API_KEY` | Required only for the optional Neynar fallback. Never exposed to generated JSON, HTML, or browser code. | yes |

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

## Onchain watcher variables

These keep Mission 3 current-auction data fresher than the hourly baseline without browser-side polling.

| Variable | Purpose | Sensitive? |
| --- | --- | --- |
| `MISSION3_WATCHER_INTERVAL_SECONDS` | Loop-mode sleep; scheduler examples use 120 seconds. | no |
| `MISSION3_WATCHER_COOLDOWN_SECONDS` | Minimum delay between non-major refreshes. | no |
| `MISSION3_WATCHER_FORCE_REFRESH_AFTER_SECONDS` | Optional local fallback interval; hourly refresh remains the baseline. | no |
| `MISSION3_WATCHER_LOOKBACK_BLOCKS` | Recent block lookback for missing state. | no |
| `MISSION3_WATCHER_SAFETY_OVERLAP_BLOCKS` | Overlap subtracted from `last_checked_block + 1` to avoid missed logs. | no |
| `MISSION3_WATCHER_LOG_CHUNK` | Max blocks per `eth_getLogs` request. | no |
| `MISSION3_WATCHER_STATE_PATH` | Local state path, normally `.local/mission3_onchain_tracker_state.json`. | can reveal local paths |
| `MISSION3_WATCHER_LOCK_PATH` | Local non-overlap lock path. | can reveal local paths |
| `MISSION3_WATCHER_LOG_PATH` | Local concise watcher log path. | can reveal local paths |
| `MISSION3_REFRESH_COMMAND` | Command to run after a real onchain signal; default is `npm run data && npm run build`. | no, unless embedding secrets |
| `MISSION3_WATCHER_AUTO_PUSH` | Must be `1` before publish-like commands are allowed. | no |
| `MISSION3_WATCHER_REQUIRE_CLEAN_TREE` | Refuse refresh with tracked changes; defaults on in auto-push mode. | no |
| `MISSION3_WATCHER_REFRESH_TIMEOUT_SECONDS` | Refresh command timeout. | no |

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
