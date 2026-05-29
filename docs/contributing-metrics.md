# Contributing metrics

Use this workflow when adding or changing dashboard metrics.

## Safe change path

1. Define the question the metric should answer.
2. Identify whether the needed input already exists in the decoded tables.
3. If needed, add decoding or enrichment in `scripts/build_dashboard.py`.
4. Add or modify a `CREATE TABLE` output in `sql/mission3_dashboard.sql`.
5. If the table should be exported, add it to `OUTPUT_TABLES` in `scripts/build_dashboard.py`.
6. Add a clear description to `DATASET_DESCRIPTIONS`.
7. Regenerate data with `npm run data`.
8. Run `npm run build` and the relevant checks.
9. Verify the dashboard UI and generated CSV/JSON outputs.

## Guardrails

- Do not change contract constants casually.
- Do not hand-edit generated CSV/JSON as the long-term fix.
- Do not mix verified archive rows with candidate rows without provenance.
- Do not commit secrets, private RPC URLs, or local files.
- Keep the public README concise; put detailed notes in `docs/` or `archive/`.

## Recommended validation

```bash
npm run data
npm run check:historical-dogs
npm run check:dashboard-ui
npm run archive:prices:validate
npm run build
git diff --check
```
