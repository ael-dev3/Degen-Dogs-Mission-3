# New-Auction Publication Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore Dog #821 to the public dashboard and prevent future new-auction transitions from wedging the local Windows/WSL publisher or shutting down its retry infrastructure.

**Architecture:** The queue drainer derives the minimum safe publisher scope from the authenticated publication target and the fixed clean-path `generated/refresh_status.json`: same-Dog bids stay on the low-latency current-only path, while a token transition, untrusted baseline, or generating recovery journal promotes to incremental Mission 3 archive plus current refresh. The root WSL anchor continues to fail closed for missing activation infrastructure but treats a failed one-shot worker as an observable retryable workload result. The existing publisher remains responsible for archive parity, rollback, quarantine, compare-and-swap push, and durable queue acknowledgement.

**Tech Stack:** Python 3.12-compatible standard library, Bash, the existing POSIX queue/lock state machine, systemd, Windows Task Scheduler, Git/GitHub Pages, PowerShell installer attestation, and the repository's native/WSL test harnesses.

**Spec:** `docs/superpowers/specs/2026-09-02-new-auction-publication-recovery-design.md`

## Global Constraints

- Do not weaken `validate_mission3_archive_parity()` or any Git, RPC quorum, rollback, quarantine, compare-and-swap, secret-scan, or Pages-verification gate.
- Queue JSON may select neither a command nor a path. Scope is derived only from the already validated fixed-shape publication target and the fixed repository status path.
- A canonical positive-decimal queue token equal to a positive integer baseline token selects current-only. A different valid token selects archive. A missing, unreadable, malformed, Boolean, zero, negative, or otherwise noncanonical baseline selects archive. An invalid authenticated target is state corruption and fails publication closed rather than guessing.
- A generating recovery journal always requests archive before Bash recovery, because its working tree may contain partial output. Bash then unions that request with the authenticated journal scope; terminal handoffs keep their existing recovery path.
- `DEGEN_DOGS_FULL_REFRESH` remains `0`; this incident needs incremental archive indexing, not an unconditional full-history refresh.
- Preserve the authenticated generation/digest and publication target through journal recovery. A pre-existing `run_scope=current` journal may be promoted by the fixed publisher's existing recovery logic.
- A failed `degen-dogs-publisher.service` or `degen-dogs-pages-verifier.service` must be logged but must not remove `/run/degen-dogs/activation-enabled` or `/run/degen-dogs/anchor-ready`. An unloaded triggered service, missing/disabled/inactive activation unit, invalid marker, failed start, or failed marker write remains fatal.
- Never clear or rewrite generation 5 by hand. Recovery must land through the normal publisher/finalizer path.
- Privileged anchor changes are installed only from an immutable reviewed commit through `install_wsl_startup_task.ps1`; never patch `/usr/local/libexec/degen-dogs-wsl-anchor` in place.
- Pushes are non-force and must fail safely if `origin/main` advances. Public success is claimed only after immutable raw `main` and GitHub Pages match the landed commit and auction tuple.

---

## Task 1: Derive the minimum safe queue publisher scope

**Files:**

- Modify: `scripts/test_drain_publication_queue.py`
- Modify: `scripts/drain_publication_queue.py`
- Modify: `scripts/test_refresh_and_publish.sh`
- Modify: `scripts/refresh_and_publish.sh`

- [ ] **Step 1: Add behavioral tests that name the four scope-selection breaks**

  Extend the real queue fixture so its repository contains a literal committed status with `current_dog_token_id: 818` and its authenticated target contains token `"818"`. Assert the launched fixed publisher receives both refresh flags as `0`.

  Add separate literal cases for target token `"819"`, missing status, invalid JSON, Boolean baseline, negative baseline, and a recovery journal carrying token `"819"`. Assert each unsafe/different case launches the fixed publisher with:

  ```text
  DEGEN_DOGS_RUN_MISSION3_ARCHIVE=1
  DEGEN_DOGS_FULL_REFRESH=0
  ```

  For the recovery case, also assert generation, digest, fixed argv, and target selection remain bound to the journal rather than a newer latest record.

  Add a real Bash publisher fixture with an authenticated publication record,
  inherited refresh-lock FD, deferred Pages verification, archive flag `1`,
  and full flag `0`. Assert archive indexing runs before bounded current
  refresh, the recovery journal/commit scope is `archive`, and the durable
  generation/digest binding is retained. Keep explicit negative cases proving
  deferred `full` and `archive_full` are rejected.

