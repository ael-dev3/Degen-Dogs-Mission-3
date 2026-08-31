# Windows Queued Publisher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the Windows WSL detector observing confirmed Mission 3 auction changes while generation, Git push, or GitHub Pages verification is in progress, without changing the Mac runner's archive authority or weakening any accuracy/recovery boundary.

**Architecture:** The Windows WSL watcher persists a quorum-validated latest observation before it acknowledges detection. A separately supervised drainer holds the existing refresh lock across the fixed Bash publisher, including an inherited lock FD, and hands each landed/no-diff/superseded generation through an explicit durable state machine. The Bash publisher prepares an immutable post-push handoff; the drainer alone acknowledges the queue generation and clears the recovery journal. A detached verifier reads an exact pending record and never mutates the checkout. Mac keeps inline mode; WSL alone pins queue mode after activation.

**Tech Stack:** Python 3.12-compatible standard library running inside the WSL `DegenDogsRunner` distribution, Bash, Linux `fcntl`, systemd, existing root-owned WSL installer/anchor, GitHub immutable raw files/Pages verification, and current test harnesses. Do not run POSIX inode/lock tests with native Windows Python.

**Spec:** `docs/superpowers/specs/2026-08-30-low-latency-auction-data-design.md`

## Non-negotiable Constraints

- `MISSION3_WATCHER_PUBLICATION_MODE` accepts exactly `inline` or `queue`; its repository default is `inline`. Only `run_wsl_runner_job.sh watcher` pins `queue` after `.env.local` has loaded. The Mac launchd path stays inline and archive-capable.
- Preserve two-provider quorum and one-block confirmation. A queued record is evidence of an already-validated observation, not authority to skip the publisher's existing generation, data validation, compare-and-swap push, secret scan, or peer-collision checks.
- A queue-mode watcher must not skip Base RPC/log scanning because `refresh.lock` is held. It must atomically persist the observation before watcher state/telemetry says that observation was handled.
- A queue record is latest-wins, but its ordering is chain-correct: accept a higher confirmed block; coalesce byte-identical observations; accept an equal-height different hash only with an explicit two-provider canonical-reorg transition referencing the prior hash; retain lower observations as stale and never publish them. Never order hashes lexicographically.
- State is private and fixed below `${DEGEN_DOGS_LOCK_DIR}/publication/`: regular non-symlink, one hard link, owner-only `0600`, size/schema bounded, atomic replace plus file and parent directory `fsync`. Production requires WSL/POSIX locking; portable unit tests inject the lock context rather than silently replacing `fcntl`.
- The durable sequence is exact: **immutable remote proof → pending Pages record `fsync` → publisher checkpoint `fsync` → exact queue generation/digest CAS → authenticated recovery-journal unlink and parent `fsync`**. Ownership is split deliberately: Bash performs only the first three handoff actions; the drainer performs only the final CAS/unlink actions while retaining the same refresh-lock FD.
- No-diff and peer-superseded publisher outcomes are terminal queue outcomes too. They must create a durable handled checkpoint and exact-generation acknowledgement before journal cleanup; a no-diff result must not invent a push timestamp.
- The verifier is checkout-read-only. It may write only `${DEGEN_DOGS_LOCK_DIR}/publication/` and `${DEGEN_DOGS_LOG_DIR}`, never calls Git, npm, a generator, or a publisher, and clears a pending record only by exact generation/commit CAS.

---

## File Structure

