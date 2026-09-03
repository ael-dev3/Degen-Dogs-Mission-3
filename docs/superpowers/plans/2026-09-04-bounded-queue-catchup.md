# Bounded Queue Catch-up Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent valid archive catch-ups from being killed by the queue drainer while retaining strict, independently enforced runtime bounds.

**Architecture:** Parse one canonical operator setting at the queue-drainer entry point, keep the lower-level function injectable, and place systemd's hard timeout five minutes beyond the maximum Python budget. Existing queue durability and child cleanup remain unchanged.

**Tech Stack:** Python 3.12, systemd, Bash/WSL2, PowerShell installer tests.

**Spec:** `docs/superpowers/specs/2026-09-04-bounded-queue-catchup-design.md`

## Global constraints

- Use strict red-green-refactor: observe each new behavior fail for the intended reason before production edits.
- Never weaken queue authentication, validation, Git compare-and-swap, process-group cleanup, or systemd sandboxing.
- Never log or commit protected environment values.
- Do not change archive quorum or concurrency settings in this task.
- Do not alter or delete live queue state while testing.

---

### Task 1: Add a bounded configurable queue runtime

**Files:**
- Modify: `scripts/test_drain_publication_queue.py`
- Modify: `scripts/drain_publication_queue.py`
- Modify: `scripts/test_wsl_publication_integration.py`
- Modify: `scripts/check_wsl_runner_health.py`

**Interfaces:**
- Consumes: `DEGEN_DOGS_QUEUE_RUNTIME_BUDGET_SECONDS` in `main()`.
- Produces: a validated runtime passed as `runtime_budget_seconds` to `drain_publication_queue()`.

- [ ] **Step 1: Add parser and entry-point regressions**

Add focused tests that prove:

```python
assert parse_runtime_budget_seconds(None) == 900.0
assert parse_runtime_budget_seconds("300") == 300.0
assert parse_runtime_budget_seconds("2700") == 2700.0
```

For each of `""`, `"0"`, `"0299"`, `"299"`, `"2701"`, `"+300"`, `"300.0"`, `" 300"`, `"300 "`, `"3e2"`, a non-ASCII digit string, and a string of several thousand ASCII digits, assert `ConfigurationError` with fixed text that does not contain the input. The very-long input must not escape as Python's integer-digit-limit `ValueError` or produce a traceback.

Stub `drain_publication_queue` in a POSIX entry-point test. Prove absent configuration forwards `900.0`, a configured boundary forwards its exact float, and invalid configuration returns `EXIT_CONFIG` without invoking the stub. Preserve and restore the process environment and signal handler.

Seed `DEGEN_DOGS_QUEUE_RUNTIME_BUDGET_SECONDS` in the publisher-environment sanitizer fixture and assert the exact key is absent from the child environment.

Add queue-health compatibility regressions in `scripts/test_wsl_publication_integration.py`: inactive lag becomes stale at 181 seconds; an authenticated active publication is healthy through 1,080 seconds by default and stale at 1,081; a configured 2,700-second budget is healthy through 2,880 seconds and stale at 2,881. Assert unrelated queue-integrity and proof-gap behavior is unchanged.

- [ ] **Step 2: Observe RED**

Run the new functions directly with `PYTHONDONTWRITEBYTECODE=1` and `PYTHONPATH=scripts`. Expected: fail because the parser and configured forwarding do not exist, the current default is 240 seconds, and active health still expires at 300 seconds.

- [ ] **Step 3: Implement the entry-point parser**

In `scripts/drain_publication_queue.py`:

```python
DEFAULT_RUNTIME_BUDGET_SECONDS = 900.0
MIN_QUEUE_RUNTIME_BUDGET_SECONDS = 300
MAX_QUEUE_RUNTIME_BUDGET_SECONDS = 2700
QUEUE_RUNTIME_BUDGET_ENV = "DEGEN_DOGS_QUEUE_RUNTIME_BUDGET_SECONDS"
```

