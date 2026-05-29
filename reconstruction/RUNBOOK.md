# Reconstruction runbook

## Normal refresh

```bash
npm run data
npm run build
git status --short
```

Expected generated paths include `README.md`, `index.html`, `generated/`, `public/generated/`, and unified archive search outputs.

## Rebuild from scratch

Use a clean clone if possible. If you need to remove generated outputs locally:

```bash
rm -rf generated public/generated dist
npm ci
npm run data
npm run build
```

Only do this when you are ready to regenerate everything. If unsure, create a fresh clone instead.

## Restore generated outputs from Git

```bash
git checkout -- generated public/generated index.html README.md
```

Add archive generated paths if those were also damaged:

```bash
git checkout -- archive/data/generated archive/dogs/by-id
```

## Recover from failed data run

1. Check RPC availability.
2. Set `BASE_RPC_URL` to a reliable endpoint.
3. Lower `BASE_LOG_CHUNK`.
4. Reduce `BASE_LOG_WORKERS`.
5. Rerun `npm run data`.
6. Compare `generated/mission3_metrics.csv` latest block/time.
7. Do not commit partial or corrupted output.

## Recover from broken README generation

1. Edit `README.template.md`, not only `README.md`.
2. If placeholders changed, update `scripts/build_dashboard.py`.
3. Rerun `npm run data`.
4. Confirm `README.md` is concise and generated correctly.

## Recover from broken GitHub Pages deploy

1. Open the Actions log for `Deploy GitHub Pages`.
2. Run `npm run build` locally.
3. Confirm `dist/` exists.
4. Confirm Pages is enabled for GitHub Actions.
5. Confirm generated data was committed before push if freshness matters.

## Recreate hourly runner

macOS:

```bash
npm run refresh:install
```

Manual run:

```bash
npm run refresh:publish
```

Linux cron example:

```cron
0 * * * * cd /path/to/Degen-Dogs-Mission-3 && npm run refresh:publish
```

Keep recovery simple. Use the existing scripts before adding new infrastructure.
