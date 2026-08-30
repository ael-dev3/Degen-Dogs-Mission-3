# Pages Data Fast-Lane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce verified current-data Git push-to-GitHub-Pages latency without allowing a source, workflow, dependency, UI-shell, or ambiguous commit to bypass the existing full Pages gate.

**Architecture:** A canonical commit-policy module recognizes exactly one trusted `current` runner commit with an exact generated-data path set. A Pages classifier fail-closes to the full gate unless the candidate has one parent, matches the current remote `main`, has unchanged non-publish source and generated UI shell fingerprints relative to a previously successful full-source gate, and has valid runner provenance. Full and data gates produce the same Pages artifact. A single serialized deployment controller checks branch head immediately before/after deployment so an older artifact cannot overwrite a newer snapshot, and safely re-dispatches only the current branch head when needed.

**Tech Stack:** Python 3.14 standard library, Bash, GitHub Actions, GitHub Checks/Actions REST APIs, existing Vite build and dashboard validators, pinned Actions.

**Depends on:** The browser live-auction plan must land first, because the full source/UI-shell baseline must include its external module, CSP, and generated page structure.

**Spec:** `docs/superpowers/specs/2026-08-30-low-latency-auction-data-design.md`

## Non-negotiable Constraints

- The full lane remains the default. Any parser/API/history/fingerprint/check-run failure, missing baseline, non-main event, source/workflow/dependency change, merge, multi-commit push, or unexpected path selects `full`, never `data`.
- Data-lane eligibility requires a one-parent candidate where its only parent is exactly the push `before` SHA and the push introduced exactly that one commit. It must have `Refresh-Run-ID`, `Refresh-Runner-ID`, and `Refresh-Run-Scope: current` trailers exactly once, plus changes only in the canonical publish allowlist.
- The data lane may inherit only from a successful `pages-full-source-gate` check run issued by GitHub Actions on a first-parent ancestor with the same non-publish source fingerprint and UI-shell fingerprint. A prior data gate is never a trust baseline.
- The canonical publish allowlist remains owned by one Python policy module. The publisher retains its local `PUBLISH_PATHS` array only as a mutation/rollback pathspec, and must call the canonical validator before pushing.
- Build/test classifications do not cancel deployments. Only the controller has `concurrency.group: pages-deploy` with `cancel-in-progress: false`; it rechecks remote `main` before deployment and never deploys an artifact whose SHA is not the current head.
- No job other than the controller gets write permissions. Tokens are never printed or passed via artifacts. The classifier outputs only validated SHAs, fingerprints, lane, and a public-safe reason.

---

## File Structure

- Create: `scripts/runner_commit_policy.py` — canonical runner trailer/path/parent policy with a small CLI.
- Create: `scripts/pages_deploy_policy.py` — fail-closed event classification, source/UI fingerprinting, successful-baseline lookup, and controller safety helpers.
- Create: `scripts/run_pages_data_validation.sh` — focused, parallel, aggregate-fail data validation.
- Create: `scripts/test_runner_commit_policy.py`, `scripts/test_pages_deploy_policy.py`, `scripts/test_run_pages_data_validation.sh`, and `scripts/test_pages_workflow_policy.py`.
- Modify: `scripts/refresh_and_publish.sh` — call canonical runner commit validation rather than duplicate parser code.
- Modify: `scripts/build_dashboard.py` — add exact generated-data region markers for shell fingerprinting.
- Modify: `scripts/check_dashboard_ui.py` — validate marker shape and generated shell fingerprint; preserve browser module/CSP checks.
- Modify: `scripts/test_refresh_and_publish.sh`, `scripts/test_build_dashboard.py`, `scripts/test_check_remote_freshness.py`, `scripts/test_run_pages_validation.sh`, `scripts/run_pages_validation.sh`, and `package.json`.
- Replace: `.github/workflows/deploy-pages.yml` with classify, full gate, data gate, and serialized deployment-controller jobs.
- Create: `docs/operations/pages-data-fast-lane.md` — eligibility, observability, rollback, and incident procedure.

## Stable Policy Interfaces

```python
# scripts/runner_commit_policy.py
@dataclass(frozen=True)
class RunnerCommit:
    commit: str
    parent: str
    run_id: str
    runner_id: str
    run_scope: str
    changed_paths: tuple[str, ...]

def is_publish_path(path: str) -> bool: ...
def read_runner_commit(repo: Path, commit: str) -> RunnerCommit: ...
def validate_runner_commit(
    repo: Path, commit: str, *, expected_parent: str | None = None,
    expected_run_id: str | None = None, expected_runner_id: str | None = None,
    expected_scope: str | None = None,
) -> RunnerCommit: ...
```

