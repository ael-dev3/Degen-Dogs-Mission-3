# Refresh runner

The public site is served by GitHub Pages, but fresh data comes from a private/local runner that regenerates static files and pushes commits.

## Available commands

```bash
npm run refresh:local
npm run refresh:publish
npm run refresh:archive
npm run refresh:install
npm run watch:install
npm run watch:onchain
npm run watch:onchain:loop
npm run watch:onchain:dry
npm run watch:onchain:force
```

- `refresh:local` runs `npm run data && npm run build` without committing or pushing.
- `refresh:publish` runs `scripts/refresh_and_publish.sh`.
- `refresh:archive` runs Mission 3 archive indexing first, then the normal publish flow.
- `refresh:install` installs the macOS launchd hourly runner.
- `watch:install` installs the macOS launchd event watcher runner.
- `watch:onchain` runs the precise Mission 3 onchain activity tracker once.
- `watch:onchain:loop` keeps the tracker running and sleeps between checks.
- `watch:onchain:dry` detects signals and prints intended refreshes without executing the command or writing state.
- `watch:onchain:force` forces the configured refresh command once, useful for first-run bootstrap or manual repair.

The older `watch:auction` scripts remain aliases for compatibility.

## Baseline hourly refresh

Keep the hourly refresh as the safety baseline:

```cron
0 * * * * cd /path/to/Degen-Dogs-Mission-3 && npm run refresh:publish
```

The Pages workflow runs `npm ci` and `npm run build`. It does not run `npm run data`; the runner must commit fresh generated data before pushing if live dashboard data should update.

## Event-aware onchain tracker

The precise tracker is a local-only accelerator for Mission 3 auction freshness. It does not add browser chain polling or a hosted backend.

`scripts/watch_mission3_onchain_activity.py` delegates to `scripts/watch_mission3_auction.py`, which:

1. Loads the verified Mission 3 auction-house address and verified event topics from `archive/mission3/config/`.
2. Reads Base `eth_blockNumber`.
3. Reads the current auction-house `auction()` state at the latest block.
4. Scans recent auction-house logs for:
   - `AuctionBid(uint256,address,uint256,bool)`,
   - `AuctionCreated(uint256,uint256,uint256)`,
   - `AuctionSettled(uint256,address,uint256)`,
   - `AuctionExtended(uint256,uint256)`.
5. Uses local state at `.local/mission3_onchain_tracker_state.json` to dedupe `(transactionHash, logIndex)` activity.
6. Triggers the configured refresh command only when a meaningful signal changed.
7. Writes concise operational logs to `logs/watch-onchain.log`.

The tracker state and logs stay local. `.local/`, `.var/`, and `logs/` are gitignored.

## Trigger logic

A refresh is triggered when any of these are new or changed:

- `AuctionBid` log ID, bidder, amount, or token ID, including same-token higher bids,
- `AuctionCreated` log or current token ID,
- `AuctionSettled` log or current settled flag,
- `AuctionExtended` log or end time,
- contract-read current auction token, bidder, amount, or settled state differs from tracker state,
- optional force interval via `MISSION3_WATCHER_FORCE_REFRESH_AFTER_SECONDS`.

On first run with no state, the tracker initializes a baseline from latest onchain state and `generated/current_auction.csv`. It does not force a full refresh unless `--force-refresh` / `npm run watch:onchain:force` is used, or the detected contract state already differs from the dashboard baseline.

## Cooldown and anti-spam

Defaults:

```bash
MISSION3_WATCHER_INTERVAL_SECONDS=120
MISSION3_WATCHER_COOLDOWN_SECONDS=180
MISSION3_WATCHER_BID_COOLDOWN_SECONDS=60
MISSION3_WATCHER_FORCE_REFRESH_AFTER_SECONDS=3600
MISSION3_WATCHER_LOOKBACK_BLOCKS=2000
MISSION3_WATCHER_SAFETY_OVERLAP_BLOCKS=50
MISSION3_WATCHER_LOG_CHUNK=2000
MISSION3_REFRESH_LOCK_PATH=~/Library/Caches/degen-dogs-mission3/refresh.lock
```

Rules:

- One-shot runs take a non-blocking watcher lock at `.local/mission3_onchain_tracker.lock` so watcher checks do not overlap.
- Refresh commands take the shared `refresh.lock` used by `scripts/refresh_and_publish.sh`, so hourly and event-triggered refreshes cannot run at the same time.
- New auctions, settlements, and token changes bypass cooldown.
- Same-token high-bid changes use `MISSION3_WATCHER_BID_COOLDOWN_SECONDS` (60s default) instead of the longer general cooldown, so real new bids publish quickly without commit-spamming every repeated signal.
- Bid-only and extension-only changes inside their active cooldown are stored as `pending_refresh` and retried after cooldown.
- Direct `auction()` end-time changes trigger `auction_end_time_changed` even if the `AuctionExtended` log was missed.
- The scan starts from `last_checked_block + 1 - safety_overlap`; duplicate logs are ignored via log IDs.
- Failed refreshes record local state and back off before retrying.
- If publish automation produces no generated diff, the publish script exits without committing.