- Create: `scripts/runner_publication_state.py` — schema parser/writer and safe state-machine primitives.
- Create: `scripts/drain_publication_queue.py` — inherited-lock Windows WSL queue drainer/finalizer.
- Create: `scripts/verify_pages_deployment.py` — immutable commit/bundle Pages verifier.
- Modify: `scripts/watch_mission3_auction.py` — publication-mode parsing, quorum event-header enrichment, queue enqueue semantics, and public-safe telemetry.
- Modify: `scripts/refresh_and_publish.sh` — deferred handoff flag, journal schema/recovery, and terminal no-diff/peer paths.
- Modify: `scripts/refresh_telemetry.py`, `scripts/check_wsl_runner_health.py`, `scripts/degen_dogs_runner_health.py`, `config/logrotate/degen-dogs-wsl.in`, and their tests — queue/push/verifier telemetry and health.
- Modify: `scripts/run_wsl_runner_job.sh`, `scripts/run_wsl_runner_anchor.sh`, `scripts/install_wsl_runner.sh`, `scripts/install_wsl_startup_task.ps1`, `config/wsl-runner.env.template`, `config/systemd/degen-dogs-runner.target`, `docs/windows-wsl-runner.md`, and `docs/refresh-runner.md`.
- Create: `config/systemd/degen-dogs-publisher.service.in`, `degen-dogs-publisher.path.in`, `degen-dogs-publisher.timer`, `degen-dogs-pages-verifier.service.in`, `degen-dogs-pages-verifier.path.in`, and `degen-dogs-pages-verifier.timer`.
- Create: `scripts/test_runner_publication_state.py`, `scripts/test_drain_publication_queue.py`, `scripts/test_verify_pages_deployment.py`, and a bounded WSL integration harness for rendered-unit isolation/120-second delay tests.
- Modify: `scripts/test_watch_mission3_auction.py`, `scripts/test_refresh_and_publish.sh`, `scripts/test_refresh_telemetry.py`, `scripts/test_degen_dogs_runner_health.py`, `scripts/test_wsl_runner_assets.py`, `scripts/test_wsl_runner_windows_policy.py`, `scripts/test_run_runner_health.sh`, and `package.json`.

## Persistent State Contracts

All records use `schema_version: 1`, canonical JSON, UTC `Z` timestamps, and strict length/format validation. Paths are fixed names, never supplied by a journal or environment field.

`publication/latest.json` is the single latest-wins record:

```json
{
  "schema_version": 1,
  "generation": 42,
  "created_at_utc": "2026-08-30T12:34:56Z",
  "runner_id": "windows-wsl",
  "run_scope": "current",
  "observation": {
    "confirmed_block_number": 123,
    "confirmed_block_hash": "0x...",
    "confirmed_block_time_utc": "2026-08-30T12:34:00Z",
    "token_id": "818",
    "amount_wei": "5500000000000000",
    "start_time_unix": "1780000000",
    "end_time_unix": "1780003600",
    "bidder_wallet": "0x...",
    "settled": false,
    "event_name": "AuctionBid",
    "event_tx_hash": "0x...",
    "event_log_index": 0,
    "event_block_number": 123,
    "event_block_hash": "0x...",
    "event_block_time_utc": "2026-08-30T12:34:00Z",
    "canonical_reorg_from_hash": null
  }
}
```

For a state-only transition, all `event_*` fields are `null`, while confirmed observation block/hash/time and the exact auction tuple remain mandatory. A same-height hash replacement requires non-null `canonical_reorg_from_hash` equal to the stored prior hash and an already-quorum-validated marker from the watcher. Generation increments only under the state lock; it is not derived from block/hash ordering.

`publication/pending.json` binds a queue generation/digest to an immutable Git commit, raw status/bundle paths and expected digest/size/block/hash, push completion time, retry deadline/count, and no secret. `publication/pushed.json` is a checkpoint for a terminal generation. It stores `outcome` (`pushed`, `no_diff`, or `peer_superseded`), generation, digest, commit SHA where applicable, and a nullable push time. The journal remains fixed at `${DEGEN_DOGS_LOCK_DIR}/publisher-recovery.json`; the checkpoint never supplies a journal path.

## Task 1: Build the WSL-only durable state machine and chain-correct queue semantics

**Files:**

- Create: `scripts/runner_publication_state.py`, `scripts/test_runner_publication_state.py`

- [ ] **Step 1: Add failing schema/durability tests**

  Cover protected record checks (regular file, owner, mode, link count, size, JSON shape), atomic file/parent `fsync`, first enqueue, byte-identical coalescing, higher block replacement, lower block retention, equal-height different hash rejection, explicit canonical-reorg acceptance, generation/digest compare-and-swap, corrupted temp file, and every journal/pending/checkpoint identity mismatch. Inject a fake lock context for portable schema tests; add a WSL-only `fcntl`/inode test for real production locking.

- [ ] **Step 2: Run tests inside WSL and confirm the module is absent**

  ```powershell
  wsl.exe -d DegenDogsRunner -u degendogs --cd /srv/degen-dogs/repo -- /bin/bash -lc 'python3 scripts/test_runner_publication_state.py'
  ```

