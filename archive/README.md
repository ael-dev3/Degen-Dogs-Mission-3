# Degen Dogs Era Archive

This folder preserves Degen Dogs era data and provenance for long-term community use. It sits beside the current Mission 3 dashboard and is meant to make the underlying data reproducible, auditable, and easier to integrate into future archive/search features.

The archive is independent and community-built. It is not official Degen Dogs accounting unless explicitly confirmed by the project.

## Era sections

| Era | Status | Notes |
| --- | --- | --- |
| Mission 1 | Historical / unknown / incomplete | Placeholder only until contracts, chain, Dog range, and source data are verified. Do not infer or invent missing details. |
| Mission 2 | Degen Chain recovery in progress | Existing recovery notes, Dune provenance, ABI fragments, and local indexing scaffolds live in `archive/mission2/`. Contract addresses and exact ranges remain unverified unless marked otherwise in that folder. |
| Mission 3 | Base live rolling archive | Active archive for Base auction logs, current auction snapshots, generated CSV/JSON, and future dashboard search/index files. |

## Current dashboard relationship

The main public dashboard remains Mission 3-focused for now. It reads from generated files under `generated/` and `public/generated/`, produced by `scripts/build_dashboard.py` and `sql/mission3_dashboard.sql`.

The archive runner is a separate path. Its job is to persist Mission 3 logs and decoded outputs locally so the dashboard and future archive UI do not depend on ephemeral RPC history scans or unreproducible manual steps.

## Existing provenance bundles

Older preserved work is still kept here for audit/provenance:

| archive | source | preserved files | status | key finding |
| --- | --- | ---: | --- | --- |
| `degen-dogs-mission3-sql-bundle-2026-05-22/` | `degen_dogs_mission3_sql_bundle.zip` Discord upload | 10 | partial reconstructed Dune SQL bundle | reconstructed auction SQL uses stale/no-code `0x3620...`; use current auction house `0x8F34...` or official Dune SQL before production |
| `live-auction-bidding-module-2026-05-25/` | commit `82bdf57008e03561bb1f3813bbf1a0d387d3b36d` | 7 | archived/reverted live auction bidding attempt | wallet connected and had funds, but the bid button stayed greyed out and bidding functionality did not work; active site reverted to cached/static dashboard flow |

## Guardrails

- Verified data, candidate data, and unknowns must be separated.
- Raw onchain logs should be preserved before decoded or derived rows where feasible.
- Generated CSV/JSON should be reproducible from RPC logs and local SQL.
- Do not commit secrets, private RPC URLs, `.env` files, Dune API keys, or local machine paths.
- Do not fabricate missing Mission 1 or Mission 2 history.
