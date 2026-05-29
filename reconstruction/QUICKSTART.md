# Quickstart: Rebuild Dashboard Locally

## Requirements

- Git
- Node.js 22 or compatible modern Node
- npm
- Python 3
- Base RPC URL recommended

## Clone

```bash
git clone https://github.com/ael-dev3/Degen-Dogs-Mission-3.git
cd Degen-Dogs-Mission-3
```

## Configure

```bash
npm ci
cp .env.example .env.local 2>/dev/null || true
```

Edit `.env.local` if you have a reliable Base RPC endpoint:

```bash
BASE_RPC_URL=
```

Leaving it empty uses public defaults, subject to rate limits.

## Run

```bash
set -a
source .env.local 2>/dev/null || true
set +a
npm run data
npm run build
npm run dev
```

Open local site:

```text
http://localhost:5173/Degen-Dogs-Mission-3/
```

## Validate

```bash
ls -lah generated public/generated
grep -R "latest_block" -n generated public/generated README.md
npm run check:historical-dogs
npm run check:dashboard-ui
```

Do not commit if data generation failed, row counts look partial, or secrets appear in
the diff.
