# Local runner reconstruction

This page explains how to recreate the private/local runners that keep the static
Degen Dogs Mission 3 dashboard fresh. GitHub Pages serves the built site, but it does
not fetch chain data. A local machine must regenerate cached CSV/JSON/HTML files and
push the resulting commit.

Use this with [`docs/refresh-runner.md`](../docs/refresh-runner.md) when moving the
runner to a new machine, repairing launchd, or asking a future agent to rebuild the
setup.

## Runner model

There are two local runners. They are intentionally separate:

1. **Hourly refresh fallback**
   - launchd label: `com.ael.degendogs.mission3.refresh`
   - installer: `scripts/install_hourly_refresh_launchd.sh`
   - npm command: `npm run refresh:install`
   - interval: `DEGEN_DOGS_REFRESH_INTERVAL_SECONDS`, default `3600`
   - action: runs `scripts/refresh_and_publish.sh`
   - purpose: always refresh from public/onchain data on a predictable cadence and
     publish generated artifacts when anything changed.

2. **Event-aware auction watcher**
   - launchd label: `com.ael.degendogs.mission3.watch-auction`
   - installer: `scripts/install_auction_watcher_launchd.sh`
   - npm command: `npm run watch:install`
   - interval: `MISSION3_WATCHER_INTERVAL_SECONDS`, default `60`
   - action: runs `python3 scripts/watch_mission3_onchain_activity.py --once`
   - purpose: cheaply scan Base auction activity and trigger the refresh/publish flow
     soon after new bids or auction-state changes.

The watcher checks both logs and direct contract state from the verified Base auction
house. It reacts to `AuctionBid`, `AuctionCreated`, `AuctionExtended`, and
`AuctionSettled` logs, plus direct changes in `auction()` token ID, bidder, bid amount,
settled flag, and end time.

## Local-only paths

Do not commit these machine-specific paths:

- launchd plists: `$HOME/Library/LaunchAgents/com.ael.degendogs.mission3.*.plist`
- logs: `$HOME/Library/Logs/degen-dogs-mission3/`
- refresh lock: `$HOME/Library/Caches/degen-dogs-mission3/refresh.lock`
- watcher state: `.local/mission3_onchain_tracker_state.json`
- watcher one-shot lock: `.local/mission3_onchain_tracker.lock`
- private `.env` files or credentialed RPC URLs

Both runners share the same `refresh.lock`, so the hourly refresh and event-triggered
refresh cannot overlap.

## Reconstruct on macOS

Start from a clean clone or the canonical local worktree:

```bash
git clone https://github.com/ael-dev3/Degen-Dogs-Mission-3.git
cd Degen-Dogs-Mission-3
npm ci
npm run build
npm run test:watcher
```

Install the hourly fallback:

```bash
DEGEN_DOGS_REPO_DIR="$(pwd)" \
DEGEN_DOGS_KICKSTART=1 \
bash scripts/install_hourly_refresh_launchd.sh
```

Install the event watcher in publish mode:

```bash
DEGEN_DOGS_REPO_DIR="$(pwd)" \
MISSION3_WATCHER_AUTO_PUSH=1 \
MISSION3_REFRESH_COMMAND="npm run refresh:publish" \
DEGEN_DOGS_KICKSTART=1 \
bash scripts/install_auction_watcher_launchd.sh
```

Use safe local-only mode instead if the new machine should test without pushing:

```bash
DEGEN_DOGS_REPO_DIR="$(pwd)" \
MISSION3_WATCHER_AUTO_PUSH=0 \
MISSION3_REFRESH_COMMAND="npm run data && npm run build" \
DEGEN_DOGS_KICKSTART=1 \
bash scripts/install_auction_watcher_launchd.sh
```

## Verify launchd state

```bash
launchctl print "gui/$(id -u)/com.ael.degendogs.mission3.refresh"
launchctl print "gui/$(id -u)/com.ael.degendogs.mission3.watch-auction"

plutil -p "$HOME/Library/LaunchAgents/com.ael.degendogs.mission3.refresh.plist"
plutil -p "$HOME/Library/LaunchAgents/com.ael.degendogs.mission3.watch-auction.plist"
```

