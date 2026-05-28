# How to Rebuild Mission 3 Archive

## Verify config only

```bash
npm run archive:mission3:verify
```

## Full rebuild

```bash
npm run archive:mission3:full
```

This removes and rebuilds `archive/mission3/data/mission3_archive.sqlite`, then exports generated CSV/JSON.

## Incremental update

```bash
npm run archive:mission3:index
```

This starts from `latest_indexed_block + 1` if the archive DB already exists.

## Incremental with public future-ready JSON

```bash
python3 scripts/archive_mission3_index.py --incremental --write-public
```

Small public files are copied under `public/generated/mission3/`.

## Combined dashboard refresh

```bash
npm run refresh:archive
```

This runs the Mission 3 archive incremental index, then the existing dashboard data/build flow. It commits and pushes by default through the normal publish script; set `DEGEN_DOGS_SKIP_PUSH=1` when you want a local dry run only.
