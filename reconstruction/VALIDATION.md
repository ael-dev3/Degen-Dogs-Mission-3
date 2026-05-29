# Validation

Run the full checks when rebuilding or publishing.

```bash
npm ci
npm run data
npm run check:historical-dogs
npm run check:dashboard-ui
npm run archive:prices:validate
npm run build
```

## Generated artifact checks

```bash
test -f generated/manifest.csv
test -f generated/mission3_metrics.csv
test -f generated/current_auction.csv
test -f public/generated/unified_dog_search_index.json
grep -R "latest_block" -n generated public/generated README.md
```

## Dashboard text checks

```bash
grep -R "Per-Dog stream estimate across 141"" Dogs" -n index.html generated public README.md docs || true
grep -R "WOOF Vault Bonus"" excluded" -n index.html generated public README.md docs || true
```

The dashboard-visible sentence should not appear in `index.html` or public generated
output.

## Link and integrity checks

- Local dashboard opens at `/Degen-Dogs-Mission-3/`.
- Current Dog is sane.
- Latest block/time are recent.
- Current auction table exists.
- Row counts are nonzero.
- No obvious duplicate auction rows.
- No broken `%2522` OpenSea trait links.
- README links to docs and reconstruction folder.
- GitHub Pages deploy passes after push.

## Secret checks

Run a value-oriented scan before committing:

```bash
python3 - <<'PY'
import pathlib, re
skip = {'.git', 'node_modules', 'dist'}
patterns = [
    re.compile(r'(' + 'DUNE' + '_API_KEY|NEYNAR' + '_API_KEY|BASE' + '_RPC_URL|PRIVATE' + '_KEY)=[^\\s#]+'),
]
for path in pathlib.Path('.').rglob('*'):
    if not path.is_file() or any(part in skip for part in path.parts):
        continue
    text = path.read_text(errors='ignore')
    for lineno, line in enumerate(text.splitlines(), 1):
        if any(p.search(line) for p in patterns):
            print(f'{path}:{lineno}:{line}')
PY
```

Blank placeholders such as `.env.example` are okay. Investigate any non-empty value
match before committing.
