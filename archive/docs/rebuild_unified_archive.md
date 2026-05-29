# Rebuild unified archive/search outputs

Run the full data pipeline from the repository root:

```bash
npm run data
```

`npm run data` performs three steps in order:

1. `python3 scripts/build_dashboard.py` — refreshes Mission 3 dashboard tables and HTML.
2. `python3 scripts/build_unified_dog_index.py` — rebuilds the cross-mission Dog archive index and per-Dog JSON records.
3. `python3 scripts/archive_apply_usd_estimates.py` — reapplies historical USD estimates from the checked-in daily price table.

For just the archive index:

```bash
npm run archive:unified:index
```

For the historical price layer:

```bash
npm run archive:prices:fetch      # refresh daily price rows from configured public sources
npm run archive:prices:apply      # enrich unified records with estimated USD values
npm run archive:prices:validate   # validate estimate/provenance coverage
```

The browser search reads `public/generated/unified_dog_search_index.json`. The archive copy is `archive/data/generated/unified_dog_search_index.json`, and per-Dog records are written to `archive/dogs/by-id/<dog_id>.json`.

No API keys or private RPC credentials are required for the checked-in rebuild path. Price fetching uses public endpoints and writes source/provenance metadata beside the generated price rows.
