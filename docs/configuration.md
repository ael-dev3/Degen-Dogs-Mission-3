# Configuration

The default pipeline can run against maintained public RPC fallbacks, but those
endpoints are rate-limited and have no production SLA. A production runner should
configure at least two independently operated, credentialed, archive-capable Base
providers in both state and log lists.

Never commit `.env`, `.env.local`, API keys, RPC secrets, private keys, local cache
paths, or machine-specific paths.

## Safe local env file

```bash
cp .env.example .env.local 2>/dev/null || true
chmod 600 .env.local
```

Fill only the values you need in `.env.local`. All three macOS launchd installers
parse `${DEGEN_DOGS_ENV_FILE:-.env.local}` as data-only `KEY=value` records, reject
unsupported or duplicate keys and files not owned by the current user or readable by
group/others, and carry the least-privilege RPC settings into worker repairs. The file
is deliberately never sourced as shell code. For direct commands, load it through
`scripts/load_runner_env.sh` and `degen_dogs_load_runner_env`. The WSL launcher uses
the same protected data-only loader.

The runner health job continuously validates this file without reading or logging its
contents. It repairs mode drift to `0600` only for an otherwise safe, current-user-owned
regular file, and refuses symlinks, hard links, unexpected ownership, or unsafe parent
components. The default `.env.local` remains optional when absent; a missing explicitly
configured `DEGEN_DOGS_ENV_FILE` is reported as configuration drift.

## Mission 3 dashboard variables