```python
# scripts/pages_deploy_policy.py
@dataclass(frozen=True)
class DeployDecision:
    lane: Literal["full", "data"]
    reason: str
    candidate_sha: str
    parent_sha: str
    baseline_sha: str | None
    source_fingerprint: str | None
    ui_shell_fingerprint: str | None

def classify_push(repo: Path, *, ref: str, before: str, after: str, api: ChecksApi) -> DeployDecision: ...
def source_fingerprint(repo: Path, commit: str) -> str: ...
def ui_shell_fingerprint(repo: Path, commit: str) -> str: ...
```

The classifier CLI reads `GITHUB_TOKEN` only from the environment, validates every SHA before using it in Git/API calls, and emits one JSON object matching `DeployDecision` to `$GITHUB_OUTPUT` through a workflow wrapper. It must never treat its own process environment, GitHub event text, or a commit message as shell code.

## Task 1: Centralize runner-commit provenance and publish-path policy

**Files:**

- Create: `scripts/runner_commit_policy.py`
- Create: `scripts/test_runner_commit_policy.py`
- Modify: `scripts/refresh_and_publish.sh`, `scripts/test_refresh_and_publish.sh`

- [ ] **Step 1: Add failing policy tests in temporary Git repositories**

  Cover a valid `current` commit; merge/multi-parent; no parent; parent mismatch; empty diff; unexpected path; path spelling/rename edge cases; missing, duplicate, body-only, malformed, and conflicting trailers; wrong run ID/runner ID/scope; and a valid archive scope rejected by a `current` expectation. Assert `git interpret-trailers --parse` is the sole trailer parser, not ad hoc substring matching.

- [ ] **Step 2: Run the test and confirm it fails before the module exists**

  ```powershell
  python scripts/test_runner_commit_policy.py
  ```

- [ ] **Step 3: Implement the canonical policy module**

  Move the exact publish-path and trailer semantics currently duplicated in `refresh_and_publish.sh` into `runner_commit_policy.py`. Require precisely one parent, at least one change, no rename/path outside the allowlist, and exact validated trailers. Give it `validate`, `trailers`, and `paths` CLI subcommands for shell callers, with stable non-secret error messages and no mutation capability.

- [ ] **Step 4: Replace shell parser duplication without changing rollback scope**

  Make the publisher call the CLI everywhere it currently validates local or remote runner commits. Keep the shell `PUBLISH_PATHS` array for staging, cleanup, and rollback only; add a test that it cannot silently diverge from `runner_commit_policy.is_publish_path()`.

- [ ] **Step 5: Run focused policy/publisher tests and commit**

  ```powershell
  python scripts/test_runner_commit_policy.py
  bash scripts/test_refresh_and_publish.sh
  git add scripts/runner_commit_policy.py scripts/test_runner_commit_policy.py scripts/refresh_and_publish.sh scripts/test_refresh_and_publish.sh
  git commit -m "refactor: centralize runner commit policy"
  ```

## Task 2: Define an exact data/UI boundary and fail-closed classifier

**Files:**

- Create: `scripts/pages_deploy_policy.py`, `scripts/test_pages_deploy_policy.py`
- Modify: `scripts/build_dashboard.py`, `scripts/test_build_dashboard.py`, `scripts/check_dashboard_ui.py`

- [ ] **Step 1: Add failing fingerprint and classification tests**

  Create temporary repositories and fake Checks APIs. Cover a valid one-commit current runner push inheriting a trusted full baseline; multi-commit push; zero/missing `before`; a source, workflow, dependency, any external module import introduced by the browser stage, style, script, CSP, static shell, file-mode, or unknown marker change; data-only current-card change; no baseline; skipped/neutral/current-run/failed/untrusted full check; API error; shallow/missing history; invalid SHA; and first-parent baseline inheritance. Add a fixture based on the real changed path set from runner commit `974c28881b5be80187aa0834a2edcd8639dc0b34` so policy continues to accept actual current Windows-generated paths. Every uncertain case must return `full` with a reason instead of raising an unsafe `data` result.

- [ ] **Step 2: Add strict generated-data region markers**

  In `write_html()`, wrap only data-generated regions with exact paired HTML comments, each appearing once and never nested:

  - `current-dog`
  - `current-detail`
  - `current-rewards`
  - `current-traits`
  - `current-dog-stage`
  - `archive-bootstrap`
  - `metrics-bootstrap`

  The wrapper, styles, CSP, any external live-module import introduced by the browser stage, inline module bytes, toolbar, and all static tokens remain outside markers. Markers may normalize only explicit data values/child content: never wrapper tag names, attribute names, CSP-bearing nodes, or script/style nodes. For example, `current-dog-stage` may normalize the anchor's dynamic `href` value and text, but the `<a>` tag, attribute names, and every static attribute stay in the shell fingerprint. Add parser tests that reject missing/duplicate/nested/unknown/malformed markers and attempts to hide structural markup inside a data region.

