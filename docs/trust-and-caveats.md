# Trust and caveats

## Independent community contribution

This repo is an independent community-built dashboard/archive contribution. Degen Dogs was created by Mark Carey / dogmaster. Do not describe this repo as official unless the Degen Dogs project explicitly approves that wording.

## Cached snapshot

The public site is a cached static snapshot. It updates when the local runner refreshes data, commits generated files, and GitHub Pages deploys.

## Data sources

- Current auction state: Base contract calls.
- Historical Mission 3 auction rows: Base event logs.
- Dog metadata and traits: token metadata fetched by the pipeline and cached locally.
- Farcaster identities: optional best-effort resolution.
- Mission 1 and Mission 2 archive rows: era-specific recovery scripts and checked-in archive outputs.

## Reward and token context

WOOF/SUP reward tiles are estimates for dashboard context. The reward basis uses 141 Dogs and the vault bonus is not included in that estimate basis. Treat the values as estimates unless confirmed against official reward/accounting logic.

## Archive completeness

- Mission 1: recovered Polygon-era archive/research with verification notes.
- Mission 2: Degen Chain archive/recovery; Dune query provenance is incomplete.
- Mission 3: live Base dashboard and rolling archive.

Do not fabricate missing history. Keep verified, candidate, and unknown data separated.

## Historical USD estimates

Highest-USD sorting in the hosted feed uses generated historical estimate fields from the static archive, not browser live price calls. Estimates are for browsing/context, not official accounting. Rows without historical USD estimates are kept in the archive and sorted below priced rows when the USD sort is active.

## Public SQL

The dashboard does not expose visitor-run SQL. Queries are approved in `sql/mission3_dashboard.sql` and executed by the local runner.