| Variable | Purpose | Sensitive? |
| --- | --- | --- |
| `BASE_RPC_URL` | Single Base RPC endpoint; overrides contract and log RPC lists. | yes if provider-specific |
| `BASE_RPC_URLS` | Comma-separated fallback Base RPC endpoints for contract calls. | yes if provider-specific |
| `BASE_LOG_RPC_URLS` | Comma-separated endpoints used for `eth_getLogs` scans. | yes if provider-specific |
| `BASE_INCLUDE_PUBLIC_FALLBACKS` | Append public defaults to explicit provider lists; off by default for production lists. | no |
| `BASE_FROM_BLOCK` | First Base block scanned for Mission 3 logs. | no |
| `BASE_LOG_CHUNK` | Maximum block range per `eth_getLogs` request; defaults to Base's reliable 2,000-block recommendation and is capped at 10,000 for provider-specific tuning. | no |
| `BASE_LOG_WORKERS` | Concurrent log-fetch workers. | no |
| `BASE_RPC_ATTEMPTS` | Bounded RPC attempts with provider failover and jitter; default 6 for full scans. | no |
| `BASE_RPC_QUORUM_DEADLINE_SECONDS` | Hard wall-clock limit for a cross-provider quorum call; default 35 seconds. | no |
| `BASE_RPC_HEAD_PROBE_DEADLINE_SECONDS` | Hard endpoint-discovery deadline; default 12 seconds. | no |
| `BASE_RPC_HEAD_PROBE_GRACE_SECONDS` | Short grace for extra healthy endpoints after the minimum quorum responds; default 0.35 seconds. | no |
| `BASE_RPC_SLOW_COOLDOWN_SECONDS` | In-process circuit-breaker cooldown for a straggling endpoint; default 60 seconds. | no |
| `BASE_RPC_MAX_HEAD_SPREAD_BLOCKS` | Maximum spread allowed when selecting an independent safe-head cluster; default 20. | no |
| `BASE_RPC_MAX_BLOCK_AGE_SECONDS` | Maximum age for a newly selected safe snapshot block; default 600 seconds. | no |
| `BASE_RPC_MAX_RESPONSE_BYTES` | Hard response-body cap protecting RPC workers from memory exhaustion; default 32 MiB. | no |
| `BASE_RPC_BATCH_LIMIT` | JSON-RPC batch size for balance/metadata calls; capped at 10. | no |
| `BASE_TOKEN_URI_CHUNK_DELAY_SECONDS` | Minimum spacing between hash-pinned cross-provider `exists`/`tokenURI` batches; defaults to 1 second to avoid public-provider throttling. | no |
| `DOG_METADATA_WORKERS` | Concurrent Dog metadata fetch workers. | no |
| `DOG_METADATA_ALLOWED_HOSTS` | Exact HTTPS host allowlist for onchain token metadata retrieval. | no |
| `DOG_METADATA_MAX_RESPONSE_BYTES` | Maximum metadata JSON body size; default 2 MiB. | no |
| `DOG_METADATA_CACHE_MAX_AGE_SECONDS` | Maximum reuse age for mutable offchain metadata content; default 24 hours. | no |
| `MISSION3_LOG_CACHE` | Enable the local RPC log cache under `.cache/rpc_logs`; default on. | no |
| `MISSION3_LOG_CACHE_OVERLAP_BLOCKS` | Re-fetch overlap when extending cached log ranges; default 100 blocks. | no |
| `MISSION3_LOG_QUORUM_MAX_BLOCKS` | Initial blocks per recent cross-provider log query; default 500. Qualified providers negotiate the largest two-witness span and explicit range/response-size rejections halve it; generic failures never split the query. | no |
| `MISSION3_LOG_QUORUM_WINDOW_BLOCKS` | Maximum recent window split into quorum-checked log queries; default 500. | no |
| `MISSION3_BALANCE_CACHE` | Enable the local WOOF holder balance cache under `.cache/woof_balances.json`; default on. | no |
| `NEYNAR_API_KEY` | Optional Farcaster profile resolution. If Neynar returns HTTP 401/403, the refresh now disables Neynar for that run after the first failed request and keeps wallet/current-miniapp fallbacks instead of spending ~25s retrying every chunk. | yes |
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
| `MISSION3_ARCHIVE_OVERLAP_BLOCKS` | Canonical overlap replaced on every incremental run; default 100 blocks. | no |
| `MISSION3_ARCHIVE_MAX_AGE_SECONDS` | Maximum age of archive state/manifests accepted by health checks; default three hours. | no |
| `MISSION3_ARCHIVE_MAX_HEAD_LAG_BLOCKS` | Maximum lag from the live safe head when archive health runs with `--rpc`; default 6,000 blocks. | no |
| `DEGEN_DOGS_RUN_MISSION3_ARCHIVE` | Hourly Mission 3 archive policy, accepting only `0` or `1`. The absent-key fallback is `1` for backward compatibility, while the fresh WSL template sets `0` because a new clone has no ignored SQLite/raw seed. Keep `0` for a latency-only peer; set `1` after seeding an archive-capable runner. Event watchers always force `0`. | no |
| `COINGECKO_API_KEY` | Optional historical price fetching. | yes |
| `HISTORICAL_PRICES_PREFER_COINGECKO` | Optional override to try CoinGecko before DefiLlama for historical price refreshes; default off to avoid public API 429s. | no |
| `DUNE_API_KEY` | Optional Dune discovery/recovery work where query IDs are available. | yes |

## Onchain watcher variables

These keep Mission 3 current-auction data fresher than the hourly baseline without browser-side RPC polling.