- [ ] **Step 3: Implement deterministic fingerprints and baseline lookup**

  `source_fingerprint()` must hash a path-sorted, NUL-safe `git ls-tree -r -z --full-tree` tuple of `(mode, type, object ID, path)` for every path that is not a canonical publish path. `ui_shell_fingerprint()` must parse the committed `index.html`, replace each valid dynamic region with a named sentinel, and hash the normalized byte stream. It must include exact `doctype`, CSP, styles, inline module, external module import, and static DOM structure.

  `classify_push()` must require `refs/heads/main`, nonzero valid SHAs, `after == HEAD`, one introduced commit, sole parent exactly `before`, valid current runner provenance, and equal fingerprints. Walk first-parent ancestors with matching fingerprints and query exact `pages-full-source-gate` checks; accept only an earlier completed/successful check issued by `github-actions`. A skipped, neutral, in-progress, failed, or current-workflow check never becomes a baseline. Missing history/API response, pagination ambiguity, or a nonmatching app selects `full`.

- [ ] **Step 4: Run classifier/dashboard tests and commit**

  ```powershell
  python scripts/test_pages_deploy_policy.py
  python scripts/test_build_dashboard.py
  python scripts/check_dashboard_ui.py
  git add scripts/pages_deploy_policy.py scripts/test_pages_deploy_policy.py scripts/build_dashboard.py scripts/test_build_dashboard.py scripts/check_dashboard_ui.py
  git commit -m "feat: classify trusted Pages data updates"
  ```

## Task 3: Build the focused data validation gate

**Files:**

- Create: `scripts/run_pages_data_validation.sh`, `scripts/test_run_pages_data_validation.sh`
- Modify: `scripts/run_pages_validation.sh`, `scripts/test_run_pages_validation.sh`, `package.json`

- [ ] **Step 1: Add failing data-gate runner tests**

  Assert the script starts exactly these four read-only validators once, in parallel, with private per-command logs and deterministic final output: `python3 scripts/refresh_telemetry.py validate-status`, `python3 scripts/build_live_snapshot_bundle.py --validate-only`, `python3 scripts/validate_dashboard_consistency.py`, and `python3 scripts/check_dashboard_ui.py`. Test a failure in each command, parallel barrier behavior, no early success, no full publisher/watcher/archive unit tests, and no secret-bearing log dump.

- [ ] **Step 2: Implement aggregate-fail focused validation**

  Use `mktemp -d` under an owner-only path, wait for every background PID, retain each status, print each bounded log in a deterministic command order, then exit non-zero if any validator failed. Do not run `pip install`, dependency audits, archive tests, watcher tests, or a publisher from this fast-lane script.

- [ ] **Step 3: Add mandatory policy tests to normal CI**

  Add `npm run test:pages-policy` that runs the Python/shell policy suite and include it in `test:dashboard` and `scripts/run_pages_validation.sh`. The full gate still runs the entire existing validation sequence; the data gate runs `npm ci --ignore-scripts`, focused validation, `npm run build`, and Pages artifact upload.

- [ ] **Step 4: Run and commit the gate**

  ```powershell
  bash scripts/test_run_pages_data_validation.sh
  bash scripts/test_run_pages_validation.sh
  npm run test:pages-policy
  git add scripts/run_pages_data_validation.sh scripts/test_run_pages_data_validation.sh scripts/run_pages_validation.sh scripts/test_run_pages_validation.sh package.json
  git commit -m "test: add focused Pages data validation"
  ```

## Task 4: Replace the workflow with separate gates and a monotonic controller

**Files:**

- Replace: `.github/workflows/deploy-pages.yml`
- Create: `scripts/test_pages_workflow_policy.py`
- Modify: `scripts/test_check_remote_freshness.py`

- [ ] **Step 1: Add failing workflow-policy tests**

  Assert exact job/check names, full history checkout, least permissions, classifier output validation, full/data command sets, one serialized controller, no workflow-level Pages concurrency, no cancellation of a running deployment, pre/post remote-head checks, stale-artifact skip before `deploy-pages`, stale-predeploy recovery dispatch, and deduplicated `workflow_dispatch` only for the current `main` SHA. Update stale test assumptions in `test_check_remote_freshness.py` from the old `group: pages` policy to the new controller policy.

