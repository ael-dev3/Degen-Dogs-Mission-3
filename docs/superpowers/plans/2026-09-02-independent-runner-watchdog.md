# Independent Runner Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a same-day durable health lease with zero new recovery mutation, then add classified at-most-once anchor recovery only after the cross-platform safety protocol is proven.

**Architecture:** Phase A binds every health result to one root-created attempt, install epoch, WSL boot, and systemd invocation; an optional Windows auditor records findings but cannot mutate runtime state. Phase B adds one shared installer/auditor lock, planned-maintenance epochs, a Windows-only at-most-once claim, exact fault classification, and bounded task-only recovery.

**Tech Stack:** Python standard library, root-owned POSIX state, systemd, PowerShell 5.1-compatible Task Scheduler/COM/CIM APIs, Windows Event Log, and existing native/WSL/Windows policy harnesses.

**Spec:** `docs/superpowers/specs/2026-09-02-independent-runner-watchdog-design.md`

## Global constraints

- The system is local, deterministic, programmatic, non-agentic, and has no LLM/API dependency.
- Public dashboard restoration is a separate prerequisite and is never blocked on this plan.
- The Phase A auditor adds no stop/start/enable/disable/unregister/kill/terminate behavior; reviewed installer activation remains unchanged.
- Only the immutable Linux helper writes `install.json` and the authoritative
  combined `state.json`; legacy split records are never read.
- Windows is the only recovery-claim authority; Linux audit state is a mirror.
- Recovery is at-most-once, not exactly-once.
- A valid lease must match install epoch, runtime/trusted commits, WSL boot ID, and health invocation, and must be at most 480 seconds old by WSL monotonic time.
- The audit repeats every two minutes; lease-silence detection is nominally bounded by 480 + 120 = 600 seconds while its principal is runnable.
- Interactive-token mode cannot run or report after logout. Password mode requires an explicit current-account credential prompt and real acceptance testing.
- Default tests never mutate production tasks or units, terminate `DegenDogsRunner`, reboot, log out, break host networking, or register a machine-wide Event Log source.
- Every privileged asset is fetched from and bound to the exact reviewed trusted commit.

---

## Phase A — durable evidence, zero recovery mutation

Phase A may be released after Task 2. Task 3 is optional and must not delay the
Linux lease release.

### Task 1: Build the immutable health state recorder

**Files:**

- Create: `scripts/record_wsl_runner_health.py`
- Create: `scripts/test_record_wsl_runner_health.py`
- Modify: `package.json`

**Interfaces:**

- `begin_health(layout, *, invocation_id, install, boot_id, now) -> dict`
- `record_health(layout, *, service_result, exit_code, exit_status, now, boot_id, uptime_seconds, expected_uid) -> dict`
- `snapshot(layout, *, boot_id, uptime_seconds) -> dict`
- Production CLI modes: `install-identity`, `prepare-runtime`, `begin-health`,
  `record-health`, `snapshot`, `record-audit-mirror`, and
  `clear-audit-mirror`; production paths are constants, never CLI/environment
  inputs.

- [ ] **Step 1: Write RED recorder tests**

  Add direct-function tests using temporary `StateLayout` paths. Assert exact canonical schemas and that only this conjunction advances the lease:

  ```python
  result = record_health(
      layout,
      service_result="success",
      exit_code="exited",
      exit_status="0",
      now=fixed_now,
      boot_id=BOOT_ID,
      uptime_seconds=900.0,
      expected_uid=os.getuid(),
  )
  assert result["lease_advanced"] is True
  assert snapshot(layout, boot_id=BOOT_ID, uptime_seconds=901.0)["lease_age_seconds"] == 1
  ```

  Cover missing candidate, nonzero service result, attempt-token/invocation
  mismatch, replay, future/old timestamp, install/boot mismatch, monotonic
  regression, backward wall-clock clamping, invalid SHA/block/generation/codes/
  schema/size, noncanonical JSON, symlink, hard link, wrong owner/mode, absent
  runtime creation under umask `077`, runner-cache independence for root modes,
  four failures retaining first failure, recovery summary before health clear,
  audit-section independence, concurrency, legacy split-state migration, and a
  failed single-record atomic replacement preserving the prior lease and
  incident together.

