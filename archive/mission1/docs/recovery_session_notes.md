# Mission 1 Recovery Session Notes

- Session time UTC: 2026-05-28T22:24:05Z
- Local time: 2026-05-29T00:24:05+02:00
- Repo: `ael-dev3/Degen-Dogs-Mission-3`
- Current branch: `main`
- Starting commit after fast-forward: `720056e7e0a324fa2666a298b1f299031f770796`
- Task source: Discord prompt `degen_dogs_mission1_archive_agent_prompt.txt`

## Phase 0 inspection

Files and areas inspected before major Mission 1 writes:

- `README.md`: generated Mission 3 analytics README from `README.template.md` / `scripts/build_dashboard.py`.
- `package.json`: Mission 3 build/data scripts plus existing Mission 2 archive scripts.
- `scripts/`: `build_dashboard.py`, `archive_mission2_index.py`, refresh/publish helpers.
- `sql/`: active Mission 3 query layer in `sql/mission3_dashboard.sql`.
- `archive/`: existing archive index plus Mission 2 archive foundation and preserved Mission 3 SQL/live-auction artifacts.
- `archive/mission2/`: isolated Mission 2 scaffold already exists with docs, config, Dune placeholders, SQL, data dirs, and fail-closed indexer.
- `archive/mission1/`: did not exist before this session.
- `generated/` and `public/generated/`: active Mission 3 generated CSV/JSON artifacts.
- `.gitignore`: ignores `node_modules`, `dist`, Python caches, `.env`, `.env.*`, `*.local`, `.cache`.
- `.github/workflows/`: CI and GitHub Pages deploy both run `npm ci` and `npm run build` on Node 22.

## Existing Mission 1 files

None before this session. This notes file is the first Mission 1 archive file.

## Current archive structure

- `archive/README.md` indexes preserved archive/provenance folders.
- `archive/mission2/` is an isolated Degen Chain archive foundation. It has docs, config, Dune recovery placeholders, SQL schema/marts, local data dirs, and a fail-closed indexer.
- `archive/degen-dogs-mission3-sql-bundle-2026-05-22/` preserves a Mission 3 SQL bundle with caveats.
- `archive/live-auction-bidding-module-2026-05-25/` preserves a reverted live auction bidding attempt.

## Verified vs unknown at inspection time

Verified/strong at inspection time:

- Mission 1 was the Polygon production era according to Degen Dogs docs and public prompt context.
- Current docs contract page lists PolygonScan addresses for historical contracts, but its heading currently references Degen Chain and contains a TODO for Degen contracts, so those addresses need source + onchain checks before promotion.
- Mission 3 live dashboard must remain unchanged by this archival task.

Unknown at inspection time:

- Exact Mission 1 auction/NFT block range.
- Exact Dog ID range and whether the “201 Polygon Dogs” claim reconciles with onchain logs.
- Dune Mission 1 dashboard/query IDs.
- PolygonScan API availability and verified source details.
- Whether Dog #1 / Ukraine Dog can be verified directly from docs/metadata/onchain.
- Whether BSCT, Idle, treasury, donation, and Superfluid stream data can be fully reconstructed in one pass.

## Environment/API/tool availability

Tooling present:

- `git`: `/opt/homebrew/bin/git`
- `gh`: `/opt/homebrew/bin/gh`
- `node`: `/opt/homebrew/bin/node`
- `npm`: `/opt/homebrew/bin/npm`
- `python3`: `/Users/marko/.hermes/hermes-agent/venv/bin/python3`
- `curl`: `/usr/bin/curl`

Environment variables present at inspection time. Only `POLYGON_RPC_URL(S)`, `POLYGONSCAN_API_KEY`, and `DUNE_API_KEY` are supported inputs for the current scripts; historical `MISSION1_*` names below were inspected only to rule out pre-existing local overrides:

- `POLYGON_RPC_URL`: no
- `POLYGON_RPC_URLS`: no
- `POLYGONSCAN_API_KEY`: no
- `DUNE_API_KEY`: no
- Historical override `MISSION1_AUCTION_HOUSE`: no
- Historical override `MISSION1_NFT`: no
- Historical override `MISSION1_FROM_BLOCK`: no
- Historical override `MISSION1_TO_BLOCK`: no
- Historical override `MISSION1_LOG_CHUNK`: no
- `BASE_RPC_URL`: no
- `NEYNAR_API_KEY`: no

Polygon RPC probe:

- `https://polygon-rpc.com`: HTTP 401, not usable without credentials in this environment.
- `https://polygon-bor-rpc.publicnode.com`: usable, `eth_chainId` returned `0x89`.
- `https://1rpc.io/matic`: usable, `eth_chainId` returned `0x89`.

PolygonScan API:

- No `POLYGONSCAN_API_KEY` present.
- Public website/API lookups may still be attempted, but scripts must not require a key for static checks.

Dune API:

- No `DUNE_API_KEY` present.
- Dune recovery must remain documented/manual unless public UI/API data is discovered without credentials.
