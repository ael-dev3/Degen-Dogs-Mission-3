# Refresh runner

The public site is served by GitHub Pages, but fresh data comes from private/local runners that regenerate static files and push commits. For a fresh-machine reconstruction runbook and future-agent prompts, see [`reconstruction/LOCAL_RUNNERS.md`](../reconstruction/LOCAL_RUNNERS.md).

For the second Windows publisher, use the supported WSL2/systemd design in
[`windows-wsl-runner.md`](windows-wsl-runner.md). Native Windows/Git Bash does
not provide the POSIX lock, descriptor, permission, and process-group semantics
required by the publisher.

## Available commands

```bash
npm run refresh:local
npm run refresh:current
npm run refresh:publish
npm run refresh:archive
npm run refresh:status
npm run refresh:status:validate
npm run refresh:metrics
npm run refresh:install
npm run watch:install
npm run watch:onchain
npm run watch:onchain:loop
npm run watch:onchain:dry
npm run watch:onchain:force
```

- `refresh:local` runs the bounded `npm run refresh:current` without committing or pushing.
- `refresh:current` performs a fast current-surface refresh: it reads the live auction contract and only scans a small recent-block overlap, avoiding a full historical RPC scan when the local cache is missing.
- `refresh:publish` runs `scripts/refresh_and_publish.sh`.
- `refresh:archive` runs Mission 3 archive indexing first, then the normal publish flow.
- `refresh:status` writes the public `generated/refresh_status.json` sidecar and public copy.
- `refresh:status:validate` checks the status sidecar against current generated metrics/auction artifacts.
- `refresh:metrics` prints local operator telemetry from refresh/watch JSONL logs, including pending refresh state and recent durations.
- `refresh:install` installs the macOS launchd hourly runner.
- `watch:install` installs the macOS launchd event watcher runner.
- `watch:onchain` runs the precise Mission 3 onchain activity tracker once.
- `watch:onchain:loop` keeps the tracker running and sleeps between checks.
- `watch:onchain:dry` detects signals and prints intended refreshes without executing the command or writing state.
- `watch:onchain:force` forces the configured refresh command once, useful for first-run bootstrap or manual repair.

The older `watch:auction` scripts remain aliases for compatibility.

The published verification label is intentionally scoped: it certifies the pinned
current snapshot hash, critical contract code, current auction state, Dog supply, and
recent auction logs across independent RPC operators. Historical cached chain data is
reorg-overlapped and cross-table validated; identities, metadata hosting, price feeds,
and reward projections are source-attributed offchain inputs and are not mislabeled as
cross-provider onchain facts.

## Baseline hourly reconcile

Keep the hourly refresh as the safety baseline:

```cron
0 * * * * cd /path/to/Degen-Dogs-Mission-3 && npm run refresh:publish
```

The Pages workflow runs `npm ci --ignore-scripts`, the full validation suite, and
`npm run build`. It does not run `npm run data`; the runner must commit fresh generated
data before pushing if live dashboard data should update.

On the deployed page, a cache-busted `refresh_status.json` check runs every 10 seconds.
When its verified snapshot changes, the browser cross-checks the lightweight current
auction, feed, bid-history, and metrics payloads before updating the current card. The
larger unified archive is fetched and validated in a separate phase, so archive loading
cannot delay the current auction. Browser clients use only same-origin GitHub Pages
artifacts; the off-host watchdog independently compares raw `main` with Pages without
expanding the browser's content trust boundary.

The installed hourly LaunchAgent defaults to `DEGEN_DOGS_FULL_REFRESH=0`, using the
bounded current-surface reconciler so it does not hold the shared lock through an
unnecessary full rebuild. If the bounded refresher returns exit code `75` because its
cached coverage cannot be updated exactly, the publish wrapper immediately falls back
to `npm run data` in the same locked run. Set `DEGEN_DOGS_FULL_REFRESH=1` when installing
to require a full rebuild every hour. Persist intentional hourly policy overrides in
the protected `.env.local` and reinstall the health LaunchAgent as well, so its drift
check uses the same nonsecret policy.

Hourly installs also default `DEGEN_DOGS_RUN_MISSION3_ARCHIVE=1`, keeping the Mission 3
archive inside its three-hour freshness guarantee. The 15-second watcher always pins
both `DEGEN_DOGS_FULL_REFRESH=0` and `DEGEN_DOGS_RUN_MISSION3_ARCHIVE=0`; event-triggered
publication therefore remains low-latency even when the shared `.env.local` enables
hourly archive maintenance. An explicit hourly `DEGEN_DOGS_RUN_MISSION3_ARCHIVE=0`
remains supported and drift-stable, but opts that runner out of the three-hour archive
freshness guarantee.
Both jobs use `RunAtLoad=true`, so a reboot or user login does not wait for the first
timer interval.

