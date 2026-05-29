# Local development

## Requirements

- Git
- Node.js 22 or compatible modern Node
- npm
- Python 3
- Internet access to Base RPC endpoints

## Setup

```bash
npm ci
cp .env.example .env.local 2>/dev/null || true
```

If you create `.env.local`, keep it local and never commit it.

## Run data and preview

```bash
set -a
source .env.local 2>/dev/null || true
set +a
npm run data
npm run dev
```

Open:

```text
http://localhost:5173/Degen-Dogs-Mission-3/
```

## Production build

```bash
npm run build
```

The build output goes to `dist/` and is ignored by git.

## Useful checks

```bash
npm run check:historical-dogs
npm run check:dashboard-ui
npm run archive:prices:validate
```

## Common local failures

- RPC timeouts: set a reliable `BASE_RPC_URL`, lower `BASE_LOG_CHUNK`, or reduce `BASE_LOG_WORKERS`.
- Stale README after editing: edit `README.template.md`, not only `README.md`, then rerun `npm run data`.
- Vite path confusion: use the configured base path `/Degen-Dogs-Mission-3/`.
- Generated data looks partial: do not commit it until `npm run data` and validation checks pass.