Expected watcher configuration:

- `ProgramArguments` ends with `watch_mission3_onchain_activity.py --once`
- `WorkingDirectory` is the absolute repo path
- `StartInterval` is `60` unless intentionally overridden
- `MISSION3_WATCHER_AUTO_PUSH=1` for publishing mode
- `MISSION3_REFRESH_COMMAND=npm run refresh:publish` for publishing mode
- `MISSION3_REFRESH_LOCK_PATH` points at `$HOME/Library/Caches/degen-dogs-mission3/refresh.lock`

A healthy one-shot job often shows `state = not running` between intervals. That is
normal. Check `last exit code = 0`, `runs`, and the logs.

## Verify logs and state

```bash
tail -n 80 "$HOME/Library/Logs/degen-dogs-mission3/refresh.log"
tail -n 80 "$HOME/Library/Logs/degen-dogs-mission3/watch-onchain.log"
tail -n 80 "$HOME/Library/Logs/degen-dogs-mission3/watcher.launchd.out.log"
tail -n 80 "$HOME/Library/Logs/degen-dogs-mission3/watcher.launchd.err.log"
python3 -m json.tool .local/mission3_onchain_tracker_state.json
```

Healthy no-change watcher output looks like:

```text
no_refresh; block=<block> token=<token_id> bidder=<wallet> amount_wei=<wei> logs=0 reasons=none
```

A real new bid should produce refresh reasons such as `auction_bid`,
`highest_bidder_changed`, or `highest_bid_amount_changed`, then run the configured
refresh command.

## Local validation commands

Run these before calling the runner healthy:

```bash
npm run validate:dashboard
npm run test:watcher
npm run check:dashboard-ui
npm run check:historical-dogs
npm run archive:mission3:health
npm run archive:prices:validate
npm run build
python3 -m py_compile scripts/build_dashboard.py scripts/watch_mission3_auction.py scripts/validate_dashboard_consistency.py
bash -n scripts/refresh_and_publish.sh scripts/install_hourly_refresh_launchd.sh scripts/install_auction_watcher_launchd.sh
```

Run a dry watcher check with temporary state:

```bash
MISSION3_WATCHER_STATE_PATH=/tmp/degendogs-watcher-dry-state.json \
MISSION3_WATCHER_LOG_PATH=- \
python3 scripts/watch_mission3_onchain_activity.py --once --dry-run
rm -f /tmp/degendogs-watcher-dry-state.json
```

## Repair checklist

1. Confirm the repo worktree is clean: `git status --short --branch`.
2. Confirm plain `git fetch`/`git push` authentication to `ael-dev3/Degen-Dogs-Mission-3`
   if publish mode is enabled.
3. Confirm both LaunchAgents point at the same repo path.
4. Confirm both LaunchAgents share the same `DEGEN_DOGS_LOG_DIR` and
   `DEGEN_DOGS_LOCK_DIR`.
5. Confirm watcher state advances: `last_checked_at_utc` and `last_checked_block`.
6. Confirm `last_error`, `consecutive_rpc_failures`, and
   `consecutive_refresh_failures` are empty or low.
7. If state is stale or corrupt, back it up instead of deleting blindly:

```bash
mkdir -p .local
mv .local/mission3_onchain_tracker_state.json \
  .local/mission3_onchain_tracker_state.$(date -u +%Y%m%dT%H%M%SZ).bak
npm run watch:onchain:dry
```

8. Reinstall the affected LaunchAgent and kickstart it.

## Agentic prompt: reconstruct runners on a new macOS machine