- [ ] **Step 3: Implement explicit primitives with one responsibility each**

  Implement `enqueue_latest_observation()`, `read_latest_with_digest()`, `prepare_pushed_handoff()`, `finalize_pushed_handoff()`, `record_terminal_outcome()`, `recover_deferred_handoff()`, and CAS clear/write helpers. Do not implement a persistent claim lease: the drainer holds the one existing refresh flock and carries a generation plus digest snapshot through the child process.

  `prepare_pushed_handoff()` writes immutable remote proof, pending record, and checkpoint only. `finalize_pushed_handoff()` validates the fixed journal/pending/checkpoint identity, clears `latest.json` only when its generation and digest still match, leaves a newer entry intact, then unlinks the authenticated journal and `fsync`s its parent. `recover_deferred_handoff()` reconstructs a missing handoff only after independently confirming the immutable remote commit; it never regenerates data merely because a journal is present.

- [ ] **Step 4: Run state tests and commit**

  ```powershell
  wsl.exe -d DegenDogsRunner -u degendogs --cd /srv/degen-dogs/repo -- /bin/bash -lc 'python3 scripts/test_runner_publication_state.py && python3 -m py_compile scripts/runner_publication_state.py'
  git add scripts/runner_publication_state.py scripts/test_runner_publication_state.py
  git commit -m "feat: add durable WSL publication state"
  ```

## Task 2: Enrich watcher observations, queue before acknowledgement, and measure event time

**Files:**

- Modify: `scripts/watch_mission3_auction.py`, `scripts/refresh_telemetry.py`
- Modify: `scripts/test_watch_mission3_auction.py`, `scripts/test_refresh_telemetry.py`, `package.json`

- [ ] **Step 1: Add failing watcher/telemetry tests**

  Test default inline mode, invalid mode rejection, queue mode scanning despite a held publisher lock, queue persistence before watcher success/state mutation, state write failure returning non-zero, and byte-for-byte retained inline behavior. Test an event header fetched by at least two providers agrees with the observed log's block hash, adds event block time/hash, and rejects disagreement. Cover state-only observations with null event fields. Require telemetry fields for event-to-observation, observation-to-push, push-to-Pages, queue generation, queue digest, outcomes, and provider failures without URLs/secrets.

- [ ] **Step 2: Run watcher/telemetry tests and confirm failure**

  ```powershell
  wsl.exe -d DegenDogsRunner -u degendogs --cd /srv/degen-dogs/repo -- /bin/bash -lc 'python3 scripts/test_watch_mission3_auction.py && python3 scripts/test_refresh_telemetry.py'
  ```

- [ ] **Step 3: Implement queue-mode only after quorum validation**

  Parse `MISSION3_WATCHER_PUBLICATION_MODE` once. In `inline`, leave current lock/process behavior unchanged. In `queue`, bypass only the publisher-lock preflight suppression; retain RPC/log quorum and transition validation, look up the selected event's quorum header/time, construct the complete record above, call `enqueue_latest_observation()`, then record watcher state/telemetry. The bid cooldown begins only after a successful durable enqueue. No queue-mode code may spawn the publisher directly.

- [ ] **Step 4: Commit observation/telemetry changes**

  ```powershell
  wsl.exe -d DegenDogsRunner -u degendogs --cd /srv/degen-dogs/repo -- /bin/bash -lc 'npm run test:watcher && python3 scripts/test_refresh_telemetry.py'
  git add scripts/watch_mission3_auction.py scripts/refresh_telemetry.py scripts/test_watch_mission3_auction.py scripts/test_refresh_telemetry.py package.json
  git commit -m "feat: queue confirmed Windows auction observations"
  ```

## Task 3: Add the Bash deferred-handoff state machine and landed-push recovery

**Files:**

- Modify: `scripts/refresh_and_publish.sh`, `scripts/test_refresh_and_publish.sh`
- Modify: `scripts/runner_publication_state.py`, `scripts/test_runner_publication_state.py`

- [ ] **Step 1: Add failing crash-boundary tests**

  Cover normal inline completion unchanged; deferred `pushed`; deferred `success_no_diff`; deferred `success_superseded_by_peer`; crash after remote push, raw immutable proof, pending `fsync`, checkpoint `fsync`, queue CAS, and journal unlink. Exercise current `recover_interrupted_generation` behavior when remote equals local after a landed commit. Assert no terminal path leaves a retrying queue generation with no checkpoint and no deferred path clears a journal before finalization.