- [ ] **Step 2: Run the focused suite and observe RED**

  ```powershell
  python scripts/test_drain_publication_queue.py
  wsl.exe -d DegenDogsRunner bash -lc 'cd /mnt/d/ddverify2 && bash scripts/test_refresh_and_publish.sh'
  ```

  Expected: the new-token/fail-safe cases fail because the drainer always exports archive `0`, and the deferred archive fixture fails at the current-only Bash context gate.

- [ ] **Step 3: Implement strict conditional archive promotion**

  Add a small pure helper that accepts `repo_dir` and `publication_target`, extracts the canonical positive-decimal target token, reads only `repo_dir / "generated" / "refresh_status.json"`, and returns whether incremental archive work is required. Parse the target and baseline independently; an invalid target raises a closed failure, while baseline read/JSON/shape/value failures return `True`. Exclude Booleans and cap the fixed-file read to a bounded size. Do not invoke Git from the Python drainer.

  Thread the selected full publication target through both selection branches:

  - recovery: `recovery["publication_target"]`, with archive forced while its handoff phase is `generating` and never downgraded from an archive-bearing journal scope;
  - latest: `latest[0]`.

  Pass that target into `run_publisher()` and `sanitized_publisher_environment()`. Export archive `1` only when the helper requires it. Keep full refresh `0`, the fixed Bash argv, inherited lock FD, environment scrub, and all timeout/finalization behavior unchanged.

  In `validate_deferred_publication_context()`, accept exactly `current` or
  `archive`. Continue rejecting `full` and `archive_full`, and leave the fixed
  queue state path, lock FD, generation, digest, skip-push, and journal checks
  unchanged.

- [ ] **Step 4: Run focused native and WSL tests and observe GREEN**

  ```powershell
  python scripts/test_drain_publication_queue.py
  wsl.exe -d DegenDogsRunner bash -lc 'cd /mnt/d/ddverify2 && python3 scripts/test_drain_publication_queue.py && bash scripts/test_refresh_and_publish.sh'
  ```

  Expected: every queue-drainer test passes in both environments.

- [ ] **Step 5: Commit the scope repair**

  ```powershell
  git add scripts/drain_publication_queue.py scripts/test_drain_publication_queue.py scripts/refresh_and_publish.sh scripts/test_refresh_and_publish.sh
  git commit -m "fix: promote new auctions to archive refresh"
  ```

---

## Task 2: Isolate retryable worker failures from anchor liveness

**Files:**

- Modify: `scripts/test_wsl_runner_assets.py`
- Modify: `scripts/run_wsl_runner_anchor.sh`

- [ ] **Step 1: Add executable anchor lifecycle regressions**

  Extend the existing dynamic Bash harness, not a source-text assertion. Mock active/enabled activation units and loaded triggered services. Make only `degen-dogs-publisher.service` report failed, enter the anchor loop, and assert a loop checkpoint is reached with both runtime markers present and a warning on stderr.

  Add a second mode in which the publisher's `LoadState` is `not-found`; assert the anchor exits nonzero and its cleanup removes both runtime markers. Retain the current failed-unit-start cleanup case.

- [ ] **Step 2: Run the asset suite and observe RED**

  ```powershell
  python scripts/test_wsl_runner_assets.py
  ```

  Expected: the failed-but-loaded publisher case exits before the loop checkpoint.

- [ ] **Step 3: Change only the one-shot failure branch**

  In `start_units()`, retain every enabled/active/load-state check. Replace the `return 1` inside the `systemctl is-failed --quiet "$unit"` branch with one deterministic stderr warning naming the failed unit and stating that activation remains armed for timer/path retry. Do not clear the failed state, restart the worker directly, or alter marker ownership/mode.

- [ ] **Step 4: Run syntax, native asset/policy, and WSL asset gates**

  ```powershell
  bash -n scripts/run_wsl_runner_anchor.sh
  python scripts/test_wsl_runner_assets.py
  python scripts/test_wsl_runner_windows_policy.py
  wsl.exe -d DegenDogsRunner bash -lc 'cd /mnt/d/ddverify2 && python3 scripts/test_wsl_runner_assets.py'
  ```

  Expected: syntax is clean; the failed-but-loaded worker preserves liveness; missing load state still fails closed; all existing installer/Task Scheduler tests pass.

- [ ] **Step 5: Commit the anchor repair**

  ```powershell
  git add scripts/run_wsl_runner_anchor.sh scripts/test_wsl_runner_assets.py
  git commit -m "fix: keep runner anchor alive across worker retries"
  ```

---

## Task 3: Prove cross-layer publication behavior

**Files:**

