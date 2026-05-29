# Refresh runner

The public site is served by GitHub Pages, but fresh data comes from a private/local runner that regenerates static files and pushes commits.

## Available commands

```bash
npm run refresh:publish
npm run refresh:archive
npm run refresh:install
```

- `refresh:publish` runs `scripts/refresh_and_publish.sh`.
- `refresh:archive` runs Mission 3 archive indexing first, then the normal publish flow.
- `refresh:install` installs the macOS launchd hourly runner.

## Publish flow

`scripts/refresh_and_publish.sh`:

1. Takes a local lock to avoid overlapping runs.
2. Refuses to overwrite tracked/untracked publish-path changes.
3. Pulls latest `main` unless disabled.
4. Installs npm dependencies if needed.
5. Optionally runs Mission 3 archive incremental indexing.
6. Runs `npm run data`.
7. Validates generated artifacts.
8. Runs `npm run build`.
9. Stages only expected generated publish paths.
10. Scans staged generated artifacts for common secret patterns.
11. Commits and pushes unless configured to skip push.

## GitHub Pages behavior

The Pages workflow runs `npm ci` and `npm run build`. It does not run `npm run data`; the runner must commit fresh generated data before pushing if the live dashboard should update.

## Recreating hourly refresh

macOS launchd:

```bash
npm run refresh:install
```

Linux cron example:

```cron
0 * * * * cd /path/to/Degen-Dogs-Mission-3 && npm run refresh:publish
```

Linux systemd timers are also fine. Keep the service simple: run the existing publish script, capture logs, and alert on non-zero exit.

## Safety

- Do not run the publish script with a dirty worktree.
- Do not commit `.env.local`, local logs, or local cache paths.
- Prefer lowering RPC concurrency over retrying aggressively when rate-limited.