- [ ] **Step 2: Extend the authenticated recovery journal**

  Add validated deferred fields to every journal reader/writer/rewrite: publication generation, queue digest, terminal outcome, and handoff phase. Existing non-deferred journals retain current recovery semantics. A deferred landed commit recovery must: verify exact remote SHA through immutable raw content; reconstruct pending/checkpoint if needed; then return control to drainer finalization. It must not immediately unlink the journal at the current remote-equals-local recovery branch.

- [ ] **Step 3: Implement a narrow deferred publisher mode**

  `DEGEN_DOGS_DEFER_PAGES_VERIFICATION=1` is valid only with canonical generation/digest state below the fixed lock directory and an inherited active lock FD. After existing compare-and-swap push and raw commit proof, Bash calls `prepare_pushed_handoff()`; for no-diff or peer supersession it calls `record_terminal_outcome()` instead. Adjust traps so successful handoff never rolls back a landed commit or unlinks the journal. Inline/Mac (`0`) retains existing live verification and cleanup byte-for-byte.

- [ ] **Step 4: Run publisher/recovery tests and commit**

  ```powershell
  wsl.exe -d DegenDogsRunner -u degendogs --cd /srv/degen-dogs/repo -- /bin/bash -lc 'bash scripts/test_refresh_and_publish.sh && python3 scripts/test_runner_publication_state.py'
  git add scripts/refresh_and_publish.sh scripts/test_refresh_and_publish.sh scripts/runner_publication_state.py scripts/test_runner_publication_state.py
  git commit -m "feat: preserve queued publication handoffs"
  ```

## Task 4: Implement the inherited-lock drainer and exact queue finalization

**Files:**

- Create: `scripts/drain_publication_queue.py`, `scripts/test_drain_publication_queue.py`
- Modify: `scripts/run_wsl_runner_job.sh`

- [ ] **Step 1: Add failing drainer tests**

  Test inherited `refresh.lock` FD handoff to Bash, no self-deadlock, recovery-before-new-publish, newer generation arrival while publishing, exact generation/digest CAS, journal cleanup only after CAS, no-diff acknowledgment, peer-superseded acknowledgment/optional immutable verification record, publisher failure retaining the queue, and service timeout preserving recoverable state. Test that the drainer never invokes a shell command chosen from a queue record.

- [ ] **Step 2: Implement one consumer under the existing flock**

  The drainer opens and owns the existing private refresh lock, marks its FD inheritable, and invokes the fixed repository Bash publisher with `DEGEN_DOGS_LOCK_HELD=1`, the exact validated lock FD/path, `DEGEN_DOGS_DEFER_PAGES_VERIFICATION=1`, generation/digest, and Python `pass_fds`. It calls deferred recovery before reading latest state. After Bash returns a prepared/terminal checkpoint, it calls `finalize_pushed_handoff()` or terminal CAS cleanup while it still owns that same lock. It loops only if a different newer `latest.json` already exists and stops before its bounded systemd timeout.

- [ ] **Step 3: Run drainer tests and commit**

  ```powershell
  wsl.exe -d DegenDogsRunner -u degendogs --cd /srv/degen-dogs/repo -- /bin/bash -lc 'python3 scripts/test_drain_publication_queue.py'
  git add scripts/drain_publication_queue.py scripts/test_drain_publication_queue.py scripts/run_wsl_runner_job.sh
  git commit -m "feat: drain Windows publication queue safely"
  ```

## Task 5: Implement immutable, CAS-safe detached Pages verification

**Files:**

- Create: `scripts/verify_pages_deployment.py`, `scripts/test_verify_pages_deployment.py`
- Modify: `scripts/runner_publication_state.py`, `scripts/test_runner_publication_state.py`

- [ ] **Step 1: Add failing verifier tests**

  Use a local HTTP fixture and private records. Test exact raw status/bundle fetch from the record commit, record digest/size/block/hash validation, Pages byte comparison, successful exact CAS clear, timeout/retry, network failure, an updated pending record during an in-memory attempt, malformed/superseded records, and private telemetry. Unit tests must assert verifier source never calls Git/npx/npm/generators; a later rendered-unit test proves the systemd read-only repo boundary.

- [ ] **Step 2: Implement latest-record semantics without unsafe deletion**

  The verifier reads one `pending.json` snapshot and attempts only that generation/commit. It may abandon an attempt after observing a newer valid pending record, logs the abandonment privately, and never clears the record just because `main` advanced. It fetches immutable raw artifacts for the expected commit, validates them against record values, verifies Pages exact bytes, then CAS-clears only the same generation+commit. A timeout leaves the pending record for the retry timer and health monitor.

