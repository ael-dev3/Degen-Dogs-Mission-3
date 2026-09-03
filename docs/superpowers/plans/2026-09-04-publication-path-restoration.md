# Publication Path Restoration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore and harden the Mission 3 publication path by making the bidder aggregate a complete fast-path baseline and teaching the archive to subdivide only explicitly oversized log requests while preserving independent quorum.

**Architecture:** The generated bidder table becomes the single complete aggregate consumed by both full and bounded builds. The archive gains a typed, secret-safe range-limit signal that can drive finite bisection; all other RPC failures remain fail-closed. Existing queue durability, validation, Git compare-and-swap, and Pages gates remain the sole publication path.

**Tech Stack:** Python 3.12, SQLite SQL, Bash/WSL2 runtime, Node/npm test orchestration, GitHub Pages.

**Spec:** `docs/superpowers/specs/2026-09-04-publication-path-restoration-design.md`

## Global Constraints

- Keep the independent RPC quorum at two or greater for every accepted result.
- Never publish from a single RPC response.
- Never delete, rewrite, skip, or synthesize the queued observation, publication journal, archive database, or quarantine evidence to make progress.
- Never bypass dashboard consistency validation, Git compare-and-swap publication, commit attribution checks, or Pages validation.
- Never emit provider URLs, credentials, raw provider messages, paths, queries, or tokens in logs, state, tests, or commits.
- Only an explicit `eth_getLogs` range/response-size rejection may cause range subdivision.
- Every subdivided child range must independently satisfy quorum and normal canonical-log validation.
- Use strict red-green-refactor: each production behavior is preceded by a regression test observed failing for the intended reason.

---

### Task 1: Make the bidder aggregate a complete fast-path baseline

**Files:**
- Modify: `scripts/test_refresh_current_surface.py`
- Modify: `scripts/test_build_dashboard.py`
- Modify: `sql/mission3_dashboard.sql`
- Modify: `scripts/refresh_current_surface.py`
- Modify: `docs/datasets.md`

**Interfaces:**
- Consumes: `apply_bidder_leaderboard_delta(rows, added, removed, all_known_rows, profiles) -> list[dict[str, Any]]` and the SQL table `auction_bidder_leaderboard`.
- Produces: a complete, deterministically ordered per-wallet leaderboard in the same existing CSV/JSON schema.

- [ ] **Step 1: Add the bounded-updater regression**

Add `test_leaderboard_delta_retains_and_updates_wallet_below_former_top_100_boundary()` to `scripts/test_refresh_current_surface.py`. Construct 101 distinct literal-format wallet rows ordered by descending `bid_eth`, choose the final wallet as the affected bidder, add one canonical bid for that wallet, and assert all of the following observable outcomes:

```python
assert len(output) == 101
updated = next(row for row in output if row["bidder_wallet"] == affected_wallet)
assert updated["bids"] == 2
assert updated["auctions_bid"] == 2
assert updated["bid_eth"] == 0.01000001
assert {row["bidder_wallet"] for row in output} == {row["bidder_wallet"] for row in rows}
```

The mutation this test catches is reinstating the `[:100]` truncation.

- [ ] **Step 2: Run the bounded-updater regression and observe RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -c 'import scripts.test_refresh_current_surface as t; t.test_leaderboard_delta_retains_and_updates_wallet_below_former_top_100_boundary()'
```

Expected: FAIL because the output contains only 100 rows or the affected wallet is absent.

- [ ] **Step 3: Add the SQL regression**

Extend the existing in-memory SQL fixture in `scripts/test_build_dashboard.py` with `test_sql_bidder_leaderboard_contains_every_distinct_bidder()`. Insert 101 distinct bidder rows into `auction_bids`, execute `dashboard.SQL_PATH`, fetch `auction_bidder_leaderboard`, and assert literal counts and boundary membership:

```python
assert len(rows) == 101
assert len({row["bidder_wallet"] for row in rows}) == 101
assert "0x0000000000000000000000000000000000000065" in {
    row["bidder_wallet"] for row in rows
}
```

Use the fixture's real `dashboard.insert_rows` and SQL script; do not test by grepping SQL source text.

- [ ] **Step 4: Run the SQL regression and observe RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -c 'import scripts.test_build_dashboard as t; t.test_sql_bidder_leaderboard_contains_every_distinct_bidder()'
```

Expected: FAIL with 100 rows instead of 101.

- [ ] **Step 5: Remove both truncation points**

In `sql/mission3_dashboard.sql`, preserve the existing ordering but remove `LIMIT 100`:

```sql
ORDER BY b.bid_eth DESC, b.bids DESC, b.bidder;
```

In `scripts/refresh_current_surface.py`, preserve the identical sort and return the complete list:

```python
return output
```

Do not weaken the validator's current-bidder presence or freshness assertions.

- [ ] **Step 6: Update the dataset contract**

Change the `auction_bidder_leaderboard` row-limit description in `docs/datasets.md` from `100` to `all bidders`, leaving the file names and columns unchanged.