- Verify: `scripts/refresh_and_publish.sh`
- Verify: `scripts/validate_dashboard_consistency.py`
- Verify: `scripts/runner_publication_state.py`
- Verify: all files changed in Tasks 1-2

- [ ] **Step 1: Run the focused publisher and state-machine suites in WSL**

  ```powershell
  wsl.exe -d DegenDogsRunner bash -lc 'cd /mnt/d/ddverify2 && bash scripts/test_refresh_and_publish.sh && python3 scripts/test_runner_publication_state.py && python3 scripts/test_drain_publication_queue.py && python3 scripts/test_wsl_publication_integration.py'
  ```

  Expected: current-only remains current for a same-Dog target, archive scope records correctly for a token transition, recovery journals promote safely, and every durable handoff/finalizer test passes.

- [ ] **Step 2: Run repository policy and generated-data regression gates**

  ```powershell
  npm run test:dashboard-data
  npm run test:live-bundle
  npm run test:current-surface
  python scripts/test_refresh_telemetry.py
  python scripts/test_degen_dogs_runner_health.py
  npm run test:wsl-runner-assets
  npm run test:wsl-windows-policy
  git diff --check
  ```

  Expected: all commands exit zero and no unrelated generated file changes remain.

- [ ] **Step 3: Record verification evidence without changing production data**

  Add the exact commands, pass counts, and commit range to the SDD report/ledger. Do not run the publisher against the development checkout.

---

## Task 4: Land the repair and recover preserved generation 5

**Files and systems:**

- Git branch: `agent/local-low-latency-runner`
- Remote branch: `origin/main`
- Production distro/repo: `DegenDogsRunner:/srv/degen-dogs/repo`
- Windows task: `Degen Dogs WSL Runner`

- [ ] **Step 1: Reconcile and push without force**

  ```powershell
  git fetch origin main
  git merge --ff-only origin/main
  git push origin HEAD:main
  ```

  Expected: the push lands only if remote `main` is still a fast-forward. If it advanced, stop this step, integrate the new commits, repeat all affected gates, and retry without force.

- [ ] **Step 2: Start the exact installed runner and watch the preserved queue**

  Inspect the production checkout and queue read-only first. Start only `Degen Dogs WSL Runner` if it is not already running. Follow the anchor, publisher, refresh, and queue logs until generation 5 either lands or yields a new concrete failure. Do not delete `latest.json`, recovery journals, or quarantine evidence.

  Expected: the production drainer fast-forwards to the landed code, derives archive scope for Dog #821, runs incremental archive plus current refresh, passes archive parity, pushes one normal runner commit, and finalizes generation 5.

- [ ] **Step 3: Install the reviewed anchor through the immutable upgrade path**

  Resolve the exact 40-character landed `main` commit. Use a clean detached bootstrap checkout and the existing installer mode. For the currently installed interactive runner, invoke:

  ```powershell
  & $newBootstrapScript -UpgradeTrustedBundle -TrustedInstallerCommit $newTrustedCommit -Activate -AtLogOnOnly
  ```

  If the installed task is already password-backed, preserve that mode and use a credential entered through `Get-Credential`; never put a password in a command, file, log, environment variable, or WSL.

  Expected: trusted bundle, root wrapper, rendered units, exact task XML/action, activation markers, and final liveness attestation all pass.

---

## Task 5: Verify public freshness and failure recovery

- [ ] **Step 1: Verify the landed auction tuple at every boundary**

  Compare the production checkout, raw immutable commit, raw `main`, and cache-busted GitHub Pages files. Require Dog `821`, bidder `0x437be13e57f822167a3206c6744d03bcd6b499ee`, amount `0.005 ETH`, end time `2026-09-02T22:58:23Z`, and identical block number/hash and live-bundle digest for the landed snapshot.

- [ ] **Step 2: Verify queue, timers, task, and health**

  Require queue lag zero; no active recovery journal; anchor-ready and activation-enabled markers present; all activation units enabled/active; publisher/verifier not failed after a successful retry; Task Scheduler enabled with exactly one correct action/process; and a successful health check newer than two health periods.

- [ ] **Step 3: Exercise the retry boundary safely**

  Use the repository's isolated systemd/asset harness to inject a failed one-shot and prove the anchor remains alive, then prove a succeeding retry replaces the failed result. Do not inject a failure into the live auction publisher while publication is pending.

- [ ] **Step 4: Soak and recheck**

  Observe at least two watcher periods and one publisher timer period. Re-read cache-busted public status and confirm its block/time never regresses. Capture final task/service/log/public evidence before declaring recovery complete.
