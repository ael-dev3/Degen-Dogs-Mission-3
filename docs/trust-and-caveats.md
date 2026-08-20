# Trust and caveats

## Independent community contribution

This repo is an independent community-built dashboard/archive contribution. Degen Dogs
was created by Mark Carey / dogmaster. Do not describe this repo as official unless the
Degen Dogs project explicitly approves that wording.

## Cached snapshot

The public site is a cached static snapshot. It updates when the local runner refreshes
data, commits generated files, and GitHub Pages deploys.

## Data sources

- Current auction state: Base contract calls.
- Historical Mission 3 auction rows: Base event logs.
- Dog metadata and traits: Base `exists()` and `tokenURI()` outcomes are hash-pinned and
  cross-provider checked; the referenced HTTPS metadata content is schema-validated,
  hashed, cached for a bounded period, and accurately described as observed offchain
  content rather than an onchain content commitment.
- Farcaster identities: optional best-effort resolution.
- Mission 1 and Mission 2 archive rows: era-specific recovery scripts and checked-in
  archive outputs.

## Reward and token context

WOOF/SUP reward tiles are estimates for dashboard context. The reward basis uses an
observed 133-Dog reward-stream snapshot from [`config/reward_stream_snapshot.json`](../config/reward_stream_snapshot.json).
The WOOF Vault Bonus is not included in that estimate basis. Treat the values as
estimates unless confirmed against official reward/accounting logic.

Season 6 SUP projections are also estimates. They use a time-sliced XP model from
settled Dog wins, apply a wallet-level cap, and do not redistribute cap overflow. The
current-bidder projection assumes the current high bidder wins at the current auction
end time; it is useful context, not official accounting.

The BID PAYBACK tile also shows a simple annualized APR estimate:

- `daily ROI % = observed estimated per-Dog daily WOOF + SUP USD flow / current bid USD * 100`
- `APR % = daily ROI % * 365`

This is APR, not APY. It does not compound, excludes the WOOF Vault Bonus for the same
reason the per-Dog reward basis excludes it, changes with token prices, current bid,
auction state, and reward-flow assumptions, and is not a guaranteed future return.
If the current bid or daily reward-flow estimate is zero/missing, the dashboard renders
payback and APR as `N/A`.

## Archive completeness

- Mission 1: recovered Polygon-era archive/research with verification notes.
- Mission 2: Degen Chain archive/recovery; Dune query provenance is incomplete.
- Mission 3: live Base dashboard and rolling archive.

Do not fabricate missing history. Keep verified, candidate, and unknown data separated.

## Rarity scope

Mission 3 currently shows **Base-existing rarity**, not a whole-collection cross-chain
rank. The Base contract's `totalSupply()` is its next token-ID ceiling: historical Dogs
below ID 590 exist on Base only after they are claimed. The runner therefore requires
independent providers to agree that `exists(id)` matches the exact `tokenURI(id)`
outcome, excludes canonically nonexistent Base IDs, and ranks the remaining verified
Base-existing Dogs.

The score is the sum of `Base-existing Dog count / trait frequency` across exactly one
each of Background, Body, Neck, Mouth, Ears, Head, and Eyes. Trait percentages use the
same Base-existing denominator. Equal scores share a competition rank. The displayed
`#rank/total`, metrics, and tooltip expose that denominator; an unavailable or malformed
metadata response for an existing Base Dog withholds every rank rather than guessing.

Fast current-auction refreshes preserve that full-set attestation only after rechecking
the latest continuity-checkpoint block hash and NFT bytecode and obtaining an independent
log quorum for every Dog `Transfer`, `BaseURIUpdated`, `MetadataUpdate`, and
`BatchMetadataUpdate` event from that checkpoint through the new snapshot. Each verified
snapshot becomes the next checkpoint, keeping refresh work bounded while its block hash
commits the chain back to the full attestation. The bounded path may extend the cached
universe only for an exact contiguous suffix of canonical mints from the zero address to
the auction house, proven by the same cross-provider log quorum. It first requires one
unexpired, content-hash-bound metadata-cache record for every previously verified rarity
Dog and exact trait agreement with the attested history, then quorum-fetches the new Dog
metadata and recomputes every rank and denominator. Missing, expired, future-dated, or
malformed cache content; a changed existing trait; a burn, historical claim, metadata/URI
mutation, mint gap, or mismatched event forces the full metadata/rank rebuild. A
temporarily lagging snapshot preserves the newer artifacts and retries later. The
published attestation block/hash and continuity-through block/hash make this reuse
explicit instead of implying that every token was re-fetched.

A whole-collection rank requires reliable multi-provider historical-chain verification
for unclaimed Polygon- and Degen-era Dogs, plus origin/Base URI and content agreement
for claimed legacy Dogs. Until that quorum is configured, the dashboard deliberately
does not present public metadata endpoints alone as whole-collection onchain truth. The
[verified Base contract source](https://basescan.org/address/0x09154248ffdbaf8aa877ae8a4bf8ce1503596428#code)
documents the next-ID and historical-claim behavior.

## Historical USD estimates

Highest-USD sorting in the hosted feed uses generated historical estimate fields from
the static archive, not browser live price calls. Estimates are for browsing/context,
not official accounting. Rows without historical USD estimates are kept in the archive
and sorted below priced rows when the USD sort is active.

## License and attribution

Original code in this repository is licensed under MIT unless otherwise noted.
Third-party materials, including Degen Dogs upstream code/assets/docs referenced here,
retain their original licenses and attribution requirements. See
[`../NOTICE.md`](../NOTICE.md) and
[`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).

## Public SQL

The dashboard does not expose visitor-run SQL. Queries are approved in
`sql/mission3_dashboard.sql` and executed by the local runner.