- [ ] **Step 2: Run the RED suite**

  Run: `PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_record_wsl_runner_health.py`

  Expected: failure because the recorder interfaces do not exist.

- [ ] **Step 3: Implement the self-contained recorder**

  Use only the standard library. Keep filesystem policy in a `StateLayout` used
  by tests; construct the production layout only inside CLI dispatch. Use pinned
  directory/file descriptors, `O_NOFOLLOW`, exact owner/mode/link/size checks,
  `flock`, file fsync, one atomic `state.json` replace, and parent fsync.
  `state.json` jointly contains `last_good` and `incident`; no split record is
  authoritative. Remove obsolete fixed split-state names during install
  migration. `snapshot` computes age from current `CLOCK_BOOTTIME` only after
  boot/install identity matches. Clamp incident/recovery display UTC against
  the preceding durable health boundary without using wall time for lease age.

- [ ] **Step 4: Prove recorder behavior**

  Run the RED command again, then:

  ```bash
  python3 -m py_compile scripts/record_wsl_runner_health.py scripts/test_record_wsl_runner_health.py
  git diff --check
  ```

  Expected: all pass.

- [ ] **Step 5: Commit the independently reviewable state machine**

  ```bash
  git add scripts/record_wsl_runner_health.py scripts/test_record_wsl_runner_health.py package.json
  git commit -m "feat: persist invocation-bound runner health"
  ```

### Task 2: Connect health attempts, probe output, systemd, and trusted installation

**Files:**

- Modify: `scripts/check_wsl_runner_health.py`
- Modify: `scripts/run_wsl_runner_job.sh`
- Modify: `config/systemd/degen-dogs-health.service.in`
- Modify: `scripts/install_wsl_runner.sh`
- Modify: `scripts/install_wsl_startup_task.ps1`
- Modify: `scripts/test_wsl_runner_assets.py`
- Modify: `scripts/test_wsl_publication_integration.py`
- Modify: `docs/windows-wsl-runner.md`

**Interfaces:**

- The probe reads fixed `/run/degen-dogs/health/attempt.json` and writes fixed `/var/cache/degen-dogs/health-report.json`.
- The PowerShell bootstrap creates one install epoch per lifecycle and passes it through both Linux bootstrap/activation passes.
- `/var/lib/degen-dogs/health/install.json` binds that epoch to exact runtime
  and trusted-installer commits plus installer-resolved numeric runner UID/GID.
- `/var/lib/degen-dogs/health/state.json` is the only authoritative lease and
  Linux incident record; its two members advance in one atomic commit.

- [ ] **Step 1: Write RED integration and asset tests**

  Assert the service contains exact root hooks:

  ```ini
  ExecStartPre=+/usr/local/libexec/degen-dogs-wsl-health-state begin-health
  ExecStopPost=+/usr/local/libexec/degen-dogs-wsl-health-state record-health
  ```

  Add fixtures proving a stale candidate cannot satisfy a new invocation,
  mutable and immutable failure-code allowlists are exactly equal, failure
  codes are fixed/sorted, queue no-change remains healthy, a queue/terminal
  failure produces a code rather than PowerShell-side parsing, bootstrap
  receipts/install identity reject a changed install epoch, and health runtime
  preparation failure/retry never prevents the anchor from starting data units.

- [ ] **Step 2: Run focused RED tests**

  ```bash
  PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_wsl_publication_integration.py
  PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_wsl_runner_assets.py
  ```

  Expected: new attempt/install-identity assertions fail.

