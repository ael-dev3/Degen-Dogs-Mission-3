# Low-Latency Auction Delivery Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement the linked plans task-by-task. Do not start a later release phase until the prior phase's verification and rollback conditions are met.

**Goal:** Make the Mission 3 dashboard show an accurate, confirmed auction state in seconds while keeping the existing GitHub Pages snapshot, Git history, Mac runner, and two-provider accuracy boundary as resilient fallbacks.

**Delivery sequence:**

1. [Browser live auction quorum](2026-08-30-browser-live-auction-quorum.md) — highest user-visible latency reduction. Ship disabled, verify production-origin CORS/CSP, then enable in a separate reviewable commit. This preserves the static Pages snapshot whenever browser quorum is unavailable.
2. [Windows queued publisher](2026-08-30-windows-queued-publisher.md) — lets the PC keep detecting later bids while an earlier update is being published/verified. It is explicitly WSL-only and does not change Mac archive authority.
3. [Pages data fast lane](2026-08-30-pages-data-fast-lane.md) — shortens the static fallback's push-to-live path only for commits that prove they inherit from a successful full-source baseline. It depends on phase 1's generated page shape and follows phase 2 to avoid concurrent refactors to the publisher.

**Release gates:**

- Never reduce confirmation depth, provider quorum, or immutable commit/bundle verification to meet a latency target.
- Every phase is separately releasable and has a reversible flag, service disablement path, or ordinary Git revert.
- The browser path is the seconds-level data plane; Pages continues to provide an attested, content-addressed fallback and audit trail.
- Measure event-to-screen p50/p95 separately for direct browser quorum, queue-to-push, and push-to-Pages. Do not call the system optimized until the documented samples meet the eight-second browser p95 target and show no direct-data regressions.

**Execution discipline:** Complete each task in the linked implementation plan with its specified failing test first, focused verification, review checkpoint, and commit boundary. Production activation happens only after the source has passed the normal full Pages gate.
