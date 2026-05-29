# Refresh runner

The public site is served by GitHub Pages, but fresh data comes from a private/local
runner that regenerates static files and pushes commits.

## Available commands

```bash
npm run refresh:local
npm run refresh:publish
npm run refresh:archive
npm run refresh:install
npm run watch:auction
npm run watch:auction:loop
```

- `refresh:local` runs `npm run data && npm run build` without committing or pushing.
- `refresh:publish` runs `scripts/refresh_and_publish.sh`.
- `refresh:archive` runs Mission 3 archive indexing first, then the normal publish flow.
- `refresh:install` installs the macOS launchd hourly runner.
- `watch:auction` runs the event-aware Mission 3 auction watcher once.
- `watch:auction:loop` keeps the watcher running and sleeps between checks.

## Publish flow

`scripts/refresh_and_publish.sh`:

1. Takes a local lock to avoid overlapping runs.
2. Refuses to overwrite tracked/untracked publish-path changes.
3. Pulls latest `main` unless disabled.
4. Installs npm dependencies if needed.
5. Optionally runs Mission 3 archive incremental indexing.
6. Runs `npm run data`.
7. Validates generated artifacts.
8. Runs `npm run build`.
9. Stages only expected generated publish paths.
10. Scans staged generated artifacts for common secret patterns.
11. Commits and pushes unless configured to skip push.

## GitHub Pages behavior

The Pages workflow runs `npm ci` and `npm run build`. It does not run `npm run data`;
the runner must commit fresh generated data before pushing if the live dashboard should
update.

## Recreating hourly refresh

macOS launchd:

```bash
npm run refresh:install
```

Linux cron example:

```cron
0 * * * * cd /path/to/Degen-Dogs-Mission-3 && npm run refresh:publish
```

Linux systemd timers are also fine. Keep the service simple: run the existing publish
script, capture logs, and alert on non-zero exit.

## Event-aware refresh watcher

The watcher complements the hourly refresh. Keep the hourly `refresh:publish`
LaunchAgent or cron job as the guaranteed consistency baseline, then run `npm run
watch:auction` every 2-5 minutes for faster updates when auction activity changes.

`scripts/watch_mission3_auction.py` performs a cheap Base check:

1. Reads the auction-house `auction()` state at the latest block.
2. Scans only recent auction-house logs for `AuctionCreated`, `AuctionBid`, and
   `AuctionSettled`.
3. Compares the result with local watcher state at `.local/mission3_watcher_state.json`.
4. If watcher state is missing, uses `generated/current_auction.csv` as the initial
   baseline so already-stale cached bids can refresh without treating old recent logs as
   new activity.
5. Triggers the configured refresh command only when meaningful activity changed.
6. Writes concise logs to `logs/watch-auction.log`.

The watcher stores only local operational state. `.local/`, `.var/`, and `logs/` are
gitignored. A non-blocking lock at `.local/mission3_watcher.lock` prevents overlapping
one-shot runs from stacking refresh commands.

### Refresh triggers

Required triggers are implemented:

- new `AuctionCreated` log,
- new `AuctionSettled` log,
- current auction token ID changed,
- current highest bidder changed,
- current highest bid amount changed.

Bid changes are detected from the on-chain `auction()` state, not from the hosted cached
data.

### Cooldowns and anti-spam

Default safeguards:

- `MISSION3_WATCHER_INTERVAL_SECONDS=300` controls loop-mode sleep.
- `MISSION3_WATCHER_COOLDOWN_SECONDS=300` avoids repeated full refreshes for rapid bid
  churn.
- New auctions, settlements, and token changes bypass cooldown because they should
  publish promptly.
- Bid-only changes inside cooldown are preserved as `pending_refresh` in local state and
  refresh after cooldown expires.
- `MISSION3_WATCHER_FORCE_REFRESH_AFTER_SECONDS=0` by default because the hourly runner
  is the baseline. Set it to `3600` only if the watcher should also act as an hourly
  fallback.
- `MISSION3_WATCHER_LOG_WINDOW_BLOCKS=2000` keeps event scans bounded when local state
  is missing or stale.
- Failed refreshes record status in local state and back off before retrying.

If a refresh produces no generated git diff, the existing publish script exits without
committing.

### Safe refresh command and auto-push

By default the watcher is local-only:

```bash
MISSION3_REFRESH_COMMAND="npm run refresh:local"
MISSION3_WATCHER_AUTO_PUSH=0
npm run watch:auction
```

To let watcher-triggered refreshes publish to GitHub Pages, opt in explicitly:

```bash
MISSION3_WATCHER_AUTO_PUSH=1
MISSION3_REFRESH_COMMAND="npm run refresh:publish"
npm run watch:auction
```

Guardrails:

- Commands that look like they publish (`git push`, `refresh:publish`, or
  `refresh_and_publish`) are refused unless `MISSION3_WATCHER_AUTO_PUSH=1`.
- `MISSION3_WATCHER_REQUIRE_CLEAN_TREE=1` is recommended for publish mode and defaults
  on when auto-push is enabled.
- The publish script still owns locking, `git pull --ff-only`, generated-path staging,
  secret scanning, and no-diff/no-commit behavior.

## One-shot mode

One-shot mode is preferred for launchd or cron:

```bash
npm run watch:auction
# equivalent:
python3 scripts/watch_mission3_auction.py --once
```

Dry-run detects changes and prints the intended refresh without running it or writing
watcher state:

```bash
python3 scripts/watch_mission3_auction.py --once --dry-run
```

Loop mode is available for supervised long-running processes:

```bash
npm run watch:auction:loop
```

## macOS launchd watcher example

Install the hourly runner with `npm run refresh:install` first. Then add a separate
LaunchAgent for the watcher with a five-minute `StartInterval`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.degendogs.mission3.watch-auction</string>
  <key>StartInterval</key>
  <integer>300</integer>
  <key>WorkingDirectory</key>
  <string>/path/to/Degen-Dogs-Mission-3</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>cd /path/to/Degen-Dogs-Mission-3 && npm run watch:auction</string>
  </array>
  <key>StandardOutPath</key>
  <string>/path/to/Degen-Dogs-Mission-3/logs/watch-auction.launchd.log</string>
  <key>StandardErrorPath</key>
  <string>/path/to/Degen-Dogs-Mission-3/logs/watch-auction.launchd.log</string>
</dict>
</plist>
```

Use your actual repo path in the plist, but do not commit machine-specific paths.

## Cron watcher example

```cron
*/5 * * * * cd /path/to/Degen-Dogs-Mission-3 && npm run watch:auction >> logs/watch-auction.log 2>&1
```

## Health checks

Daily health checks should keep checking the hourly runner and live dashboard freshness.
Add watcher-state checks for:

- `.local/mission3_watcher_state.json` exists after the watcher is enabled,
- no long-running stale `.local/mission3_watcher.lock` owner is active beyond the
  expected refresh runtime,
- `last_checked_at_utc` is recent,
- `last_seen_block` advances over time,
- `consecutive_rpc_failures` and `consecutive_refresh_failures` are zero or below the
  alert threshold,
- `last_refresh_status` is `success` or absent when no watcher-triggered refresh was
  needed.

The state file intentionally stays local; do not publish it to GitHub Pages.

## Safety

- Do not run the publish script with a dirty tracked worktree.
- Do not commit `.env.local`, local logs, `.local/`, `.var/`, or cache paths.
- Prefer lowering RPC concurrency over retrying aggressively when rate-limited.
- Keep browser-side chain polling out of the public static site; the watcher runs only
  on the private/local runner.