- [ ] **Step 3: Implement candidate and install wiring**

  Pin both health paths after `.env.local` loading. Write the canonical candidate
  atomically with mode `0600`. Add the recorder to both trusted-asset manifests,
  install it root-owned at `/usr/local/libexec/degen-dogs-wsl-health-state`,
  create the root mode `0700` health directory, render the hooks, and bind the
  bootstrap receipt schema to the install epoch. Bind numeric runner identity
  separately in root-owned `install.json`.
  Anchor preparation warns/fail-opens and retries until successful; it must not
  gate publication units. A probe failure-vocabulary/schema change must update
  and reinstall the trusted helper in the same lifecycle. Do not change anchor
  task triggers or restart settings.

- [ ] **Step 4: Run Phase A Linux release gates**

  ```bash
  PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_record_wsl_runner_health.py
  PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_wsl_publication_integration.py
  PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_wsl_runner_assets.py
  PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_wsl_runner_windows_policy.py
  bash scripts/test_run_runner_health.sh
  git diff --check
  ```

  Run the rendered-systemd isolation gate only in the existing controlled WSL test environment. Expected: all pass and no production service changes occur.

- [ ] **Step 5: Commit the Phase A release boundary**

  ```bash
  git add scripts/check_wsl_runner_health.py scripts/run_wsl_runner_job.sh \
    config/systemd/degen-dogs-health.service.in scripts/install_wsl_runner.sh \
    scripts/install_wsl_startup_task.ps1 scripts/test_wsl_runner_assets.py \
    scripts/test_wsl_publication_integration.py docs/windows-wsl-runner.md
  git commit -m "feat: record durable WSL health leases"
  ```

### Task 3 (optional Phase A): Add a detection-only Windows auditor

**Files:**

- Create: `scripts/audit_wsl_runner.ps1`
- Create: `scripts/test_wsl_runner_audit.ps1`
- Modify: `scripts/install_wsl_startup_task.ps1`
- Modify: `scripts/test_wsl_runner_windows_policy.py`
- Modify: `scripts/test_wsl_runner_assets.py`
- Modify: `docs/windows-wsl-runner.md`
- Modify: `package.json`

**Interfaces:**

- Exact audit result: normalized codes, install/boot identity, lease state, task/instance evidence, `mutation_count=0`.
- Shared pair-lock identity: current SID plus canonical anchor task name.
- Host control phases available in Phase A: `maintenance`, `active`, `failed`.

- [ ] **Step 1: Write RED policy and behavior tests**

  Extract production PowerShell functions and inject COM, CIM, process, WSL, clock, and filesystem actions. Cover zero/one/multiple instances, wrong EnginePID/executable/command, PID reuse, stale/current-boot lease, per-call timeout, lock contention, maintenance neutrality, activation grace, failure-gap reset, malformed/reparse state, interactive capability reporting, and exact audit XML.

  Every fixture must assert:

  ```powershell
  if ($result.MutationCount -ne 0) { throw 'Phase A auditor attempted mutation' }
  ```

- [ ] **Step 2: Run RED Windows tests**

  ```powershell
  powershell.exe -NoLogo -NoProfile -NonInteractive -File scripts/test_wsl_runner_audit.ps1
  python.exe scripts/test_wsl_runner_windows_policy.py
  ```

  Expected: failure because the detection task and exact policies do not exist.

- [ ] **Step 3: Implement only the detection plane**

  Verify the audit source blob against `TrustedInstallerCommit`, render only validated constant names/paths/digests, install below the bounded non-reparse LocalAppData tree with strict ACLs, and register an exact two-minute audit task. Use explicit timeouts for every external operation. The installer holds the shared pair lock for its full lifecycle, disables/stops the exact audit before writing `phase=maintenance`, quiesces the anchor only afterward, records `phase=failed` on rollback, and enables the audit last after successful anchor activation. The auditor acquires the same lock non-blocking and treats contention or maintenance as neutral. Do not add a mutating auditor function or remove the anchor's existing watchdog/retry settings.

