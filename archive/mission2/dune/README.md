# Mission 2 Dune Recovery

Use this folder to recover and preserve the public/historical Dune SQL behind `Degen Dogs Mission 2` by `ael_dev`.

## Manual recovery steps

1. Search Dune UI for:
   - owner: `ael_dev`
   - title: `Degen Dogs Mission 2`
   - chain: `Degen`
2. Record dashboard URL/slug/dashboard ID in `query_ids.json`.
3. Find every query used in that dashboard.
4. For every query:
   - save query ID
   - save query title
   - fetch SQL through Dune API if possible: `GET https://api.dune.com/api/v1/query/{queryId}`
   - save official SQL to `archive/mission2/dune/queries/{query_id}_{slug}.sql`
   - save latest results snapshot to `archive/mission2/dune/results/{query_id}_{slug}.json` or `.csv`
5. Extract hardcoded constants from SQL:
   - contract addresses
   - event topic hashes
   - Degen table names such as possible `degen.logs`
   - block ranges
   - token IDs / Dog ID filters
   - known wallet lists
   - WOOF/WOOFx addresses
   - MintClub token addresses
   - Superfluid pool addresses
   - date ranges
   - assumptions like reserve price, token decimals, price sources
6. Write constants to `archive/mission2/dune/hardcoded_constants.json`.
7. Do not rewrite or beautify official SQL in place. Store ported versions separately under `archive/mission2/sql/ported/`.

## API helper

After query IDs are recorded and `DUNE_API_KEY` is set:

```bash
python3 archive/mission2/scripts/recover_dune_queries.py
```

If no query IDs are listed, the script exits cleanly with instructions.