An independent GitHub Actions freshness watchdog runs at minutes 7, 22, 37, and 52.
It compares the raw `main` status sidecar with the Pages copy, re-dispatches the Pages
deployment when raw data is newer, opens or updates one deduplicated uptime issue on a
stale/invalid snapshot, and closes that issue after recovery. This is an off-host
backstop for the local Mac runner and Pages propagation; GitHub scheduled workflows
can be delayed or dropped under load, so the five-minute local health LaunchAgent
remains the primary supervisor.

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
7. Writes concise operational logs to `logs/watch-onchain.log` and structured watcher rows to `.local/watcher_checks.jsonl`.

The tracker state and logs stay local. `.local/`, `.var/`, and `logs/` are gitignored. Public refresh freshness is exposed only through sanitized `generated/refresh_status.json` and `public/generated/refresh_status.json`.

## Trigger logic

A refresh is triggered when any of these are new or changed:

- `AuctionBid` log ID, bidder, amount, or token ID, including same-token higher bids,
- `AuctionCreated` log or current token ID,
- `AuctionSettled` log or current settled flag,
- `AuctionExtended` log or end time,
- contract-read current auction token, bidder, amount, or settled state differs from tracker state,
- optional force interval via `MISSION3_WATCHER_FORCE_REFRESH_AFTER_SECONDS` when no hourly fallback is installed.

On first run with no state, the tracker initializes a baseline from latest onchain state and `generated/current_auction.csv`. It does not force a full refresh unless `--force-refresh` / `npm run watch:onchain:force` is used, or the detected contract state already differs from the dashboard baseline.

## Cooldown and anti-spam

Defaults:

```bash
MISSION3_WATCHER_INTERVAL_SECONDS=15
MISSION3_WATCHER_COOLDOWN_SECONDS=30
MISSION3_WATCHER_BID_COOLDOWN_SECONDS=15
MISSION3_WATCHER_FORCE_REFRESH_AFTER_SECONDS=0
MISSION3_WATCHER_LOOKBACK_BLOCKS=100
MISSION3_WATCHER_SAFETY_OVERLAP_BLOCKS=50
MISSION3_WATCHER_LOG_CHUNK=2000
MISSION3_REFRESH_LOCK_PATH=~/Library/Caches/degen-dogs-mission3/refresh.lock
```

Rules:

- One-shot runs take a non-blocking watcher lock at `.local/mission3_onchain_tracker.lock` so watcher checks do not overlap.
- Watcher checks defer before RPC/log scanning while the shared publisher lock is active, avoiding duplicate long-outage catch-up work after a reboot.
- Log catch-up starts with 2,000-block quorum requests and halves the range on provider rejection, preserving fail-closed quorum checks without thousands of fixed 50-block calls.
- Refresh commands take the shared `refresh.lock` used by `scripts/refresh_and_publish.sh`, so hourly and event-triggered refreshes cannot run at the same time.
- New auctions, settlements, and token changes bypass cooldown.
- Same-token high-bid changes use `MISSION3_WATCHER_BID_COOLDOWN_SECONDS` (15s default), so real new bids publish quickly without commit-spamming every repeated signal.
- Time-based force refresh is disabled by default. Keep the hourly LaunchAgent as the baseline and enable `MISSION3_WATCHER_FORCE_REFRESH_AFTER_SECONDS` only if the watcher is the sole runner, otherwise the watcher and hourly runner both perform full API-heavy refreshes.
- Bid-only and extension-only changes inside their active cooldown are stored as `pending_refresh` and retried after cooldown.
- Failed, deferred, or lock-blocked refreshes also stay pending. The watcher may advance `last_checked_*` and `last_observed_*` for operator visibility, but `last_seen_*` is the published/acknowledged cursor and only advances after a successful refresh command (or dry-run acknowledgement). This prevents a same-token bid from being observed once, failing to publish, and then being suppressed as already handled.
- Direct `auction()` end-time changes trigger `auction_end_time_changed` even if the `AuctionExtended` log was missed.
- The scan starts from `last_checked_block + 1 - safety_overlap`; duplicate logs are ignored via log IDs.
- Failed refreshes record local state and back off before retrying.
- If publish automation produces no generated diff, the publish script exits without committing.

## Structured telemetry and refresh status

`scripts/refresh_and_publish.sh` exports phase timestamps and records a redacted JSONL row at the end of every run:

- `.local/refresh_runs.jsonl` for local/private run history,
- `logs/refresh-metrics.jsonl` for operator metrics,
- `.local/watcher_checks.jsonl` for one-shot watcher checks.

The JSONL rows include trigger, reasons, event metadata, lock wait, data/build/push durations, no-diff/skip-push/failure/live-timeout outcomes, and changed-file lists. The helper redacts private paths and provider/API tokens before writing rows. Publish-specific final outcomes live in the private JSONL rows; the public status sidecar reports the generated snapshot and its generation result, so it does not need to expose local runner/push internals.

Fetch, fast-forward pull, and push use four bounded attempts by default with exponential
backoff and jitter. A failure after generation but before commit restores tracked
publish artifacts to the pre-run commit and removes only runner-created untracked
publish artifacts. This prevents one interrupted generation from blocking every later
run. After a push, live Pages verification is enabled by default and a timeout is a
failed run, not a false success. Tune these with
`DEGEN_DOGS_GIT_RETRY_{ATTEMPTS,BASE_SECONDS,MAX_SECONDS,JITTER_SECONDS}` and
`DEGEN_DOGS_LIVE_VERIFY_{AFTER_PUSH,TIMEOUT_SECONDS,INTERVAL_SECONDS}`.

