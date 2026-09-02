# Independent Runner Watchdog Design

## Purpose

The runner needs a recovery and incident plane independent of the long-running
WSL anchor. An active systemd timer or a Task Scheduler state of `Running` is
not proof that health checks still succeed or that data reaches the public
dashboard.

The watchdog is local, deterministic, programmatic, non-agentic, and has no
LLM or agent-runtime dependency. GitHub's off-host freshness workflow remains a
separate incident plane.

## Delivery strategy

Recovery is split into two independently releasable phases. Restoring and
validating the public dashboard does not wait for either phase.

### Phase A: durable evidence, zero recovery mutation

Phase A ships the root-owned health attempt, lease, and incident recorder. It
may also ship a separate detection-only Windows audit task after its task XML,
source provenance, state-file handling, and installer coordination tests pass.

The Phase A Windows audit may inspect exact task/process state, call the fixed
root snapshot command, write private audit state, and emit a pre-registered
Event Log event. It must not call any operation that enables, disables, starts,
stops, unregisters, kills, or terminates a task, process, service, unit, or WSL
distro. The existing anchor triggers and native retry policy remain unchanged
during Phase A.

Phase A is a safe same-day boundary: it improves diagnosis and preserves
evidence without changing recovery behavior. The Linux recorder alone may ship
if the optional Windows audit would delay public recovery.

### Phase B: classified, at-most-once anchor recovery

Phase B removes the anchor's repeated trigger and native restart loop and
allows the independent Windows audit to perform one bounded recovery attempt.
It may ship only after all of these prerequisites are implemented and tested:

- one SID/task-pair lock shared by the installer and auditor;
- an installer maintenance/install epoch that makes planned quiescence neutral;
- one Windows-only authoritative, durable, at-most-once recovery claim;
- a lease bound to the active install epoch, WSL boot, and health invocation;
- a fault-classification table that permits mutation only for exact anchor
  liveness faults;
- explicit per-operation and total execution timeouts; and
- fail-closed two-task registration, activation, rollback, and uninstall
  invariants.

Phase B never terminates the runner distro, broadly kills `wsl.exe`, enables a
disabled task, resets a failed systemd service, or treats a data/dependency
failure as an anchor fault.

## Invariants

- The mutable checkout never executes as Linux root.
- Only an immutable root-owned helper writes the Linux `install.json` and
  authoritative combined `state.json` records.
- A failed, timed-out, missing, malformed, replayed, or mismatched health
  candidate never advances the lease.
- Linux health success can clear only the Linux health incident subsection. It
  cannot erase a Windows audit incident or recovery claim.
- The Windows state record is the only authority that may grant one recovery
  attempt. Linux audit state is a mirror and never grants permission.
- Recovery semantics are at-most-once. A crash after the claim is durably
  written may result in no recovery action, but never a second action.
- Planned installer maintenance is neither a failed audit nor a recovery
  opportunity.
- Queue and publication policy has one implementation in the existing Python
  health probe. PowerShell never reinterprets queue records.
- All task and process matches are exact and case-aware where Windows permits;
  ambiguous, foreign, multiple, linked, or malformed state fails closed.

## Linux health attempt and candidate

The health service uses an immutable helper at
`/usr/local/libexec/degen-dogs-wsl-health-state` in both lifecycle hooks:

1. `ExecStartPre=+... begin-health` removes any old candidate and atomically
   creates a root-owned attempt record under `/run/degen-dogs/health/`.
2. The unprivileged repository health probe reads that attempt and atomically
   writes `/var/cache/degen-dogs/health-report.json`.
3. `ExecStopPost=+... record-health` consumes the fixed systemd result fields,
   validates the attempt and candidate, and updates the root-owned state.

The attempt contains an unpredictable bounded token, the systemd invocation
ID, active install epoch, and current WSL boot ID. The candidate must echo the
token and invocation ID. Candidate deletion alone is not accepted as replay
protection.

The candidate is canonical bounded JSON with exactly these fields:

```json
{
  "attempt_token": "64 lowercase hex characters",
  "checked_at_utc": "strict UTC second",
  "failure_codes": ["normalized_code"],
  "invocation_id": "32 lowercase hex characters",
  "latest_generated_block": 50789720,
  "publication_generation": 5,
  "runner_head": "40 lowercase hex characters",
  "schema_version": 1,
  "status": "healthy"
}
```

