# Reconstruction runbook

## Normal refresh

```bash
npm run data
npm run build
git status --short
```

Expected generated paths include `README.md`, `index.html`, `generated/`,
`public/generated/`, and unified archive search outputs.

## Rebuild from scratch

Use a clean clone if possible. If you need to remove generated outputs locally:

```bash
rm -rf generated public/generated dist
npm ci
npm run data
npm run build
```

Only do this when you are ready to regenerate everything. If unsure, create a fresh
clone instead.

## Restore generated outputs from Git

```bash
git checkout -- generated public/generated index.html README.md
```

Add archive generated paths if those were also damaged:

```bash
git checkout -- archive/data/generated archive/dogs/by-id
```

## Recover from failed data run

1. Check RPC availability.
2. Set `BASE_RPC_URL` to a reliable endpoint.
3. Lower `BASE_LOG_CHUNK`.
4. Reduce `BASE_LOG_WORKERS`.
5. Rerun `npm run data`.
6. Compare `generated/mission3_metrics.csv` latest block/time.
7. Do not commit partial or corrupted output.

## Recover from broken README generation

1. Edit `README.template.md`, not only `README.md`.
2. If placeholders changed, update `scripts/build_dashboard.py`.
3. Rerun `npm run data`.
4. Confirm `README.md` is concise and generated correctly.

## Recover from broken GitHub Pages deploy

1. Open the Actions log for `Deploy GitHub Pages`.
2. Run `npm run build` locally.
3. Confirm `dist/` exists.
4. Confirm Pages is enabled for GitHub Actions.
5. Confirm generated data was committed before push if freshness matters.

## Recreate hourly runner

macOS:

```bash
npm run refresh:install
```

Manual run:

```bash
npm run refresh:publish
```

Linux cron example:

```cron
0 * * * * cd /path/to/Degen-Dogs-Mission-3 && npm run refresh:publish
```

## Event-aware onchain tracker

For exact macOS launchd reconstruction commands and future-agent prompts, also see
[`LOCAL_RUNNERS.md`](LOCAL_RUNNERS.md).

The hourly runner remains the reliability baseline. The onchain tracker is an additional cheap check that can refresh faster when Mission 3 auction activity changes.

One-shot check:

```bash
npm run watch:onchain
```

Dry-run without refreshing or writing watcher state:

```bash
npm run watch:onchain:dry
```

Schedule it every two minutes, separately from the hourly full refresh:

```cron
*/2 * * * * cd /path/to/Degen-Dogs-Mission-3 && npm run watch:onchain >> logs/watch-onchain.log 2>&1
```

For macOS launchd, use `StartInterval=120` and run `cd /path/to/Degen-Dogs-Mission-3 && npm run watch:onchain` through `/bin/bash -lc`.

Operational state lives at `.local/mission3_onchain_tracker_state.json`, the one-shot overlap lock lives at `.local/mission3_onchain_tracker.lock`, and logs go to `logs/watch-onchain.log`; all are local-only and gitignored.

Triggers:

1. New `AuctionBid` log or changed current bidder/amount.
2. New `AuctionCreated` log or changed current Dog token ID.
3. New `AuctionSettled` log or changed settled flag.
4. New `AuctionExtended` log or changed end time.

Cooldown defaults to 180 seconds. New auctions, settlements, and token changes bypass cooldown; bid-only or extension-only churn inside cooldown becomes a pending refresh and is retried after cooldown.

Default mode is local-only (`npm run data && npm run build`). To publish from watcher-triggered refreshes, set both:

```bash
MISSION3_WATCHER_AUTO_PUSH=1
MISSION3_REFRESH_COMMAND="npm run refresh:publish"
```

If the watcher looks stuck, inspect:

```bash
python3 -m json.tool .local/mission3_onchain_tracker_state.json
```

Look for stale `last_checked_at_utc`, stale `last_checked_block`, repeated `consecutive_rpc_failures`, repeated `consecutive_refresh_failures`, or a future `next_allowed_refresh_after_utc` from backoff.

Keep recovery simple. Use the existing scripts before adding new infrastructure.