The public sidecar is intentionally small and safe:

```bash
npm run refresh:status
npm run refresh:status:validate
npm run refresh:metrics
```

`generated/refresh_status.json` mirrors the current generated block, Dog, bid, high bidder, auction status, trigger/reason, and last refresh result. It must match `public/generated/refresh_status.json` and is validated as part of `refresh:publish`.

## Safe refresh command and auto-push

Default behavior is local and safe:

```bash
MISSION3_REFRESH_COMMAND="npm run refresh:current"
MISSION3_WATCHER_AUTO_PUSH=0
npm run watch:onchain
```

To publish watcher-triggered refreshes, opt in explicitly:

```bash
MISSION3_WATCHER_AUTO_PUSH=1
MISSION3_REFRESH_COMMAND="npm run refresh:current"
npm run watch:onchain
```

Guardrails:

- `MISSION3_REFRESH_COMMAND` accepts exactly `npm run refresh:current` or `npm run refresh:publish`. It is mapped to a fixed argument vector and executed without a shell; paths, extra arguments, metacharacters, and whitespace variants are rejected by both the watcher and launchd installers.
- `npm run refresh:publish` additionally requires `MISSION3_WATCHER_AUTO_PUSH=1`. No other publish or legacy command form is supported.
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

Create the protected runner configuration once. The three installers source it
automatically and preserve the same RPC configuration during health self-repair:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements.txt
cp .env.example .env.local
chmod 600 .env.local
# Set BASE_RPC_URLS and BASE_LOG_RPC_URLS to at least two independent,
# credentialed, archive-capable Base providers before production use.
```

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

Install the independent health LaunchAgent after both workers:

```bash
MISSION3_WATCHER_AUTO_PUSH=1 \
bash scripts/install_runner_health_launchd.sh
```

Both worker LaunchAgents share `refresh.lock`, so an hourly refresh and an event-triggered refresh cannot run at the same time. The watcher LaunchAgent runs `--once` every `MISSION3_WATCHER_INTERVAL_SECONDS` seconds; this is preferred over a long-running launchd loop because failures are visible in launchd logs. The installed default is 15 seconds. The health LaunchAgent runs every five minutes, repairs plist/service drift, kickstarts stale workers, validates watcher state/failure counters, and checks the live cache-busted refresh-status sidecar.

The health pass also bounds launchd, runner, watcher, and local JSONL telemetry logs.
It preserves launchd file inodes, retains complete newest lines, and checks free bytes
and free percentage on each distinct filesystem. Low disk is critical and suppresses
new repair kickstarts until space is restored.

Useful status checks:

```bash
launchctl print gui/$(id -u)/com.ael.degendogs.mission3.refresh
launchctl print gui/$(id -u)/com.ael.degendogs.mission3.watch-auction
launchctl print gui/$(id -u)/com.ael.degendogs.mission3.health
tail -n 80 ~/Library/Logs/degen-dogs-mission3/refresh.log
tail -n 80 ~/Library/Logs/degen-dogs-mission3/watch-onchain.log
tail -n 80 ~/Library/Logs/degen-dogs-mission3/watcher.launchd.out.log
tail -n 80 ~/Library/Logs/degen-dogs-mission3/watcher.launchd.err.log
tail -n 80 ~/Library/Logs/degen-dogs-mission3/health.launchd.err.log
```

Do not commit machine-specific plist files, private RPC URLs, logs, or local state.

## Cron watcher example

```cron
* * * * * cd /path/to/Degen-Dogs-Mission-3 && npm run watch:onchain >> logs/watch-onchain.log 2>&1
```

## Inspecting local state

```bash
python3 -m json.tool .local/mission3_onchain_tracker_state.json
```

Check:

- `last_checked_block` advances,
- `last_observed_bid_tx` / `last_observed_bid_log_index` match the newest onchain bid,
- `last_seen_bid_tx` / `last_seen_bid_log_index` match the newest bid that was successfully refreshed/published,
- `last_observed_amount_wei` and `last_observed_high_bidder` match `generated/current_auction.json[0]`; if they do not, `pending_refresh` should be set until the retry succeeds,
- `last_refresh_status` is `success` after a triggered refresh,
- `pending_refresh` clears after cooldown,
- `pending_bid_log_id`, `pending_amount_wei`, and `pending_high_bidder` identify the unpublished event while a retry/backoff is active,
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
- Refresh failures record exit code/backoff and preserve the pending event identity; they do not acknowledge `last_seen_*` until the retry path succeeds.
- `npm run validate:dashboard` compares generated/public/rendered current-auction surfaces against `.local/mission3_onchain_tracker_state.json` when that state contains `last_observed_*`, catching consistently stale artifacts after a failed same-token bid refresh.
- Keep browser-side chain polling out of the static site.