Add a side-effect-free parser that accepts `None` or a canonical ASCII positive decimal string, applies the inclusive bounds, returns a float, and otherwise raises `ConfigurationError` with fixed text. Parse only in `main()` and pass the value as `runtime_budget_seconds`. Print only the fixed configuration diagnostic and return 78 on rejection.

Keep `drain_publication_queue()`'s existing positive-value validation so short injected test budgets remain valid. Confirm the queue-budget environment key is removed by the existing dynamic child-field sanitizer.

In `scripts/check_wsl_runner_health.py`, consume the drainer's parser and constants so there is one accepted-value contract. Pass an active queue stale limit of the validated budget plus 180 seconds into `publication_health_summary()`. Preserve the 180-second inactive limit. Invalid queue-budget configuration must fail health with fixed, non-echoing configuration text. Do not use the watcher-pending threshold for this calculation.

- [ ] **Step 4: Run focused and full GREEN tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts python3 -c 'import test_drain_publication_queue as t; t.test_runtime_budget_parser_accepts_default_and_boundaries(); t.test_runtime_budget_parser_rejects_noncanonical_and_out_of_range_values(); t.test_main_forwards_validated_runtime_budget_and_rejects_invalid_configuration()'
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_drain_publication_queue.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_wsl_publication_integration.py
```

Expected: all pass, including existing injected 100-second deadline tests and child-environment sanitization assertions.

- [ ] **Step 5: Commit Task 1**

```bash
git add scripts/drain_publication_queue.py scripts/test_drain_publication_queue.py scripts/check_wsl_runner_health.py scripts/test_wsl_publication_integration.py
git commit -m "fix: bound queued archive catch-up runtime"
```

### Task 2: Align the service hard timeout and operator contract

**Files:**
- Modify: `scripts/test_wsl_runner_assets.py`
- Modify: `config/systemd/degen-dogs-publisher.service.in`
- Modify: `config/wsl-runner.env.template`
- Modify: `docs/windows-wsl-runner.md`

**Interfaces:**
- Consumes: the Task 1 budget contract.
- Produces: a 50-minute systemd ceiling and documented 15-minute default/45-minute maximum.

- [ ] **Step 1: Add asset regressions**

Change the publisher asset assertion to require exactly `TimeoutStartSec=50min`. Add assertions that the environment template contains exactly one canonical default assignment:

```text
DEGEN_DOGS_QUEUE_RUNTIME_BUDGET_SECONDS=900
```

and documents the accepted 300-to-2,700-second range. Assert the operations guide describes the same default, range, and fail-closed behavior.

The template and guide contract is evaluated after `load_runner_env.sh` normalizes unquoted assignment whitespace. Quoted or embedded whitespace still reaches the parser and is rejected.

- [ ] **Step 2: Observe RED**

Run the focused asset test(s) directly in WSL. Expected: fail because the service still says `5min` and the setting is absent from the template and guide.

- [ ] **Step 3: Update service and documentation assets**

Set only publisher `TimeoutStartSec=50min`; leave stop timeout, kill mode, and sandbox directives unchanged. Add the default setting and range comment to the template. Document the operational setting next to the queued publisher description and explicitly state that malformed/out-of-range values prevent the drainer from starting.

- [ ] **Step 4: Run GREEN verification**

Use the production-equivalent unprivileged trusted WSL Python runtime where required:

```bash
PYTHONDONTWRITEBYTECODE=1 /var/lib/degen-dogs/python-runtime/bin/python3 scripts/test_wsl_runner_assets.py
```

Then run the exact repository package entry points below. Run the ordinary asset and publication suites with the unprivileged trusted WSL runtime. Run `test:wsl-runner-isolation` from native Windows PowerShell; that test deliberately delegates only its rendered-systemd fixture to root inside `DegenDogsRunner`. Expected: all pass, including the existing post-archive `chmod -R go-w` fixture.

```bash
PYTHONDONTWRITEBYTECODE=1 PATH=/var/lib/degen-dogs/python-runtime/bin:/usr/bin:/bin npm run test:wsl-runner-assets
PYTHONDONTWRITEBYTECODE=1 PATH=/var/lib/degen-dogs/python-runtime/bin:/usr/bin:/bin npm run test:wsl-publication-integration
```

From native Windows PowerShell, run the privileged-isolation delegator separately:

```powershell
npm run test:wsl-runner-isolation
```

- [ ] **Step 5: Commit Task 2**

```bash
git add scripts/test_wsl_runner_assets.py config/systemd/degen-dogs-publisher.service.in config/wsl-runner.env.template docs/windows-wsl-runner.md
git commit -m "fix: extend bounded publisher service window"
```

### Task 3: Combined verification, exact-commit rollout, and outcome proof

**Files:**
- Verify and deploy only; do not hand-edit generated artifacts or queue state.

- [ ] **Step 1: Run focused suites**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_drain_publication_queue.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_wsl_runner_assets.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_wsl_publication_integration.py
```

