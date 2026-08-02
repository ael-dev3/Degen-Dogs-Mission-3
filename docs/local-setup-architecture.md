# Local setup architecture

The public Degen Dogs Mission 3 dashboard is static. The local setup is the private
runner layer that keeps the checked-in CSV/JSON/HTML snapshot fresh and then lets
GitHub Pages serve it.

## System diagram

```mermaid
flowchart LR
  base[Base RPC\nauction logs + auction() calls] --> data[Mac mini / local runner\nscripts/build_dashboard.py]
  data --> sqlite[in-memory SQLite\napproved tables]
  sqlite --> sql[sql/mission3_dashboard.sql]
  sql --> artifacts[generated/ + public/generated/\nCSV and JSON]
  artifacts --> render[index.html + README.md\nstatic render]
  render --> publish[scripts/refresh_and_publish.sh\ngit commit + push]
  publish --> actions[GitHub Actions\nnpm ci --ignore-scripts + validate + build]
  actions --> pages[GitHub Pages\nstatic dashboard]

  watcher[launchd watcher\ncom.ael.degendogs.mission3.watch-auction] --> data
  hourly[launchd hourly fallback\ncom.ael.degendogs.mission3.refresh] --> publish
  health[launchd health watchdog\ncom.ael.degendogs.mission3.health] -. repairs/checks .-> hourly
  health -. repairs/checks .-> watcher
```

## Current runner roles

### 1. Hourly refresh fallback

- LaunchAgent label: `com.ael.degendogs.mission3.refresh`
- Installer: `scripts/install_hourly_refresh_launchd.sh`
- NPM helper: `npm run refresh:install`
- Default interval: `3600` seconds
- Main command: `scripts/refresh_and_publish.sh`
- Purpose: run a full public/onchain refresh on a predictable cadence and publish when
  generated artifacts changed.

The publish script owns the reliable refresh sequence:

1. acquire the shared `refresh.lock`,
2. refuse dirty tracked worktrees before refresh, and separately refuse pre-existing
   untracked publish-path files,
3. `git fetch` + `git pull --ff-only`,
4. install npm dependencies if needed and verify the hash-locked Python runtime,
5. run `npm run data`,
6. validate generated artifacts and public status sidecar,
7. run `npm run build`,
8. stage only expected generated/public/static files,
9. commit with the configured prefix when there is a diff,
10. push `main` so Pages deploys.

The installer enables `RunAtLoad`, full reconciliation, live post-push verification,
and bounded jittered retries for fetch, pull, and push. If generation fails before a
commit, the wrapper restores generated publish paths to the pre-run commit so the next
scheduled run starts clean. A bounded watcher-triggered refresh that exits `75` because
it cannot prove an exact incremental update falls back immediately to the full builder.

### 2. Event-aware onchain watcher

- LaunchAgent label: `com.ael.degendogs.mission3.watch-auction`
- Installer: `scripts/install_auction_watcher_launchd.sh`
- NPM helper: `npm run watch:install`
- Default interval: `15` seconds
- Main command: `python3 scripts/watch_mission3_onchain_activity.py --once`
- Publish mode command: `MISSION3_REFRESH_COMMAND="npm run refresh:publish"`
- Publish mode gate: `MISSION3_WATCHER_AUTO_PUSH=1`
- Execution policy: only the exact current/publish commands are accepted and each is executed as a fixed argv without a shell.

The watcher is a one-shot job scheduled every 15 seconds, not a long-running public server.
It checks recent auction-house logs and direct `auction()` state for meaningful changes:

- `AuctionBid`, including same-token higher bids,
- `AuctionCreated`,
- `AuctionExtended`,
- `AuctionSettled`,
- token ID, bidder, bid amount, end time, or settled-state drift from direct contract
  reads.

When it sees a publish-worthy change, it runs the configured refresh command. If a
refresh is blocked by cooldown, a busy lock, dirty tree, RPC failure, or command failure,
the watcher keeps the event pending so the next successful loop can retry. It may advance
`last_observed_*` for operator visibility, but it only advances `last_seen_*` after the
refresh/publish path succeeds.

### 3. Health watchdog

The health watchdog is an independent LaunchAgent, not part of the public site. It
checks that both worker jobs, plists, executable bits, refresh logs, watcher state and
failure counters, and the cache-busted live status sidecar are healthy. It stays silent
when there is nothing to repair.

- Script source: `scripts/degen_dogs_runner_health.py`
- Installer: `scripts/install_runner_health_launchd.sh`
- LaunchAgent label: `com.ael.degendogs.mission3.health`
- Default interval: `300` seconds with `RunAtLoad=true`
- Hermes wrapper on the current Mac mini: `~/.hermes/scripts/degen_dogs_runner_health_alert.sh`
- Expected behavior: no output on healthy dry runs
- Scope: repair local launchd drift, kick stale jobs, and report actionable issues only
- Disk/retention guard: compact each idle runner/watcher/health log above 8 MiB to a
  complete-line 2 MiB tail without changing its inode; active logs defer until idle
  unless they cross the 32 MiB emergency cap. This includes watcher/refresh JSONL
  telemetry. Alert and defer new repair kickstarts below 5 GiB or 5% free space.