```text
You are reconstructing the Degen Dogs Mission 3 local refresh runners on macOS.

Work in a clean clone of https://github.com/ael-dev3/Degen-Dogs-Mission-3. Do not
redesign the dashboard. Do not commit logs, plists, local state, private RPC URLs,
API keys, or secrets.

Tasks:
1. Inspect README.md, docs/refresh-runner.md, reconstruction/LOCAL_RUNNERS.md,
   package.json, scripts/refresh_and_publish.sh, scripts/install_hourly_refresh_launchd.sh,
   scripts/install_auction_watcher_launchd.sh, and scripts/watch_mission3_auction.py.
2. Run npm ci, npm run build, and npm run test:watcher.
3. Install the hourly runner without kickstarting it: DEGEN_DOGS_REPO_DIR="$(pwd)"
   bash scripts/install_hourly_refresh_launchd.sh.
4. Install the event watcher without kickstarting it. Default to local-only mode unless the
   human explicitly wants this machine to publish: DEGEN_DOGS_REPO_DIR="$(pwd)"
   MISSION3_WATCHER_AUTO_PUSH=0 MISSION3_REFRESH_COMMAND="npm run data && npm run build"
   bash scripts/install_auction_watcher_launchd.sh.
5. If publish mode is explicitly approved, reinstall the watcher with
   MISSION3_WATCHER_AUTO_PUSH=1 and MISSION3_REFRESH_COMMAND="npm run refresh:publish".
6. Verify launchctl state, plists, logs, shared refresh.lock, and watcher state.
7. Run a temp-state watcher dry-run and confirm no secrets are written.
8. If you need to smoke-test launchd without pushing, reinstall with DEGEN_DOGS_SKIP_PUSH=1
   before kickstarting.
9. Report exact commands run, launchd labels, intervals, log paths, state path, and
   any blocker. Do not push unless explicitly asked.
```

## Agentic prompt: audit and repair existing runners

```text
You are auditing existing Degen Dogs Mission 3 local runners for bugs.

Use the current repo worktree. Do not modify public dashboard behavior unless a real
runner bug requires it. Do not expose or commit secrets.

Check:
1. git status --short --branch and current commit.
2. npm script entries for refresh:install, refresh:publish, watch:install,
   watch:onchain, watch:onchain:dry, and test:watcher.
3. launchctl print for com.ael.degendogs.mission3.refresh and
   com.ael.degendogs.mission3.watch-auction.
4. plutil -p for both LaunchAgent plists.
5. Logs under $HOME/Library/Logs/degen-dogs-mission3/.
6. .local/mission3_onchain_tracker_state.json for advancing block/time, current token,
   current bidder, bid amount, pending refresh, and errors.
7. Shared refresh lock path under $HOME/Library/Caches/degen-dogs-mission3/refresh.lock.
8. Run npm run test:watcher and a temp-state watcher dry-run.

If the watcher is missing, points at the wrong repo, lacks auto-push when publishing is
intended, logs to the wrong place, or does not share refresh.lock, fix the scripts or
reinstall launchd. Rerun tests and report the exact evidence.
```

## Agentic prompt: diagnose missed new-bid refresh

```text
A Degen Dogs Mission 3 auction received a new bid, but the public dashboard did not
refresh quickly. Diagnose without fabricating data.

Steps:
1. Compare live generated/current_auction.json, generated/auction_feed.json, and the
   onchain auction() state at the same latest block if possible.
2. Inspect .local/mission3_onchain_tracker_state.json for last_checked_block,
   last_seen_bid_tx, last_seen_high_bidder, last_seen_amount_wei, pending_refresh,
   next_allowed_refresh_after_utc, and last_error.
3. Inspect $HOME/Library/Logs/degen-dogs-mission3/watch-onchain.log and
   watcher.launchd.*.log for the bid window.
4. Verify launchctl state and StartInterval for com.ael.degendogs.mission3.watch-auction.
5. Verify MISSION3_WATCHER_AUTO_PUSH=1 and MISSION3_REFRESH_COMMAND=npm run refresh:publish
   if the watcher is expected to publish.
6. Verify refresh.lock was not busy. If it was busy, confirm pending_refresh is set and
   replayed after cooldown.
7. Run npm run test:watcher and a temp-state dry-run.
8. Fix only the root cause, rerun validation, commit with [verified] if code/docs changed,
   push, watch CI/Pages, and live-verify with a cache-busting URL.
```
