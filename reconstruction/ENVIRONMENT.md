# Environment and secrets

## Required tools

- Git
- Node.js / npm
- Python 3
- Internet access

## Recommended external services

- Reliable Base RPC provider for Mission 3 refreshes.
- Optional Neynar API key for Farcaster identity resolution.
- Optional price/Dune API keys for archive recovery work.

## Minimal `.env.local`

```bash
BASE_RPC_URL=
BASE_LOG_CHUNK=5000
BASE_LOG_WORKERS=4
```

A blank `BASE_RPC_URL` means the script uses public defaults.

## Secrets policy

Never commit `.env`, `.env.local`, API keys, RPC secrets, private keys, or local machine
paths.

The dashboard does not need wallet private keys. It reads public/onchain data and writes
static files.

## Variables

See [`../docs/configuration.md`](../docs/configuration.md) for the full table.