- [ ] **Step 4: Prove zero-mutation Phase A behavior**

  Run both RED commands, `python3 scripts/test_wsl_runner_assets.py`, and `git diff --check`. Search the installed auditor action surface and require no `Start-ScheduledTask`, `Stop-ScheduledTask`, `Enable-ScheduledTask`, `Disable-ScheduledTask`, `Unregister-ScheduledTask`, `Stop-Process`, `systemctl start/reset-failed`, or `wsl --terminate` token.

- [ ] **Step 5: Commit the optional detection plane**

  ```bash
  git add scripts/audit_wsl_runner.ps1 scripts/test_wsl_runner_audit.ps1 \
    scripts/install_wsl_startup_task.ps1 scripts/test_wsl_runner_windows_policy.py \
    scripts/test_wsl_runner_assets.py docs/windows-wsl-runner.md package.json
  git commit -m "feat: add detection-only Windows runner audit"
  ```

---

## Phase B — classified at-most-once recovery

Phase B begins only after Phase A is deployed and stable. Each task requires a
fresh review before the next task starts.

### Task 4: Implement the authoritative Windows incident state and pure recovery decision

**Files:**

- Modify: `scripts/audit_wsl_runner.ps1`
- Modify: `scripts/test_wsl_runner_audit.ps1`

**Interfaces:**

- `Get-WslRunnerFaultClass -Evidence $evidence -> recoverable_anchor_absence | recoverable_anchor_liveness | data_dependency | linux_supervision | unsafe_task_process | unreachable_ambiguous | maintenance`
- `Get-WslRunnerRecoveryDecision -State $state -Evidence $evidence -> none | claim_start | claim_restart | await_evidence | recover | latch`
- The pure decision function performs no I/O or mutation.

- [ ] **Step 1: Write RED transition-table tests**

  Test two same-epoch/same-boot failures, success reset, boot/install/gap reset, every nonrecoverable class, claim persistence, crash immediately after claim, awaiting-evidence behavior, and proof that a pre-claim lease cannot recover an incident.

- [ ] **Step 2: Run RED tests** using the Phase A PowerShell command and confirm the missing transition functions fail.

- [ ] **Step 3: Implement canonical private state and the pure classifier**

  Persist schema/install epoch/Windows boot identity/audit times/codes/count/incident ID/claim time/phase under the shared lock. Flush a bounded temporary file and atomically replace it. Windows alone changes `phase` to `claimed`; Linux receives only a mirror.

- [ ] **Step 4: Run tests and commit**

  ```bash
  powershell.exe -NoLogo -NoProfile -NonInteractive -File scripts/test_wsl_runner_audit.ps1
  git diff --check
  git add scripts/audit_wsl_runner.ps1 scripts/test_wsl_runner_audit.ps1
  git commit -m "feat: classify bounded runner recovery"
  ```

### Task 5: Add task-pair transactions and the single bounded recovery action

**Files:**

- Modify: `scripts/audit_wsl_runner.ps1`
- Modify: `scripts/test_wsl_runner_audit.ps1`
- Modify: `scripts/install_wsl_startup_task.ps1`
- Modify: `scripts/test_wsl_runner_windows_policy.py`
- Modify: `scripts/test_wsl_runner_assets.py`
- Modify: `docs/windows-wsl-runner.md`

- [ ] **Step 1: Write RED recovery and pair-transaction tests**

  Cover old single-task upgrade, audit-first isolation, pair lock exclusion, maintenance record before anchor stop, both-new-tasks-disabled attestation, anchor-first activation, audit-enabled-last, registration failure at every boundary, failed-phase persistence, uninstall, per-operation timeout, identity recheck immediately before stop, process disappearance before start, and a second audit never repeating a claimed action.

- [ ] **Step 2: Run RED Windows policy/audit tests** and confirm the new task settings and mutation calls are absent.