- [ ] **Step 7: Run focused GREEN verification**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_refresh_current_surface.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_build_dashboard.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_validate_dashboard_consistency.py
```

Expected: all suites pass and the existing validator still rejects a missing current bidder in its negative fixture.

- [ ] **Step 8: Commit Task 1**

```bash
git add scripts/test_refresh_current_surface.py scripts/test_build_dashboard.py sql/mission3_dashboard.sql docs/datasets.md
git commit -m "fix: retain complete bidder leaderboard baseline"
```

### Task 2: Adaptively subdivide archive log ranges without weakening quorum

**Files:**
- Modify: `scripts/test_watch_mission3_auction.py`
- Modify: `scripts/archive_mission3_index.py`

**Interfaces:**
- Consumes: `rpc_call`, `rpc_consensus`, `fetch_log_range`, `canonical_logs`, and the configured independent RPC provider set.
- Produces: `RpcLogRangeLimit`, `is_explicit_log_range_error(code: int, message: str) -> bool`, and finite child-range quorum fetching with the existing `fetch_log_range` return shape.

- [ ] **Step 1: Add secret-safe classification regressions**

Add `test_archive_classifies_only_explicit_log_range_errors_without_leaking_provider_text()` to `scripts/test_watch_mission3_auction.py`. Replace `archive.post_json` with exact JSON-RPC envelopes. Prove that a negative code plus `"block range is too large secret-token"` raises `archive.RpcLogRangeLimit` with the fixed text `"eth_getLogs provider range/response limit"`, and that neither `secret-token` nor a credential-bearing test URL is present. Prove a generic `-32602` `"invalid parameters secret-token"` remains a normal `RuntimeError` containing only the numeric code.

- [ ] **Step 2: Run the classifier regression and observe RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -c 'import scripts.test_watch_mission3_auction as t; t.test_archive_classifies_only_explicit_log_range_errors_without_leaking_provider_text()'
```

Expected: FAIL because `archive_mission3_index.py` has no `RpcLogRangeLimit` and currently flattens both responses.

- [ ] **Step 3: Add adaptive-range behavioral regressions**

Add these focused tests:

```python
def test_archive_log_range_bisects_only_explicit_range_limit_and_keeps_child_quorum():
    # Stub rpc_consensus: 1..100 raises RpcLogRangeLimit; 1..50 and 51..100
    # each return a canonical result plus two distinct agreeing URLs.
    # Assert calls == [(1, 100), (1, 50), (51, 100)], returned bounds are
    # (1, 100), rows are ordered, and every row keeps redacted quorum provenance.

def test_archive_log_range_does_not_bisect_generic_quorum_failure():
    # Stub rpc_consensus to raise RuntimeError and assert exactly one call.

def test_archive_one_block_range_limit_fails_closed():
    # Stub rpc_consensus to raise RpcLogRangeLimit for 7..7 and assert the
    # typed exception escapes after exactly one call.
```

Use complete canonical log dictionaries in the successful child responses. Assertions target `fetch_log_range` behavior, not mock call existence alone: returned logs, order, bounds, and provenance must all be checked.

- [ ] **Step 4: Run adaptive-range regressions and observe RED**

Run the three new test functions directly with `PYTHONDONTWRITEBYTECODE=1`. Expected: the first fails because subdivision is absent; the generic failure test already passes only after its sibling RED is established; the one-block test fails until the typed exception exists.

- [ ] **Step 5: Implement the typed range-limit boundary**

In `scripts/archive_mission3_index.py`:

```python
class RpcLogRangeLimit(RuntimeError):
    """An explicit eth_getLogs range/response-size limit, safe to retry smaller."""


def is_explicit_log_range_error(code: int, message: str) -> bool:
    if code >= 0:
        return False
    normalized = message.casefold()
    return any(marker in normalized for marker in (
        "block range", "range limit", "range is too", "maximum range",
        "max range", "too many results", "response size",
        "query returned more than", "please limit the query",
    ))
```

Classify HTTP 413 and `eth_getLogs` response-size overflow as the same fixed typed signal. In `rpc_call`, inspect a validated JSON-RPC error message only to call the classifier, then discard it. Never include it in any raised/logged text.

- [ ] **Step 6: Preserve typed failures through quorum**

Make a single-provider `rpc_consensus` worker re-raise `RpcLogRangeLimit` without retries. Count those typed failures in the collector. After exhausting possible successful quorum, raise `RpcLogRangeLimit("eth_getLogs range was rejected by the independent RPC quorum")` only when:

```python
method == "eth_getLogs" and range_limit_errors > 0 and top_votes + range_limit_errors >= required
```

All other outcomes retain the existing generic quorum error and sanitized details.

- [ ] **Step 7: Implement finite child-range quorum fetching**

Wrap the quorum request in `fetch_log_range`. On `RpcLogRangeLimit`, fail immediately when `start >= end`; otherwise split at `midpoint = (start + end) // 2`, call `fetch_log_range` for `[start, midpoint]` and `[midpoint + 1, end]`, concatenate and deterministically sort their already validated logs, and return the original `(start, end)` bounds. Every child call receives the unchanged independent provider set. Preserve each child's existing redacted `__source_rpc` value; the summary source string must contain redacted labels only.