- Critical alert path: if no successful refresh crosses the critical stale threshold,
  tracked worktree changes block refresh, launchd drifts/misses, or the live site check
  fails, the watchdog emits a Discord message that tags Ael and creates or comments on
  the GitHub issue `Local runner critical health alert` with sanitized cause
  classification, recent `refresh.log` failure signals, dirty paths, refresh history,
  and watcher history. Repeated failures are deduped by fingerprint and re-alert only
  when the cause changes or the repeat window elapses. Recovery comments close the open
  GitHub issue.

## Shared locks and local-only state

Both launchd jobs use the same refresh lock so hourly and event-triggered refreshes do
not overlap:

```text
~/Library/Caches/degen-dogs-mission3/refresh.lock
```

Local/private files are intentionally not committed:

- launchd plists: `~/Library/LaunchAgents/com.ael.degendogs.mission3.*.plist`
- launchd/operator logs: `~/Library/Logs/degen-dogs-mission3/`
- watcher state: `.local/mission3_onchain_tracker_state.json`
- watcher lock: `.local/mission3_onchain_tracker.lock`
- private refresh telemetry: `.local/refresh_runs.jsonl`
- local watcher telemetry: `.local/watcher_checks.jsonl`
- local health alert state: `~/Library/Caches/degen-dogs-mission3/critical-alert-state.json`
- private `.env` files and credentialed RPC/API URLs

Public-safe freshness is exposed only through checked-in generated artifacts such as:

- `generated/refresh_status.json`
- `public/generated/refresh_status.json`
- `generated/current_auction.json`
- `public/generated/current_auction.json`

## Current macOS installation shape

The current Mac mini runner uses the same repo as the source of truth. On a replacement
machine, set `DEGEN_DOGS_REPO_DIR` to the local clone path before installing launchd.
For the current local runner, that path is:

```text
/Users/marko/projects/Degen-Dogs-Mission-3
```

Publishing watcher install:

```bash
DEGEN_DOGS_REPO_DIR="/Users/marko/projects/Degen-Dogs-Mission-3" \
MISSION3_WATCHER_AUTO_PUSH=1 \
MISSION3_REFRESH_COMMAND="npm run refresh:publish" \
DEGEN_DOGS_KICKSTART=1 \
bash scripts/install_auction_watcher_launchd.sh
```

Hourly fallback install:

```bash
DEGEN_DOGS_REPO_DIR="/Users/marko/projects/Degen-Dogs-Mission-3" \
DEGEN_DOGS_KICKSTART=1 \
bash scripts/install_hourly_refresh_launchd.sh
```

Independent health watchdog install (production watcher repair defaults to publish
mode):

```bash
DEGEN_DOGS_REPO_DIR="/Users/marko/projects/Degen-Dogs-Mission-3" \
MISSION3_WATCHER_AUTO_PUSH=1 \
DEGEN_DOGS_KICKSTART=1 \
bash scripts/install_runner_health_launchd.sh
```

## Verification commands

Use these to prove the local setup is actually installed, not just present in the repo:

```bash
git status --short --branch
npm run test:watcher
npm run validate:dashboard
npm run refresh:status:validate
npm run runner:health:dry
npm run build

launchctl print "gui/$(id -u)/com.ael.degendogs.mission3.refresh"
launchctl print "gui/$(id -u)/com.ael.degendogs.mission3.watch-auction"
launchctl print "gui/$(id -u)/com.ael.degendogs.mission3.health"
plutil -p "$HOME/Library/LaunchAgents/com.ael.degendogs.mission3.refresh.plist"
plutil -p "$HOME/Library/LaunchAgents/com.ael.degendogs.mission3.watch-auction.plist"
plutil -p "$HOME/Library/LaunchAgents/com.ael.degendogs.mission3.health.plist"
```

The manual health commands securely load the protected local runner policy,
then start the watchdog with a clean, least-privilege environment. RPC
endpoints, API credentials, token-price overrides, and Git remotes are never
inherited by the health process.

Use the `npm run runner:health` and `npm run runner:health:dry` entrypoints for
manual checks. They start the wrapper with macOS `/bin/bash` privileged mode so
inherited Bash startup hooks and shell-option variables are ignored before the
protected configuration is opened.

A no-change watcher dry run should exit `0` and print a `no_refresh` line. A healthy
one-shot launchd watcher can show `state = not running` between intervals; check `last
exit code`, run count, logs, and tracker state instead of treating that as failure.

## What is not exposed publicly

- No public Mac mini server
- No visitor-run SQL editor
- No browser wallet bidding or transaction module
- No private RPC/API credentials in generated JSON
- No launchd plist, logs, local state, or private telemetry in git

The public website remains a GitHub Pages static snapshot. The local setup only produces
and publishes the snapshot.