- [ ] **Step 2: Run repository checks**

```bash
npm run test:dashboard
npm run build
git diff --check origin/main...HEAD
git status --short
```

- [ ] **Step 3: Publish the reviewed source commit non-forcing**

Fetch `origin/main`, require it to equal the recorded expected parent, rebase only if an independently generated data commit advanced main, rerun affected tests, and push with compare-and-swap/non-forcing semantics. Require GitHub CI and Pages to complete for the exact source commit before privileged rollout.

- [ ] **Step 4: Upgrade and activate the exact reviewed commit**

Follow the detached-checkout upgrade block in `docs/windows-wsl-runner.md` exactly: clone a new isolated no-checkout bootstrap directory, detach at the exact 40-character reviewed commit on public `main`, verify exact HEAD and a clean tracked tree, mark its installer read-only, then invoke `scripts/install_wsl_startup_task.ps1` with `-UpgradeTrustedBundle`, `-TrustedInstallerCommit <exact-sha>`, and `-Activate` using the already configured task mode and credential policy. Do not execute a mutable working-tree installer or reuse an older bootstrap receipt.

- [ ] **Step 5: Attest the activated runtime and observe automatic resumption**

The exact upgrade/activation lifecycle verifies the rendered unit and protected environment before it creates the runtime activation marker. Capture that installer evidence. Activation then starts the publisher path/timer, whose first timer event may automatically begin draining the preserved queue after ten seconds; do not assume publication is still paused and do not start a second drain.

Immediately attest that the installed publisher unit has exactly `TimeoutStartSec=50min`, the effective protected non-secret setting is `DEGEN_DOGS_QUEUE_RUNTIME_BUDGET_SECONDS=900`, and the runner checkout and trusted bundle equal the reviewed public-main SHA. Verify activation/path/timer units are healthy and record whether the publisher is waiting, running, or already successful. Keep the queue and journal untouched.

- [ ] **Step 6: Complete and prove the preserved real publication**

Observe the single drain automatically resumed by activation; if it already completed, use its authenticated durable state and service result. Do not manually start a competing drain or create synthetic queue work. Observe the existing authenticated recovery journal through successful generation, validation, non-forcing Git publication, Pages verification, and finalization. Require:

- publisher service `Result=success` and exit status zero;
- queue lag zero and handled generation equal to the durable latest generation;
- no pending publication or recovery journal;
- remote `main`, the Pages deployment, and the public live bundle all identify the same current auction tuple and compatible block;
- health reports no queue, service, local-freshness, or remote-freshness problem.

Record timings and final state. Completion of the preserved incident is the end-to-end proof; do not delay restoration with an artificial four-minute sleep or mutate live state for a synthetic timeout demonstration.
