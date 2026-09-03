# Publication Path Restoration and Hardening Design

**Status:** Approved for implementation under the operator's standing instruction to choose and deploy the fastest reliable design.

## Objective

Restore publication of the preserved Mission 3 auction observation without weakening any correctness gate, then remove the two deterministic failure modes that caused the Windows/WSL producer to remain stale.

The immediate incident has two independent causes:

1. `auction_bidder_leaderboard` is documented and emitted as a top-100 table, while the bounded updater consumes it as a complete all-time per-wallet baseline and the validator requires the current high bidder to be present. A valid bidder below rank 100 therefore forces a full rebuild and the rebuilt candidate is then rejected.
2. The archive requests a fixed `eth_getLogs` span. The live three-provider pool has two or three matching witnesses at spans of 50 blocks or less, but one provider explicitly rejects a 57-block four-topic request. Mixed one-vote/range-limit/transient failures are flattened into a generic quorum failure, so the archive never discovers the smaller span that can satisfy quorum.

## Safety invariants

- Keep the independent RPC quorum at two or greater for every accepted result.
- Never publish from a single RPC response.
- Never delete, rewrite, skip, or synthesize the queued observation, publication journal, archive database, or quarantine evidence to make progress.
- Never bypass dashboard consistency validation, Git compare-and-swap publication, commit attribution checks, or Pages validation.
- Never emit provider URLs, credentials, raw provider messages, paths, queries, or tokens in logs, state, tests, or commits.
- Only an explicit `eth_getLogs` range/response-size rejection may cause range subdivision. HTTP 410, HTTP 429, generic `-32602`, transport errors, timeouts, and ordinary quorum disagreement remain fail-closed.
- Each subdivided range independently obtains the configured quorum and is validated against the requested contract, topics, block bounds, and canonical log schema.
- A one-block range that is explicitly rejected fails closed; subdivision is finite.

## Design decisions

### Complete bidder baseline

`auction_bidder_leaderboard` becomes a complete, deterministically ranked per-wallet aggregate. Remove both the SQL `LIMIT 100` and the bounded updater's `[:100]` truncation. Keep the current-high-bidder presence and freshness validator unchanged.

There are currently 150 distinct bidders. The complete JSON is expected to be about 59 KB rather than 39 KB, and this table is not part of the latency-sensitive live snapshot bundle. The change therefore removes repeated full rebuilds for low-ranked existing bidders without materially increasing browser refresh latency or identity-resolution work.

A genuinely first-ever bidder may still require one full rebuild because the previous complete baseline cannot contain that wallet. That fail-closed transition is intentional; after the full rebuild the wallet is part of the complete baseline.

### Adaptive archive log ranges

Add a private `RpcLogRangeLimit` signal and a strict message classifier equivalent to the existing dashboard/watcher implementation. Provider text is inspected only in memory; externally visible errors use fixed strings.

The archive quorum collector counts typed range-limit outcomes separately. It returns normally whenever a unique quorum exists. It raises `RpcLogRangeLimit` only when at least one typed range rejection could join the strongest completed vote to satisfy the configured quorum. Other degraded mixtures stay generic failures so subdivision cannot amplify an outage.

When a log range receives `RpcLogRangeLimit`, bisect it and quorum-fetch both non-overlapping children. Preserve deterministic ordering and per-log redacted quorum provenance. Reaching one block terminates with the typed failure.

The immediate local runtime is also pinned to 50-block archive and dashboard log requests, because that bound was directly proven against the current three independent providers. This operational setting restores the queue before the source rollout; adaptive source behavior removes reliance on that exact provider-specific value later.

## Deployment order

1. Apply the proven local 50-block bound and allow the existing publisher to retry the preserved queue.
2. Implement and test the complete bidder baseline.
3. Implement and test adaptive archive range subdivision.
4. Review the combined branch, run focused and full verification, and push non-forcing to `main` only if its expected parent still matches.
5. Verify the local queue reaches zero lag, the GitHub source commit contains the exact queued auction tuple, Pages deploys that commit, CI is green, and the public bundle matches source.
6. Only after publication is healthy, continue the separately approved independent Windows audit/recovery plan in `docs/superpowers/plans/2026-09-02-independent-runner-watchdog.md`.

## Acceptance criteria

- A current bidder below the former rank-100 boundary is retained and updated by the bounded path.
- The full SQL build emits every distinct bidder in deterministic rank order.
- The validator still rejects a missing or stale current bidder row.
- An explicit range rejection causes finite bisection and every child has two matching independent votes.
- Generic errors and rate limits do not cause range bisection or single-provider acceptance.
- Secrets and provider-controlled error text do not appear in diagnostics.
- The preserved Dog 822 generation is published normally; no manual artifact copy or validation bypass is used.
