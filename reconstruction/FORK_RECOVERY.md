# Fork recovery

Use this when the original local runner is offline and another maintainer needs to republish from a fork.

## Steps

1. Fork `https://github.com/ael-dev3/Degen-Dogs-Mission-3`.
2. Clone the fork.
3. Run `npm ci`.
4. Create `.env.local` from `.env.example` and set a reliable Base RPC if available.
5. Run `npm run data`.
6. Run `npm run build`.
7. Run validation checks from `VALIDATION.md`.
8. Inspect `git diff` for expected generated changes only.
9. Commit source/docs/generated changes.
10. Push to the fork.
11. Enable GitHub Pages in the fork if needed.
12. Verify the new dashboard URL.
13. Optionally recreate scheduled local refresh.

## Important: generated data

The Pages deploy workflow builds the static site. It does not fetch fresh chain data. Run `npm run data` locally before pushing if the fork needs a fresh snapshot.

## GitHub Pages notes

- Workflow file: `.github/workflows/deploy-pages.yml`.
- Build command: `npm run build`.
- Build output: `dist/`.
- The workflow uses checked-in `index.html` and `public/generated/*` after the local data run.

## Fork URL expectation

The original site uses `/Degen-Dogs-Mission-3/` as the Vite base path. If the fork keeps the same repo name, the path remains valid. If the fork changes repo name, update build base config/scripts before publishing.
