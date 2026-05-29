# Degen Dogs Era Archive

This folder preserves Degen Dogs era data and provenance for long-term community use. It
sits beside the current Mission 3 dashboard and is meant to make the underlying data
reproducible, auditable, and easier to integrate into future archive/search features.

The archive is independent and community-built. It is not official Degen Dogs accounting
unless explicitly confirmed by the project.

## Era sections

### Mission 1

- **Status:** Polygon historical archive recovered.
- **Notes:** Verified Polygon contract constants, receipt-backed auction/NFT/BSCT logs,
  per-Dog bid summaries for token IDs `0-200`, and reconciliation notes live in
  `archive/mission1/`.
- **Dashboard relationship:** Archive-only and not wired into the live Mission 3
  dashboard.

### Mission 2

- **Status:** Degen Chain recovery in progress.
- **Notes:** Existing recovery notes, Dune provenance, ABI fragments, and local indexing
  scaffolds live in `archive/mission2/`.
- **Caveat:** Contract addresses and exact ranges remain unverified unless marked
  otherwise in that folder.

### Mission 3

- **Status:** Base live rolling archive.
- **Notes:** Active archive for Base auction logs, current auction snapshots, generated
  CSV/JSON, and future dashboard search/index files.

## Current dashboard relationship

The main public dashboard remains Mission 3-focused for now. It reads from generated
files under `generated/` and `public/generated/`, produced by
`scripts/build_dashboard.py` and `sql/mission3_dashboard.sql`.

The archive runner is a separate path. Its job is to persist Mission 3 logs and decoded
outputs locally so the dashboard and future archive UI do not depend on ephemeral RPC
history scans or unreproducible manual steps.

## Existing provenance bundles

Older preserved work is still kept here for audit/provenance:

### `degen-dogs-mission3-sql-bundle-2026-05-22/`

- **Source:** `degen_dogs_mission3_sql_bundle.zip` Discord upload.
- **Preserved files:** 10.
- **Status:** Partial reconstructed Dune SQL bundle.
- **Key finding:** Reconstructed auction SQL uses stale/no-code `0x3620...`; use the
  current auction house `0x8F34...` or official Dune SQL before production.

### `live-auction-bidding-module-2026-05-25/`

- **Source:** commit `82bdf57008e03561bb1f3813bbf1a0d387d3b36d`.
- **Preserved files:** 7.
- **Status:** Archived/reverted live auction bidding attempt.
- **Key finding:** Wallet connected and had funds, but the bid button stayed greyed out
  and bidding functionality did not work; active site reverted to cached/static
  dashboard flow.

## Guardrails

- Verified data, candidate data, and unknowns must be separated.
- Raw onchain logs should be preserved before decoded or derived rows where feasible.
- Generated CSV/JSON should be reproducible from RPC logs and local SQL.
- Do not commit secrets, private RPC URLs, `.env` files, Dune API keys, or local machine
  paths.
- Do not fabricate missing Mission 1 or Mission 2 history.