- [ ] **Step 3: Run verifier tests and commit**

  ```powershell
  wsl.exe -d DegenDogsRunner -u degendogs --cd /srv/degen-dogs/repo -- /bin/bash -lc 'python3 scripts/test_verify_pages_deployment.py && python3 scripts/test_runner_publication_state.py'
  git add scripts/verify_pages_deployment.py scripts/test_verify_pages_deployment.py scripts/runner_publication_state.py scripts/test_runner_publication_state.py
  git commit -m "feat: verify Pages outside the publisher lock"
  ```

## Task 6: Add telemetry, queue-aware health, and the blocked-Pages proof

**Files:**

- Modify: `scripts/refresh_telemetry.py`, `scripts/check_wsl_runner_health.py`, `scripts/degen_dogs_runner_health.py`, `config/logrotate/degen-dogs-wsl.in`
- Modify: `scripts/test_refresh_telemetry.py`, `scripts/test_degen_dogs_runner_health.py`, `scripts/test_run_runner_health.sh`, `package.json`
- Create: a WSL integration test invoked by `npm run test:wsl-publication-integration`

- [ ] **Step 1: Add failing health/telemetry tests**

  Require public-safe summaries of latest observed generation, handled/pushed generation, queue lag/age, unresolved verification generation/commit/age, last direct-data-compatible static block, and provider failures. Store detailed Pages-verification JSONL under the private lock/log path outside the repository and rotate it. In queue mode, a held publisher lock must no longer excuse a stale watcher timestamp. Test the artificial 120-second Pages delay: a later confirmed observation is persisted, then the newest generation drains next after the active safe transaction.

- [ ] **Step 2: Implement metrics and health conditions**

  Extend watcher/refresh rows with validated event block time and generation fields; extend refresh metrics with observation-to-push and push-to-Pages calculations when inputs exist. Health reads the protected state records with the same safe parser, reports explicit queue/verifier failures, and remains backward-compatible when queue mode is absent on Mac. Add bounded retention/redaction to logrotate and test that no RPC URLs, keys, repo path, host, or user values enter public status output.

- [ ] **Step 3: Run health/integration suite and commit**

  ```powershell
  wsl.exe -d DegenDogsRunner -u degendogs --cd /srv/degen-dogs/repo -- /bin/bash -lc 'python3 scripts/test_refresh_telemetry.py && python3 scripts/test_degen_dogs_runner_health.py && bash scripts/test_run_runner_health.sh && npm run test:wsl-publication-integration'
  git add scripts/refresh_telemetry.py scripts/check_wsl_runner_health.py scripts/degen_dogs_runner_health.py config/logrotate/degen-dogs-wsl.in scripts/test_refresh_telemetry.py scripts/test_degen_dogs_runner_health.py scripts/test_run_runner_health.sh package.json
  git commit -m "feat: monitor queued Windows publication"
  ```

## Task 7: Supervise WSL-only workers through the complete trusted lifecycle

**Files:**

- Create six publisher/verifier systemd unit assets listed above.
- Modify: `scripts/run_wsl_runner_job.sh`, `scripts/run_wsl_runner_anchor.sh`, `scripts/install_wsl_runner.sh`, `scripts/install_wsl_startup_task.ps1`, `config/systemd/degen-dogs-runner.target`, `config/wsl-runner.env.template`, docs, and WSL asset/policy tests.

- [ ] **Step 1: Add failing privileged-lifecycle tests**

  Require all six units and new scripts in root-owned trusted-stage inventories, runtime manifests, render/copy lists, systemd verification, target/anchor start lists, activation liveness proof, quiesce, uninstall, and rollback lists in both Bash and PowerShell. Assert literal rendered path watches: `PathChanged=@LOCK_DIR@/publication/latest.json` and `PathChanged=@LOCK_DIR@/publication/pending.json`; do not watch a whole directory because receipts/temp files would self-trigger. Assert queue mode is WSL-pinned only, publisher gets repo write access, and verifier has `ReadOnlyPaths=@REPO_DIR@` plus `ReadWritePaths=@LOG_DIR@ @LOCK_DIR@`.

