# Recovery notes

Inspection date: 2026-05-29 13:03 UTC

Current commit inspected: `e186e2d5c11e18e7ccb58d0d1ea01873796e4348` (`e186e2d`)

## Current dashboard architecture

```text
Base RPC + contract calls
  -> local runner / Mac mini
  -> Python decoder + in-memory SQLite
  -> sql/mission3_dashboard.sql
  -> generated CSV/JSON + index.html
  -> GitHub Pages dashboard
```

The public site is static. The local runner does the chain reads, SQL execution, artifact generation, commit, and push.

## Existing npm scripts

- `npm run archive:mission1:check` - `python3 scripts/archive_mission1_index.py --check-config`
- `npm run archive:mission1:discover` - `python3 scripts/archive_mission1_discover.py`
- `npm run archive:mission1:full` - `python3 scripts/archive_mission1_index.py --full-refresh`
- `npm run archive:mission1:index` - `python3 scripts/archive_mission1_index.py --incremental`
- `npm run archive:mission1:reconcile` - `python3 scripts/archive_mission1_reconcile.py`
- `npm run archive:mission2` - `python3 scripts/archive_mission2_index.py`
- `npm run archive:mission2:build` - `python3 scripts/archive_mission2_build.py`
- `npm run archive:mission2:check` - `python3 scripts/archive_mission2_index.py --check-config`
- `npm run archive:mission2:discover` - `python3 archive/mission2/scripts/recover_dune_queries.py`
- `npm run archive:mission2:index` - `python3 scripts/archive_mission2_index.py`
- `npm run archive:mission2:reconcile` - `python3 scripts/archive_mission2_reconcile.py`
- `npm run archive:mission3:full` - `python3 scripts/archive_mission3_index.py --full-refresh --write-public`
- `npm run archive:mission3:health` - `python3 scripts/check_mission3_archive.py`
- `npm run archive:mission3:index` - `python3 scripts/archive_mission3_index.py --incremental --write-public`
- `npm run archive:mission3:verify` - `python3 scripts/archive_mission3_index.py --verify-only`
- `npm run archive:prices:apply` - `python3 scripts/archive_apply_usd_estimates.py`
- `npm run archive:prices:fetch` - `python3 scripts/archive_fetch_historical_prices.py`
- `npm run archive:prices:validate` - `python3 scripts/archive_validate_usd_estimates.py`
- `npm run archive:unified:index` - `python3 scripts/build_unified_dog_index.py`
- `npm run build` - `vite build --base=/Degen-Dogs-Mission-3/`
- `npm run check:dashboard-ui` - `python3 scripts/check_dashboard_ui.py`
- `npm run check:historical-dogs` - `python3 scripts/check_historical_dog_search.py`
- `npm run data` - `python3 scripts/build_dashboard.py && python3 scripts/build_unified_dog_index.py && python3 scripts/archive_apply_usd_estimates.py`
- `npm run dev` - `vite --host 0.0.0.0 --base=/Degen-Dogs-Mission-3/`
- `npm run preview` - `vite preview --host 0.0.0.0`
- `npm run refresh:archive` - `bash scripts/refresh_archive_and_publish.sh`
- `npm run refresh:install` - `bash scripts/install_hourly_refresh_launchd.sh`
- `npm run refresh:publish` - `bash scripts/refresh_and_publish.sh`

## Generated output directories

- `generated/` - main generated CSV/JSON exports.
- `public/generated/` - public static copies used by the browser.
- `archive/data/generated/` - unified Dog/archive search outputs.
- `archive/dogs/by-id/` - per-Dog archive JSON records.
- `archive/mission*/data/generated/` - mission-specific generated archive outputs.
- `index.html` - generated dashboard shell.
- `README.md` - generated from `README.template.md`.

## GitHub Pages workflow behavior

`.github/workflows/deploy-pages.yml` runs `npm ci` and `npm run build` only. It does not run `npm run data`.

Therefore, fresh data must be generated and committed before pushing if a fork or recovery maintainer wants an updated live snapshot.

## Source-of-truth files

- `scripts/build_dashboard.py`
- `scripts/build_unified_dog_index.py`
- `scripts/archive_apply_usd_estimates.py`
- `sql/mission3_dashboard.sql`
- `README.template.md`
- `package.json`
- `.github/workflows/deploy-pages.yml`
- verified configs in `archive/mission1/config/`, `archive/mission2/config/`, and `archive/mission3/config/`

## Generated files

- `README.md`
- `index.html`
- `generated/`
- `public/generated/`
- `archive/data/generated/`
- `archive/dogs/by-id/`
- mission-specific archive generated CSV/JSON outputs

Do not hand-edit generated files as the durable fix. Update source/template/generator files, then rerun the pipeline.
