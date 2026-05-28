# Mission 2 Archive Scripts

Root-level package scripts call the local indexer:

```bash
npm run archive:mission2:check
npm run archive:mission2
```

The indexer is located at `scripts/archive_mission2_index.py` so repo-level npm scripts can call it consistently.

Archive-local helper:

```bash
python3 archive/mission2/scripts/recover_dune_queries.py
```

This optional helper reads `DUNE_API_KEY` and query IDs from `archive/mission2/dune/query_ids.json`. If no query IDs have been recovered, it prints the manual recovery instructions and exits cleanly.