- [ ] **Step 2: Add services, path triggers, and bounded retry timers**

  Both services use the existing activation marker, service user/group, `UMask=0077`, `NoNewPrivileges=true`, private tmp, strict system protection, empty capability set, and only AF_UNIX/AF_INET/AF_INET6. The publisher path/timer starts the drainer; the verifier path/timer starts immutable verification. A rendered-systemd integration test attempts a verifier worktree write and requires systemd denial; Python unit tests only prove no mutating command path exists.

- [ ] **Step 3: Run asset/installer tests and commit**

  ```powershell
  wsl.exe -d DegenDogsRunner -u root --cd /srv/degen-dogs/repo -- /bin/bash -lc 'bash -n scripts/install_wsl_runner.sh scripts/run_wsl_runner_job.sh scripts/run_wsl_runner_anchor.sh'
  python scripts/test_wsl_runner_assets.py
  python scripts/test_wsl_runner_windows_policy.py
  git add config/systemd scripts/run_wsl_runner_job.sh scripts/run_wsl_runner_anchor.sh scripts/install_wsl_runner.sh scripts/install_wsl_startup_task.ps1 config/wsl-runner.env.template docs/windows-wsl-runner.md docs/refresh-runner.md scripts/test_wsl_runner_assets.py scripts/test_wsl_runner_windows_policy.py
  git commit -m "feat: supervise queued Windows publication"
  ```

## Task 8: Deploy safely, soak at 15 seconds, then consider the approved 5-second cadence

**Files:** source/config/systemd timer only after every earlier gate passes.

- [ ] **Step 1: Run the complete production-relevant suite from the WSL clone**

  ```powershell
  wsl.exe -d DegenDogsRunner -u degendogs --cd /srv/degen-dogs/repo -- /bin/bash -lc 'python3 scripts/test_runner_publication_state.py && python3 scripts/test_watch_mission3_auction.py && python3 scripts/test_drain_publication_queue.py && python3 scripts/test_verify_pages_deployment.py && bash scripts/test_refresh_and_publish.sh && python3 scripts/test_refresh_telemetry.py && python3 scripts/test_degen_dogs_runner_health.py && npm run test:wsl-publication-integration && npm run test:watcher'
  ```

  Keep `npm run test:wsl-runner-assets` in the ordinary non-root CI/Pages gate
  for portable asset checks. From the exact reviewed Windows checkout, also run
  `python scripts/test_wsl_runner_assets.py --require-rendered-systemd-isolation`;
  this separate fail-closed gate must execute the rendered verifier denial as
  root inside the isolated WSL distro. Never treat a portable-suite pass as a
  receipt for the privileged systemd isolation test.

- [ ] **Step 2: Push, install through the trusted bootstrap, and activate queue mode**

  Push reviewed source through the normal full Pages gate. Use the existing trusted Windows bootstrap with the exact reviewed commit; inspect all rendered units and activation liveness conditions before enabling. Confirm Mac's launchd settings and archive role are unchanged. Verify that a held verifier does not suppress a 15-second watcher scan and that no Windows archive artifacts are emitted.

- [ ] **Step 3: Soak and measure before touching cadence**

  Keep the current 15-second Windows timer for at least 72 hours and 100 successful queue observations. Require zero lost/duplicate generations, no unhandled recovery journal, p95 queue-to-push lag below 15 seconds when Git is available, no provider-throttle/circuit-breaker increase, and a successful artificial 120-second verification-delay test. Record event-to-observation, observation-to-push, and push-to-Pages p50/p95 separately.

- [ ] **Step 4: Make the 5-second cadence a separate reviewed release only if soak gates pass**

  Change the WSL timer and documented/configured interval from 15 to 5 seconds in a dedicated source commit, preserving one confirmation, two-provider quorum, log chunk limits, endpoint circuit breakers, and the Mac offset. Add timer/config tests proving the exact five-second schedule and throttled retry behavior, run the complete suite again, deploy through the full gate, and observe another 72-hour soak. Do not make this change as part of queue activation.

- [ ] **Step 5: Use a race-free rollback**

  On generation loss, queue/journal inconsistency, verifier worktree mutation, unsafe provider behavior, or failed post-activation health: use the trusted activation rollback to stop/disable the Windows watcher **and all new publisher/verifier path/timer/service units first**; preserve private queue/checkpoint/pending records for diagnosis; rely on the Mac/static fallback; land a normal source revert through the full gate; then reinstall/activate that exact trusted revert. Never enable inline watcher while queued workers may still run, and never delete queued records as a rollback shortcut.