- [ ] **Step 2: Implement the `classify` job**

  Use a pinned checkout with `fetch-depth: 0`, `persist-credentials: false`, and only `contents: read` plus `checks: read`. Pass event ref/before/after as quoted action inputs to the Python CLI; never interpolate commit data into shell source. Publish validated `lane`, SHAs, fingerprints, baseline, and reason as job outputs. A classifier error must explicitly output `full`, not fail open.

- [ ] **Step 3: Implement full and data gates**

  Name the full job/check exactly `pages-full-source-gate`; preserve the present Python/Node install, dependency audit, `run_pages_validation.sh`, Vite build, configure Pages, and artifact-upload sequence. Name the fast job/check exactly `pages-data-gate`; it uses the same deterministic Node install, `npm ci --ignore-scripts`, `run_pages_data_validation.sh`, Vite build, configure Pages, and artifact upload. Only one gate runs for each decision, and both artifacts carry the candidate SHA in their metadata/name.

- [ ] **Step 4: Implement the serialized deployment controller**

  `pages-deployment-controller` needs `classify`, `full_gate`, and `data_gate`; it runs when exactly one gate succeeded. Give only it `contents: read`, `actions: write`, `pages: write`, and `id-token: write`, and configure job-level:

  ```yaml
  concurrency:
    group: pages-deploy
    cancel-in-progress: false
  ```

  After entering the serialized job, validate the candidate SHA, fetch `refs/heads/main`, and skip deployment if current head differs. On this pre-deploy stale branch, immediately list queued/in-progress `deploy-pages.yml` runs and dispatch `workflow_dispatch` on `main` only if no active run already has that exact current head SHA. Deploy only the matching gate artifact. Recheck main after deploy and apply the same active-run deduplicated redispatch logic if it advanced. Validate every API SHA and never dispatch a supplied ref other than `main`.

- [ ] **Step 5: Run workflow tests and commit**

  ```powershell
  python scripts/test_pages_workflow_policy.py
  python scripts/test_check_remote_freshness.py
  git add .github/workflows/deploy-pages.yml scripts/test_pages_workflow_policy.py scripts/test_check_remote_freshness.py
  git commit -m "ci: deploy Pages through verified data lane"
  ```

## Task 5: Deploy, establish the first baseline, and measure latency

**Files:** deployment/docs only after all prior tasks pass.

- [ ] **Step 1: Run complete local verification from a clean checkout**

  ```powershell
  git status --short
  python scripts/test_runner_commit_policy.py
  python scripts/test_pages_deploy_policy.py
  bash scripts/test_run_pages_data_validation.sh
  bash scripts/test_run_pages_validation.sh
  python scripts/test_pages_workflow_policy.py
  bash scripts/test_refresh_and_publish.sh
  npm run test:pages-policy
  npm run test:dashboard
  npm run validate:dashboard
  npm run check:dashboard-ui
  npm run build
  ```

- [ ] **Step 2: Write the operator runbook before changing production**

  Create `docs/operations/pages-data-fast-lane.md`. Document the exact eligibility proof (parent, paths, trailers, fingerprints, full baseline), how to inspect the classifier and controller outputs, which validators each lane ran, the non-cancelling deployment behavior, and the revert-to-full-only procedure. State explicitly that a full-source change normally takes the full lane and that a data-lane result is evidence to inspect, not permission to bypass validation.

  ```powershell
  git add docs/operations/pages-data-fast-lane.md
  git commit -m "docs: document Pages data fast lane"
  ```

- [ ] **Step 3: Push the workflow/source implementation through the full lane**

  The implementation commit necessarily changes source/workflow/UI-shell fingerprints, so it must classify as `full`. Confirm the successful `pages-full-source-gate` on its exact commit; that check is the first trusted baseline. Do not manually force a data lane.

- [ ] **Step 4: Validate a subsequent real `current` runner commit**

  Confirm classifier outputs `data`, exact candidate/parent/baseline SHAs, equal fingerprints, and a valid current runner record. Verify the data gate ran exactly the four focused validators plus build, the controller deployed the current head only, and the immutable deployed status/bundle matches that commit.

- [ ] **Step 5: Rollback and incident procedure**

  If classification is unexpectedly `data`, a deployment is stale, gate evidence is ambiguous, or validation differs from the full gate, revert the workflow/policy commit (or disable the data gate by making classifier always `full`) and let the existing full Pages path continue. Do not delete deployment records or force-push. Document elapsed push-to-live p50/p95 before/after using the existing telemetry, with the target of reducing the current roughly 95-second median while preserving the full gate for every source-affecting change.