- [ ] **Step 3: Implement Phase B task policy and recovery**

  Remove the anchor time trigger and native restart policy. Give the audit Logon plus two-minute repetition, password-mode Startup, `IgnoreNew`, `StartWhenAvailable`, battery-safe/Wake settings, two-minute execution limit, and no native retry. Register/attest both disabled, activate/verify the anchor, then enable the audit last.

  Recovery may execute only after a durable Windows claim and only for the two recoverable classes. Re-resolve the exact task instance and PID identity before stopping. Stop the exact task, wait with a fixed deadline, start it once, write `awaiting_evidence`, and exit. Never enable, unregister, kill, terminate WSL, reset systemd, or wait for a new lease in the recovery invocation.

- [ ] **Step 4: Run complete Windows gates and commit**

  ```bash
  powershell.exe -NoLogo -NoProfile -NonInteractive -File scripts/test_wsl_runner_audit.ps1
  PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_wsl_runner_windows_policy.py
  PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_wsl_runner_assets.py
  git diff --check
  git add scripts/audit_wsl_runner.ps1 scripts/test_wsl_runner_audit.ps1 \
    scripts/install_wsl_startup_task.ps1 scripts/test_wsl_runner_windows_policy.py \
    scripts/test_wsl_runner_assets.py docs/windows-wsl-runner.md
  git commit -m "feat: add at-most-once anchor recovery"
  ```

### Task 6: Prove lifecycle behavior without touching production

**Files:**

- Modify: `scripts/test_wsl_publication_integration.py`
- Modify: `scripts/test_wsl_runner_assets.py`
- Modify: `scripts/test_wsl_runner_windows_policy.py`
- Modify: `scripts/test_wsl_runner_audit.ps1`
- Modify: `package.json`

- [ ] **Step 1: Add safe isolated lifecycle tests**

  Use a GUID-suffixed temporary systemd service for four failures plus `start-limit-hit`. Use injected Windows task/process fixtures for normal gates. Any real Task Scheduler test uses GUID-suffixed inert tasks behind an explicit opt-in flag. Any WSL termination test uses a disposable imported distro; the literal production command `wsl.exe --terminate DegenDogsRunner` is forbidden in the default suite.

- [ ] **Step 2: Add dependency-failure fixtures**

  Simulate DNS, RPC, GitHub, Git authentication, Pages, queue lag, stale terminal publication, and disk failures. For every case assert `mutation_count == 0`, the old lease is not advanced, and the incident is deduplicated.

- [ ] **Step 3: Run all non-destructive release gates**

  ```bash
  npm run test:ops
  PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_wsl_publication_integration.py
  powershell.exe -NoLogo -NoProfile -NonInteractive -File scripts/test_wsl_runner_audit.ps1
  git diff --check
  ```

  Expected: pass with no production task/unit/distro mutation.

- [ ] **Step 4: Commit lifecycle coverage**

  ```bash
  git add scripts/test_wsl_publication_integration.py scripts/test_wsl_runner_assets.py \
    scripts/test_wsl_runner_windows_policy.py scripts/test_wsl_runner_audit.ps1 package.json
  git commit -m "test: prove bounded runner watchdog lifecycle"
  ```

### Task 7: Roll out with explicit phase gates

- [ ] Fast-forward reviewed commits to `main`; never force-push.
- [ ] Build a new immutable trusted bundle and run the installer under the shared pair lock.
- [ ] Verify the installer enters `maintenance` before isolation and leaves `active` only after the anchor is attested and the audit is enabled last.
- [ ] Verify two healthy audit periods, a current-install/current-boot lease, private state ownership/ACLs, clean Git state, and fresh public dashboard data.
- [ ] Perform one controlled exact-anchor absence test and prove one claim, one start, `awaiting_evidence`, and recovery only on the next fresh lease.
- [ ] Keep interactive mode explicitly marked `requires_logged_on=true`. Do not claim logout/reboot uptime from that mode.
- [ ] If the user selects password-backed mode, obtain credentials only through `Get-Credential`, then manually test limited-token task control, reboot/logout survival, invalid-credential reporting, and exact rollback. These are acceptance steps, not default automated tests.
- [ ] Confirm the off-host GitHub freshness workflow remains enabled and detects local silence independently.