| Variable | Purpose | Sensitive? |
| --- | --- | --- |
| `MISSION3_WATCHER_INTERVAL_SECONDS` | Loop-mode sleep and launchd schedule; defaults to 15 seconds. | no |
| `MISSION3_WATCHER_COOLDOWN_SECONDS` | Minimum delay between non-bid, non-major refreshes. | no |
| `MISSION3_WATCHER_BID_COOLDOWN_SECONDS` | Shorter minimum delay for same-token bid amount/high-bidder refreshes; default 15 seconds. | no |
| `MISSION3_WATCHER_FORCE_REFRESH_AFTER_SECONDS` | Optional local fallback interval; default `0` disables duplicate time-based watcher refreshes because hourly refresh remains the baseline. | no |
| `MISSION3_WATCHER_LOOKBACK_BLOCKS` | Recent block lookback for missing state. | no |
| `MISSION3_WATCHER_SAFETY_OVERLAP_BLOCKS` | Overlap subtracted from `last_checked_block + 1` to avoid missed logs. | no |
| `MISSION3_WATCHER_LOG_CHUNK` | Initial max blocks per `eth_getLogs` request; defaults to 2,000 and halves automatically if the independent RPC quorum rejects a range. | no |
| `MISSION3_WATCHER_STATE_PATH` | Local state path, normally `.local/mission3_onchain_tracker_state.json`. | can reveal local paths |
| `MISSION3_WATCHER_LOCK_PATH` | Local watcher non-overlap lock path. | can reveal local paths |
| `MISSION3_WATCHER_LOG_PATH` | Local concise watcher log path. | can reveal local paths |
| `MISSION3_REFRESH_LOCK_PATH` | Shared refresh lock path used to avoid hourly/event refresh overlap. | can reveal local paths |
| `MISSION3_REFRESH_COMMAND` | Exact supported action after a real onchain signal: `npm run refresh:current` or `npm run refresh:publish`. The watcher uses a fixed argv with no shell and rejects paths, extra arguments, metacharacters, and whitespace variants. Defaults to current, or publish when the installer is explicitly put in auto-push mode. | no |
| `MISSION3_WATCHER_AUTO_PUSH` | Must be `1` before publish-like commands are allowed. | no |
| `MISSION3_WATCHER_REQUIRE_CLEAN_TREE` | Refuse refresh with tracked changes; mandatory in auto-push mode. | no |
| `MISSION3_WATCHER_REFRESH_TIMEOUT_SECONDS` | Refresh command timeout; minimum 60 seconds, default 1800. | no |

## Runner health and retention variables

| Variable | Purpose | Sensitive? |
| --- | --- | --- |
| `DEGEN_DOGS_HEALTH_INTERVAL_SECONDS` | Health LaunchAgent interval; default 300 seconds. | no |
| `DEGEN_DOGS_HEALTH_LIVE_STALE_SECONDS` | Maximum accepted age of the deployed refresh-status sidecar; default 90 minutes (one hourly cycle plus deployment/cache buffer). | no |
| `DEGEN_DOGS_HEALTH_LOG_MAX_BYTES` | Idle log compaction threshold per managed file; default 8 MiB. | no |
| `DEGEN_DOGS_HEALTH_LOG_RETAIN_BYTES` | Newest complete-line tail retained after compaction; default 2 MiB. | no |
| `DEGEN_DOGS_HEALTH_LOG_EMERGENCY_MAX_BYTES` | Hard threshold that permits inode-preserving compaction even while the owning worker is active; default 32 MiB. | no |
| `DEGEN_DOGS_HEALTH_MIN_FREE_BYTES` | Minimum runner filesystem free bytes; default 5 GiB. | no |
| `DEGEN_DOGS_HEALTH_MIN_FREE_PERCENT` | Minimum runner filesystem free percentage; default 5%. | no |

Retention covers launchd stdout/stderr, `refresh.log`, `watch-onchain.log`, and the
high-frequency local JSONL streams `.local/watcher_checks.jsonl`,
`.local/refresh_runs.jsonl`, and `logs/refresh-metrics.jsonl`. Launchd-owned files are
compacted in place so their inode does not change underneath an open descriptor.

## What works without secrets

- Static build from checked-in generated files.
- Most public Base refreshes using default RPCs, subject to rate limits.

Public fallback operation is a degraded, best-effort mode. It can preserve dashboard
availability, but it is not equivalent to a credentialed multi-provider production
SLA.
- Unified archive rebuild from checked-in Mission 1/2/3 generated indexes.
- Historical USD estimate application from checked-in price tables.

## What benefits from API keys

- Reliable high-volume RPC scans.
- Farcaster profile resolution.
- Dune query recovery or export fetching, when query IDs are known.
- Historical price fetching with higher provider limits.