`latest_generated_block` and `publication_generation` may be `null` only when
the corresponding validated local state does not yet exist. Failure codes come
from a fixed allowlist, are sorted and deduplicated, and never contain paths,
URLs, exception text, credentials, or arbitrary provider output.
The mutable probe allowlist must exactly equal the immutable recorder allowlist.
Any vocabulary or candidate-schema change therefore requires a reviewed trusted
helper reinstall in the same lifecycle as the mutable probe update.

The recorder opens the cache directory and candidate with pinned descriptors
and no-follow semantics. It validates expected runner UID/GID, mode `0600`, one
hard link, a small fixed maximum size, canonical bytes, exact schema, attempt
identity, and systemd's `SERVICE_RESULT`, `EXIT_CODE`, and `EXIT_STATUS`. Lease
advancement requires all result fields and the candidate to independently say
success.

## Root-owned install identity, lease, and incident state

The installer writes a root-owned install identity at
`/var/lib/degen-dogs/health/install.json`. It contains a schema version, one
new install epoch, the exact runtime commit, and the exact trusted-installer
commit, plus the installer-resolved numeric runner UID and GID. The health
helper validates but does not infer this identity from the mutable checkout.
Root-only snapshot and runtime-preparation modes derive their runner identity
from this root-owned record and do not depend on the runner-owned cache path.

The helper is the sole writer of these files beneath a root-owned mode `0700`
directory:

- `state.json`, mode `0600`: one canonical authoritative record containing
  both `last_good` and `incident`; `last_good` carries install epoch, WSL boot
  ID, systemd invocation ID, root-recorded UTC time, boot-monotonic completion
  time, runner HEAD, latest generated block, and publication generation, while
  `incident` carries independent `health` and `audit_mirror` subsections plus
  one bounded last-recovery summary; and
- `state.lock`, mode `0600`: a never-unlinked `flock` serialization point.

Lease and incident changes are assembled in memory and installed as one
canonical bounded `state.json` replacement under the lock, with file fsync,
atomic replace, and parent fsync. A failed replacement leaves the prior lease
and incident jointly authoritative. Legacy `last-good.json` and `incident.json`
are never read; a successful install-identity migration removes those obsolete
fixed names before writing the current identity.

The immutable `snapshot` mode reads no path from arguments or environment. It
compares the lease with the current root-owned install identity and current WSL
boot ID, rejects monotonic regression, computes age from `CLOCK_BOOTTIME`, and
prints a bounded public-safe snapshot. Windows does not calculate lease age by
subtracting its wall clock from a WSL timestamp.

A lease is currently valid only when all of the following are true:

- its install epoch and exact runtime/trusted commits match `install.json`;
- its boot ID matches the current WSL boot;
- its boot-monotonic completion is not in the future;
- its computed age is no greater than 480 seconds; and
- the root state files pass type, owner, mode, link-count, size, schema, and
  canonical-byte validation.

After a restart claim, recovery evidence must additionally have a health
completion later than that claim. A fresh-looking lease from before the claim
cannot clear the incident.

Health failures preserve the first failure time, keep the last failure time
nondecreasing, advance the consecutive count, and replace normalized codes only
within the health subsection. A later success records a bounded recovery
summary before clearing that subsection. If the WSL wall clock moves backward,
durable health-failure and recovery display timestamps are clamped to the
preceding durable health boundary; lease validity and age continue to use only
`CLOCK_BOOTTIME`. Audit
mirror updates never change the authoritative Windows claim.

The root anchor attempts volatile health-runtime preparation before starting
units, but preparation failure is detection-only: it warns and still starts the
existing data units. It retries preparation once per anchor cycle until it
succeeds. A missing or failing helper therefore makes the health service report
failure without becoming a publication startup dependency.

## Windows task-pair control plane

The anchor and audit tasks form one managed pair. Their exact root task names,
distro name, current SID, expected executables, arguments, triggers,
principals, and settings are rendered from one reviewed install operation.

### Shared pair lock and maintenance epoch

The installer and auditor use the same exclusive lock in the bounded Degen Dogs
LocalAppData tree. Its identity is derived from the current SID and canonical
anchor task name. The installer holds it for the complete task-pair and WSL
lifecycle. The auditor uses a non-blocking acquisition and exits successfully
without changing counters when the lock is held.

Before the installer isolates the anchor, it disables/stops the exact managed
audit task and atomically writes a private host control record with operation
ID, target commits, start time, and `phase=maintenance`. It then isolates the
anchor and quiesces WSL. A failed install leaves `phase=failed`, both tasks
isolated, the activation markers absent, and every Linux activation unit
inactive. It does not restore an old anchor over partially upgraded Linux
assets.