- [ ] **Step 8: Run focused GREEN and confidentiality verification**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -c 'import scripts.test_watch_mission3_auction as t; t.test_archive_classifies_only_explicit_log_range_errors_without_leaking_provider_text(); t.test_archive_log_range_bisects_only_explicit_range_limit_and_keeps_child_quorum(); t.test_archive_log_range_does_not_bisect_generic_quorum_failure(); t.test_archive_one_block_range_limit_fails_closed()'
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_rpc_redaction.py
```

Expected: all focused tests pass and no secret marker appears in output.

- [ ] **Step 9: Run the archive/watcher suite in native Linux as the unprivileged runner**

Use the trusted WSL Python runtime and a native-ext4 checkout owned by `degendogs`, with `PYTHONDONTWRITEBYTECODE=1`:

```bash
/var/lib/degen-dogs/python-runtime/bin/python3 scripts/test_watch_mission3_auction.py
```

Expected: `watcher_tests=pass` with zero failures. Do not run the symlink fixture as root because root ownership changes the test's trust boundary.

- [ ] **Step 10: Commit Task 2**

```bash
git add scripts/test_watch_mission3_auction.py scripts/archive_mission3_index.py
git commit -m "fix: adapt archive RPC ranges to provider limits"
```

### Task 3: Validate the current unified row according to onchain auction state

**Files:**
- Modify: `scripts/test_validate_dashboard_consistency.py`
- Modify: `scripts/validate_dashboard_consistency.py`

**Interfaces:**
- Consumes: `current_state`, `expected_feed_status`, `archive_current_rank`, and each generated/public unified-index mirror.
- Produces: state-aware uniqueness, status, and active-rank validation for the current Mission 3 Dog.

- [ ] **Step 1: Add the ended-unsettled regression**

Add `test_validator_accepts_unique_ended_unsettled_current_dog_without_active_rank()` using the real consistency fixture. Change the current auction state to `ended_unsettled`, the feed/current unified status to `ended pending settlement`, and every mirrored/rendered status field required by the fixture. Assert full `validate_current_surface()` succeeds, the current Dog row is unique, and no Mission 3 row has `archive_current_rank(row) == 1`.

- [ ] **Step 2: Observe RED**

Run the new test directly with `PYTHONDONTWRITEBYTECODE=1`. Expected: fail at the unconditional exactly-one-ongoing assertion.

- [ ] **Step 3: Add negative uniqueness and stale-active regressions**

Add two focused negative tests from the same ended-unsettled fixture:

- duplicate the Mission 3 current-Dog row and require a fixed validation failure;
- add or relabel another Mission 3 row as `ongoing` and require a fixed validation failure because no row may remain active-ranked when the actual current auction is ended.

These tests must validate parsed output behavior, not grep source text.

- [ ] **Step 4: Implement state-aware unified validation**

For each unified mirror, collect Mission 3 rows matching `current_dog_id` and require exactly one. Require that row's normalized public status to equal `expected_feed_status`.

Keep `archive_current_rank()` unchanged. If `current_state == "live"`, retain the existing exactly-one-active-row requirement and require that active row to be the current Dog. If `current_state == "ended_unsettled"`, require zero active-ranked Mission 3 rows. Fail closed on any unsupported current state.

Reuse the already-validated unique current row for the downstream wallet, amount, rarity, and activity assertions instead of performing a second first-match lookup.

- [ ] **Step 5: Run GREEN verification**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_validate_dashboard_consistency.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_build_unified_dog_index.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_build_dashboard.py
```

Expected: all pass; existing live-current fixtures still require exactly one active Dog, and stale historical pending rows remain non-active.

- [ ] **Step 6: Commit Task 3**

```bash
git add scripts/test_validate_dashboard_consistency.py scripts/validate_dashboard_consistency.py
git commit -m "fix: validate ended current auction in unified index"
```

### Task 4: Combined verification and deployment readiness

**Files:**
- Verify only; do not change generated production data in the implementation worktree.

**Interfaces:**
- Consumes: Tasks 1 and 2 as one publication pipeline.
- Produces: reviewable test evidence and a branch ready for the controller's non-forcing deployment.

- [ ] **Step 1: Run all focused suites**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_refresh_current_surface.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_build_dashboard.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_validate_dashboard_consistency.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_rpc_redaction.py
```

- [ ] **Step 2: Run repository checks that do not require live mutation**

```bash
npm run test:dashboard
npm run build
```

Expected: both commands exit zero. Any environment-only failure must be reproduced in the documented production-equivalent WSL test environment before it can be classified as non-code.

- [ ] **Step 3: Confirm branch hygiene**

```bash
git status --short
git diff --check origin/main...HEAD
git log --oneline origin/main..HEAD
```

Expected: clean worktree, no whitespace errors, and only the planned commits.