## Safe refresh command and auto-push

Default behavior is local and safe:

```bash
MISSION3_REFRESH_COMMAND="npm run data && npm run build"
MISSION3_WATCHER_AUTO_PUSH=0
npm run watch:onchain
```

To publish watcher-triggered refreshes, opt in explicitly:

```bash
MISSION3_WATCHER_AUTO_PUSH=1
MISSION3_REFRESH_COMMAND="npm run refresh:publish"
npm run watch:onchain
```

Guardrails:

- Commands that look like they publish (`git push`, `refresh:publish`, `refresh:archive`, `refresh_and_publish`) are refused unless `MISSION3_WATCHER_AUTO_PUSH=1`.
- `MISSION3_REFRESH_LOCK_PATH` defaults to the same `refresh.lock` path as the hourly publish script (`DEGEN_DOGS_LOCK_DIR` or `~/Library/Caches/degen-dogs-mission3/refresh.lock`). If that lock is busy, the watcher marks the refresh pending instead of starting a second run.
- `MISSION3_WATCHER_REQUIRE_CLEAN_TREE=1` is enabled by default when auto-push is enabled.
- The publish script still owns `git pull --ff-only`, expected-path staging, secret scanning, and no-diff/no-commit behavior.

## One-shot, dry-run, and loop mode

```bash
npm run watch:onchain
python3 scripts/watch_mission3_onchain_activity.py --once
```

Dry-run:

```bash
npm run watch:onchain:dry
python3 scripts/watch_mission3_onchain_activity.py --once --dry-run
```

Loop mode:

```bash
npm run watch:onchain:loop
```

Prefer scheduler-driven one-shot mode for launchd/cron so crashes do not leave a silent long-running process.

## macOS launchd watcher setup

Install the hourly fallback first:

```bash
npm run refresh:install
```

Install the event watcher in safe local-only mode:

```bash
npm run watch:install
```

Install the event watcher in publish mode on the Mac mini/local runner:

```bash
MISSION3_WATCHER_AUTO_PUSH=1 \
MISSION3_REFRESH_COMMAND="npm run refresh:publish" \
npm run watch:install
```

Both LaunchAgents share `refresh.lock`, so an hourly refresh and an event-triggered refresh cannot run at the same time. The watcher LaunchAgent runs `--once` every `MISSION3_WATCHER_INTERVAL_SECONDS` seconds; this is preferred over a long-running launchd loop because failures are visible in launchd logs.

Useful status checks:

```bash
launchctl print gui/$(id -u)/com.ael.degendogs.mission3.refresh
launchctl print gui/$(id -u)/com.ael.degendogs.mission3.watch-auction
tail -n 80 ~/Library/Logs/degen-dogs-mission3/refresh.log
tail -n 80 ~/Library/Logs/degen-dogs-mission3/watch-onchain.log
```

Do not commit machine-specific plist files, private RPC URLs, logs, or local state.

## Cron watcher example

```cron
*/2 * * * * cd /path/to/Degen-Dogs-Mission-3 && npm run watch:onchain >> logs/watch-onchain.log 2>&1
```

## Inspecting local state

```bash
python3 -m json.tool .local/mission3_onchain_tracker_state.json
```

Check:

- `last_checked_block` advances,
- `last_seen_bid_tx` / `last_seen_bid_log_index` match the newest bid,
- `last_seen_amount_wei` and `last_seen_high_bidder` match `generated/current_auction.json[0]`,
- `last_refresh_status` is `success` after a triggered refresh,
- `pending_refresh` clears after cooldown,
- `consecutive_rpc_failures` and `consecutive_refresh_failures` stay low.

Safely reset watcher state if it gets wedged or after moving runners:

```bash
mv .local/mission3_onchain_tracker_state.json .local/mission3_onchain_tracker_state.$(date -u +%Y%m%dT%H%M%SZ).bak
npm run watch:onchain:dry
```

Check whether the latest bid has been published:

```bash
python3 - <<'PY'
import json
from pathlib import Path
current = json.loads(Path('generated/current_auction.json').read_text())[0]
feed = json.loads(Path('generated/auction_feed.json').read_text())[0]
print('current:', current['token_id'], current['current_bid_eth'], current['bidder'], current['bidder_wallet'], current['latest_block'])
print('feed:', feed['dog'], feed['amount_eth'], feed['bidder_winner'], feed['bidder_winner_wallet'])
PY
```

## Failure handling

- RPC/log failures write `last_error` and exit non-zero in one-shot mode.
- Missing state initializes from current onchain/generated data.
- Dirty tracked worktrees are refused in auto-push mode.
- Refresh failures record exit code and backoff; the hourly runner remains the fallback.
- Keep browser-side chain polling out of the static site.