On successful activation, the installer writes the same install epoch into the
Linux install identity and Windows control state, verifies the anchor, and
enables the audit last. The first audit observes an explicit activation grace;
planned maintenance, task registration, and initial health startup never count
as consecutive failures.

### Detection-only Phase A audit

The optional Phase A audit verifies and records:

- both exact managed task definitions and current principal mode;
- exact anchor task state and zero/one/multiple running instances;
- when one instance exists, its Task Scheduler instance identity, EnginePID,
  creation time, exact System32 executable, and exact command line;
- bounded WSL snapshot reachability;
- anchor and activation markers;
- required systemd unit load/enabled/active state; and
- the root helper's install/boot/invocation-bound lease and normalized incident
  codes.

Every COM, CIM, process, and `wsl.exe` call has its own timeout, and the complete
audit normally finishes within 45 seconds and is hard-limited to two minutes.
Phase A records findings only.

### Phase B authoritative incident and recovery state

The Windows audit owns one private canonical record with:

- schema and install epoch;
- validated Windows boot identity and last audit time;
- first/last failure times and normalized fault codes;
- consecutive failure count;
- incident ID;
- recovery claim time; and
- phase: `healthy`, `failed`, `claimed`, `stop_requested`, `start_requested`,
  `awaiting_evidence`, `recovered`, or `latched`.

The state directory rejects reparse points and unsafe ACLs. Writes use an
exclusive lock, bounded canonical JSON, a flushed temporary file, and atomic
replacement. If state is missing unexpectedly, malformed, linked, outside the
known-folder boundary, or cannot be durably replaced, the auditor records what
it safely can and performs no recovery.

Failures are consecutive only within the same install epoch and Windows boot,
without an intervening success, and with no audit gap greater than three audit
periods. Otherwise the next failure starts a new sequence at one.

On the second consecutive recoverable failure, the auditor durably writes
`phase=claimed` before any mutation. Later invocations never issue another
claim for that incident. A successful stop/start request changes the phase to
`awaiting_evidence` and exits; the next audit, not the recovery invocation,
proves the new lease and records recovery.

### Fault classification

| Class | Examples | Phase B action |
| --- | --- | --- |
| Recoverable anchor absence | Exact enabled managed anchor has zero instances | Claim once, start exact task once |
| Recoverable anchor liveness | Exactly one fully attested anchor instance; bounded WSL is responsive; markers or activation units fail twice | Claim once, stop exact task, prove it stopped, start exact task once |
| Data/dependency health | RPC, DNS, GitHub, Git authentication, Pages, queue lag, stale terminal publication, disk, or repository health | Latch and report; never restart anchor |
| Linux supervision | Health service `start-limit-hit` or another failed worker | Latch and report; no implicit `reset-failed` |
| Unsafe task/process | Wrong or disabled task, XML mismatch, multiple instances, wrong EnginePID/path/command, PID identity change | Latch and report; never mutate |
| Unreachable/ambiguous WSL | Any WSL query timeout, malformed snapshot, boot/install mismatch | Latch and report; never stop, start, kill, or terminate |
| Planned maintenance | Pair lock held or host control phase is `maintenance` | Neutral success; do not change incident state |

Before stopping an anchor, the auditor re-reads the exact Task Scheduler
instance and process identity and requires them to equal the previously
attested values. It stops only that exact managed task, waits a bounded time for
the instance and PID to disappear, then starts the same exact task once. A
timeout or identity change latches the incident. It never enables a disabled
task, calls `wsl.exe --terminate`, or kills a process tree.

## Scheduled Task policy and interactive limitation

In Phase B, the long-running anchor has only a current-user Logon trigger, plus
a Startup trigger in password-backed mode. It uses `IgnoreNew`, unlimited
execution time, no repeated time trigger, and no native restart-on-failure.

The audit has Logon and two-minute repetition triggers, plus Startup in
password-backed mode. It uses `IgnoreNew`, `StartWhenAvailable`, battery-safe
settings, `WakeToRun`, a two-minute execution limit, and no native retry.
Startup/logon activation grace prevents simultaneous task startup from counting
as failure.

Interactive-token mode cannot run or report after logout and cannot provide
pre-login recovery. It records `requires_logged_on=true` and Event Log
integration as available or degraded while it is running. Silence after logout
is observable only through the independent off-host freshness workflow.

Password-backed mode is the recommended unattended choice. It uses credentials
entered directly through the Windows credential prompt for the exact current
account because WSL distributions are registered per user. Credentials never
appear in repository files, task arguments, logs, environment variables, WSL,
or watchdog state. Real limited-token access to the password-backed anchor must
pass acceptance testing before that mode is declared supported.

