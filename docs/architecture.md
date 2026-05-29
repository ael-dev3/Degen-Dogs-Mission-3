# Architecture

```text
Base RPC + contract calls
  -> local runner / Mac mini
  -> Python decoder + in-memory SQLite
  -> sql/mission3_dashboard.sql
  -> generated CSV/JSON + index.html
  -> GitHub Pages dashboard
```

## Static public site

The public dashboard is static. Visitors download HTML, CSS, JavaScript, images, and generated JSON/CSV files. They do not run SQL and do not call the auction contracts directly from the browser.

## Local runner

The runner is a trusted local machine that performs the expensive/reliable work:

1. Fetch Base contract state and event logs.
2. Decode Mission 3 auction and token data.
3. Resolve Dog metadata and optional identity data.
4. Load temporary SQLite tables.
5. Execute `sql/mission3_dashboard.sql`.
6. Export approved result tables.
7. Rebuild `index.html` and `README.md`.
8. Commit/push refreshed artifacts when running the publish script.

## GitHub Pages

`.github/workflows/deploy-pages.yml` installs dependencies and runs `npm run build`. It does not run `npm run data`. Fresh data must already be committed before a Pages deploy if the dashboard needs an updated snapshot.

## Why no public SQL editor

The current design favors reproducibility and low hosting risk over interactive queries. Public SQL would require a hosted database, rate-limit controls, abuse prevention, cost management, and a separate security model.
