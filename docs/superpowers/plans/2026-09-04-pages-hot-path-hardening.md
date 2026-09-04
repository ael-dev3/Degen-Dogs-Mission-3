# Pages Hot Path Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the low-latency Pages lane eligible only for an exact, reproducible current-auction data commit and prevent stale build artifacts from deploying.

**Architecture:** A fail-closed classifier proves an exact non-forced push identity, terminal runner provenance, publish-path/mode constraints, deterministic dashboard rendering, and an exact successful Pages deployment job and step for the parent. Build jobs may run concurrently, while a serialized deploy controller compares the candidate to the live main ref immediately before deployment and schedules one recovery run when it observes an advance without an exact-current workflow in any documented nonterminal state.

**Tech Stack:** Python 3 standard library, Bash, Git plumbing, GitHub Actions REST API, GitHub Actions YAML.

**Spec:** Independent review requirements delivered to task `/root/optimize_pages_hot_path` on 2026-09-04.

## Global Constraints

- Do not push, deploy, or modify the live WSL runner.
- Every ambiguous classifier or controller state fails closed.
- The fast lane retains the four production artifact gates and the production build.
- Source changes and non-current refresh scopes retain the complete Pages regression suite.
- Tests must precede each behavioral implementation change.

---

### Task 1: Bind Fast Eligibility to the Exact Push

**Files:**
- Modify: `scripts/classify_pages_validation.py`
- Modify: `scripts/test_classify_pages_validation.py`
- Modify: `.github/workflows/deploy-pages.yml`
- Modify: `scripts/test_check_remote_freshness.py`

**Interfaces:**
- Consumes: GitHub event name, ref, forced flag, before SHA, after SHA, repository, API URL, and token.
- Produces: `fast<TAB>reason` only for one direct `refs/heads/main` push commit.

- [x] Add failing fixtures for dispatch, forced or ambiguous pushes, wrong ref, invalid/zero SHAs, mismatched before/after, and multi-commit pushes.
- [x] Pass explicit event inputs from the workflow and require `forced == false`, `after == HEAD`, `before == HEAD^`, and `rev-list --count before..after == 1`.
- [x] Run classifier and workflow tests until green.

### Task 2: Require Canonical Terminal Current-Run Trailers

**Files:**
- Modify: `scripts/classify_pages_validation.py`
- Modify: `scripts/test_classify_pages_validation.py`
- Modify: `scripts/refresh_and_publish.sh`
- Modify: `scripts/test_refresh_and_publish.sh`

**Interfaces:**
- Consumes: `git interpret-trailers --parse` output for the commit message.
- Produces: exact single runner ID, run ID, and `current` scope provenance.

- [x] Add failing body-only, duplicate, non-current-scope, and publisher-format tests.
- [x] Parse only the terminal trailer block and emit future publisher trailers as one contiguous block.
- [x] Run classifier and publisher static tests until green; the complete publisher fixture requires a Linux system-path Node runtime.

### Task 3: Prove Deterministic Dashboard Reconstruction

**Files:**
- Modify: `scripts/classify_pages_validation.py`
- Modify: `scripts/test_classify_pages_validation.py`

**Interfaces:**
- Consumes: every `build_dashboard.OUTPUT_TABLES` CSV header and typed JSON row blob plus `public/mark-profile.png` at `HEAD`.
- Produces: a boolean proof that two isolated `write_html()` renders are LF-only and byte-identical to the committed `index.html` blob.

- [x] Add failing script, style, CSP, arbitrary-index-tamper, malformed-schema, missing-image, and two-render tests.
- [x] Reconstruct typed table tuples in isolated owned temporary roots and compare exact UTF-8 bytes.
- [x] Make any import, schema, render, determinism, or byte mismatch select the full suite before the parent API lookup.
- [x] Run focused and real-repository reconstruction tests until green.

### Task 4: Serialize and Guard Deployment

**Files:**
- Create: `scripts/pages_deploy_controller.py`
- Create: `scripts/test_pages_deploy_controller.py`
- Modify: `.github/workflows/deploy-pages.yml`
- Modify: `package.json`
- Modify: `scripts/run_pages_validation.sh`
- Modify: `scripts/test_run_pages_validation.sh`
- Modify: `scripts/test_check_remote_freshness.py`

**Interfaces:**
- Consumes: candidate SHA, exact current main ref, and workflow runs for the current SHA.
- Produces: a pre-deploy output flag and, only when needed, a `workflow_dispatch` request for `main`.

- [x] Add failing API behavior tests for current, every documented nonterminal state, paginated active results, stale-without-run, post-deploy advance, redirect, exact response status, and malformed responses.
- [x] Implement a no-redirect REST client, exact main lookup, bounded status-filtered active-run lookups, exact-204 recovery dispatch, and exact parent deploy-job/step proof.
- [x] Remove workflow-level concurrency; serialize only the deploy controller with cancellation disabled.
- [x] Give only the controller the write permissions, skip stale artifacts, and recheck after deployment.
- [x] Add the controller tests to CI/full validation and run focused tests until green.

### Task 5: Verify and Review

**Files:**
- Modify: `scripts/test_classify_pages_validation.py`

**Interfaces:**
- Consumes: the canonical allowlist literals in `refresh_and_publish.sh`.
- Produces: a regression failure when classifier and publisher path policy drift.

- [x] Add an exact allowlist parity regression across all three publisher policy blocks.
- [x] Run all focused tests, production artifact validators, build, syntax checks, and the available full suite.
- [x] Request independent code review and address every Critical or Important finding.
- [x] Amend the local commit and report the exact SHA and verification evidence without pushing.