## Trusted Windows and Linux assets

Every new Linux root-consumed asset is included in the independently fetched
trusted-bundle manifest, digest, ownership/mode checks, and immutable installer
copy. The root recorder is self-contained standard-library code and never
imports from the mutable checkout.

The Windows auditor source is resolved from the exact
`TrustedInstallerCommit`, compared to its Git blob before rendering, installed
at one bounded non-reparse path with a strict ACL, and bound to an exact SHA-256
and exact Task Scheduler action. Its action uses the exact System32 PowerShell
and contains only reviewed constant arguments. LocalAppData mode protects
against accidental drift, not a malicious current Windows account; that
account already owns the per-user WSL registration and can invoke WSL root.

Task-name derivation reserves space for the audit suffix and rejects
case-insensitive collisions, path separators, control characters, and names
outside the reviewed length bound.

## Detection and recovery bounds

The Linux health timer normally runs every five minutes. Its current scheduling
jitter and 90-second probe timeout can consume approximately 420 seconds, so a
420-second stale threshold has no safety margin. The watchdog uses a
480-second lease threshold.

With a two-minute Windows audit interval, a valid lease becoming silent is
detected within a nominal maximum of 600 seconds: 480 seconds to become stale
plus at most 120 seconds to the next audit. This bound excludes machine-off,
sleep states that Windows does not wake from, logged-out interactive mode,
invalid stored credentials, and Task Scheduler or OS failure.

Exact anchor absence or marker failure is detected on the next audit. Requiring
two consecutive recoverable failures permits one recovery claim within a
nominal four minutes. Recovery evidence is checked on the following audit, so
the recovery call never waits for a full health period inside its two-minute
execution limit.

## Incident visibility

Linux keeps the last-good lease and first/last health failure evidence across
WSL restarts. Windows keeps the authoritative audit incident and recovery claim
even when WSL is unreachable. Elevated installation may register one dedicated
Application Event Log source; non-elevated installation reports Event Log as a
degraded capability and retains private state plus Scheduled Task history.

The off-host GitHub freshness workflow remains active. A local task cannot
monitor itself while Windows is off, the interactive user is logged out, stored
credentials fail, or Task Scheduler itself is broken.

## Test strategy and safety

Default tests must not stop the production task, terminate
`DegenDogsRunner`, reboot Windows, log out the user, register a machine-wide
Event Log source, or alter production systemd units.

- Linux state tests use temporary directories and an isolated uniquely named
  systemd fixture for real `start-limit-hit` behavior.
- Windows policy tests extract production functions and inject fake COM, CIM,
  clock, process, WSL, and state actions.
- Optional host integration tests use GUID-suffixed temporary tasks and a
  disposable temporary WSL distro. They require an explicit flag and clean up
  only exact attempt-owned resources.
- Reboot/logout survival, stored-credential behavior, and elevated Event Log
  registration are manual acceptance tests with pre-recorded rollback steps.
- DNS, RPC, GitHub, Git authentication, and queue failures are deterministic
  fixtures; tests do not intentionally break host networking.

Tests cover candidate replay and invocation mismatch, install/boot mismatch,
backward wall-clock clamping, monotonic regression, malformed/linked state,
single-record atomicity, legacy split-state migration, health-preparation
fail-open/retry behavior, maintenance races,
installer/auditor lock exclusion, failure-gap reset, claim crash boundaries,
wrong/multiple/PID-reused task instances, per-operation timeout, pair rollback,
and proof that data/dependency faults never call a mutating action.

## Success criteria

### Phase A

- A successful current-invocation health probe creates a valid boot/install-
  bound lease.
- Failed, missing, timed-out, malformed, or replayed probes preserve the prior
  lease and update a durable incident.
- Four repeated health failures and `start-limit-hit` remain visible even while
  the timer is active.
- The optional Windows audit emits durable detection evidence and provably
  executes zero recovery mutations.
- Existing runner scheduling and publication behavior are unchanged.

### Phase B

- Planned installer maintenance can never trigger recovery.
- Exact anchor liveness failure gets at most one recovery claim; persistent
  faults latch without a restart loop.
- Data, dependency, queue, and start-limit failures never restart the anchor.
- Recovery clears only after a current-install/current-boot lease completed
  after the claim.
- Nominal lease-silence detection is no more than ten minutes while the audit
  principal is runnable.
- Password-backed acceptance resumes checks after reboot/logout without an
  interactive Codex or shell session.
