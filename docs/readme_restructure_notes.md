# README restructure notes

Inspection date: 2026-05-29 13:03 UTC

Current commit inspected: `e186e2d5c11e18e7ccb58d0d1ea01873796e4348` (`e186e2d`)

## README generation path

- `README.md` is generated from `README.template.md` by `scripts/build_dashboard.py`.
- `npm run data` runs `python3 scripts/build_dashboard.py`, then `python3 scripts/build_unified_dog_index.py`, then `python3 scripts/archive_apply_usd_estimates.py`.
- `scripts/build_dashboard.py` writes `README.md`, `index.html`, `generated/*`, and `public/generated/*`.
- Durable README copy must be edited in `README.template.md` and, when placeholders change, in `scripts/build_dashboard.py`.

## What stays in the top-level README

- Project identity and independent/community wording.
- Live dashboard link.
- Short architecture explanation.
- Compact generated snapshot.
- Quick start commands.
- Links to docs, generated exports, archive, and reconstruction runbook.
- Short trust/caveat summary and creator credit.

## What moved deeper into docs

- Full dataset catalog: `docs/datasets.md`.
- Environment variables and secret handling: `docs/configuration.md`.
- Refresh runner details: `docs/refresh-runner.md`.
- Contract details: `docs/contracts.md`.
- Change workflow for metrics: `docs/contributing-metrics.md`.
- Full caveats and archive status: `docs/trust-and-caveats.md`, `docs/roadmap.md`.

## Risks

- Hand-editing `README.md` will be overwritten by `npm run data`.
- The Pages workflow runs `npm run build` only; it does not fetch fresh chain data.
- Public RPC rate limits can make `npm run data` fail unless chunk/workers are tuned.
- Generated files should not be edited manually except for emergency restore/revert work.

## Validation commands

```bash
npm run data
npm run build
grep -n "Published datasets" README.md || true
grep -n "BASE_RPC_URL" README.md || true
find docs -maxdepth 2 -type f | sort
```
