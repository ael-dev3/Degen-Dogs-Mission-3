# Troubleshooting

## RPC timeout

Symptom: `npm run data` fails during Base calls or log fetching.

Likely cause: public RPC rate limits or network instability.

Fix:

- Set a reliable `BASE_RPC_URL`.
- Lower `BASE_LOG_CHUNK`.
- Reduce `BASE_LOG_WORKERS`.
- Rerun `npm run data`.

Validate: `generated/mission3_metrics.csv` updates and `npm run build` passes.

## `eth_getLogs` range too large

Symptom: RPC returns range/rate-limit errors.

Fix: lower `BASE_LOG_CHUNK`, for example `5000` or `2000`.

## Missing Python module

Symptom: Python import error.

Fix: use the repo's documented Python/runtime environment. Current scripts are mostly
standard-library Python; if a new dependency is added, document and pin it.

## Node/Vite build failure

Symptom: `npm run build` fails.

Fix:

- Run `npm ci`.
- Confirm `index.html` exists.
- Confirm generated public files exist.
- Inspect the Vite error.

## README overwritten

Symptom: README edits disappear after `npm run data`.

Fix: edit `README.template.md` and any placeholder logic in
`scripts/build_dashboard.py`.

## Generated files stale

Symptom: Pages deploy passes but live data is old.

Cause: Pages workflow does not run `npm run data`.

Fix: run `npm run data`, commit generated changes, push, then wait for Pages deploy.

## GitHub Pages deploy failed

Fix:

- Check Actions logs.
- Run `npm run build` locally.
- Confirm `dist/` exists.
- Confirm GitHub Pages is enabled for GitHub Actions.

## Farcaster profile resolution missing

Symptom: wallet addresses show instead of Farcaster handles.

Likely cause: no optional identity API key or resolution failure.

Fix: provide optional identity API credentials locally or accept wallet fallback output.

## Current auction read failed

Symptom: current Dog/bid fields are blank or stale.

Fix: verify Base RPC, auction-house address, and current contract call responses.

## Onchain watcher not refreshing

Symptom: `npm run watch:onchain` runs, but the dashboard remains stale after a real bid.

Fix:

- Inspect `.local/mission3_onchain_tracker_state.json`.
- Check `last_checked_block` and `last_observed_bid_tx` advanced.
- Compare `last_observed_amount_wei` / `last_observed_high_bidder` with `generated/current_auction.json[0]`. A mismatch means the watcher saw a newer onchain state than the generated dashboard.
- Check `last_seen_bid_tx`; this is the acknowledged refreshed/published cursor and should not advance until the refresh command succeeds.
- Check `pending_refresh`, `pending_bid_log_id`, `pending_amount_wei`, `pending_high_bidder`, and `next_allowed_refresh_after_utc`; cooldown/backoff may be active.
- Run `npm run watch:onchain:dry` to see detected reasons without writing state.
- Run `npm run validate:dashboard` after a local refresh to catch top-card/feed mismatches and stale generated/public/rendered surfaces versus watcher-observed onchain state.
- If RPC failed, set a reliable `BASE_RPC_URL` or lower `MISSION3_WATCHER_LOG_CHUNK`.

Root-cause pattern to avoid: a same-token `AuctionBid` was detected, the publish command failed or was refused by dirty tracked files, and the watcher advanced its dedupe cursor anyway. The watcher must keep the event pending and retryable until refresh/publish succeeds.

## Onchain watcher refresh refused

Symptom: watcher logs mention auto-push or dirty worktree refusal.

Fix:

- Default `MISSION3_REFRESH_COMMAND` should be `npm run data && npm run build`.
- Publish commands require `MISSION3_WATCHER_AUTO_PUSH=1`.
- In publish mode, clear tracked worktree changes or set `MISSION3_WATCHER_REQUIRE_CLEAN_TREE=0` only for intentional local operation.
- After a refusal, verify the pending event stayed in `.local/mission3_onchain_tracker_state.json`; the next successful watcher run should retry instead of logging `reasons=none`.

## Archive data incomplete

Symptom: unified search misses a mission or row counts are lower than expected.

Fix:

- Check mission-specific archive docs.
- Rebuild relevant mission index.
- Run `npm run archive:unified:index`.
- Run `npm run archive:prices:apply`.

## Dune unavailable

Symptom: Dune discovery/export work cannot continue.

Fix: preserve the missing state in docs and do not fabricate query IDs or SQL. Use
onchain recovery paths where available.
